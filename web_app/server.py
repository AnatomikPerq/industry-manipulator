#!/usr/bin/env python3
"""
Локальный веб-интерфейс анализатора EPLAN-схем.

Бэкенд на стандартной библиотеке Python (http.server) - без внешних зависимостей,
работает офлайн. Сам анализ (analyzer_to_errors/main.py:run_pipeline) запускается
ОТДЕЛЬНЫМ ПОДПРОЦЕССОМ (_pipeline_runner.py), а не в потоке текущего процесса -
только так кнопка «Отменить анализ» может оборвать его мгновенно и гарантированно,
убив весь этот процесс и его потомков (эквивалент Ctrl+C во всех его окнах разом),
а не ждать, пока пайплайн сам заметит запрос на отмену где-то на границе стадии.

Запуск:
    python web_app/server.py            # http://localhost:8000
    python web_app/server.py --port 9000

Эндпоинты:
    GET  /                     - страница интерфейса
    GET  /static/<file>        - статика (logo, css, js)
    GET  /api/config           - типы документов, версия, адрес сервера ИИ
    POST /api/upload           - загрузка файлов (multipart) -> base_files
    GET  /api/files            - что сейчас лежит в base_files
    POST /api/analyze          - запустить анализ {mode, types}
    POST /api/cancel           - мгновенно оборвать текущий анализ (kill процесса)
    GET  /api/status           - статус текущего анализа + консоль (лог)
    GET  /api/report           - последний merged_report.json
    GET  /api/check-llm        - какие модели ИИ загружены/доступны на сервере
"""

import argparse
import cgi
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
ANALYZER_DIR = PROJECT_ROOT / "analyzer_to_errors"
STATIC_DIR = HERE / "static"
RUNNER_SCRIPT = HERE / "_pipeline_runner.py"
STORY_DIR = PROJECT_ROOT / "story"

# Пайплайн лежит рядом, в analyzer_to_errors - добавляем его в путь импорта.
# (нужен и здесь: конфиг, пути, resolve_path - используются напрямую, не только
# подпроцессом-раннером)
sys.path.insert(0, str(ANALYZER_DIR))

import main as pipeline          # noqa: E402
from llm_check import check_server_alive  # noqa: E402  (быстрая проверка сервера)

PROJECT_VERSION = "V1.2 beta"

# Типы документов, которые принимает анализатор. Отсюда же фронтенд берёт список
# для выпадающего выбора типа у каждого загруженного файла - единый источник.
DOC_TYPES = [
    {"key": "scheme", "title": "Принципиальная схема (Э3)",
     "hint": "Векторный PDF монтажной/принципиальной схемы EPLAN"},
    {"key": "assembly", "title": "Сборочный чертёж (СБ)",
     "hint": "Векторный PDF сборочного чертежа шкафа: вид шкафа с размещением изделий"},
    {"key": "spec", "title": "Спецификация оборудования (СО)",
     "hint": "Книга Excel (.xlsx) со спецификацией по ГОСТ 21.110"},
    {"key": "netlist", "title": "Нетлист внешних подключений",
     "hint": "Таблица подключений (соединений) по ГОСТ"},
]
VALID_TYPE_KEYS = {t["key"] for t in DOC_TYPES}

# .xlsx - для спецификации (единственный документ связки не в PDF).
ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xlsm"}


# =====================================================================
# Состояние текущего анализа (один прогон за раз - инструмент локальный)
# =====================================================================

class AnalysisState:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stage = "idle"          # idle | running | done | error | cancelled
        self.mode = None             # scripts | full
        self.log = deque(maxlen=5000)  # строки консоли (переживает отмену - не чистится)
        self.error = None
        self.started_at = None
        self.finished_at = None
        self.n_findings = None
        self.cancel_requested = False
        self.proc = None             # subprocess.Popen текущего прогона пайплайна

    def reset(self, mode):
        with self.lock:
            self.running = True
            self.stage = "running"
            self.mode = mode
            self.log.clear()
            self.error = None
            self.started_at = time.time()
            self.finished_at = None
            self.n_findings = None
            self.cancel_requested = False
            self.proc = None

    def add_log(self, line):
        with self.lock:
            self.log.append(line)

    def set_proc(self, proc):
        with self.lock:
            self.proc = proc

    def request_cancel(self):
        """Помечает анализ на отмену. Возвращает (proc, already_requested,
        was_running): proc - текущий подпроцесс (может быть None, если ещё не
        успел стартовать), was_running - было ли вообще что отменять."""
        with self.lock:
            if not self.running:
                return None, False, False
            already = self.cancel_requested
            self.cancel_requested = True
            return self.proc, already, True

    def is_cancel_requested(self):
        with self.lock:
            return self.cancel_requested

    def finish(self, n_findings=None, error=None, cancelled=False):
        with self.lock:
            self.running = False
            self.stage = "cancelled" if cancelled else ("error" if error else "done")
            self.error = error
            self.finished_at = time.time()
            self.n_findings = n_findings
            self.proc = None

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "stage": self.stage,
                "mode": self.mode,
                "log": list(self.log),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "n_findings": self.n_findings,
                "cancel_requested": self.cancel_requested,
            }


STATE = AnalysisState()


def _kill_process_tree(proc):
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


def _config_path():
    return str(ANALYZER_DIR / "config.yaml")


def _base_files_dir(cfg):
    return pipeline.resolve_path(cfg["paths"]["base_files_dir"])


# =====================================================================
# Пометки типа документа, выставленные пользователем в интерфейсе.
#
# Хранятся в файле-спутнике рядом с base_files (сама папка - вход для
# пайплайна, туда лишнего лучше не класть). Без этого при перезагрузке
# страницы пометки терялись: они жили только в памяти вкладки браузера,
# а не на сервере, и /api/files всякий раз заново гадал тип по имени файла.
# =====================================================================

_types_lock = threading.Lock()


def _types_sidecar_path(cfg):
    return _base_files_dir(cfg).parent / ".doc_types.json"


def _load_type_overrides(cfg):
    path = _types_sidecar_path(cfg)
    if not path.is_file():
        return {}
    try:
        with _types_lock:
            return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_type_overrides(cfg, overrides):
    path = _types_sidecar_path(cfg)
    with _types_lock:
        path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def _clear_type_overrides(cfg):
    path = _types_sidecar_path(cfg)
    if path.exists():
        path.unlink()


# =====================================================================
# Архив истории анализов: story/<имя первого файла>/<дата-время>/
# =====================================================================

def _sanitize_story_name(filename):
    stem = Path(filename).stem
    return re.sub(r'[\\/:*?"<>|]', "_", stem).strip() or "без_имени"


def _archive_run(types, started_at):
    """Копирует результаты завершённого прогона (и объединённый merged_report.json,
    и необъединённые report_1.json/report_2.json) в story/ - постоянный архив
    истории анализов, отдельный от output/, который каждый следующий запуск чистит.
    Папка: story/<имя первого файла анализа>/<дата-время анализа>/."""
    cfg = pipeline.load_config(_config_path())
    out_dir = pipeline.resolve_path(cfg["paths"]["output_dir"])
    if not out_dir.is_dir():
        return
    files = [p for p in out_dir.iterdir() if p.is_file()]
    if not files:
        return

    first_file = next(iter(types), None) if types else None
    folder_name = _sanitize_story_name(first_file) if first_file else "без_файла"
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started_at))
    dest = STORY_DIR / folder_name / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dest / f.name)
    STATE.add_log(f"=== Результаты сохранены в историю: story/{folder_name}/{stamp}/ ===")


# =====================================================================
# Запуск анализа в фоне (отдельным процессом - см. docstring модуля)
# =====================================================================

def _cleanup_after_cancel(cfg):
    """После жёсткой отмены подчищает только результаты незавершённого прогона:
    output/ (частично сформированные report_*.json / merged_report.json) и
    рабочую папку агента (your_helping_scripts_and_files). Файлы пользователя
    (base_files), уже извлечённые данные документов и скрипты-парсеры не трогаем -
    работа над ними была завершена корректно и пригодится на следующем запуске."""
    for key in ("output_dir", "helper_scripts_dir"):
        d = pipeline.resolve_path(cfg["paths"][key])
        if not d.exists():
            continue
        for item in d.iterdir():
            try:
                item.unlink() if item.is_file() else shutil.rmtree(item)
            except OSError:
                pass  # файл на миг остался занят ОС после kill - не критично


def _run_analysis(mode, types):
    """Фоновый прогон. mode: 'scripts' (без ИИ) | 'full' (со всеми стадиями).

    Пайплайн запускается подпроцессом _pipeline_runner.py, а не вызывается
    напрямую в этом потоке - только так /api/cancel может оборвать его мгновенно
    (см. _kill_process_tree), убив процесс целиком, а не дожидаясь кооперации."""
    started_at = time.time()
    args = {
        "config_path": _config_path(),
        "doc_types": types or None,
        "skip_agents": (mode == "scripts"),
        "clear_previous": True,
    }
    tmp_dir = Path(tempfile.mkdtemp(prefix="ia_run_"))
    args_path = tmp_dir / "args.json"
    result_path = tmp_dir / "args.json.result.json"
    args_path.write_text(json.dumps(args, ensure_ascii=False), encoding="utf-8")

    STATE.add_log(f"=== Запуск анализа: режим '{mode}' ===")

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
        STATE.add_log(f"!!! Не удалось запустить процесс анализа: {e}")
        STATE.finish(error=str(e))
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return

    STATE.set_proc(proc)
    if STATE.is_cancel_requested():  # отмена успела прийти в узкую щель до старта
        _kill_process_tree(proc)

    for line in proc.stdout:
        STATE.add_log(line.rstrip("\n"))
    proc.wait()

    cancelled = STATE.is_cancel_requested()
    result = None
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            result = None
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if cancelled:
        STATE.add_log("=== Анализ остановлен пользователем ===")
        cfg = pipeline.load_config(_config_path())
        _cleanup_after_cancel(cfg)
        STATE.add_log("=== Частичные результаты отменённого прогона очищены ===")
        STATE.finish(cancelled=True, error="Анализ отменён пользователем")
        return

    try:
        _archive_run(types, started_at)
    except Exception as e:  # noqa: BLE001 - архив не должен ронять сам анализ
        STATE.add_log(f"!!! Не удалось сохранить результаты в историю: {e}")

    if result is not None and result.get("ok"):
        n = result.get("n_findings")
        STATE.add_log(f"=== Готово. Найдено замечаний: {n} ===")
        STATE.finish(n_findings=n)
        return

    if result is not None and not result.get("ok"):
        err = result.get("error") or "неизвестная ошибка"
        STATE.add_log("!!! " + err)
        STATE.finish(error=err)
        return

    # процесс завершился, не оставив result.json - упал неожиданно
    err = f"процесс анализа неожиданно завершился (код {proc.returncode})"
    STATE.add_log("!!! " + err)
    STATE.finish(error=err)


# =====================================================================
# Проверка серверов ИИ (быстрая, через нативный API LM Studio)
# =====================================================================

def _native_models_url(base_url):
    """Из base_url вида http://host:1234/v1 делаем http://host:1234/api/v1/models -
    нативный эндпоинт LM Studio, где виден статус загрузки каждой модели."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root + "/api/v1/models"


def _check_llm():
    cfg = pipeline.load_config(_config_path())
    servers = cfg["llm_servers"]
    # какие модели реально нужны пайплайну (по config)
    wanted = {servers["agent_1"]["model"], servers["agent_2"]["model"]}

    # уникальные базовые адреса
    bases = {}
    for key in ("agent_1", "agent_2"):
        bases.setdefault(servers[key]["base_url"], servers[key])

    result = {"servers": [], "wanted_models": sorted(wanted)}
    for base_url, scfg in bases.items():
        entry = {"base_url": base_url, "reachable": False, "models": [], "error": None}
        try:
            url = _native_models_url(base_url)
            req = urllib.request.Request(url)
            if scfg.get("api_key") and scfg["api_key"] != "not-needed":
                req.add_header("Authorization", f"Bearer {scfg['api_key']}")
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.load(r)
            entry["reachable"] = True
            for m in data.get("models", []):
                if m.get("type") != "llm":
                    continue
                key = m.get("key")
                entry["models"].append({
                    "key": key,
                    "display_name": m.get("display_name") or key,
                    "params": m.get("params_string"),
                    "max_context": m.get("max_context_length"),
                    "loaded": bool(m.get("loaded_instances")),
                    "wanted": key in wanted,
                })
        except Exception as e:  # noqa: BLE001
            # запасной путь: OpenAI-совместимый /v1/models (без статуса загрузки)
            alive = check_server_alive(scfg)
            if alive["ok"]:
                entry["reachable"] = True
                for key in alive["models"]:
                    entry["models"].append({
                        "key": key, "display_name": key, "params": None,
                        "max_context": None, "loaded": None, "wanted": key in wanted,
                    })
            else:
                entry["error"] = f"{type(e).__name__}: {e}"
        result["servers"].append(entry)
    return result


# =====================================================================
# HTTP
# =====================================================================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # не засоряем stdout сервера запросами

    # ---- утилиты ответа ----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type):
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- GET ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".png": "image/png",
            }.get(Path(name).suffix, "application/octet-stream")
            return self._send_file(STATIC_DIR / name, ctype)
        if path == "/api/config":
            return self._api_config()
        if path == "/api/files":
            return self._api_files()
        if path == "/api/status":
            return self._send_json(STATE.snapshot())
        if path == "/api/report":
            return self._api_report()
        if path == "/api/check-llm":
            return self._api_check_llm()
        self.send_error(404)

    # ---- POST ----
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/upload":
            return self._api_upload()
        if path == "/api/analyze":
            return self._api_analyze()
        if path == "/api/cancel":
            return self._api_cancel()
        if path == "/api/set-type":
            return self._api_set_type()
        self.send_error(404)

    # ---- реализации ----
    def _api_config(self):
        cfg = pipeline.load_config(_config_path())
        servers = cfg["llm_servers"]
        self._send_json({
            "version": PROJECT_VERSION,
            "doc_types": DOC_TYPES,
            "llm_server": servers["agent_1"]["base_url"],
            "models": {
                "agent_1": servers["agent_1"]["model"],
                "agent_2": servers["agent_2"]["model"],
            },
        })

    def _api_files(self):
        cfg = pipeline.load_config(_config_path())
        base = _base_files_dir(cfg)
        overrides = _load_type_overrides(cfg)
        files = []
        if base.is_dir():
            import bundles
            import ingest
            # rglob, а не iterdir: комплекты можно разложить по подпапкам
            # ("base_files/связка 1/..."), и такие файлы тоже надо показать
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in ALLOWED_SUFFIXES:
                    continue
                if p.name.startswith("~$") or p.name.startswith("."):
                    continue          # временный файл открытой книги Excel
                # приоритет: явный выбор пользователя (сохранён на сервере) ->
                # иначе тип по имени файла (марка вида по ГОСТ или пометка)
                detected = overrides.get(p.name) or ingest.detect_doc_type(p.name)
                rel = p.relative_to(base)
                subfolder = rel.parts[0] if len(rel.parts) > 1 else None
                files.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "detected_type": detected,
                    # связка, в которую файл попадёт при анализе. Обычно None:
                    # все документы прогона - один проект (см. bundles.py), и
                    # подписывать это на каждой строке незачем. Имя появляется,
                    # только если пользователь сам разложил файлы по подпапкам.
                    "bundle": subfolder,
                })
        self._send_json({"files": files})

    def _api_upload(self):
        if STATE.running:
            return self._send_json({"error": "Идёт анализ, загрузка недоступна"}, 409)
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send_json({"error": "Ожидается multipart/form-data"}, 400)

        cfg = pipeline.load_config(_config_path())
        base = _base_files_dir(cfg)
        # загрузка через сайт ЗАМЕЩАЕТ прежний набор файлов - вместе с ним
        # сбрасываем и сохранённые пометки типа, они относились к старым файлам
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)
        _clear_type_overrides(cfg)

        form = cgi.FieldStorage(
            fp=self.rfile, headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype})

        saved, skipped = [], []
        items = form["files"] if "files" in form else []
        if not isinstance(items, list):
            items = [items]
        for item in items:
            if not getattr(item, "filename", None):
                continue
            name = Path(item.filename).name
            if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                skipped.append({"name": name,
                                "reason": "неподдерживаемый формат (нужен PDF для схем "
                                          "и чертежей либо .xlsx для спецификации)"})
                continue
            (base / name).write_bytes(item.file.read())
            saved.append(name)

        self._send_json({"saved": saved, "skipped": skipped})

    def _api_set_type(self):
        """Сохраняет выбор типа документа для ОДНОГО файла - вызывается фронтендом
        сразу при изменении select'а, чтобы пометка пережила перезагрузку страницы."""
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "Некорректный JSON"}, 400)

        name, doc_type = body.get("name"), body.get("type")
        if not name:
            return self._send_json({"error": "Не указано имя файла"}, 400)
        if doc_type and doc_type not in VALID_TYPE_KEYS:
            return self._send_json({"error": f"Недопустимый тип: {doc_type}"}, 400)

        cfg = pipeline.load_config(_config_path())
        base = _base_files_dir(cfg)
        if not (base / name).is_file():
            return self._send_json({"error": f"Файл не найден: {name}"}, 404)

        overrides = _load_type_overrides(cfg)
        if doc_type:
            overrides[name] = doc_type
        else:
            overrides.pop(name, None)  # пустой выбор - сброс пометки
        _save_type_overrides(cfg, overrides)
        self._send_json({"ok": True})

    def _api_analyze(self):
        if STATE.running:
            return self._send_json({"error": "Анализ уже идёт"}, 409)
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            return self._send_json({"error": "Некорректный JSON"}, 400)

        mode = body.get("mode", "full")
        if mode not in ("scripts", "full"):
            return self._send_json({"error": f"Неизвестный режим: {mode}"}, 400)
        types = body.get("types") or {}
        bad = {k: v for k, v in types.items() if v not in VALID_TYPE_KEYS}
        if bad:
            return self._send_json({"error": f"Недопустимые типы: {bad}"}, 400)

        cfg = pipeline.load_config(_config_path())
        base = _base_files_dir(cfg)
        has_files = base.is_dir() and any(
            p.suffix.lower() in ALLOWED_SUFFIXES for p in base.rglob("*") if p.is_file())
        if not has_files:
            return self._send_json({"error": "Нет загруженных файлов для анализа"}, 400)

        STATE.reset(mode)
        threading.Thread(target=_run_analysis, args=(mode, types), daemon=True).start()
        self._send_json({"started": True, "mode": mode})

    def _api_cancel(self):
        """Мгновенная отмена: убивает подпроцесс анализа целиком, вместе со всем
        деревом потомков (см. _kill_process_tree) - эквивалент Ctrl+C во всех его
        окнах разом, только гарантированный. Консоль не чистится - в неё лишь
        дописывается отметка об отмене. Частичные результаты прогона (output/,
        рабочая папка агента) подчистит фоновый поток _run_analysis, как только
        подтвердит, что процесс действительно завершился."""
        proc, already, was_running = STATE.request_cancel()
        if not was_running:
            return self._send_json({"error": "Сейчас анализ не выполняется"}, 409)
        if not already:
            STATE.add_log("=== Отмена запрошена: останавливаем процесс анализа немедленно ===")
        _kill_process_tree(proc)
        self._send_json({"cancelling": True})

    def _api_report(self):
        cfg = pipeline.load_config(_config_path())
        report_path = pipeline.resolve_path(cfg["paths"]["output_dir"]) / "merged_report.json"
        if not report_path.exists():
            return self._send_json({"error": "Отчёт ещё не сформирован"}, 404)
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return self._send_json({"error": f"Не удалось прочитать отчёт: {e}"}, 500)
        self._send_json(data)

    def _api_check_llm(self):
        try:
            self._send_json(_check_llm())
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)


def main():
    ap = argparse.ArgumentParser(description="Веб-интерфейс анализатора EPLAN-схем")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Интерфейс анализатора: http://{args.host}:{args.port}")
    print("Ctrl+C для остановки.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        server.shutdown()


if __name__ == "__main__":
    main()
