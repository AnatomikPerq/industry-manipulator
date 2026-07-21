"""
Хранилище сессий: адресация файлов, пометки типов, подготовка прогона.

Здесь проверяется то, что ломалось на альбоме и ломалось МОЛЧА: у каждого
шкафа своя подпапка со своим «Общий вид», имена документов повторяются, и
любая адресация по имени попадает не в тот файл (или сразу во все).
"""

import json

import pytest

from sessions import FULL_PROJECT_TYPE, SessionError, SessionStore

VALID_TYPES = {"scheme", "assembly", "spec", "netlist", FULL_PROJECT_TYPE}


@pytest.fixture()
def store(tmp_path):
    return SessionStore(tmp_path / "sessions")


@pytest.fixture()
def album_session(store):
    """Сессия с альбомом: две подпапки-шкафа с ОДИНАКОВЫМИ именами файлов."""
    meta = store.create("альбом")
    sid = meta["id"]
    paths = store.paths_of(sid)
    for cabinet in ("ЩС1", "ЩС2"):
        (paths["base_files_dir"] / cabinet).mkdir(parents=True, exist_ok=True)
        (paths["base_files_dir"] / cabinet / "Общий вид.pdf").write_bytes(b"%PDF-")
    (paths["base_files_dir"] / "альбом.pdf").write_bytes(b"%PDF-")
    return store, sid, paths


def test_files_addressed_by_path(album_session):
    store, sid, _ = album_session
    paths = {f["path"] for f in store.files(sid)}
    assert paths == {
        "base_files/ЩС1/Общий вид.pdf",
        "base_files/ЩС2/Общий вид.pdf",
        "base_files/альбом.pdf",
    }


def test_set_type_touches_one_file_only(album_session):
    """Главное, ради чего пометка переехала с имени на путь: «Общий вид» есть
    у каждого шкафа альбома, и по имени пометка легла бы сразу на все."""
    store, sid, _ = album_session
    store.set_type(sid, "base_files/ЩС1/Общий вид.pdf", "assembly", VALID_TYPES)

    types = {f["path"]: f["detected_type"] for f in store.files(sid)}
    assert types["base_files/ЩС1/Общий вид.pdf"] == "assembly"
    assert types["base_files/ЩС2/Общий вид.pdf"] is None


def test_set_type_rejects_traversal(album_session):
    store, sid, _ = album_session
    with pytest.raises(SessionError) as e:
        store.set_type(sid, "../../../etc/passwd", "scheme", VALID_TYPES)
    assert e.value.status == 400


def test_set_type_rejects_missing_file(album_session):
    """Раньше в session.json записывался любой присланный ключ, и пометка
    несуществующего файла молча оседала мусором."""
    store, sid, _ = album_session
    with pytest.raises(SessionError) as e:
        store.set_type(sid, "base_files/нет-такого.pdf", "scheme", VALID_TYPES)
    assert e.value.status == 404


def test_bare_name_still_works_for_flat_files(album_session):
    """Вкладка, открытая до обновления сервера, присылает голое имя. Для файла
    прямо в base_files это однозначно."""
    store, sid, _ = album_session
    store.set_type(sid, "альбом.pdf", "spec", VALID_TYPES)
    types = {f["path"]: f["detected_type"] for f in store.files(sid)}
    assert types["base_files/альбом.pdf"] == "spec"


def test_legacy_name_keys_migrated(album_session):
    """Сессии на диске переживают обновление. Имя, которому нашёлся ровно один
    файл, переезжает на его путь; неоднозначное - отбрасывается, потому что
    гадать нельзя, а оставить его значит навсегда сохранить пометку, которая
    больше никогда не сработает."""
    store, sid, _ = album_session
    meta = store.get(sid)
    meta["doc_types"] = {"альбом.pdf": "spec", "Общий вид.pdf": "assembly"}
    store._write_meta(meta)

    migrated = store._doc_types(sid)
    assert migrated == {"base_files/альбом.pdf": "spec"}


def test_prepare_run_moves_album_and_rekeys(album_session):
    """Файл, помеченный альбомом, переезжает в full_projects (там его ждёт
    full_project.py), а его пометка уходит совсем: путь изменился, а о том,
    что это альбом, теперь говорит сама папка."""
    store, sid, paths = album_session
    store.set_type(sid, "base_files/альбом.pdf", FULL_PROJECT_TYPE, VALID_TYPES)
    store.set_type(sid, "base_files/ЩС1/Общий вид.pdf", "assembly", VALID_TYPES)

    _, pipeline_types = store.prepare_run(sid)

    assert (paths["full_projects_dir"] / "альбом.pdf").is_file()
    assert not (paths["base_files_dir"] / "альбом.pdf").exists()
    # ключи для ingest - относительно base_files: про папку data он не знает
    assert pipeline_types == {"ЩС1/Общий вид.pdf": "assembly"}
    assert json.loads((paths["data_dir"] / ".doc_types.json").read_text(
        encoding="utf-8")) == pipeline_types
    # тип альбома в пайплайн не уезжает: такого типа документа у ingest нет
    assert FULL_PROJECT_TYPE not in pipeline_types.values()


def test_album_still_listed_after_move(album_session):
    """После прогона альбом лежит в full_projects, и в base_files его больше
    нет. Не перечислив ту папку, интерфейс показал бы, что файл пропал, а
    рядом - десяток непонятно откуда взявшихся связок."""
    store, sid, _ = album_session
    store.set_type(sid, "base_files/альбом.pdf", FULL_PROJECT_TYPE, VALID_TYPES)
    store.prepare_run(sid)

    listed = {f["path"]: f["detected_type"] for f in store.files(sid)}
    assert listed["full_projects/альбом.pdf"] == FULL_PROJECT_TYPE


def test_has_files_counts_albums(album_session):
    """Повторный запуск сессии с альбомом был невозможен, а выглядело это как
    «файл пропал»: альбом опознаётся уже ВНУТРИ прогона и переезжает из
    base_files. Оборви прогон на чтении штампов (самое долгое место) - и
    base_files пуст, хотя сессия полностью исправна."""
    store, sid, paths = album_session
    store.set_type(sid, "base_files/альбом.pdf", FULL_PROJECT_TYPE, VALID_TYPES)
    store.prepare_run(sid)
    for cabinet in ("ЩС1", "ЩС2"):
        for p in (paths["base_files_dir"] / cabinet).iterdir():
            p.unlink()
        (paths["base_files_dir"] / cabinet).rmdir()

    assert list(paths["base_files_dir"].iterdir()) == []
    assert store.has_files(sid) is True


def test_delete_file_drops_its_type(album_session):
    store, sid, _ = album_session
    store.set_type(sid, "base_files/ЩС1/Общий вид.pdf", "assembly", VALID_TYPES)
    store.delete_file(sid, "base_files/ЩС1/Общий вид.pdf")
    assert store._doc_types(sid) == {}


def test_deleting_last_album_clears_generated_parts(store):
    """Вместе с альбомом уходят и нарезанные из него части: иначе в base_files
    остались бы связки-шкафы от документа, которого в сессии больше нет, и
    следующий прогон молча сверял бы призраков (нарезку чистит только сама
    нарезка)."""
    sid = store.create("а")["id"]
    paths = store.paths_of(sid)
    (paths["full_projects_dir"] / "альбом.pdf").write_bytes(b"%PDF-")
    part_dir = paths["base_files_dir"] / "ЩС1"
    part_dir.mkdir(parents=True)
    (part_dir / ".from_full_project").touch()
    (part_dir / "(scheme)Схема.pdf").write_bytes(b"%PDF-")

    store.delete_file(sid, "full_projects/альбом.pdf")
    assert not part_dir.exists()


def test_files_cannot_escape_session(album_session):
    store, sid, _ = album_session
    for bad in ("../session.json", "base_analysis_scripts/profiles.py",
                "manifest.json"):
        with pytest.raises(SessionError):
            store.resolve_file(sid, bad)


def test_file_count_is_stored_not_recomputed(album_session):
    """Число файлов хранится в session.json: список сессий опрашивается раз в
    2 с по ВСЕМ сессиям сразу, и обход диска на каждый опрос давал тысячи
    stat'ов в секунду при десятке сессий с нарезанным альбомом."""
    store, sid, _ = album_session
    assert store.file_count(sid) == 3
    assert store.get(sid)["n_files"] == 3


def test_file_count_follows_changes(album_session):
    store, sid, paths = album_session
    store.delete_file(sid, "base_files/ЩС1/Общий вид.pdf")
    assert store.file_count(sid) == 2

    store.save_upload(sid, "новый СО.xlsx", b"x")
    store.refresh_file_count(sid)
    assert store.file_count(sid) == 3


def test_file_count_converges_when_stale(album_session):
    """Гарантия схождения: как бы ни разошлось хранимое число (папку правили
    руками, прогон оборвали посреди нарезки), открытие сессии его чинит."""
    store, sid, paths = album_session
    meta = store.get(sid)
    meta["n_files"] = 99
    store._write_meta(meta)

    assert len(store.files(sid)) == 3
    assert store.get(sid)["n_files"] == 3


def test_upload_target_rejects_bad_extension(album_session):
    """Решение «принимаем ли файл» принимается ДО чтения его тела: загрузка
    теперь льётся сразу в файл, и двести мегабайт не должны сначала осесть в
    памяти, чтобы потом выясниться, что это .exe."""
    store, sid, _ = album_session
    with pytest.raises(SessionError):
        store.upload_target(sid, "вирус.exe")
    with pytest.raises(SessionError):
        store.upload_target(sid, "")


def test_upload_target_strips_client_path(album_session):
    """Имя приходит от клиента - путь в нём отрезается целиком."""
    store, sid, paths = album_session
    target = store.upload_target(sid, "../../../злой.pdf")
    assert target.parent == paths["base_files_dir"]
    assert target.name == "злой.pdf"
