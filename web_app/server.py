#!/usr/bin/env python3
"""
Локальный веб-интерфейс анализатора проектной документации.

Бэкенд на стандартной библиотеке Python (http.server) - без внешних зависимостей,
работает офлайн.

Единица работы - СЕССИЯ (sessions.py): комплект документов одного шкафа плюс её
прогон и её отчёт, со своей папкой на диске. Сессии создаются прямо в интерфейсе,
видны всем без авторизации (инструмент корпоративный) и выполняются глобальной
очередью строго по одной (queue_worker.py) - LM Studio на всех один. Поставив
сессию в очередь, можно закрыть вкладку: статус, лог и отчёт живут на диске, а
не в памяти браузера или процесса.

Сам анализ (analyzer_to_errors/main.py:run_pipeline) запускается ОТДЕЛЬНЫМ
ПОДПРОЦЕССОМ (_pipeline_runner.py), а не в потоке текущего процесса - только так
кнопка «Отменить анализ» может оборвать его мгновенно и гарантированно, убив
весь этот процесс и его потомков (эквивалент Ctrl+C во всех его окнах разом),
а не ждать, пока пайплайн сам заметит запрос на отмену где-то на границе стадии.

Запуск:
    python web_app/server.py            # http://localhost:8000
    python web_app/server.py --port 9000

Эндпоинты:
    GET  /                              - страница интерфейса
    GET  /static/<file>                 - статика (logo, css, js)
    GET  /api/config                    - типы документов, версия, адрес сервера ИИ
    GET  /api/check-llm                 - какие модели ИИ загружены/доступны
    GET  /api/sessions                  - список сессий + состояние очереди
    POST /api/sessions                  - создать сессию {name}
    GET  /api/sessions/<id>             - метаданные сессии + её файлы
    POST /api/sessions/<id>/rename      - переименовать {name}
    POST /api/sessions/<id>/delete      - удалить сессию целиком
    POST /api/sessions/<id>/upload      - дозагрузить файлы (multipart)
    POST /api/sessions/<id>/file-delete - удалить один файл {name}
    POST /api/sessions/<id>/set-type    - тип документа {name, type}
    POST /api/sessions/<id>/enqueue     - поставить в очередь {mode}
    POST /api/sessions/<id>/cancel      - снять с очереди либо оборвать прогон
    GET  /api/sessions/<id>/log?since=N - лог прогона начиная со строки N
    GET  /api/sessions/<id>/report      - отчёт этой сессии
"""

import argparse
import cgi
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
ANALYZER_DIR = PROJECT_ROOT / "analyzer_to_errors"
STATIC_DIR = HERE / "static"

# Пайплайн лежит рядом, в analyzer_to_errors - добавляем его в путь импорта.
# (нужен и здесь: конфиг, resolve_path, detect_doc_type - используются напрямую,
# не только подпроцессом-раннером)
sys.path.insert(0, str(ANALYZER_DIR))

import main as pipeline          # noqa: E402
from llm_check import check_server_alive  # noqa: E402  (быстрая проверка сервера)

from queue_worker import AnalysisQueue    # noqa: E402
from sessions import FULL_PROJECT_TYPE, SessionError, SessionStore  # noqa: E402

PROJECT_VERSION = "V1.3"

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
    # Не вид документа, а КОНТЕЙНЕР документов: альбом целиком на 200+ листов.
    # Пайплайн режет его на отдельные документы по графе «наименование»
    # штампа и раскладывает по связкам-шкафам (full_project.py), так что тип
    # каждой части определяется потом сам и указывать его не нужно.
    {"key": FULL_PROJECT_TYPE, "title": "Полный проект (альбом целиком)",
     "hint": "Один PDF на 180-300 листов со схемами, чертежами и спецификацией "
             "нескольких шкафов. Будет автоматически разрезан на документы, "
             "каждый шкаф станет отдельной связкой"},
]
VALID_TYPE_KEYS = {t["key"] for t in DOC_TYPES}


def _config_path():
    return str(ANALYZER_DIR / "config.yaml")


STORE = SessionStore()
QUEUE = AnalysisQueue(STORE, _config_path())


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

    def _body_json(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or "{}")
        except json.JSONDecodeError:
            raise SessionError("Некорректный JSON", 400)

    # ---- маршрутизация ----
    #
    # Пути сессий содержат id, поэтому сравнением строк уже не обойтись:
    # разбираем /api/sessions/<id>/<действие> на части.

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/":
                return self._send_file(STATIC_DIR / "index.html",
                                       "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/api/config":
                return self._api_config()
            if path == "/api/check-llm":
                return self._api_check_llm()
            if path == "/api/sessions":
                return self._api_sessions_list()
            if path.startswith("/api/sessions/"):
                session_id, action = self._split_session_path(path)
                if action is None:
                    return self._send_json(self._session_view(session_id))
                if action == "log":
                    since = parse_qs(parsed.query).get("since", ["0"])[0]
                    return self._api_log(session_id, since)
                if action == "report":
                    return self._send_json(STORE.report(session_id))
            self.send_error(404)
        except SessionError as e:
            self._send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/sessions":
                return self._api_session_create()
            if path.startswith("/api/sessions/"):
                session_id, action = self._split_session_path(path)
                if action == "rename":
                    meta = STORE.rename(session_id, self._body_json().get("name"))
                    return self._send_json({"ok": True, "name": meta["name"]})
                if action == "delete":
                    STORE.delete(session_id)
                    return self._send_json({"ok": True})
                if action == "upload":
                    return self._api_upload(session_id)
                if action == "file-delete":
                    STORE.delete_file(session_id, self._body_json().get("name"))
                    return self._send_json({"ok": True})
                if action == "set-type":
                    body = self._body_json()
                    if not body.get("name"):
                        raise SessionError("Не указано имя файла", 400)
                    STORE.set_type(session_id, body["name"], body.get("type"),
                                   VALID_TYPE_KEYS)
                    return self._send_json({"ok": True})
                if action == "enqueue":
                    return self._api_enqueue(session_id)
                if action == "cancel":
                    QUEUE.cancel(session_id)
                    return self._send_json({"ok": True})
            self.send_error(404)
        except SessionError as e:
            self._send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    @staticmethod
    def _split_session_path(path):
        """/api/sessions/<id>[/<действие>] -> (id, действие|None)."""
        rest = path[len("/api/sessions/"):].strip("/")
        parts = rest.split("/", 1)
        session_id = parts[0]
        action = parts[1] if len(parts) > 1 else None
        return session_id, action

    def _serve_static(self, name):
        # имя приходит из URL - не даём выйти за пределы static/
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            return self.send_error(404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
        }.get(target.suffix, "application/octet-stream")
        self._send_file(target, ctype)

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

    def _api_check_llm(self):
        try:
            self._send_json(_check_llm())
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def _api_sessions_list(self):
        """Список всех сессий - общий для всех пользователей, локализации нет.
        Номер в очереди считаем здесь, а не храним: очередь живёт в памяти
        воркера, и её порядок - единственное, что его определяет."""
        positions = QUEUE.positions()
        snap = QUEUE.snapshot()
        sessions = []
        for meta in STORE.list():
            item = dict(meta)
            item["queue_position"] = positions.get(meta["id"])
            item["n_files"] = len(STORE.files(meta["id"]))
            sessions.append(item)
        self._send_json({
            "sessions": sessions,
            "running_id": snap["running_id"],
            "queued": snap["queued"],
        })

    def _session_view(self, session_id):
        meta = dict(STORE.get(session_id))
        meta["files"] = STORE.files(session_id)
        meta["queue_position"] = QUEUE.positions().get(session_id)
        meta["is_running"] = QUEUE.snapshot()["running_id"] == session_id
        return meta

    def _api_session_create(self):
        meta = STORE.create(self._body_json().get("name"))
        self._send_json(meta, 201)

    def _api_upload(self, session_id):
        """Дозагрузка файлов в сессию. В отличие от прежней глобальной загрузки,
        НИЧЕГО не стирает: у каждой сессии свой base_files, и затирать чужие
        файлы больше нечем."""
        STORE.get(session_id)      # 404, если сессии нет - до чтения тела запроса
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise SessionError("Ожидается multipart/form-data", 400)

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
            try:
                saved.append(STORE.save_upload(session_id, item.filename, item.file.read()))
            except SessionError as e:
                if e.status == 409:
                    raise      # сессия занята - дальше загружать нечего
                skipped.append({"name": Path(item.filename).name, "reason": str(e)})
        self._send_json({"saved": saved, "skipped": skipped})

    def _api_enqueue(self, session_id):
        mode = self._body_json().get("mode", "full")
        if mode not in ("scripts", "full"):
            raise SessionError(f"Неизвестный режим: {mode}", 400)
        position = QUEUE.enqueue(session_id, mode)
        self._send_json({"ok": True, "mode": mode, "queue_position": position})

    def _api_log(self, session_id, since):
        """Лог отдаём порциями по смещению: браузер присылает номер строки, на
        которой остановился, и получает только новые. Иначе поллинг раз в
        секунду каждый раз тащил бы весь лог целиком."""
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = 0
        lines, next_index = STORE.read_log(session_id, since)
        meta = STORE.get(session_id)
        self._send_json({
            "lines": lines,
            "next": next_index,
            "status": meta["status"],
            "error": meta.get("error"),
            "n_findings": meta.get("n_findings"),
            "queue_position": QUEUE.positions().get(session_id),
        })


def main():
    ap = argparse.ArgumentParser(description="Веб-интерфейс анализатора документации")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    QUEUE.start()
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
