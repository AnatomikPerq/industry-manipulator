#!/usr/bin/env python3
"""
Пользователи интерфейса: логины, вход, регистрация, токены и права.

ЗАЧЕМ ЭТО ЕСТЬ (раньше авторизации не было сознательно). Инструмент вырос из
«одной машины на всех» в «у каждого свои сессии»: теперь у сессии есть владелец
(поле owner в session.json), и человек видит только свои прогоны. Разделение
это мягкое, а не защита от злоумышленника: пароли лежат в открытом виде, сеть
доверенная (LAN бюро), и весь смысл - развести рабочие пространства коллег, а не
устоять против атаки. Ровно поэтому пароль без хэширования - осознанное решение
заказчика, а не недосмотр.

Модель:
  * ОБЫЧНЫЙ пользователь - только логин. Ввёл новый логин - предложат
    зарегистрировать; ввёл существующий - сразу вошёл.
  * АДМИНИСТРАТОР - логин + пароль. Видит сессии ВСЕХ и управляет
    пользователями. Учётка admin/admin заводится при первом запуске, если ни
    одного администратора ещё нет (идемпотентно на каждом старте).

Хранилище - users.json рядом с sessions/ и config.yaml (это данные установки, а
не код: переживают обновление программы, лежат снаружи exe в собранной версии).
Формат:
    {
      "users": {"<логин в нижнем регистре>": {login, is_admin, password, created_at}},
      "tokens": {"<токен>": "<логин в нижнем регистре>"}
    }

ТОКЕН, А НЕ «клиент присылает логин». Без токена любой мог бы объявить себя
логином admin и войти без пароля - тогда пароль администратора не значил бы
ничего. Токен выдаётся при успешном входе, кладётся сервером в cookie и
хранится здесь же, поэтому переживает перезапуск сервера: смысл сессий как раз
в том, чтобы «закрыть вкладку и вернуться завтра».
"""

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from paths import ANALYZER_DIR

USERS_FILE = ANALYZER_DIR / "users.json"

# Учётка администратора по умолчанию. Заводится при первом запуске, если ни
# одного администратора ещё нет. Пароль сменяется в панели пользователей либо
# правкой users.json.
DEFAULT_ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"

# Логин: буквы (лат./кир.), цифры, пробел и . _ - @, до 64 символов. Логин
# СЛУЖИТ И ИМЕНЕМ ПАПКИ ВЛАДЕЛЬЦА на диске (sessions/<владелец>/<id>), поэтому
# кроме charset проверяется ещё и пригодность как имя каталога (_reject_unsafe_login).
_LOGIN_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё _.\-@]{1,64}$")

# Логин не должен выглядеть как ИДЕНТИФИКАТОР СЕССИИ: имена папок владельцев и
# папок сессий лежат рядом (sessions/<владелец> и sessions/<id> у старых), и
# хранилище различает их именно по формату id (sessions._ID_RE). Совпади логин с
# ним - папку владельца приняли бы за сессию.
_ID_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{4}$")

# Зарезервированные имена устройств Windows: каталог с таким именем не создать.
_RESERVED_NAMES = ({"con", "prn", "aux", "nul"}
                   | {f"com{i}" for i in range(1, 10)}
                   | {f"lpt{i}" for i in range(1, 10)})


def _reject_unsafe_login(display) -> None:
    """Бракует логины, непригодные как имя папки владельца. Не о безопасности
    доступа (её обеспечивает токен), а о файловой системе и о том, чтобы папку
    владельца не спутать с папкой сессии."""
    s = (display or "").strip()
    low = s.lower()
    if _ID_LIKE_RE.match(s):
        raise UserError("Логин не должен выглядеть как код сессии (дата_время_хвост)", 400)
    if s.strip(". ") == "":
        raise UserError("Логин не может состоять из одних точек", 400)
    if s.startswith(".") or s.endswith("."):
        raise UserError("Логин не может начинаться или заканчиваться точкой", 400)
    if low == "_ownerless" or low.split(".", 1)[0] in _RESERVED_NAMES:
        raise UserError("Этот логин зарезервирован системой", 400)


class UserError(Exception):
    """Ошибка, которую можно показать пользователю (занятый логин, нет прав)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def canon(login) -> str:
    """Канонический ключ логина: сравнение без учёта регистра и пробелов по
    краям. Именно он лежит в owner сессии и в tokens - отображаемую форму
    (с исходным регистром) храним отдельно."""
    return (login or "").strip().lower()


class UserStore:
    """Хранилище пользователей на файловой системе (users.json).

    Всё состояние - на диске, в памяти ничего не кэшируется: перезапуск сервера
    не должен ни разлогинивать людей, ни терять учётки. Файл крошечный, поэтому
    читается и пишется целиком под общим замком.
    """

    def __init__(self, path: Path = USERS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.ensure_default_admin()

    # ---------- низкий уровень ----------

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"users": {}, "tokens": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Битый файл не должен ронять сервер целиком: считаем, что учёток
            # нет, - ensure_default_admin заведёт администратора заново.
            return {"users": {}, "tokens": {}}
        data.setdefault("users", {})
        data.setdefault("tokens", {})
        return data

    def _write(self, data: dict) -> None:
        """Атомарная запись через временный файл + os.replace: сервер могут убить
        в любой момент, и половина JSON означала бы потерянные учётки."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _public(user: dict) -> dict:
        """То, что можно отдать наружу: без пароля."""
        return {
            "login": user["login"],
            "canonical": canon(user["login"]),
            "is_admin": bool(user.get("is_admin")),
        }

    def _new_token(self, data: dict, key: str) -> str:
        token = secrets.token_hex(24)
        data["tokens"][token] = key
        return token

    def _admin_count(self, data: dict) -> int:
        return sum(1 for u in data["users"].values() if u.get("is_admin"))

    # ---------- начальная учётка ----------

    def ensure_default_admin(self) -> None:
        """Заводит admin/admin, если администраторов ещё нет. Идемпотентно:
        существующего администратора не трогает, пароль не сбрасывает."""
        with self._lock:
            data = self._read()
            if self._admin_count(data) == 0:
                key = canon(DEFAULT_ADMIN_LOGIN)
                data["users"][key] = {
                    "login": DEFAULT_ADMIN_LOGIN,
                    "is_admin": True,
                    "password": DEFAULT_ADMIN_PASSWORD,
                    "created_at": time.time(),
                }
                self._write(data)

    def primary_admin(self) -> str | None:
        """Канонический логин «главного» администратора - которому достаются
        бесхозные сессии. Учётка admin, если есть; иначе самый ранний админ."""
        data = self._read()
        admins = [k for k, u in data["users"].items() if u.get("is_admin")]
        if canon(DEFAULT_ADMIN_LOGIN) in admins:
            return canon(DEFAULT_ADMIN_LOGIN)
        admins.sort(key=lambda k: data["users"][k].get("created_at") or 0)
        return admins[0] if admins else None

    # ---------- вход/регистрация ----------

    def login(self, login, password=None) -> dict:
        """Первый и единственный шаг входа. Возвращает словарь со статусом:

          not_found         - такого логина нет, интерфейс предложит регистрацию;
          password_required - это администратор, нужен пароль (в этот раз не прислан);
          bad_password      - администратор, пароль неверный;
          ok                - вошёл, в ответе token и user.

        Обычный пользователь входит по одному логину. Администратор без пароля
        получает password_required, с неверным - bad_password, с верным - ok.
        """
        key = canon(login)
        if not key:
            raise UserError("Пустой логин", 400)
        with self._lock:
            data = self._read()
            user = data["users"].get(key)
            if user is None:
                return {"status": "not_found", "login": (login or "").strip()}
            if user.get("is_admin"):
                if password is None:
                    return {"status": "password_required", "login": user["login"]}
                if (password or "") != (user.get("password") or ""):
                    return {"status": "bad_password", "login": user["login"]}
            token = self._new_token(data, key)
            self._write(data)
            return {"status": "ok", "token": token, "user": self._public(user)}

    def register(self, login) -> dict:
        """Регистрация ОБЫЧНОГО пользователя (без пароля). Администратора так не
        создать - его заводит другой администратор в панели."""
        display = (login or "").strip()
        key = canon(display)
        if not _LOGIN_RE.match(display):
            raise UserError(
                "Логин: буквы, цифры, пробел и . _ - @ (до 64 символов)", 400)
        _reject_unsafe_login(display)
        with self._lock:
            data = self._read()
            if key in data["users"]:
                raise UserError("Такой логин уже занят — попробуйте войти", 409)
            data["users"][key] = {
                "login": display, "is_admin": False,
                "password": None, "created_at": time.time(),
            }
            token = self._new_token(data, key)
            self._write(data)
            return {"status": "ok", "token": token,
                    "user": self._public(data["users"][key])}

    # ---------- токены ----------

    def resolve_token(self, token) -> dict | None:
        """Публичные данные пользователя по токену либо None (нет/протух)."""
        if not token:
            return None
        data = self._read()
        key = data["tokens"].get(token)
        if not key:
            return None
        user = data["users"].get(key)
        return self._public(user) if user else None

    def revoke(self, token) -> None:
        if not token:
            return
        with self._lock:
            data = self._read()
            if data["tokens"].pop(token, None) is not None:
                self._write(data)

    # ---------- управление (только администратор) ----------

    def list_users(self) -> list:
        data = self._read()
        out = [self._public(u) for u in data["users"].values()]
        out.sort(key=lambda u: (not u["is_admin"], u["login"].lower()))
        return out

    def display_names(self) -> dict:
        """canonical -> отображаемый логин: для подписи владельца в списке сессий."""
        return {canon(u["login"]): u["login"] for u in self._read()["users"].values()}

    def create_user(self, login, is_admin=False, password=None) -> dict:
        display = (login or "").strip()
        key = canon(display)
        if not _LOGIN_RE.match(display):
            raise UserError(
                "Логин: буквы, цифры, пробел и . _ - @ (до 64 символов)", 400)
        _reject_unsafe_login(display)
        if is_admin and not (password or "").strip():
            raise UserError("Администратору нужен пароль", 400)
        with self._lock:
            data = self._read()
            if key in data["users"]:
                raise UserError("Такой логин уже занят", 409)
            data["users"][key] = {
                "login": display, "is_admin": bool(is_admin),
                "password": (password if is_admin else None),
                "created_at": time.time(),
            }
            self._write(data)
            return self._public(data["users"][key])

    def delete_user(self, login) -> None:
        key = canon(login)
        with self._lock:
            data = self._read()
            user = data["users"].get(key)
            if user is None:
                raise UserError("Пользователь не найден", 404)
            if user.get("is_admin") and self._admin_count(data) <= 1:
                raise UserError("Нельзя удалить единственного администратора", 409)
            data["users"].pop(key)
            # Токены удалённого пользователя тоже выкидываем - иначе он остался
            # бы «залогинен» по старой cookie.
            data["tokens"] = {t: k for t, k in data["tokens"].items() if k != key}
            self._write(data)

    def update_user(self, login, is_admin=None, password=None) -> dict:
        """Смена прав и/или пароля. Снять админство с последнего администратора
        нельзя (иначе панель управления станет никому не доступна)."""
        key = canon(login)
        with self._lock:
            data = self._read()
            user = data["users"].get(key)
            if user is None:
                raise UserError("Пользователь не найден", 404)

            if is_admin is not None:
                is_admin = bool(is_admin)
                if user.get("is_admin") and not is_admin and self._admin_count(data) <= 1:
                    raise UserError(
                        "Нельзя снять права у единственного администратора", 409)
                if is_admin and not user.get("is_admin"):
                    # Стал администратором - нужен пароль. Его должны прислать в
                    # этом же запросе (иначе учётка без пароля не сможет войти).
                    if not (password or "").strip():
                        raise UserError("Администратору нужен пароль", 400)
                user["is_admin"] = is_admin
                if not is_admin:
                    user["password"] = None      # у обычного пароля нет

            if password is not None:
                if not user.get("is_admin"):
                    raise UserError("Пароль есть только у администратора", 400)
                if not (password or "").strip():
                    raise UserError("Пустой пароль", 400)
                user["password"] = password

            data["users"][key] = user
            self._write(data)
            return self._public(user)
