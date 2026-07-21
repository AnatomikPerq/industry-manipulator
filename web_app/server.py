#!/usr/bin/env python3
"""
Локальный веб-интерфейс анализатора проектной документации.

Бэкенд на стандартной библиотеке Python (http.server) - без внешних зависимостей,
работает офлайн.

Единица работы - СЕССИЯ (sessions.py): комплект документов одного шкафа плюс её
прогон и её отчёт, со своей папкой на диске. Сессии создаются прямо в интерфейсе
и видны всем без авторизации (инструмент корпоративный). Поставив сессию на
исполнение, можно закрыть вкладку: статус, лог и отчёт живут на диске, а не в
памяти браузера или процесса.

СКРИПТЫ СЧИТАЮТСЯ СРАЗУ, В ОЧЕРЕДЬ ВСТАЁТ ТОЛЬКО СТАДИЯ ИИ (queue_worker.py):
извлечение и детерминированные чекеры грузят локальный процессор и друг другу не
мешают, а LM Studio на всех один. Раньше в очереди стоял весь прогон - и человек
ждал чужой работы с моделью ради находок чекера, которые считаются за секунды.

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
    POST /api/sessions/<id>/file-delete - удалить один файл {path}
    POST /api/sessions/<id>/set-type    - тип документа {name, type}
    POST /api/sessions/<id>/enqueue     - поставить на исполнение {mode}
    POST /api/sessions/<id>/cancel      - снять с очереди либо оборвать прогон
    GET  /api/sessions/<id>/log?since=N - лог прогона начиная со строки N,
                                          плюс стадия и текущий лист
    GET  /api/sessions/<id>/report      - отчёт этой сессии (JSON)
    GET  /api/sessions/<id>/report.pdf  - тот же отчёт одним PDF
    GET  /api/sessions/<id>/file?path=  - исходный документ (открыть во вкладке)
    GET  /api/sessions/<id>/fragment?…  - PNG с фрагментом чертежа у находки
"""

import argparse
import cgi
import json
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

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

import queue_worker                       # noqa: E402  (константы пула - во фронтенд)
from queue_worker import AnalysisQueue    # noqa: E402
from sessions import FULL_PROJECT_TYPE, SessionError, SessionStore  # noqa: E402

PROJECT_VERSION = "V1.6"

# Content-Type для просмотра исходных документов сессии прямо в браузере.
# PDF отдаём inline (вкладка откроет встроенный просмотрщик), книгу Excel -
# вложением: показать её браузер всё равно не умеет, а скачать - полезно.
VIEWABLE_TYPES = {
    ".pdf": ("application/pdf", "inline"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              "attachment"),
    ".xlsm": ("application/vnd.ms-excel.sheet.macroEnabled.12", "attachment"),
}

# Типы документов, которые принимает анализатор. Отсюда же фронтенд берёт список
# для выпадающего выбора типа у каждого загруженного файла - единый источник.
DOC_TYPES = [
    {"key": "scheme", "title": "Принципиальная схема (Э3)",
     "hint": "Векторный PDF монтажной/принципиальной схемы EPLAN"},
    {"key": "assembly", "title": "Сборочный чертёж (СБ)",
     "hint": "Векторный PDF сборочного чертежа шкафа: вид шкафа с размещением изделий"},
    {"key": "spec", "title": "Спецификация оборудования (СО)",
     "hint": "Книга Excel (.xlsx) со спецификацией по ГОСТ 21.110"},
    # Под этим ключом живут ТРИ вида табличных документов - вид определяется
    # по заголовкам таблицы уже при извлечении (netlist_to_json.detect_table_kind),
    # выбирать между ними пользователю не нужно.
    {"key": "netlist", "title": "Нетлист / табличный документ",
     "hint": "Таблица подключений (соединений) по ГОСТ, перечень входных/выходных "
             "сигналов ПЛК или кабельный журнал — вид таблицы распознаётся "
             "автоматически"},
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
                if action == "report.pdf":
                    return self._api_report_pdf(session_id)
                if action == "file":
                    query = parse_qs(parsed.query)
                    return self._api_file(session_id, query.get("path", [""])[0])
                if action == "fragment":
                    return self._api_fragment(session_id, parse_qs(parsed.query))
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
                    body = self._body_json()
                    # path, а не name: имена в подпапках-связках повторяются
                    STORE.delete_file(session_id, body.get("path") or body.get("name"))
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
        Номера в очередях считаем здесь, а не храним: очереди живут в памяти
        воркера, и их порядок - единственное, что его определяет."""
        positions = QUEUE.positions()
        llm_positions = QUEUE.llm_positions()
        snap = QUEUE.snapshot()
        sessions = []
        for meta in STORE.list():
            item = dict(meta)
            item["queue_position"] = positions.get(meta["id"])
            item["llm_position"] = llm_positions.get(meta["id"])
            item["n_files"] = len(STORE.files(meta["id"]))
            sessions.append(item)
        self._send_json({
            "sessions": sessions,
            "running": snap["running"],
            "queued": snap["queued"],
            "llm_queue": snap["llm_queue"],
            "llm_busy": snap["llm_busy"],
            "script_workers": queue_worker.SCRIPT_WORKERS,
        })

    def _session_view(self, session_id):
        meta = dict(STORE.get(session_id))
        meta["files"] = STORE.files(session_id)
        meta["queue_position"] = QUEUE.positions().get(session_id)
        meta["llm_position"] = QUEUE.llm_positions().get(session_id)
        meta["is_running"] = session_id in QUEUE.snapshot()["running"]
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
            # что именно считается прямо сейчас и на каком листе
            "stage": meta.get("stage"),
            "progress": meta.get("progress"),
            "llm_position": QUEUE.llm_positions().get(session_id),
        })

    # ---- просмотр исходных документов ----

    def _api_file(self, session_id, rel_path):
        """Отдаёт исходный документ сессии, чтобы открыть его в соседней вкладке.

        Путь берётся из поля "path" в списке файлов и проверяется в
        SessionStore.resolve_file: наружу пускается только base_files и
        full_projects, а не вся папка сессии (рядом лежат извлечённые данные и
        копия скриптов - показывать их незачем)."""
        target = STORE.resolve_file(session_id, rel_path)
        ctype, disposition = VIEWABLE_TYPES.get(
            target.suffix.lower(), ("application/octet-stream", "attachment"))
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # RFC 5987: имена документов кириллические, голым latin-1 их в заголовок
        # не положить - браузер получил бы кракозябры вместо имени файла
        self.send_header("Content-Disposition",
                         f"{disposition}; filename*=UTF-8''{quote(target.name)}")
        self.end_headers()
        self.wfile.write(data)

    def _api_report_pdf(self, session_id):
        """Отчёт сессии одним PDF - тем, что уходит инженеру и в архив проекта."""
        import report_pdf          # импорт здесь: тянет fitz, серверу он нужен
                                   # только в этом эндпоинте

        meta = STORE.get(session_id)
        report = STORE.report(session_id)      # 404, если отчёта ещё нет
        paths = STORE.paths_of(session_id)
        data = report_pdf.build(
            report=report,
            session=meta,
            manifest_path=paths["data_dir"] / "manifest.json",
            doc_types=DOC_TYPES,
            version=PROJECT_VERSION,
        )
        name = f"Отчёт — {meta['name']}.pdf"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(name)}")
        self.end_headers()
        self.wfile.write(data)

    def _api_fragment(self, session_id, query):
        """PNG с фрагментом чертежа вокруг места находки.

        Инженеру мало прочитать «обозначение QF1 отсутствует в спецификации» -
        ему нужно увидеть это место на листе. Координат в находке нет (чекеры
        работают с извлечённым текстом, а не с геометрией), поэтому место
        отыскивается поиском по самому PDF - см. fragment.py."""
        import fragment            # импорт здесь: тянет fitz

        paths = STORE.paths_of(session_id)
        one = lambda k: (query.get(k) or [""])[0]   # noqa: E731
        try:
            png, info = fragment.render(
                manifest_path=paths["data_dir"] / "manifest.json",
                document=one("document"),
                sheet=one("sheet"),
                needles=query.get("q") or [],
            )
        except fragment.FragmentError as e:
            # 404 с человеческим текстом: «фрагмент показать не удалось» - это
            # нормальный исход (надпись начерчена линиями, лист не тот), а не сбой
            raise SessionError(str(e), 404)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "no-store")
        # Чем оказался показанный кусок - в заголовки: подпись под картинкой
        # обязана называть ЛИСТ, который на ней действительно виден, а он не
        # всегда тот, что назван в находке (см. fragment.render).
        self.send_header("X-Fragment-Page", str(info["page"]))
        self.send_header("X-Fragment-Fallback", "1" if info["fallback"] else "0")
        self.send_header("X-Fragment-Hits", str(info["hits"]))
        self.end_headers()
        self.wfile.write(png)


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
