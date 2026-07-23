"""
Пользователи и разграничение сессий по владельцам.

Проверяется то, что при поломке молча пускает или молча не пускает: обычный
входит по логину, администратор - по паролю, чужой логин предлагается к
регистрации, токен переживает и опознаётся, а бесхозные сессии достаются
администратору. Пароли в открытом виде - это осознанное решение (мягкое
разграничение в доверенной сети), поэтому здесь не проверяется «хэширование»,
которого и нет.
"""

import pytest

from sessions import SessionStore
from users import DEFAULT_ADMIN_LOGIN, DEFAULT_ADMIN_PASSWORD, UserError, UserStore, canon


@pytest.fixture()
def users(tmp_path):
    return UserStore(tmp_path / "users.json")


@pytest.fixture()
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


# ---------- начальная учётка ----------

def test_default_admin_created(users):
    """При первом запуске заводится admin/admin - иначе управлять было бы некому."""
    admin = users.login(DEFAULT_ADMIN_LOGIN, DEFAULT_ADMIN_PASSWORD)
    assert admin["status"] == "ok"
    assert admin["user"]["is_admin"] is True


def test_default_admin_not_reset_if_admin_exists(tmp_path):
    """Идемпотентность: если администратор уже есть, admin/admin не навязывается
    и чужой пароль не сбрасывается на каждом старте."""
    path = tmp_path / "users.json"
    first = UserStore(path)
    first.update_user(DEFAULT_ADMIN_LOGIN, password="секрет")
    # второй запуск на том же файле не должен вернуть пароль к admin
    second = UserStore(path)
    assert second.login(DEFAULT_ADMIN_LOGIN, DEFAULT_ADMIN_PASSWORD)["status"] == "bad_password"
    assert second.login(DEFAULT_ADMIN_LOGIN, "секрет")["status"] == "ok"


# ---------- вход ----------

def test_unknown_login_offers_registration(users):
    assert users.login("новичок")["status"] == "not_found"


def test_regular_user_logs_in_by_login_alone(users):
    users.register("инженер")
    res = users.login("инженер")
    assert res["status"] == "ok" and res["user"]["is_admin"] is False


def test_admin_requires_password(users):
    assert users.login(DEFAULT_ADMIN_LOGIN)["status"] == "password_required"
    assert users.login(DEFAULT_ADMIN_LOGIN, "не тот")["status"] == "bad_password"
    assert users.login(DEFAULT_ADMIN_LOGIN, DEFAULT_ADMIN_PASSWORD)["status"] == "ok"


def test_login_is_case_insensitive(users):
    users.register("Иванов")
    assert users.login("иванов")["status"] == "ok"
    assert users.login("  ИВАНОВ ")["status"] == "ok"


# ---------- регистрация ----------

def test_register_rejects_duplicate(users):
    users.register("петров")
    with pytest.raises(UserError) as e:
        users.register("Петров")           # тот же логин в другом регистре
    assert e.value.status == 409


def test_register_rejects_bad_login(users):
    for bad in ("", "  ", "a/b", "плохой\tтаб", "x" * 65):
        with pytest.raises(UserError):
            users.register(bad)


def test_register_rejects_id_like_and_reserved(users):
    """Логин - это ещё и имя папки владельца на диске. Похожий на код сессии
    спутался бы с папкой сессии; имя устройства Windows (CON/NUL) как каталог не
    создать; точки по краям небезопасны."""
    for bad in ("2024-01-02_03-04-05_ab12", "CON", "nul", "..", ".", ".скрытый", "имя."):
        with pytest.raises(UserError):
            users.register(bad)


# ---------- токены ----------

def test_token_resolves_and_revokes(users):
    token = users.register("сидоров")["token"]
    who = users.resolve_token(token)
    assert who["login"] == "сидоров"
    users.revoke(token)
    assert users.resolve_token(token) is None


def test_token_survives_reload(tmp_path):
    """Токен хранится на диске: перезапуск сервера не должен разлогинивать -
    весь смысл сессий в «закрыл вкладку, вернулся завтра»."""
    path = tmp_path / "users.json"
    token = UserStore(path).register("длинный")["token"]
    assert UserStore(path).resolve_token(token)["login"] == "длинный"


# ---------- управление (администратор) ----------

def test_promote_and_demote(users):
    users.register("будущий_админ")
    users.update_user("будущий_админ", is_admin=True, password="pw")
    assert users.login("будущий_админ", "pw")["status"] == "ok"
    users.update_user("будущий_админ", is_admin=False)
    # снятый админ снова входит по одному логину
    assert users.login("будущий_админ")["status"] == "ok"


def test_promote_requires_password(users):
    users.register("без_пароля")
    with pytest.raises(UserError):
        users.update_user("без_пароля", is_admin=True)   # админу нужен пароль


def test_cannot_delete_last_admin(users):
    with pytest.raises(UserError) as e:
        users.delete_user(DEFAULT_ADMIN_LOGIN)
    assert e.value.status == 409


def test_cannot_demote_last_admin(users):
    with pytest.raises(UserError):
        users.update_user(DEFAULT_ADMIN_LOGIN, is_admin=False)


def test_delete_user_drops_tokens(users):
    token = users.register("временный")["token"]
    users.delete_user("временный")
    assert users.resolve_token(token) is None


# ---------- сессии и владельцы ----------

def test_session_stores_owner(store):
    sid = store.create("моя", owner="инженер")["id"]
    assert store.owner_of(sid) == "инженер"


def test_session_lives_in_owner_folder(store):
    """Раскладка на диске: sessions/<владелец>/<id>. Папка названа отображаемым
    логином (читабельнее), а поле owner - каноническим."""
    meta = store.create("моя", owner="иванов", owner_display="Иванов")
    sid = meta["id"]
    session_dir = store.paths_of(sid)["session_dir"]
    assert session_dir.parent.name == "Иванов"
    assert session_dir.parent.parent == store.root
    assert session_dir.name == sid
    assert (session_dir / "session.json").is_file()
    # и находится по id несмотря на вложенность
    assert store.get(sid)["owner"] == "иванов"


def test_unsafe_owner_folder_is_hashed(store):
    """Непроходное имя владельца (легаси/правка руками) не роняет создание -
    папка получает безопасное хэш-имя, а сессия всё равно ищется по id."""
    sid = store.create("моя", owner="иванов", owner_display="..")["id"]
    parent = store.paths_of(sid)["session_dir"].parent
    assert parent.name.startswith("user_")
    assert store.get(sid)["id"] == sid


def test_legacy_flat_session_is_found(store):
    """Старые сессии лежат ПЛОСКО (sessions/<id>) и не переезжают: их манифесты
    хранят путь через relative_to(PROJECT_ROOT). Хранилище обязано находить их
    наравне с вложенными."""
    import json as _json
    sid = "2024-01-02_03-04-05_ab12"
    flat = store.root / sid
    flat.mkdir(parents=True)
    (flat / "session.json").write_text(
        _json.dumps({"id": sid, "owner": "admin", "status": "done",
                     "created_at": 1.0}), encoding="utf-8")
    assert store.get(sid)["owner"] == "admin"
    assert sid in {m["id"] for m in store.list()}


def test_delete_removes_empty_owner_folder(store):
    sid = store.create("моя", owner="петя", owner_display="Петя")["id"]
    owner_folder = store.paths_of(sid)["session_dir"].parent
    assert owner_folder.is_dir()
    store.delete(sid)
    assert not owner_folder.exists()          # опустевшую папку владельца убрали


def test_owner_folder_kept_while_other_sessions_remain(store):
    a = store.create("a", owner="петя", owner_display="Петя")["id"]
    b = store.create("b", owner="петя", owner_display="Петя")["id"]
    owner_folder = store.paths_of(a)["session_dir"].parent
    store.delete(a)
    assert owner_folder.is_dir()              # b ещё здесь - папку не трогаем
    assert store.get(b)["id"] == b


def test_assign_ownerless_gives_to_admin(store, users):
    """Бесхозные сессии (созданные до появления пользователей) достаются
    администратору - иначе их не увидел бы никто, кроме как через панель."""
    old = store.create("старая")["id"]           # без владельца
    mine = store.create("новая", owner="петя")["id"]

    moved = store.assign_ownerless(users.primary_admin())
    assert moved == 1
    assert store.owner_of(old) == canon(DEFAULT_ADMIN_LOGIN)
    assert store.owner_of(mine) == "петя"         # чужую не тронули

    # повторный вызов идемпотентен: раздавать больше нечего
    assert store.assign_ownerless(users.primary_admin()) == 0
