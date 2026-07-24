"""
Обычный чат с ИИ: хранилище диалога и сборка сообщений для модели.

Здесь охраняется то, чего в самом чате не видно:
  * «Новый чат» АРХИВИРУЕТ старый на диск, а не стирает (требование заказчика:
    для пользователя он исчезает, но остаётся в chats/.../archive/);
  * приложенный файл адресуется путём внутри папки чата и НЕ выходит наружу;
  * все файлы пользователя доходят до модели - картинки image-частью, прочее
    извлечённым текстом (мы говорим с LM Studio по API, где само оно PDF не
    читает, см. chat_llm).

Сеть не трогаем: stream_reply - тонкая обёртка над openai, а вся логика, которая
может сломаться молча, - это сборка сообщений (build_messages), она и проверяется.
"""

import pytest

from chats import ChatError, ChatStore, file_kind


@pytest.fixture()
def store(tmp_path):
    return ChatStore(tmp_path / "chats")


OWNER = "иванов"


# ---------------------------------------------------------------- активный чат

def test_active_chat_created_empty(store):
    chat = store.get_active(OWNER)
    assert chat["messages"] == []
    assert chat["model"] is None
    assert chat["owner"] == OWNER


def test_set_model_and_reject_junk(store):
    store.set_model(OWNER, "qwen-vl")
    assert store.get_active(OWNER)["model"] == "qwen-vl"
    # пустое значение - это «модель не выбрана», не ошибка
    store.set_model(OWNER, "")
    assert store.get_active(OWNER)["model"] is None
    with pytest.raises(ChatError):
        store.set_model(OWNER, "x" * 400)


def test_append_message(store):
    store.append_message(OWNER, "user", "привет")
    store.append_message(OWNER, "assistant", "здравствуйте")
    msgs = store.get_active(OWNER)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "привет"


# ---------------------------------------------------------------- новый чат = архив

def test_new_chat_archives_old_and_keeps_model(store):
    store.set_model(OWNER, "модель-1")
    store.append_message(OWNER, "user", "старое сообщение")
    old_id = store.get_active(OWNER)["id"]

    fresh = store.new_chat(OWNER)

    # активный чат пуст и другой, но модель унаследована (человек только что с
    # ней общался - переспрашивать на каждый «новый чат» назойливо)
    assert fresh["messages"] == []
    assert fresh["id"] != old_id
    assert fresh["model"] == "модель-1"

    # старый чат НЕ исчез - лежит в архиве на диске
    archived = store._archive_dir(OWNER) / old_id / "chat.json"
    assert archived.is_file()


def test_empty_chat_is_not_archived(store):
    """Пустой активный чат архивировать незачем - архив засорился бы пустышками."""
    store.get_active(OWNER)          # создаёт пустой активный
    store.new_chat(OWNER)
    archive = store._archive_dir(OWNER)
    assert not archive.exists() or not any(archive.iterdir())


# ---------------------------------------------------------------- файлы

def test_upload_and_resolve(store):
    store.get_active(OWNER)
    target, ref = store.upload_target(OWNER, "фото.PNG")
    target.write_bytes(b"\x89PNG data")

    assert ref["kind"] == "image"
    assert ref["path"].startswith("files/")
    # адресуется обратно по ссылке
    assert store.resolve_file(OWNER, ref["path"]) == target


def test_upload_strips_client_path(store):
    store.get_active(OWNER)
    target, ref = store.upload_target(OWNER, "../../злой.txt")
    assert target.parent == store.active_files_dir(OWNER)
    assert ref["name"] == "злой.txt"


def test_resolve_rejects_traversal(store):
    store.get_active(OWNER)
    for bad in ("../chat.json", "../../users.json", "/etc/passwd", "files/../chat.json"):
        with pytest.raises(ChatError):
            store.resolve_file(OWNER, bad)


def test_clean_file_refs_rebuilds_from_disk(store):
    store.get_active(OWNER)
    target, ref = store.upload_target(OWNER, "doc.pdf")
    target.write_bytes(b"%PDF-1.4 xxxx")

    clean = store.clean_file_refs(OWNER, [{"path": ref["path"], "name": "подделка"}])
    assert len(clean) == 1
    assert clean[0]["name"] == "подделка"          # имя как подпись - принимаем
    assert clean[0]["kind"] == "file"              # тип берём с диска, не у клиента
    assert clean[0]["size"] == len(b"%PDF-1.4 xxxx")


def test_clean_file_refs_rejects_missing(store):
    store.get_active(OWNER)
    with pytest.raises(ChatError):
        store.clean_file_refs(OWNER, [{"path": "files/нет-такого.txt"}])


def test_file_kind():
    assert file_kind("a.JPG") == "image"
    assert file_kind("a.pdf") == "file"
    assert file_kind("a") == "file"


# ---------------------------------------------------------------- разные владельцы

def test_owners_are_isolated(store):
    store.append_message("иванов", "user", "моё")
    store.append_message("петров", "user", "чужое")
    assert len(store.get_active("иванов")["messages"]) == 1
    assert store.get_active("иванов")["messages"][0]["content"] == "моё"


# ---------------------------------------------------------------- сборка сообщений

def test_build_messages_plain_text_stays_string():
    """Сообщение без файлов - обычная строка (проще и совместимее), а не список
    частей из одного элемента."""
    import chat_llm

    chat = {"messages": [
        {"role": "user", "content": "вопрос", "files": []},
        {"role": "assistant", "content": "ответ", "files": []},
    ]}
    msgs = chat_llm.build_messages(chat, resolve=lambda p: None)
    assert msgs == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ]


def test_build_messages_inlines_file_text(tmp_path):
    """Текст файла доходит до модели ВРЕЗКОЙ в сообщение: по API LM Studio сам
    PDF не читает, поэтому содержимое передаём явно."""
    import chat_llm

    doc = tmp_path / "note.txt"
    doc.write_text("СЕКРЕТНОЕ СОДЕРЖИМОЕ", encoding="utf-8")

    chat = {"messages": [{
        "role": "user", "content": "что в файле?",
        "files": [{"name": "note.txt", "path": "files/x__note.txt", "kind": "file"}],
    }]}
    msgs = chat_llm.build_messages(chat, resolve=lambda p: doc)

    assert len(msgs) == 1
    parts = msgs[0]["content"]
    assert isinstance(parts, list)
    text_part = parts[0]["text"]
    assert "что в файле?" in text_part
    assert "СЕКРЕТНОЕ СОДЕРЖИМОЕ" in text_part


def test_build_messages_image_becomes_image_part(tmp_path):
    import chat_llm

    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n fake")

    chat = {"messages": [{
        "role": "user", "content": "посмотри",
        "files": [{"name": "pic.png", "path": "files/x__pic.png", "kind": "image"}],
    }]}
    msgs = chat_llm.build_messages(chat, resolve=lambda p: img)

    parts = msgs[0]["content"]
    kinds = [p["type"] for p in parts]
    assert "text" in kinds and "image_url" in kinds
    image_part = next(p for p in parts if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_build_messages_notes_unreadable_file(tmp_path):
    """Нечитаемый файл не роняет сообщение - о нём просто есть пометка, чтобы
    модель (и человек) знали, что файл был."""
    import chat_llm

    binary = tmp_path / "data.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")

    chat = {"messages": [{
        "role": "user", "content": "разбери",
        "files": [{"name": "data.bin", "path": "files/x__data.bin", "kind": "file"}],
    }]}
    msgs = chat_llm.build_messages(chat, resolve=lambda p: binary)
    assert "извлечь текст не удалось" in msgs[0]["content"][0]["text"]


def test_extract_text_from_txt(tmp_path):
    import chat_llm

    p = tmp_path / "a.md"
    p.write_text("# Заголовок\nтекст", encoding="utf-8")
    assert "Заголовок" in chat_llm.extract_file_text(p)


def test_extract_text_returns_none_for_unknown_binary(tmp_path):
    import chat_llm

    p = tmp_path / "a.bin"
    p.write_bytes(b"\x00\x01")
    assert chat_llm.extract_file_text(p) is None


# ---------------------------------------------------------------- раздумья модели
# ThinkSplitter отделяет видимый ответ от рассуждения по инлайновым тегам
# <think>…</think>. Тег может прийти РАЗРЕЗАННЫМ между чанками потока - это и есть
# место, где наивная реализация молча ломается (кусок тега утёк бы в ответ).

def _run_splitter(chunks):
    from chat_llm import ThinkSplitter
    s = ThinkSplitter()
    out = []
    for c in chunks:
        out += s.feed(c)
    out += s.flush()
    return out


def test_think_inline_single_chunk():
    assert _run_splitter(["<think>размышляю</think>ответ"]) == [
        ("reasoning", "размышляю"), ("content", "ответ")]


def test_think_tag_split_across_chunks():
    out = _run_splitter(["привет <th", "ink>мысль", " ещё</thi", "nk>итог"])
    kinds = [(k, t) for k, t in out]
    # ни один кусок открывающего/закрывающего тега не утёк в текст
    assert ("content", "привет ") in kinds
    assert ("reasoning", "мысль") in kinds
    assert ("content", "итог") in kinds
    joined = "".join(t for k, t in out if k == "content")
    assert "<think" not in joined and "think>" not in joined


def test_think_absent_is_all_content():
    assert _run_splitter(["обычный ", "ответ"]) == [
        ("content", "обычный "), ("content", "ответ")]


def test_stray_lt_is_not_a_tag():
    assert _run_splitter(["a < b"]) == [("content", "a < b")]


def test_unterminated_think_flushes_as_reasoning():
    assert _run_splitter(["<think>всё ещё думаю"]) == [
        ("reasoning", "всё ещё думаю")]
