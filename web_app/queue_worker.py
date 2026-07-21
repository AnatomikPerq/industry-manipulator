#!/usr/bin/env python3
"""
Исполнение прогонов: скрипты - сразу, к серверу ИИ - по очереди.

ЧТО ЗДЕСЬ ГЛАВНОЕ. Раньше в очередь вставал ВЕСЬ прогон, и это было неверно.
Прогон состоит из двух совершенно разных по природе стадий:

  * СКРИПТЫ (извлечение PDF, детерминированные чекеры, сверка связок) - считают
    локальный процессор, идут секунды-минуты и друг другу не мешают ничем;
  * АГЕНТЫ - это LM Studio, и он на всех один. Две сессии, пущенные к нему
    разом, дерутся за одну видеокарту и обе идут медленнее, чем шли бы
    по очереди.

Выстраивая в очередь целиком, мы заставляли человека ждать чужой сорокаминутной
работы с моделью ради находок чекера, которые считаются за секунды и никакой
модели не требуют. Тем более что режим «без ИИ — только скрипты» ждал в той же
очереди, хотя к серверу ИИ не обращается вовсе.

Теперь очередей две:
  * СКРИПТОВЫЙ СЛОТ (SCRIPT_WORKERS штук) - сессия начинает считаться сразу,
    если слот свободен. Слотов конечное число: у машины конечное число ядер, и
    десять одновременных разборов альбома по 300 листов просто загрузят диск.
  * СЛОТ ИИ (ровно один) - его сессия занимает ТОЛЬКО на стадию агентов.
    Дойдя до неё, подпроцесс печатает маркер и замирает на своём stdin;
    воркер ставит сессию в llm-очередь, а когда слот освободится, отвечает
    строкой в stdin - и агенты стартуют. См. _pipeline_runner.wait_for_llm_slot.

СЛОТЫ, А НЕ ФИКСИРОВАННЫЙ ПУЛ ПОТОКОВ - И ЭТО ГЛАВНАЯ ПРАВКА V1.7. Раньше
воркеров было ровно SCRIPT_WORKERS, и поток воркера оставался занят сессией до
самого конца прогона - в том числе всё время, пока она СТОЯЛА В ОЧЕРЕДИ К ИИ.
Достаточно было четырёх сессий в полном режиме, дошедших до гейта, чтобы пятая
не начала считать скрипты вовсе: все потоки заняты ожиданием единственного
слота ИИ. То есть модуль ровно тем и грешил, что объявлял исправленным.

Теперь скриптовый слот ОТПУСКАЕТСЯ ПЕРЕД ОЖИДАНИЕМ ИИ (_pass_llm_gate), и на
его место немедленно входит следующая сессия. Поток у сессии свой на всё время
прогона - он обязан продолжать читать stdout подпроцесса, - но потоков этих
столько, сколько живых прогонов, а не сколько разрешено считать одновременно.
Число сессий, одновременно ждущих у гейта, равно длине очереди к ИИ: каждая
держит живой подпроцесс, потому что после своей очереди он пойдёт дальше. Это
свойство самого гейта, а не пула.

Прогон исполняется отдельным процессом, а не в этом потоке - только так отмена
может оборвать его мгновенно, убив всё дерево процессов, а не дожидаясь, пока
пайплайн заметит просьбу остановиться.
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

# Сколько сессий считают СКРИПТЫ одновременно. Скриптовая стадия упирается в
# процессор и диск, а не в сеть: смысл поднимать это значение есть ровно до
# числа ядер. Четыре - осторожная величина для рабочей станции бюро; на сервере
# её можно поднять, ничего в коде не меняя (изоляция сессий - по путям).
#
# Это ЧИСЛО СЛОТОВ, а не число потоков: слот сессия занимает только на время
# скриптов и отпускает его, уходя ждать очереди к ИИ.
SCRIPT_WORKERS = 4

# Слот к серверу ИИ ровно один и поднимать его нельзя: LM Studio на всех один.
# Если однажды серверов станет несколько, поднимать надо ЗДЕСЬ и раздавать
# воркеру адрес занятого слота, а не просто увеличивать число.
LLM_SLOTS = 1

# Маркер, которым подпроцесс просится на стадию агентов (см. _pipeline_runner).
LLM_WAIT_MARKER = "@@LLM_WAIT"

# Маркер сообщения о ходе разбора (см. data/base_analysis_scripts/progress.py).
# Такие строки в лог НЕ кладутся: альбом на 300 листов дал бы 300 строк шума.
PROGRESS_MARKER = "@@PROGRESS"


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
    """Скриптовые слоты + один слот к серверу ИИ.

    Единственный источник правды о статусе - session.json на диске (см.
    sessions.py). В памяти лежат только очереди и хэндлы текущих процессов:
    всё, что нужно, чтобы отменить прогон и чтобы после перезапуска сервера
    ничего не потерялось.
    """

    def __init__(self, store, base_config_path):
        self.store = store
        self.base_config_path = str(base_config_path)
        self._cv = threading.Condition()

        self._pending = deque()        # ждут свободного скриптового слота
        self._running = {}             # id сессии -> Popen
        self._script_busy = 0          # сколько скриптовых слотов занято
        self._script_held = set()      # id сессий, держащих скриптовый слот
        self._llm_queue = deque()      # id сессий, ждущих слота к ИИ
        self._llm_busy = 0             # сколько слотов ИИ занято
        self._cancelled = set()        # id, для которых пришла отмена
        self._progress_written = {}    # id -> когда прогресс последний раз лёг на диск
        self._progress_pending = {}    # id -> придержанное сообщение о листе

    # ---------- запуск ----------

    def start(self):
        """Поднимает диспетчер и возвращает очередь в то состояние, в котором её
        застал перезапуск сервера."""
        for meta in self.store.restore_after_restart():
            self._pending.append(meta["id"])
        threading.Thread(target=self._dispatch, daemon=True).start()

    # ---------- скриптовые слоты ----------

    def _release_script(self, session_id) -> None:
        """Отпустить скриптовый слот. Идемпотентно: зовётся и на гейте к ИИ, и
        в finally прогона, и порядок между ними не гарантирован."""
        with self._cv:
            if session_id in self._script_held:
                self._script_held.discard(session_id)
                self._script_busy -= 1
                self._cv.notify_all()

    def _dispatch(self):
        """Единственный поток-раздатчик: пускает сессию, как только освободился
        скриптовый слот, и заводит ей собственный поток на весь прогон.

        Поток нужен именно на весь прогон: он читает stdout подпроцесса
        построчно (лог, прогресс, маркер гейта), и бросить это чтение нельзя.
        А вот СЛОТ прогон отдаёт раньше - уходя ждать очереди к ИИ.
        """
        while True:
            with self._cv:
                while not self._pending or self._script_busy >= SCRIPT_WORKERS:
                    self._cv.wait()
                session_id = self._pending.popleft()
                if session_id in self._cancelled:
                    continue          # отменили, пока ждала очереди
                self._script_busy += 1
                self._script_held.add(session_id)
                self._running[session_id] = None
            threading.Thread(target=self._session_thread,
                             args=(session_id,), daemon=True).start()

    def _session_thread(self, session_id):
        """Один прогон целиком. Обязан пережить любую сессию: упавшая сессия не
        должна ни утащить с собой слот, ни оставить очередь без раздатчика."""
        try:
            self._run_session(session_id)
        except Exception as e:  # noqa: BLE001
            try:
                self.store.append_log(session_id, f"!!! Сбой прогона: {e}")
                self.store.update(session_id, status="error", error=str(e),
                                  finished_at=time.time(), stage=None, progress=None)
            except SessionError:
                pass                  # папку сессии могли удалить руками
        finally:
            self._release_script(session_id)
            try:
                # Нарезка альбома создаёт документы уже ВНУТРИ прогона, а
                # очистка перед ним стирает прошлые - число файлов сессии за
                # прогон меняется, и в списке сессий оно должно сойтись.
                self.store.refresh_file_count(session_id)
            except SessionError:
                pass
            with self._cv:
                self._running.pop(session_id, None)
                if session_id in self._llm_queue:
                    self._llm_queue.remove(session_id)
                self._cancelled.discard(session_id)
                self._cv.notify_all()

    # ---------- публичное API ----------

    def enqueue(self, session_id, mode) -> int:
        """Ставит сессию на исполнение и возвращает её позицию в скриптовой
        очереди (0 - есть свободный воркер, начнём считать сейчас же)."""
        meta = self.store.get(session_id)
        if meta["status"] in ("queued", "running"):
            raise SessionError("Сессия уже в очереди или выполняется", 409)
        if not self.store.has_files(session_id):
            raise SessionError("В сессии нет файлов для анализа", 400)

        self.store.update(session_id, status="queued", mode=mode,
                          queued_at=time.time(), started_at=None, finished_at=None,
                          n_findings=None, error=None, stage=None, progress=None,
                          llm_position=None)
        with self._cv:
            self._cancelled.discard(session_id)
            self._pending.append(session_id)
            # Свободные слоты считаем по _script_busy, а не по числу живых
            # прогонов: сессия, ушедшая ждать очереди к ИИ, слот уже отдала и
            # никому не мешает, хотя её процесс жив и в _running она есть.
            free = max(SCRIPT_WORKERS - self._script_busy, 0)
            position = max(len(self._pending) - free, 0)
            self._cv.notify_all()
        self.store.append_log(session_id, "=== Сессия принята к исполнению ===")
        return position

    def cancel(self, session_id) -> None:
        """Отмена на любой стадии: в очереди, на скриптах, в ожидании ИИ, на ИИ.

        Из очереди - просто выбрасываем id, убивать нечего. На ходу - убиваем
        дерево процессов; довести сессию до статуса cancelled и подчистить
        недописанный output/ - работа самого воркера, он как раз ждёт на чтении
        stdout убитого процесса. Ожидание слота ИИ обрывается тем же убийством:
        подпроцесс висит на read() своего stdin, и смерть процесса снимает это
        ожидание вернее любого флага.
        """
        with self._cv:
            if session_id in self._pending:
                self._pending.remove(session_id)
                self._cancelled.add(session_id)
                self.store.update(session_id, status="cancelled",
                                  finished_at=time.time(), stage=None, progress=None,
                                  error="Сессия снята с очереди пользователем")
                self.store.append_log(session_id, "=== Сессия снята с очереди ===")
                return
            if session_id not in self._running:
                raise SessionError("Эта сессия сейчас не в очереди и не выполняется", 409)
            first_request = session_id not in self._cancelled
            self._cancelled.add(session_id)
            if session_id in self._llm_queue:
                self._llm_queue.remove(session_id)
            proc = self._running.get(session_id)
        if first_request:
            self.store.append_log(
                session_id,
                "=== Отмена запрошена: останавливаем процесс анализа немедленно ===")
        kill_process_tree(proc)

    def snapshot(self) -> dict:
        """Что сейчас считается и кто чего ждёт - для списка сессий.

        running - все живые прогоны, script_busy - сколько из них реально
        занимают процессор. Числа расходятся ровно на сессии, стоящие в
        очереди к ИИ: их процесс жив, но считать он ничего не считает.
        """
        with self._cv:
            return {
                "running": sorted(self._running),
                "script_busy": self._script_busy,
                "script_slots": SCRIPT_WORKERS,
                "queued": list(self._pending),
                "llm_queue": list(self._llm_queue),
                "llm_busy": self._llm_busy,
            }

    def positions(self) -> "OrderedDict":
        """{id сессии: её номер в скриптовой очереди, начиная с 1}."""
        with self._cv:
            return OrderedDict((sid, i + 1) for i, sid in enumerate(self._pending))

    def llm_positions(self) -> "OrderedDict":
        """{id сессии: её номер в очереди К СЕРВЕРУ ИИ, начиная с 1}."""
        with self._cv:
            return OrderedDict((sid, i + 1) for i, sid in enumerate(self._llm_queue))

    def _is_cancelled(self, session_id) -> bool:
        with self._cv:
            return session_id in self._cancelled

    # ---------- слот к серверу ИИ ----------

    def _acquire_llm(self, session_id) -> bool:
        """Дождаться своей очереди к серверу ИИ. False - отменили, пока ждали."""
        with self._cv:
            self._llm_queue.append(session_id)
            while True:
                if session_id in self._cancelled:
                    if session_id in self._llm_queue:
                        self._llm_queue.remove(session_id)
                    return False
                first = self._llm_queue[0] if self._llm_queue else None
                if first == session_id and self._llm_busy < LLM_SLOTS:
                    self._llm_queue.popleft()
                    self._llm_busy += 1
                    return True
                self._cv.wait(timeout=1.0)

    def _release_llm(self):
        with self._cv:
            if self._llm_busy > 0:
                self._llm_busy -= 1
            self._cv.notify_all()

    # ---------- один прогон ----------

    def _run_session(self, session_id):
        meta = self.store.get(session_id)
        mode = meta.get("mode") or "full"
        self.store.update(session_id, status="running", started_at=time.time(),
                          stage="скрипты")

        paths, doc_types = self.store.prepare_run(session_id)
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
            # уже приведены к ключам, которых ждёт ingest (относительно
            # base_files) - см. SessionStore.prepare_run
            "doc_types": doc_types or None,
            "skip_agents": (mode == "scripts"),
            "clear_previous": True,
        }
        tmp_dir = Path(tempfile.mkdtemp(prefix="ia_run_"))
        args_path = tmp_dir / "args.json"
        result_path = tmp_dir / "args.json.result.json"
        args_path.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")

        popen_kwargs = dict(
            # stdin нужен, чтобы отпускать подпроцесс на стадию агентов:
            # он ждёт нашей строки, а не опрашивает файл-семафор
            stdin=subprocess.PIPE,
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
                              finished_at=time.time(), stage=None)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return

        with self._cv:
            self._running[session_id] = proc
        if self._is_cancelled(session_id):   # отмена успела прийти в щель до старта
            kill_process_tree(proc)

        held_llm = False
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")

                if line.startswith(PROGRESS_MARKER):
                    self._note_progress(session_id, line)
                    continue

                if line.startswith(LLM_WAIT_MARKER):
                    held_llm = self._pass_llm_gate(session_id, proc, log)
                    continue

                log(line)
            proc.wait()
        finally:
            if held_llm:
                self._release_llm()

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
                              finished_at=time.time(), stage=None, progress=None,
                              llm_position=None)
            return

        if result is not None and result.get("ok"):
            n = result.get("n_findings")
            log(f"=== Готово. Найдено замечаний: {n} ===")
            self.store.update(session_id, status="done", n_findings=n,
                              error=None, finished_at=time.time(),
                              stage=None, progress=None, llm_position=None)
            return

        if result is not None and not result.get("ok"):
            err = result.get("error") or "неизвестная ошибка"
            log("!!! " + err)
            self.store.update(session_id, status="error", error=err,
                              finished_at=time.time(), stage=None, progress=None,
                              llm_position=None)
            return

        # процесс завершился, не оставив result.json - упал неожиданно
        err = f"процесс анализа неожиданно завершился (код {proc.returncode})"
        log("!!! " + err)
        self.store.update(session_id, status="error", error=err,
                          finished_at=time.time(), stage=None, progress=None,
                          llm_position=None)

    def _pass_llm_gate(self, session_id, proc, log) -> bool:
        """Подпроцесс досчитал скрипты и просится к серверу ИИ.

        Возвращает True, если слот ИИ занят нами и его надо будет отпустить.
        """
        self.store.update(session_id, stage="очередь к ИИ", progress=None)
        log("=== Скрипты отработали. Ожидание очереди к серверу ИИ ===")

        # Скриптовый слот отдаём ЗДЕСЬ, до ожидания. Дальше эта сессия
        # процессор не занимает - она стоит в очереди к LM Studio, - и держать
        # из-за неё чужие прогоны незачем. Именно этим раньше и вырождался
        # весь замысел: четыре сессии, дошедшие до гейта, занимали все четыре
        # воркера, и пятая не начинала считать скрипты вовсе.
        self._release_script(session_id)

        if not self._acquire_llm(session_id):
            return False        # отменили, пока стояли в очереди

        self.store.update(session_id, stage="ИИ", llm_position=None)
        log("=== Очередь подошла: запуск анализа нейросетями ===")
        try:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            pass                # процесс уже мёртв - воркер увидит это на stdout
        return True

    def _note_progress(self, session_id, line):
        """Строка @@PROGRESS от парсера -> поле progress в session.json.

        В лог такие строки не идут: на альбоме их триста штук, и читать после
        них настоящий лог было бы нельзя.
        """
        try:
            payload = json.loads(line[len(PROGRESS_MARKER):].strip())
        except (json.JSONDecodeError, ValueError):
            return
        kind = payload.get("kind")

        # Придерживаем запись: листы летят десятками в секунду, а браузер
        # опрашивает статус раз в секунду - чаще писать файл незачем. Смену
        # документа, стадию и конец работы пишем всегда: их пропуск был бы виден.
        #
        # Придержанное сообщение ЗАПОМИНАЕТСЯ, а не выбрасывается, и уходит на
        # диск следующим тиком. Иначе последний лист документа (после которого
        # сообщений уже не будет) терялся, и на экране навсегда оставался
        # предпоследний - а на быстрых документах и вовсе первый попавшийся.
        now = time.monotonic()
        if kind == "page":
            self._progress_pending[session_id] = payload
            if now - self._progress_written.get(session_id, 0.0) < 0.4:
                return
            payload = self._progress_pending.pop(session_id, payload)
        else:
            self._progress_pending.pop(session_id, None)
        self._progress_written[session_id] = now

        try:
            if kind == "done":
                self.store.update(session_id, progress=None)
                return
            meta = self.store.get(session_id)
            progress = dict(meta.get("progress") or {})
            if kind == "document":
                # новый документ - счётчик листов прежнего больше не о чём
                progress.update(document=payload.get("name"),
                                doc_type=payload.get("doc_type"),
                                doc_index=payload.get("index"),
                                doc_total=payload.get("total"),
                                # по нему интерфейс подсвечивает строку в списке файлов
                                path=payload.get("path"),
                                page=None, page_total=None, stage=None)
            elif kind == "page":
                progress.update(page=payload.get("page"),
                                page_total=payload.get("total"),
                                stage=payload.get("stage"))
            elif kind == "stage":
                progress.update(stage=payload.get("stage"), page=None, page_total=None)
            else:
                return
            self.store.update(session_id, progress=progress)
        except SessionError:
            pass                # сессию удалили прямо во время прогона
