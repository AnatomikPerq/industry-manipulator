#!/usr/bin/env python3
"""
Очередь прогонов: один воркер, строго по одной сессии за раз.

Почему очередь, а не параллельный запуск: LM Studio на всех один, и стадия
агентов - это одна модель, которая физически не может считать две сессии
одновременно. Пытаться - значит получить два прогона, дерущихся за одну
видеокарту, и оба медленнее, чем последовательные. Поэтому сессии выстраиваются
в глобальную очередь: пользователь ставит свою и уходит, результат ждёт его в
списке сессий.

WORKERS вынесен в константу как задел, но параллелизм НЕ реализован: изоляция
по путям (см. sessions.py) его выдержит, а вот LM Studio - нет. Поднимать
значение можно только вместе с проверкой, что стадия агентов сериализована
отдельным семафором.

Прогон исполняется отдельным процессом (_pipeline_runner.py), а не в этом
потоке - только так отмена может оборвать его мгновенно, убив всё дерево
процессов, а не дожидаясь, пока пайплайн заметит просьбу остановиться.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

from sessions import SessionError

HERE = Path(__file__).resolve().parent
RUNNER_SCRIPT = HERE / "_pipeline_runner.py"

WORKERS = 1


def kill_process_tree(proc):
    """Мгновенно и гарантированно убивает подпроцесс анализа вместе со всеми его
    потомками - как если бы во всех его окнах разом нажали Ctrl+C, только без
    надежды на то, что код внутри вообще заметит сигнал (сетевой вызов к ИИ или
    Open Interpreter кооперативную отмену вполне могут проигнорировать)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # /T - вместе со всем деревом потомков, /F - принудительно, без вопросов
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass


class AnalysisQueue:
    """Глобальная очередь сессий с одним рабочим потоком.

    Единственный источник правды о статусе - session.json на диске (см.
    sessions.py). В памяти лежат только очередь ожидающих и хэндл текущего
    процесса: всё, что нужно, чтобы отменить прогон и чтобы после перезапуска
    сервера ничего не потерялось.
    """

    def __init__(self, store, base_config_path):
        self.store = store
        self.base_config_path = str(base_config_path)
        self._cv = threading.Condition()
        self._queue = deque()             # id сессий, ждущих своей очереди
        self._running_id = None
        self._proc = None
        self._cancelled = set()           # id, для которых пришла отмена
        self._thread = None

    # ---------- запуск ----------

    def start(self):
        """Поднимает воркера и возвращает очередь в то состояние, в котором её
        застал перезапуск сервера."""
        for meta in self.store.restore_after_restart():
            self._queue.append(meta["id"])
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ---------- публичное API ----------

    def enqueue(self, session_id, mode) -> int:
        """Ставит сессию в очередь и возвращает её позицию (1 - следующая на
        очереди; 0 означает, что воркер свободен и возьмёт её сейчас же)."""
        meta = self.store.get(session_id)
        if meta["status"] in ("queued", "running"):
            raise SessionError("Сессия уже в очереди или выполняется", 409)
        if not self.store.has_files(session_id):
            raise SessionError("В сессии нет файлов для анализа", 400)

        self.store.update(session_id, status="queued", mode=mode,
                          queued_at=time.time(), started_at=None, finished_at=None,
                          n_findings=None, error=None)
        with self._cv:
            self._cancelled.discard(session_id)
            self._queue.append(session_id)
            position = len(self._queue) - (0 if self._running_id else 1)
            self._cv.notify()
        self.store.append_log(session_id, "=== Сессия поставлена в очередь ===")
        return max(position, 0)

    def cancel(self, session_id) -> None:
        """Отмена и из очереди, и на ходу.

        Из очереди - просто выбрасываем id, ничего убивать не надо. На ходу -
        убиваем дерево процессов; довести сессию до статуса cancelled и подчистить
        недописанный output/ - работа самого воркера, он как раз ждёт на чтении
        stdout убитого процесса."""
        with self._cv:
            if session_id in self._queue:
                self._queue.remove(session_id)
                self._cancelled.add(session_id)
                self.store.update(session_id, status="cancelled",
                                  finished_at=time.time(),
                                  error="Сессия снята с очереди пользователем")
                self.store.append_log(session_id, "=== Сессия снята с очереди ===")
                return
            if self._running_id != session_id:
                raise SessionError("Эта сессия сейчас не в очереди и не выполняется", 409)
            first_request = session_id not in self._cancelled
            self._cancelled.add(session_id)
            proc = self._proc
        if first_request:
            self.store.append_log(
                session_id,
                "=== Отмена запрошена: останавливаем процесс анализа немедленно ===")
        kill_process_tree(proc)

    def snapshot(self) -> dict:
        """Что сейчас в работе и кто за кем стоит - для списка сессий."""
        with self._cv:
            return {
                "running_id": self._running_id,
                "queued": list(self._queue),
            }

    def positions(self) -> "OrderedDict":
        """{id сессии: её номер в очереди, начиная с 1}."""
        with self._cv:
            return OrderedDict((sid, i + 1) for i, sid in enumerate(self._queue))

    # ---------- воркер ----------

    def _worker(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                session_id = self._queue.popleft()
                if session_id in self._cancelled:
                    continue          # отменили, пока ждала очереди
                self._running_id = session_id
            try:
                self._run_session(session_id)
            except Exception as e:  # noqa: BLE001 - воркер обязан пережить любую сессию
                try:
                    self.store.append_log(session_id, f"!!! Сбой прогона: {e}")
                    self.store.update(session_id, status="error", error=str(e),
                                      finished_at=time.time())
                except SessionError:
                    pass              # папку сессии могли удалить руками
            finally:
                with self._cv:
                    self._running_id = None
                    self._proc = None
                    self._cancelled.discard(session_id)

    def _is_cancelled(self, session_id) -> bool:
        with self._cv:
            return session_id in self._cancelled

    def _run_session(self, session_id):
        meta = self.store.get(session_id)
        mode = meta.get("mode") or "full"
        self.store.update(session_id, status="running", started_at=time.time())

        paths = self.store.prepare_run(session_id)
        log = lambda line: self.store.append_log(session_id, line)  # noqa: E731
        log(f"=== Запуск анализа: режим '{mode}' ===")

        args = {
            "base_config_path": self.base_config_path,
            "session_config_path": str(paths["config"]),
            "paths": {
                "base_files_dir": str(paths["base_files_dir"]),
                "full_projects_dir": str(paths["full_projects_dir"]),
                "scripts_dir": str(paths["scripts_dir"]),
                "helper_scripts_dir": str(paths["helper_scripts_dir"]),
                "input_dir": str(paths["data_dir"]),
                "output_dir": str(paths["output_dir"]),
            },
            "doc_types": meta.get("doc_types") or None,
            "skip_agents": (mode == "scripts"),
            "clear_previous": True,
        }
        tmp_dir = Path(tempfile.mkdtemp(prefix="ia_run_"))
        args_path = tmp_dir / "args.json"
        result_path = tmp_dir / "args.json.result.json"
        args_path.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")

        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
        )
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(RUNNER_SCRIPT), str(args_path)], **popen_kwargs)
        except OSError as e:
            log(f"!!! Не удалось запустить процесс анализа: {e}")
            self.store.update(session_id, status="error", error=str(e),
                              finished_at=time.time())
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        with self._cv:
            self._proc = proc
        if self._is_cancelled(session_id):   # отмена успела прийти в щель до старта
            kill_process_tree(proc)

        for line in proc.stdout:
            log(line.rstrip("\n"))
        proc.wait()

        cancelled = self._is_cancelled(session_id)
        result = None
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                result = None
        shutil.rmtree(tmp_dir, ignore_errors=True)

        if cancelled:
            log("=== Анализ остановлен пользователем ===")
            self.store.cleanup_after_cancel(session_id)
            log("=== Частичные результаты отменённого прогона очищены ===")
            self.store.update(session_id, status="cancelled",
                              error="Анализ отменён пользователем",
                              finished_at=time.time())
            return

        if result is not None and result.get("ok"):
            n = result.get("n_findings")
            log(f"=== Готово. Найдено замечаний: {n} ===")
            self.store.update(session_id, status="done", n_findings=n,
                              error=None, finished_at=time.time())
            return

        if result is not None and not result.get("ok"):
            err = result.get("error") or "неизвестная ошибка"
            log("!!! " + err)
            self.store.update(session_id, status="error", error=err,
                              finished_at=time.time())
            return

        # процесс завершился, не оставив result.json - упал неожиданно
        err = f"процесс анализа неожиданно завершился (код {proc.returncode})"
        log("!!! " + err)
        self.store.update(session_id, status="error", error=err, finished_at=time.time())
