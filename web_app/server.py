#!/usr/bin/env python3
"""
Локальный веб-интерфейс анализатора проектной документации.

Бэкенд на стандартной библиотеке Python (http.server) - без внешних зависимостей,
работает офлайн.

Единица работы - СЕССИЯ (sessions.py): комплект документов одного шкафа плюс её
прогон и её отчёт, со своей папкой на диске. Поставив сессию на исполнение,
можно закрыть вкладку: статус, лог и отчёт живут на диске, а не в памяти
браузера или процесса.

ВХОД ПО ЛОГИНУ (users.py): у сессии есть владелец, человек видит только свои
сессии, администратор - все и управляет пользователями. Разграничение мягкое
(токен в cookie, пароли в открытом виде): цель - развести рабочие пространства
коллег в доверенной сети, а не устоять против атаки.

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
    POST /api/auth/login                - вход по логину {login[, password]}
    POST /api/auth/register             - регистрация обычного пользователя {login}
    POST /api/auth/logout               - выход (гасит токен)
    GET  /api/auth/me                   - кто вошёл (или null)
    GET  /api/users                     - список пользователей (админ)
    POST /api/users                     - создать пользователя (админ)
    POST /api/users/<login>/{delete,update} - удалить/изменить (админ)
    GET  /api/config                    - типы документов, версия, адрес сервера ИИ
    GET  /api/check-llm                 - какие модели ИИ загружены/доступны
    GET  /api/models                    - список моделей сервера для выбора в интерфейсе
    GET  /api/lmstudio/models           - все модели сервера со статусом (админ)
    POST /api/lmstudio/load             - загрузить модель в память {model, params} (админ)
    POST /api/lmstudio/unload           - выгрузить модель {instance_id} (админ)
    GET  /api/admin/config              - глобальный конфиг ИИ (админ)
    POST /api/admin/config              - сохранить конфиг ИИ в config.local.yaml (админ)
    GET  /api/sessions                  - список сессий + состояние очереди
    POST /api/sessions                  - создать сессию {name}
    GET  /api/sessions/<id>             - метаданные сессии + её файлы
    POST /api/sessions/<id>/rename      - переименовать {name}
    POST /api/sessions/<id>/delete      - удалить сессию целиком
    POST /api/sessions/<id>/upload      - дозагрузить файлы (multipart)
    POST /api/sessions/<id>/file-delete - удалить один файл {path}
    POST /api/sessions/<id>/set-type    - тип документа {name, type}
    POST /api/sessions/<id>/set-llm     - модели и число агентов этой сессии
    POST /api/sessions/<id>/enqueue     - поставить на исполнение {mode}
    POST /api/sessions/<id>/cancel      - снять с очереди либо оборвать прогон
    GET  /api/sessions/<id>/log?since=N - лог прогона начиная со строки N,
                                          плюс стадия и текущий лист
    GET  /api/sessions/<id>/report      - отчёт этой сессии (JSON)
    GET  /api/sessions/<id>/report.pdf  - тот же отчёт одним PDF
    GET  /api/sessions/<id>/file?path=  - исходный документ (открыть во вкладке)
    GET  /api/sessions/<id>/fragment?…  - PNG с фрагментом чертежа у находки
    GET  /api/sessions/<id>/lmstudio.log - транскрипт обмена с ИИ за прогон
    GET  /api/chat                      - активный чат пользователя (сообщения + модель)
    GET  /api/chat/file?path=           - файл/картинка, приложенные к сообщению
    POST /api/chat/set-model            - выбрать модель для чата {model}
    POST /api/chat/upload               - приложить файлы к чату (multipart)
    POST /api/chat/send                 - отправить сообщение, ответ ПОТОКОМ (ndjson)
    POST /api/chat/new                  - архивировать текущий чат и начать новый
"""

import argparse
import json
import mimetypes
import os
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from paths import ANALYZER_DIR, PROJECT_ROOT, setup_console_utf8  # noqa: E402

HERE = Path(__file__).resolve().parent
# Статика (index.html, css, js) ВКОМПИЛИРОВАНА в exe (см. package.spec: datas
# кладут её в _internal/web_app/static). В собранном виде __file__ указывает в
# архив, а не на диск, поэтому берём папку из распакованного бандла
# (sys._MEIPASS = _internal), а не из HERE.
if getattr(sys, "frozen", False):
    STATIC_DIR = Path(sys._MEIPASS) / "web_app" / "static"
else:
    STATIC_DIR = HERE / "static"

# Пайплайн лежит рядом, в analyzer_to_errors - добавляем его в путь импорта.
# (нужен и здесь: конфиг, resolve_path, detect_doc_type - используются напрямую,
# не только подпроцессом-раннером)
sys.path.insert(0, str(ANALYZER_DIR))

import ingest                   # noqa: E402  (сверка списка типов документов)
import main as pipeline          # noqa: E402
from llm_check import check_server_alive  # noqa: E402  (быстрая проверка сервера)

import multipart                          # noqa: E402  (потоковый разбор загрузки)
import queue_worker                       # noqa: E402  (константы пула - во фронтенд)
import lmstudio                           # noqa: E402  (управление моделями LM Studio)
import config_admin                       # noqa: E402  (правка конфига ИИ админом)
from queue_worker import AnalysisQueue    # noqa: E402
from chats import ChatError, ChatStore    # noqa: E402  (обычный чат с ИИ)
from config_admin import ConfigError      # noqa: E402
from lmstudio import LMStudioError        # noqa: E402
from sessions import FULL_PROJECT_TYPE, SessionError, SessionStore  # noqa: E402
from users import UserError, UserStore, canon  # noqa: E402

PROJECT_VERSION = "V2.2 beta"

# Имя cookie с токеном входа. Токен кладём именно в cookie, а не в localStorage:
# исходные документы, PDF-отчёт и фрагменты чертежа открываются НАТИВНО браузером
# (<a href>, <img src>, переход по ссылке), и заголовок X-Auth-Token туда не
# подставить - а cookie уходит с каждым таким запросом сама.
AUTH_COOKIE = "im_auth"

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
# для выпадающего выбора типа у каждого загруженного файла.
#
# ТЕКСТЫ ЗДЕСЬ СВОИ, А НЕ ИЗ ingest.DOC_TYPES, и это осознанно: там описания
# написаны ДЛЯ АГЕНТА (что лежит в извлечённых данных и как оно получено), а
# здесь - для инженера, который выбирает пункт в выпадающем списке. Сводить их
# в один текст значило бы испортить оба.
#
# А вот НАБОР КЛЮЧЕЙ и допустимые расширения обязаны совпадать с пайплайном -
# это проверяется ниже, на импорте. Расхождение уже случалось: подсказка
# обещала спецификацию только книгой Excel, хотя PDF-спецификация внутри
# альбома поддержана с V1.4.
DOC_TYPES = [
    {"key": "scheme", "title": "Принципиальная схема (Э3)",
     "hint": "Векторный PDF монтажной/принципиальной схемы EPLAN"},
    {"key": "assembly", "title": "Сборочный чертёж (СБ)",
     "hint": "Векторный PDF сборочного чертежа шкафа: вид шкафа с размещением изделий"},
    {"key": "spec", "title": "Спецификация оборудования (СО)",
     "hint": "Спецификация по ГОСТ 21.110: книга Excel (.xlsx) либо PDF — "
             "листом альбома, если спецификация идёт в составе полного проекта"},
    # Под этим ключом живут ТРИ вида табличных документов - вид определяется
    # по заголовкам таблицы уже при извлечении (netlist_to_json.detect_table_kind),
    # выбирать между ними пользователю не нужно.
    {"key": "functional", "title": "Функциональная схема автоматизации (ФСА)",
     "hint": "Схема по ГОСТ 21.408: технологический процесс с приборами в кружках. "
             "Электрической схемой не является — извлекаются позиции приборов "
             "(PT206, TE303), которыми она сшивается с кабельным журналом и "
             "перечнем параметров"},
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

# Список типов не должен разъезжаться с пайплайном. Проверяем на импорте, а не
# «когда-нибудь заметим»: добавленный в ingest тип, забытый здесь, означает
# документ, который анализатор умеет читать, но выбрать который в интерфейсе
# нельзя; забытый в ingest - выбор, после которого файл молча уедет в
# skipped_files с невнятной причиной.
_pipeline_types = set(ingest.DOC_TYPES)
_ui_types = VALID_TYPE_KEYS - {FULL_PROJECT_TYPE}   # альбом - контейнер, а не вид
assert _ui_types == _pipeline_types, (
    f"Список типов документов в интерфейсе разошёлся с пайплайном: "
    f"только в интерфейсе {sorted(_ui_types - _pipeline_types)}, "
    f"только в ingest.DOC_TYPES {sorted(_pipeline_types - _ui_types)}")


def _config_path():
    return str(ANALYZER_DIR / "config.yaml")


STORE = SessionStore()
QUEUE = AnalysisQueue(STORE, _config_path())
USERS = UserStore()
CHATS = ChatStore()


# Публичные пути - до которых пускаем без входа: сама страница, статика и
# эндпоинты аутентификации (иначе войти было бы нечем). Всё остальное под /api
# требует действующего токена.
def _is_public(path) -> bool:
    return (path == "/" or path.startswith("/static/")
            or path.startswith("/api/auth/"))


def _auth_cookie_header(token) -> tuple:
    # HttpOnly: JS токен читать незачем (кто вошёл - скажет /api/auth/me), а так
    # его не утащить со страницы. SameSite=Lax достаточно: межсайтовых POST у
    # инструмента нет. Max-Age на год - «закрыл вкладку, вернулся завтра».
    return ("Set-Cookie",
            f"{AUTH_COOKIE}={token}; Path=/; SameSite=Lax; HttpOnly; Max-Age=31536000")


def _clear_cookie_header() -> tuple:
    return ("Set-Cookie", f"{AUTH_COOKIE}=; Path=/; SameSite=Lax; HttpOnly; Max-Age=0")


# =====================================================================
# Проверка серверов ИИ и управление моделями (нативный API LM Studio)
# =====================================================================
#
# Разбор ответа /api/v1/models и load/unload живут в отдельном модуле lmstudio.py
# (тоже на голой стандартной библиотеке). Здесь - только его вызовы: адрес сервера
# берём из конфига и обрабатываем ошибки.

def _ai_server_cfg():
    """Сервер ИИ для списка/управления моделями - берём у agent_1 (его base_url
    уже слит с config.local.yaml). Он и agent_2 указывают на один LM Studio."""
    cfg = pipeline.load_config(_config_path())
    return cfg["llm_servers"]["agent_1"]


def _api_models_payload():
    """Список моделей для выпадающих списков интерфейса + что выбрано сейчас."""
    cfg = pipeline.load_config(_config_path())
    servers = cfg["llm_servers"]
    result = {
        "models": [], "error": None,
        "defaults": {
            "agent_1": servers["agent_1"].get("model"),
            "agent_2": servers["agent_2"].get("model"),
            "vision": (servers.get("vision") or {}).get("model"),
            "agents_count": cfg.get("agents", {}).get("count", 1),
            "single_agent": cfg.get("agents", {}).get("single_agent", "agent_1"),
        },
    }
    try:
        result["models"] = lmstudio.list_models(servers["agent_1"])
    except Exception as e:  # noqa: BLE001
        # Сервер не отвечает - это НЕ повод спрятать настройку: уже выбранное
        # показать надо, а список просто останется пустым с честной причиной.
        result["error"] = f"{type(e).__name__}: {e}"
    return result


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
            # Разбор ответа сервера один на обе кнопки: и на проверку моделей,
            # и на выпадающие списки выбора. Две копии разъехались бы.
            models = lmstudio.list_models(scfg)
            entry["reachable"] = True
            for m in models:
                entry["models"].append({**m, "wanted": m["key"] in wanted})
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
    def _send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    # ---- аутентификация ----
    def _cookie_token(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == AUTH_COOKIE:
                return value
        return None

    def _resolve_user(self):
        """Текущий пользователь по токену (заголовок X-Auth-Token имеет приоритет
        над cookie - на случай программного доступа), либо None."""
        token = self.headers.get("X-Auth-Token") or self._cookie_token()
        return USERS.resolve_token(token)

    def _require_admin(self):
        if not (self.user and self.user["is_admin"]):
            raise SessionError("Требуются права администратора", 403)

    def _require_session_access(self, session_id):
        """Пускает к сессии только её владельца и любого администратора.
        owner_of заодно даёт 404, если сессии нет вовсе."""
        if self.user and self.user["is_admin"]:
            STORE.owner_of(session_id)      # 404, если сессии нет
            return
        owner = STORE.owner_of(session_id)
        if owner != (self.user or {}).get("canonical"):
            raise SessionError("Нет доступа к этой сессии", 403)

    def _send_file(self, path: Path, content_type, disposition=None):
        """Отдаёт файл ПОТОКОМ, не поднимая его в память целиком.

        Раньше здесь стоял read_bytes(), и открытие альбома в соседней вкладке
        (первое, что делает инженер, увидев замечание) поднимало в память
        сервера сотни мегабайт - на каждый такой клик.
        """
        if not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, length=64 * 1024)

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
            self.user = self._resolve_user()
            if not _is_public(path) and self.user is None:
                # auth:true - сигнал фронтенду показать экран входа, а не тост
                return self._send_json({"error": "Требуется вход", "auth": True}, 401)

            if path == "/":
                return self._send_file(STATIC_DIR / "index.html",
                                       "text/html; charset=utf-8")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            if path == "/api/auth/me":
                return self._send_json({"user": self.user})
            if path == "/api/config":
                return self._api_config()
            if path == "/api/models":
                return self._send_json(_api_models_payload())
            if path == "/api/check-llm":
                return self._api_check_llm()
            if path == "/api/lmstudio/models":
                # Панель управления моделями - только администратору (обычному
                # выбору хватает /api/models, где эмбеддинги уже отсеяны).
                self._require_admin()
                return self._api_lmstudio_models()
            if path == "/api/admin/config":
                self._require_admin()
                return self._send_json(config_admin.admin_view(_config_path()))
            if path == "/api/users":
                self._require_admin()
                return self._send_json({"users": USERS.list_users()})
            if path == "/api/chat":
                return self._send_json({"chat": CHATS.get_active(self.user["canonical"])})
            if path == "/api/chat/file":
                return self._api_chat_file(parse_qs(parsed.query))
            if path == "/api/sessions":
                return self._api_sessions_list()
            if path.startswith("/api/sessions/"):
                session_id, action = self._split_session_path(path)
                self._require_session_access(session_id)
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
                if action == "lmstudio.log":
                    return self._api_lmstudio_log(session_id)
            self.send_error(404)
        except (SessionError, UserError, ChatError, LMStudioError, ConfigError) as e:
            self._send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            self.user = self._resolve_user()

            # Эндпоинты входа - до проверки авторизации (иначе войти нечем).
            if path == "/api/auth/login":
                return self._api_login()
            if path == "/api/auth/register":
                return self._api_register()
            if path == "/api/auth/logout":
                return self._api_logout()

            if self.user is None:
                return self._send_json({"error": "Требуется вход", "auth": True}, 401)

            # Обычный чат с ИИ. Не под /api/sessions: чат привязан к самому
            # пользователю (владелец = кто вошёл), а не к сессии, и разграничение
            # доступа тут не нужно - каждый работает только со своим чатом.
            if path == "/api/chat/set-model":
                model = self._body_json().get("model")
                chat = CHATS.set_model(self.user["canonical"], model)
                return self._send_json({"ok": True, "model": chat.get("model")})
            if path == "/api/chat/new":
                chat = CHATS.new_chat(self.user["canonical"])
                return self._send_json({"ok": True, "chat": chat})
            if path == "/api/chat/upload":
                return self._api_chat_upload()
            if path == "/api/chat/send":
                return self._api_chat_send()

            # Управление сервером ИИ и глобальным конфигом - только администратору.
            if path == "/api/lmstudio/load":
                self._require_admin()
                return self._api_lmstudio_load()
            if path == "/api/lmstudio/unload":
                self._require_admin()
                return self._api_lmstudio_unload()
            if path == "/api/admin/config":
                self._require_admin()
                view = config_admin.apply_admin_config(_config_path(), self._body_json())
                return self._send_json({"ok": True, **view})

            # Управление пользователями - только администратору.
            if path == "/api/users":
                self._require_admin()
                return self._api_user_create()
            if path.startswith("/api/users/"):
                self._require_admin()
                return self._api_user_action(path)

            if path == "/api/sessions":
                return self._api_session_create()
            if path.startswith("/api/sessions/"):
                session_id, action = self._split_session_path(path)
                self._require_session_access(session_id)
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
                    # path, а не name: у частей альбома имена повторяются от
                    # шкафа к шкафу, и по имени пометка легла бы сразу на все
                    # одноимённые файлы. name принимается ради старых вкладок,
                    # открытых до обновления сервера.
                    rel_path = body.get("path") or body.get("name")
                    if not rel_path:
                        raise SessionError("Не указан файл", 400)
                    STORE.set_type(session_id, rel_path, body.get("type"),
                                   VALID_TYPE_KEYS)
                    return self._send_json({"ok": True})
                if action == "set-llm":
                    llm = STORE.set_llm(session_id, self._body_json())
                    return self._send_json({"ok": True, "llm": llm})
                if action == "enqueue":
                    return self._api_enqueue(session_id)
                if action == "cancel":
                    QUEUE.cancel(session_id)
                    return self._send_json({"ok": True})
            self.send_error(404)
        except (SessionError, UserError, ChatError, LMStudioError, ConfigError) as e:
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

    # ---- управление моделями LM Studio (админ) ----

    def _api_lmstudio_models(self):
        """Все модели сервера (включая эмбеддинги) со статусом - для админ-панели.
        Сервер недоступен - НЕ ошибка эндпоинта: отдаём пустой список с причиной,
        чтобы панель открылась и показала «сервер не отвечает», а не молчала."""
        try:
            models = lmstudio.list_models(_ai_server_cfg(), include_non_llm=True)
            self._send_json({"models": models, "error": None})
        except LMStudioError as e:
            self._send_json({"models": [], "error": str(e)})

    def _api_lmstudio_load(self):
        body = self._body_json()
        result = lmstudio.load_model(_ai_server_cfg(), body.get("model"),
                                     body.get("params"))
        self._send_json({"ok": True, "result": result})

    def _api_lmstudio_unload(self):
        result = lmstudio.unload_model(_ai_server_cfg(),
                                       self._body_json().get("instance_id"))
        self._send_json({"ok": True, "result": result})

    def _api_lmstudio_log(self, session_id):
        """Транскрипт обмена с LM Studio за последний прогон - открывается в
        соседней вкладке. Нет файла (прогон без ИИ или ещё не запускался) - 404 с
        человеческим текстом, а не пустая страница."""
        path = STORE.paths_of(session_id)["output_dir"] / "lmstudio.log"
        if not path.is_file():
            raise SessionError(
                "Лог LM Studio ещё не сформирован: запустите анализ с участием "
                "нейросетей (полный или визуальный режим).", 404)
        self._send_file(path, "text/plain; charset=utf-8", "inline")

    def _api_sessions_list(self):
        """Список сессий текущего пользователя (администратору - всех).
        Номера в очередях считаем здесь, а не храним: очереди живут в памяти
        воркера, и их порядок - единственное, что его определяет.

        Состояние очередей (running/queued/llm) - ОБЩЕЕ на всех: сервер ИИ один,
        и человеку важно видеть, что перед ним в очереди стоят чужие прогоны,
        даже если самих чужих сессий он не видит."""
        is_admin = self.user["is_admin"]
        me = self.user["canonical"]
        positions = QUEUE.positions()
        llm_positions = QUEUE.llm_positions()
        snap = QUEUE.snapshot()
        names = USERS.display_names() if is_admin else {}
        sessions = []
        for meta in STORE.list():
            if not is_admin and meta.get("owner") != me:
                continue
            item = dict(meta)
            item["queue_position"] = positions.get(meta["id"])
            item["llm_position"] = llm_positions.get(meta["id"])
            # число файлов берём из session.json, а не пересчитываем обходом
            # диска: этот список опрашивается раз в 2 с по ВСЕМ сессиям сразу
            item["n_files"] = STORE.file_count(meta["id"], meta)
            # подпись владельца нужна только администратору - он группирует
            # список по людям; у обычного пользователя все сессии его же
            if is_admin:
                owner = meta.get("owner")
                item["owner_display"] = names.get(owner, owner) if owner else None
            sessions.append(item)
        self._send_json({
            "sessions": sessions,
            "is_admin": is_admin,
            "running": snap["running"],
            # сколько прогонов реально занимают процессор: сессия, ждущая
            # очереди к ИИ, жива, но ничего не считает
            "script_busy": snap["script_busy"],
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
        # Владельца показываем администратору, зашедшему в чужую сессию: иначе
        # непонятно, чья она. Себе показывать незачем - это и так его сессия.
        owner = meta.get("owner")
        if self.user["is_admin"] and owner and owner != self.user["canonical"]:
            meta["owner_display"] = USERS.display_names().get(owner, owner)
        return meta

    def _api_session_create(self):
        # owner - канонический логин (ключ доступа), owner_display - как назвать
        # папку владельца на диске (читабельнее: "Иванов", а не "иванов").
        meta = STORE.create(self._body_json().get("name"),
                            owner=self.user["canonical"],
                            owner_display=self.user["login"])
        self._send_json(meta, 201)

    # ---- аутентификация и пользователи ----
    def _api_login(self):
        body = self._body_json()
        result = USERS.login(body.get("login"), body.get("password"))
        if result["status"] == "ok":
            # cookie с токеном ставит сервер; фронтенду отдаём только, кто вошёл
            return self._send_json(
                {"status": "ok", "user": result["user"]},
                extra_headers=[_auth_cookie_header(result["token"])])
        # not_found / password_required / bad_password - это ПРИКЛАДНОЙ исход
        # диалога входа, а не отказ транспорта: интерфейс по нему спросит пароль,
        # предложит регистрацию или скажет «неверный пароль». Поэтому 200, а не
        # 401 - код 401 с auth:true у нас означает другое (протух токен, показать
        # экран входа), и bad_password в нём утонул бы как «Unauthorized».
        self._send_json(result, 200)

    def _api_register(self):
        result = USERS.register(self._body_json().get("login"))
        self._send_json(
            {"status": "ok", "user": result["user"]},
            extra_headers=[_auth_cookie_header(result["token"])])

    def _api_logout(self):
        token = self.headers.get("X-Auth-Token") or self._cookie_token()
        USERS.revoke(token)
        self._send_json({"ok": True}, extra_headers=[_clear_cookie_header()])

    def _api_user_create(self):
        body = self._body_json()
        user = USERS.create_user(body.get("login"),
                                 is_admin=bool(body.get("is_admin")),
                                 password=body.get("password"))
        self._send_json({"ok": True, "user": user}, 201)

    def _api_user_action(self, path):
        """/api/users/<login>/<delete|update>. Логин в пути уже раскодирован
        (do_POST делает unquote), а слэшей в нём быть не может - _LOGIN_RE их не
        пропускает, поэтому разбор по первому '/' однозначен."""
        rest = path[len("/api/users/"):].strip("/")
        login, _, action = rest.partition("/")
        if action == "delete":
            USERS.delete_user(login)
            return self._send_json({"ok": True})
        if action == "update":
            body = self._body_json()
            user = USERS.update_user(
                login,
                is_admin=body.get("is_admin"),
                password=body.get("password"))
            return self._send_json({"ok": True, "user": user})
        self.send_error(404)

    def _api_upload(self, session_id):
        """Дозагрузка файлов в сессию. НИЧЕГО не стирает: у каждой сессии свой
        base_files, и затирать чужие файлы больше нечем.

        Тело запроса льётся СРАЗУ В ФАЙЛ, кусками (multipart.py). Прежний
        cgi.FieldStorage складывал загрузку в память целиком, а здесь грузят
        альбомы на сотни мегабайт - и каждый оседал в оперативной памяти
        сервера дважды: сам разбор плюс копия от item.file.read().
        """
        STORE.get(session_id)      # 404, если сессии нет - до чтения тела запроса

        saved, skipped, open_files = [], [], []

        def open_part(field, filename):
            """Куда писать эту часть. None - часть пропускается (её байты всё
            равно будут прочитаны: недочитанное тело браузер видит как обрыв
            соединения, а не как ответ с объяснением)."""
            if field != "files":
                return None
            try:
                target = STORE.upload_target(session_id, filename)
            except SessionError as e:
                if e.status == 409:
                    raise          # сессия занята - принимать нечего вовсе
                skipped.append({"name": Path(filename).name, "reason": str(e)})
                return None
            handle = open(target, "wb")
            open_files.append((target, handle))
            saved.append(target.name)
            return handle

        try:
            multipart.parse(self.rfile, self.headers.get("Content-Type", ""),
                            self.headers.get("Content-Length"), open_part)
        except multipart.MultipartError as e:
            # недописанные файлы убираем: оборванная загрузка не должна
            # оставить в сессии PDF, который не откроется
            for target, handle in open_files:
                handle.close()
                target.unlink(missing_ok=True)
            raise SessionError(str(e), 400)
        finally:
            for _, handle in open_files:
                if not handle.closed:
                    handle.close()

        STORE.refresh_file_count(session_id)
        self._send_json({"saved": saved, "skipped": skipped})

    def _api_enqueue(self, session_id):
        mode = self._body_json().get("mode", "full")
        # Список режимов - у очереди: она же его и раскладывает на флаги
        # прогона. Свой список здесь означал бы, что новый режим надо не забыть
        # добавить в двух местах.
        if mode not in queue_worker.MODES:
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
        # RFC 5987: имена документов кириллические, голым latin-1 их в заголовок
        # не положить - браузер получил бы кракозябры вместо имени файла
        self._send_file(target, ctype,
                        f"{disposition}; filename*=UTF-8''{quote(target.name)}")

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

    # ---- обычный чат с ИИ ----

    def _write_ndjson(self, obj):
        """Одно событие потока: JSON-объект в строку, сразу на провод.

        flush обязателен - иначе ответ модели копился бы в буфере и приходил бы
        не потоком, а разом в конце, ровно то, ради чего стриминг и затевался."""
        self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.wfile.flush()

    def _api_chat_file(self, query):
        """Файл или картинка, приложенные к сообщению чата. Картинку показываем
        inline (превью прямо в диалоге), прочее - вложением на скачивание."""
        rel = (query.get("path") or [""])[0]
        target = CHATS.resolve_file(self.user["canonical"], rel)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        disposition = "inline" if ctype.startswith("image/") else "attachment"
        self._send_file(target, ctype,
                        f"{disposition}; filename*=UTF-8''{quote(target.name)}")

    def _api_chat_upload(self):
        """Приложить файлы к активному чату. Тело льётся в файл потоком, ровно
        как загрузка документов сессии (multipart.py): альбом или большой PDF не
        должен оседать в памяти сервера целиком."""
        owner = self.user["canonical"]
        CHATS.get_active(owner)         # заведёт папку чата, если её ещё нет
        skipped, open_files = [], []

        def open_part(field, filename):
            if field != "files":
                return None
            try:
                target, ref = CHATS.upload_target(owner, filename)
            except ChatError as e:
                skipped.append({"name": Path(filename).name, "reason": str(e)})
                return None
            handle = open(target, "wb")
            open_files.append((target, handle, ref))
            return handle

        try:
            multipart.parse(self.rfile, self.headers.get("Content-Type", ""),
                            self.headers.get("Content-Length"), open_part)
        except multipart.MultipartError as e:
            for target, handle, _ in open_files:
                handle.close()
                target.unlink(missing_ok=True)
            raise ChatError(str(e), 400)
        finally:
            for _, handle, _ in open_files:
                if not handle.closed:
                    handle.close()

        saved = []
        for target, _, ref in open_files:
            ref["size"] = target.stat().st_size
            saved.append(ref)
        self._send_json({"files": saved, "skipped": skipped})

    def _api_chat_send(self):
        """Отправить сообщение и вернуть ответ модели ПОТОКОМ (ndjson-события
        {type: delta|done|error}).

        Отказ модели или сервера ОБЯЗАН доехать до пользователя (главное правило
        проекта - «проверить не удалось» это находка, а не тишина): заголовки
        уже ушли, поэтому ошибку показываем событием error в потоке, а не сменой
        HTTP-кода. Частичный ответ, если что-то успели получить, сохраняем - он
        часть диалога.
        """
        import chat_llm          # ленивый импорт: тянет openai (и fitz для PDF)

        owner = self.user["canonical"]
        body = self._body_json()
        text = (body.get("text") or "").strip()
        refs = CHATS.clean_file_refs(owner, body.get("files") or [])
        if not text and not refs:
            raise ChatError("Пустое сообщение", 400)

        chat = CHATS.get_active(owner)
        model = chat.get("model")
        if not model:
            raise ChatError("Не выбрана модель — выберите её над полем ввода", 400)

        cfg = pipeline.load_config(_config_path())
        server_cfg = chat_llm.server_cfg_from_config(cfg)

        # Вопрос фиксируем на диске ДО обращения к модели: даже если сервер ИИ не
        # ответит, сообщение пользователя из истории не пропадёт.
        CHATS.append_message(owner, "user", text, refs)
        chat = CHATS.get_active(owner)
        messages = chat_llm.build_messages(chat, lambda p: CHATS.resolve_file(owner, p))

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        # В историю сохраняем ТОЛЬКО видимый ответ (content), а не рассуждение:
        # раздумья эфемерны, как в привычных чатах, и в контекст модели их не
        # пересылаем (build_messages их не знает). Событие content уезжает к
        # браузеру как "delta" - имя оставлено прежним, чтобы не плодить сущности.
        acc = []
        try:
            for ev in chat_llm.stream_reply(server_cfg, model, messages):
                kind = ev.get("type")
                if kind == "content":
                    acc.append(ev["text"])
                    self._write_ndjson({"type": "delta", "text": ev["text"]})
                elif kind == "reasoning":
                    self._write_ndjson({"type": "reasoning", "text": ev["text"]})
                elif kind == "stats":
                    self._write_ndjson({"type": "stats", "tokens": ev.get("tokens"),
                                        "seconds": ev.get("seconds"), "tps": ev.get("tps")})
            CHATS.append_message(owner, "assistant", "".join(acc))
            self._write_ndjson({"type": "done"})
        except Exception as e:  # noqa: BLE001
            if acc:
                CHATS.append_message(owner, "assistant", "".join(acc))
            try:
                self._write_ndjson({"type": "error", "error": f"{type(e).__name__}: {e}"})
            except Exception:  # noqa: BLE001 - клиент уже отключился, писать некуда
                pass


def main():
    # До первого print: иначе первые же русские строки уйдут в консоль кашей.
    setup_console_utf8()

    # Значения по умолчанию берём из окружения - это единственный способ задать
    # адрес/порт, когда программу запускают ДВОЙНЫМ КЛИКОМ по exe (аргументы там
    # не передашь). Поставляемый .bat "открытый доступ" ставит IM_HOST=0.0.0.0.
    # CLI-флаг, если он задан, всё равно старше окружения.
    default_host = os.environ.get("IM_HOST", "127.0.0.1")
    default_port = int(os.environ.get("IM_PORT", "8000"))

    ap = argparse.ArgumentParser(description="Веб-интерфейс анализатора документации")
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--host", default=default_host,
                    help="адрес, на котором слушать. По умолчанию только этот "
                         "компьютер (127.0.0.1). 0.0.0.0 - открыть доступ из "
                         "сети (осознанное решение: авторизации в интерфейсе "
                         "нет, см. предупреждение при запуске). Можно задать и "
                         "переменной окружения IM_HOST/IM_PORT.")
    args = ap.parse_args()

    if getattr(sys, "frozen", False):
        local_cfg = ANALYZER_DIR / "config.local.yaml"
        example_cfg = ANALYZER_DIR / "config.local.example.yaml"
        if not local_cfg.exists() and example_cfg.exists():
            shutil.copy2(example_cfg, local_cfg)
            print(f"Создан {local_cfg} из образца - при необходимости "
                  f"поправьте в нём адрес сервера ИИ.")

    # Разовая передача бесхозных сессий администратору. Сессии, созданные до
    # появления пользователей, владельца не имеют - без этого их не увидел бы
    # никто, кроме как через панель. Идемпотентно: у сессии с owner ничего не
    # меняется, поэтому вызов на каждом старте безвреден.
    moved = STORE.assign_ownerless(USERS.primary_admin())
    if moved:
        print(f"Прежние сессии ({moved}) переданы администратору.")
    print("Вход в интерфейс по логину. Учётка администратора по умолчанию: "
          "admin / admin (смените пароль в разделе «Пользователи»).")

    QUEUE.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)

    # Адрес для БРАУЗЕРА - не тот, на котором слушаем: по 0.0.0.0 (слушать «на
    # всех интерфейсах») подключиться нельзя, это не адрес назначения. С той же
    # машины в браузер идёт localhost, а по сети - реальный IP компьютера.
    browse_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{browse_host}:{args.port}"
    if args.host in ("0.0.0.0", "::"):
        print(f"Интерфейс анализатора: {url} (с этого компьютера); "
              f"по сети - http://<IP этого компьютера>:{args.port}")
    else:
        print(f"Интерфейс анализатора: {url}")

    # Открываем браузер сами только в собранном exe: пользователь двойным
    # кликом запускает программу и должен сразу увидеть интерфейс, а не
    # догадываться, что нужно вручную набрать адрес. При запуске из консоли
    # (python web_app/server.py) это осталось бы неожиданным - там открывают
    # тот же адрес во вкладке, которая уже открыта.
    if getattr(sys, "frozen", False):
        import threading
        import webbrowser
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    # Вход по логину есть, но он МЯГКИЙ: пароли в открытом виде, а учётка
    # администратора по умолчанию - admin/admin. Пока сервер слушает localhost,
    # это неважно; открытый наружу - уже значит, что чужой в сети может войти
    # админом с паролем по умолчанию и увидеть все сессии. Сказать об этом надо
    # в момент запуска, а не в README, который не читают.
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"ВНИМАНИЕ: сервер слушает {args.host} - он доступен из сети.\n"
              f"         Вход по логину есть, но пароли хранятся в открытом виде, "
              f"а учётка администратора по умолчанию admin/admin.\n"
              f"         СМЕНИТЕ пароль администратора в разделе «Пользователи», "
              f"иначе любой в сети получит доступ ко всем сессиям.")
    print("Ctrl+C для остановки.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        server.shutdown()


if __name__ == "__main__":
    main()
