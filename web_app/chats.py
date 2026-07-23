#!/usr/bin/env python3
"""
Обычный чат с нейросетью - отдельная от анализа функция интерфейса.

ЗАЧЕМ ЭТО ОТДЕЛЬНО ОТ СЕССИЙ. Сессия - это комплект документов ОДНОГО шкафа и
её прогон пайплайна: очередь к серверу ИИ, извлечение, чекеры, отчёт. Чат - это
живой диалог с моделью, ему всё это не нужно и вредно (интерактивный чат не
должен ждать в очереди чужой сорокаминутный анализ). Поэтому у чата своё
хранилище и свой прямой вызов LM Studio, минуя очередь.

РАСКЛАДКА - У КАЖДОГО ВЛАДЕЛЬЦА РОВНО ОДИН АКТИВНЫЙ ЧАТ, лежит в отдельном корне
`chats/` (в КОРНЕ проекта, рядом с exe в сборке - это данные установки, как
sessions/ и users.json, а не код):

    chats/<владелец>/
        active/                 - текущий чат, который видит пользователь
            chat.json           - {id, owner, model, created_at, updated_at, messages}
            files/<uid>__<имя>   - файлы и картинки, приложенные к сообщениям
        archive/                - «Новый чат» переносит сюда старый и открывает пустой
            <id старого чата>/
                chat.json
                files/...

«НОВЫЙ ЧАТ» = АРХИВАЦИЯ, А НЕ УДАЛЕНИЕ. По требованию заказчика старый чат
удаляется ДЛЯ ПОЛЬЗОВАТЕЛЯ (в интерфейсе он его больше не видит), но остаётся на
диске в archive/ на хранении. Списка прошлых чатов у пользователя нет - активный
всегда ровно один.

Имя папки владельца берётся у SessionStore._owner_dirname - той же функцией, что
и у сессий, СОЗНАТЕЛЬНО: две копии логики «безопасное имя папки владельца»
однажды разъехались бы на одну букву (ровно то, чем этот проект уже обжигался -
см. normalize.py, script_loader.py). Владельца адресуем по КАНОНИЧЕСКОМУ логину
(users.canon), поэтому имя папки детерминировано и чат ищется прямым путём, без
обхода каталога (в отличие от сессий, где адресация по уникальному id).

Модуль на голой стандартной библиотеке: ни fitz, ни openai здесь нет. Всё, что
касается вызова модели и извлечения текста из файлов, живёт в chat_llm.py -
ChatStore только хранит.
"""

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from paths import PROJECT_ROOT
from sessions import SessionStore

CHATS_DIR = PROJECT_ROOT / "chats"

# Картинки уходят модели как image-части (vision); всё остальное - «файл», из
# которого chat_llm попытается извлечь текст. Список расширений - единственное
# место, где «картинка» отличается от «файла».
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class ChatError(Exception):
    """Ошибка, которую можно показать пользователю (нет модели, битый путь)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def file_kind(name) -> str:
    return "image" if Path(name).suffix.lower() in IMAGE_EXTS else "file"


class ChatStore:
    """Хранилище чатов на файловой системе.

    Состояние живёт в chat.json, а не в памяти процесса: пользователь может
    закрыть вкладку и вернуться к тому же диалогу завтра, ровно как к сессии.
    """

    def __init__(self, root: Path = CHATS_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---------- пути ----------

    def _owner_dir(self, owner) -> Path:
        # Тот же безопасный преобразователь имени, что у сессий (см. заголовок).
        return self.root / SessionStore._owner_dirname(owner)

    def _active_dir(self, owner) -> Path:
        return self._owner_dir(owner) / "active"

    def _archive_dir(self, owner) -> Path:
        return self._owner_dir(owner) / "archive"

    def active_files_dir(self, owner) -> Path:
        return self._active_dir(owner) / "files"

    # ---------- чтение/запись ----------

    def _read_chat(self, chat_dir: Path) -> dict | None:
        meta = chat_dir / "chat.json"
        if not meta.is_file():
            return None
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_chat(self, chat_dir: Path, data: dict) -> None:
        """Атомарная запись через временный файл + os.replace: сервер могут
        убить в любой момент, и половина JSON означала бы потерянный диалог."""
        chat_dir.mkdir(parents=True, exist_ok=True)
        path = chat_dir / "chat.json"
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _new_id() -> str:
        # Читается глазами в проводнике и сортируется как строка; хвост uuid -
        # на случай двух чатов в одну секунду.
        return "{}_{}".format(time.strftime("%Y-%m-%d_%H-%M-%S"), uuid.uuid4().hex[:4])

    def _fresh_chat(self, owner, model=None) -> dict:
        now = time.time()
        return {
            "id": self._new_id(),
            "owner": owner or None,
            "model": model,
            "created_at": now,
            "updated_at": now,
            "messages": [],
        }

    # ---------- активный чат ----------

    def get_active(self, owner) -> dict:
        """Текущий чат владельца. Если его ещё нет - заводит пустой."""
        with self._lock:
            active = self._active_dir(owner)
            chat = self._read_chat(active)
            if chat is None:
                chat = self._fresh_chat(owner)
                self._write_chat(active, chat)
                (active / "files").mkdir(parents=True, exist_ok=True)
            return chat

    def set_model(self, owner, model) -> dict:
        """Модель, с которой пользователь общается в этом чате. Существование
        модели на сервере тут НЕ проверяется - сервер бывает временно выключен,
        а прогон честно упадёт с понятной ошибкой (как и у выбора моделей сессии)."""
        if model in (None, ""):
            model = None
        elif not isinstance(model, str) or len(model) > 200:
            raise ChatError("Недопустимое имя модели", 400)
        with self._lock:
            active = self._active_dir(owner)
            chat = self.get_active(owner)
            chat["model"] = model
            chat["updated_at"] = time.time()
            self._write_chat(active, chat)
            return chat

    def append_message(self, owner, role, content, files=None) -> dict:
        """Добавляет сообщение в активный чат и возвращает его.

        Пишется сразу на диск: ответ модели идёт потоком и может занять минуту,
        и потеря диалога при обрыве недопустима (частичный ответ мы тоже
        сохраняем - см. server._api_chat_send)."""
        message = {
            "role": role,
            "content": content or "",
            "files": files or [],
            "ts": time.time(),
        }
        with self._lock:
            active = self._active_dir(owner)
            chat = self.get_active(owner)
            chat["messages"].append(message)
            chat["updated_at"] = message["ts"]
            self._write_chat(active, chat)
            return message

    def new_chat(self, owner) -> dict:
        """Архивирует текущий чат и открывает пустой.

        Пустой активный чат архивировать незачем - просто оставляем его (архив
        засорился бы пустыми диалогами). Новый чат наследует ВЫБРАННУЮ МОДЕЛЬ:
        человек только что общался с ней и, начиная новый диалог, почти наверняка
        хочет ту же - заставлять выбирать заново на каждый «Новый чат» назойливо.
        """
        with self._lock:
            active = self._active_dir(owner)
            chat = self.get_active(owner)
            model = chat.get("model")
            if chat.get("messages"):
                dest = self._archive_dir(owner) / chat["id"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                # На случай коллизии id (архивировали дважды в одну секунду -
                # хвост uuid тот же) добавляем различитель, чтобы move не упал.
                if dest.exists():
                    dest = dest.with_name(dest.name + "_" + uuid.uuid4().hex[:4])
                shutil.move(str(active), str(dest))
            else:
                # Диалог пуст - переиспользуем папку, только сбрасываем id/время.
                shutil.rmtree(active, ignore_errors=True)
            fresh = self._fresh_chat(owner, model=model)
            self._write_chat(active, fresh)
            (active / "files").mkdir(parents=True, exist_ok=True)
            return fresh

    # ---------- файлы ----------

    def upload_target(self, owner, filename) -> tuple:
        """Куда лечь загружаемому файлу и его будущая ссылка. Проверяет имя, но
        НЕ пишет: тело запроса льётся в файл потоком (multipart.py), и решение
        «принимаем ли» надо принять до первого байта.

        Возвращает (абсолютный путь, ссылка для сообщения). Ссылка - путь
        относительно папки active/ (files/<uid>__<имя>): по нему файл потом
        адресуется на просмотр и по нему же chat_llm читает его содержимое.
        """
        name = Path(filename or "").name        # отрезаем путь со стороны клиента
        if not name:
            raise ChatError("пустое имя файла", 400)
        files_dir = self.active_files_dir(owner)
        files_dir.mkdir(parents=True, exist_ok=True)
        # uid спереди: одно и то же имя можно приложить дважды, и второе не должно
        # затирать первое.
        stored = uuid.uuid4().hex[:8] + "__" + name
        rel = "files/" + stored
        return files_dir / stored, {"name": name, "path": rel, "kind": file_kind(name)}

    def resolve_file(self, owner, rel_path) -> Path:
        """Абсолютный путь приложенного файла по ссылке из сообщения.

        Проверка обязательна - ссылка приходит из URL/тела: пускаем только
        внутрь active/files, наружу папки чата ничего не отдаём."""
        rel = Path(str(rel_path or ""))
        if rel.is_absolute() or ".." in rel.parts:
            raise ChatError(f"Недопустимый путь файла: {rel_path}", 400)
        active = self._active_dir(owner)
        target = (active / rel).resolve()
        files_root = self.active_files_dir(owner).resolve()
        if not (target == files_root or files_root in target.parents):
            raise ChatError(f"Недопустимый путь файла: {rel_path}", 400)
        if not target.is_file():
            raise ChatError(f"Файл не найден: {rel.name}", 404)
        return target

    def clean_file_refs(self, owner, refs) -> list:
        """Проверяет присланные ссылки на файлы и пересобирает их по ДИСКУ, а не
        по данным клиента: имя и размер берём с файла, тип - из расширения. Файл
        обязан существовать под active/files (resolve_file это и проверяет)."""
        out = []
        for ref in refs or []:
            path = ref.get("path") if isinstance(ref, dict) else ref
            target = self.resolve_file(owner, path)         # 400/404, если не тот
            name = ref.get("name") if isinstance(ref, dict) else None
            name = Path(name).name if name else target.name
            out.append({
                "name": name,
                "path": str(path),
                "kind": file_kind(name),
                "size": target.stat().st_size,
            })
        return out
