#!/usr/bin/env python3
"""
Сессии анализа: единица работы веб-интерфейса.

Сессия - это комплект документов ОДНОГО шкафа плюс её прогон и её отчёт. У
каждой сессии своя папка на диске со своими base_files, своими извлечёнными
данными и своим output - поэтому две сессии больше не затирают друг друга
(раньше все прогоны делили одни и те же data/ и output/ из config.yaml, а
загрузка новых файлов через сайт стирала файлы предыдущего пользователя).

ПОЧЕМУ СЕССИИ ЛЕЖАТ ВНУТРИ analyzer_to_errors/, а не где угодно: ingest.py
записывает пути документов в манифест через relative_to(PROJECT_ROOT), а
main.py собирает их обратно как PROJECT_ROOT / doc["data_dir"]. Папка сессии
за пределами analyzer_to_errors уронит извлечение невнятным ValueError. Если
захочется вынести сессии на другой диск - сначала чинить эти три места в
ingest.py, а не менять SESSIONS_DIR здесь.

Раскладка:
    analyzer_to_errors/sessions/<id>/
        session.json    - метаданные и статус (единственный источник правды)
        config.yaml     - конфиг прогона: копия базового с путями этой сессии
        log.txt         - лог прогона (append-only, отдаётся браузеру по смещению)
        data/
            base_files/                     - файлы пользователя
            base_analysis_scripts/          - копия общей папки парсеров
            your_helping_scripts_and_files/ - песочница агента
            <имя документа>/                - извлечённые данные
        output/merged_report.json

Аутентификации нет и поля «автор» нет сознательно: инструмент корпоративный,
любой сотрудник создаёт сессии и видит чужие (в т.ч. может отменить и удалить).
"""

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANALYZER_DIR = HERE.parent / "analyzer_to_errors"
SESSIONS_DIR = ANALYZER_DIR / "sessions"

# Общая папка скриптов-парсеров, откуда её копия раскладывается в каждую сессию.
SHARED_SCRIPTS_DIR = ANALYZER_DIR / "data" / "base_analysis_scripts"

# .xlsx - для спецификации (единственный документ связки не в PDF).
ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xlsm"}

# Пометка "это альбом целиком, а не документ". Стоит в одном ряду с scheme/
# assembly/spec/netlist в выпадающем списке интерфейса, но означает другое: не
# вид документа, а контейнер документов. Файл с такой пометкой перед запуском
# переезжает в full_projects/, где его разбирает full_project.py, а в
# .doc_types.json пайплайна НЕ уходит - там она была бы бессмысленна, такого
# типа документа у ingest.py нет.
FULL_PROJECT_TYPE = "full_project"

# draft     - создана, пользователь докладывает файлы
# queued    - поставлена в очередь, ждёт воркера
# running   - выполняется прямо сейчас
# done      - отчёт готов
# error     - прогон упал
# cancelled - отменена пользователем (из очереди или на ходу)
# interrupted - сервер перезапустили посреди прогона
ACTIVE_STATUSES = {"queued", "running"}
FINAL_STATUSES = {"done", "error", "cancelled", "interrupted"}

# id генерируем сами, но приходит он из URL - поэтому проверяем формат перед
# любым обращением к диску (иначе "../.." в пути сессии).
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_[0-9a-f]{4}$")


class SessionError(Exception):
    """Ошибка, которую можно показать пользователю (сессии нет, статус не тот)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def valid_id(session_id) -> bool:
    return bool(session_id) and bool(_ID_RE.match(str(session_id)))


class SessionStore:
    """Хранилище сессий на файловой системе.

    Состояние сессии живёт в session.json, а не в памяти процесса: перезапуск
    сервера не должен терять ни очередь, ни готовые отчёты. Оперативная память
    здесь - только кэш индекса для быстрого списка.
    """

    def __init__(self, root: Path = SESSIONS_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---------- пути ----------

    def dir_of(self, session_id) -> Path:
        if not valid_id(session_id):
            raise SessionError(f"Некорректный идентификатор сессии: {session_id}", 400)
        return self.root / session_id

    def paths_of(self, session_id) -> dict:
        """Абсолютные пути сессии - ровно те, что уезжают в paths её config.yaml."""
        d = self.dir_of(session_id)
        data = d / "data"
        return {
            "session_dir": d,
            "meta": d / "session.json",
            "config": d / "config.yaml",
            "log": d / "log.txt",
            "data_dir": data,
            "base_files_dir": data / "base_files",
            # Альбомы целиком (200+ листов). Лежат ВНЕ base_files: тот
            # сканируется рекурсивно, и подпапка в нём означает связку
            # (bundles.py), так что альбом внутри стал бы "связкой" из одного
            # файла, который к тому же не проходит определение типа. Сюда
            # альбом переносит сам пайплайн, опознав его по числу листов
            # (full_project.collect_albums) - web_app работает на голой
            # стандартной библиотеке и открыть PDF не может.
            "full_projects_dir": data / "full_projects",
            "scripts_dir": data / "base_analysis_scripts",
            "helper_scripts_dir": data / "your_helping_scripts_and_files",
            "output_dir": d / "output",
            "report": d / "output" / "merged_report.json",
        }

    # ---------- чтение/запись метаданных ----------

    def _read_meta(self, session_id) -> dict:
        path = self.paths_of(session_id)["meta"]
        if not path.is_file():
            raise SessionError(f"Сессия не найдена: {session_id}", 404)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise SessionError(f"Не удалось прочитать сессию {session_id}: {e}", 500)

    def _write_meta(self, meta: dict) -> None:
        """Атомарная запись: временный файл рядом + os.replace. Сервер могут
        убить в любой момент, и половина JSON на диске означала бы потерянную
        сессию."""
        path = self.paths_of(meta["id"])["meta"]
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ---------- жизненный цикл ----------

    def create(self, name=None) -> dict:
        """Создаёт пустую сессию в статусе draft и возвращает её метаданные."""
        with self._lock:
            # id читается глазами в проводнике и сортируется как строка;
            # хвост из uuid - на случай двух сессий в одну секунду
            session_id = "{}_{}".format(
                time.strftime("%Y-%m-%d_%H-%M-%S"), uuid.uuid4().hex[:4])
            paths = self.paths_of(session_id)
            for key in ("base_files_dir", "full_projects_dir", "helper_scripts_dir", "output_dir"):
                paths[key].mkdir(parents=True, exist_ok=True)
            meta = {
                "id": session_id,
                "name": (name or "").strip() or "Без названия",
                "status": "draft",
                "mode": None,
                "created_at": time.time(),
                "queued_at": None,
                "started_at": None,
                "finished_at": None,
                "n_findings": None,
                "error": None,
                "doc_types": {},
            }
            self._write_meta(meta)
            return meta

    def get(self, session_id) -> dict:
        with self._lock:
            return self._read_meta(session_id)

    def update(self, session_id, **fields) -> dict:
        """Точечно меняет поля session.json. Пишет целиком - файл крошечный."""
        with self._lock:
            meta = self._read_meta(session_id)
            meta.update(fields)
            self._write_meta(meta)
            return meta

    def list(self) -> list:
        """Все сессии, новые сверху. Битые папки просто пропускаем: сессия -
        это папка на диске, её могли скопировать/удалить руками."""
        with self._lock:
            out = []
            for d in self.root.iterdir():
                if not d.is_dir() or not valid_id(d.name):
                    continue
                try:
                    out.append(self._read_meta(d.name))
                except SessionError:
                    continue
            out.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
            return out

    def delete(self, session_id) -> None:
        with self._lock:
            meta = self._read_meta(session_id)
            if meta["status"] in ACTIVE_STATUSES:
                raise SessionError(
                    "Сессия в очереди или выполняется - сначала отмените её", 409)
            shutil.rmtree(self.dir_of(session_id), ignore_errors=True)

    def rename(self, session_id, name) -> dict:
        name = (name or "").strip()
        if not name:
            raise SessionError("Пустое название сессии", 400)
        return self.update(session_id, name=name)

    def restore_after_restart(self) -> list:
        """Приводит статусы в чувство после перезапуска сервера и возвращает
        сессии, которые надо вернуть в очередь (в порядке постановки).

        running -> interrupted: процесс прогона умер вместе с прежним сервером,
        и делать вид, что он идёт, нельзя. Автоматически перезапускать тоже не
        станем: пользователь мог перезапустить сервер именно для того, чтобы
        прогон прекратился.
        queued -> остаются queued: их просто никто ещё не начинал.
        """
        with self._lock:
            requeue = []
            for meta in self.list():
                if meta["status"] == "running":
                    self.update(meta["id"], status="interrupted",
                                finished_at=time.time(),
                                error="Сервер был перезапущен во время анализа")
                    self.append_log(meta["id"],
                                    "!!! Сервер был перезапущен - прогон прерван")
                elif meta["status"] == "queued":
                    requeue.append(meta)
            requeue.sort(key=lambda m: m.get("queued_at") or m.get("created_at") or 0)
            return requeue

    # ---------- файлы сессии ----------

    def files(self, session_id) -> list:
        """Файлы сессии с типом документа: явный выбор пользователя, иначе
        догадка по имени файла (марка вида по ГОСТ - см. ingest.detect_doc_type).

        rglob, а не iterdir: пользователь может разложить комплект по подпапкам
        base_files/, и такие подпапки - явные связки (bundles.py)."""
        import ingest  # импорт здесь: analyzer_to_errors уже в sys.path у сервера

        meta = self._read_meta(session_id)
        paths = self.paths_of(session_id)
        base = paths["base_files_dir"]
        overrides = meta.get("doc_types") or {}
        files = []
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in ALLOWED_SUFFIXES:
                    continue
                if p.name.startswith("~$") or p.name.startswith("."):
                    continue          # временный файл открытой книги Excel
                rel = p.relative_to(base)
                files.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "detected_type": overrides.get(p.name) or ingest.detect_doc_type(p.name),
                    # связка. Обычно None: все документы прогона - один проект;
                    # имя появляется, только если файлы разложены по подпапкам
                    "bundle": rel.parts[0] if len(rel.parts) > 1 else None,
                })

        # Альбомы. Показываются вместе с остальными файлами, хотя лежат в другой
        # папке: для пользователя это такой же загруженный им файл. После
        # прогона альбом оказывается здесь и в base_files его уже нет - не
        # перечислив эту папку, интерфейс показал бы, что файл пропал, а рядом
        # четырнадцать непонятно откуда взявшихся связок.
        fp_dir = paths["full_projects_dir"]
        if fp_dir.is_dir():
            for p in sorted(fp_dir.iterdir()):
                if not p.is_file() or p.suffix.lower() != ".pdf":
                    continue
                if p.name.startswith("~$") or p.name.startswith("."):
                    continue
                files.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "detected_type": FULL_PROJECT_TYPE,
                    "bundle": None,
                })
        return files

    def save_upload(self, session_id, filename, data: bytes) -> str:
        """Кладёт один файл в base_files сессии. В отличие от прежней загрузки,
        НИЧЕГО не стирает: файлы докладываются, а изоляция пользователей теперь
        обеспечена самой сессией."""
        meta = self._read_meta(session_id)
        if meta["status"] in ACTIVE_STATUSES:
            raise SessionError("Сессия в очереди или выполняется - файлы менять нельзя", 409)
        name = Path(filename).name          # отрезаем любой путь со стороны клиента
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise SessionError("неподдерживаемый формат (нужен PDF для схем и чертежей "
                               "либо .xlsx для спецификации)", 400)
        base = self.paths_of(session_id)["base_files_dir"]
        base.mkdir(parents=True, exist_ok=True)
        (base / name).write_bytes(data)
        return name

    def delete_file(self, session_id, filename) -> None:
        meta = self._read_meta(session_id)
        if meta["status"] in ACTIVE_STATUSES:
            raise SessionError("Сессия в очереди или выполняется - файлы менять нельзя", 409)
        paths = self.paths_of(session_id)
        name = Path(filename).name
        target = None
        # base_files рекурсивно (файл может лежать в подпапке-связке), затем
        # full_projects: после прогона альбом лежит там, и не заглянув сюда,
        # удалить его из интерфейса было бы нельзя вовсе.
        for p in list(paths["base_files_dir"].rglob("*")) + \
                list(paths["full_projects_dir"].glob("*")):
            if p.is_file() and p.name == name:
                target = p
                break
        if target is None:
            raise SessionError(f"Файл не найден: {name}", 404)
        target.unlink()
        with self._lock:
            meta = self._read_meta(session_id)
            if meta.get("doc_types", {}).pop(name, None) is not None:
                self._write_meta(meta)

    def set_type(self, session_id, filename, doc_type, valid_types) -> None:
        """Пометка типа документа. Хранится в session.json, а не в общем
        data/.doc_types.json: тот был один на весь сервер и ключевался голым
        именем файла, так что две сессии с одинаковым именем файла спорили за
        одну запись. В data/.doc_types.json сессии пометки уезжают только перед
        запуском - их там ждёт ingest."""
        if doc_type and doc_type not in valid_types:
            raise SessionError(f"Недопустимый тип: {doc_type}", 400)
        with self._lock:
            meta = self._read_meta(session_id)
            if meta["status"] in ACTIVE_STATUSES:
                raise SessionError("Сессия в очереди или выполняется - типы менять нельзя", 409)
            types = dict(meta.get("doc_types") or {})
            if doc_type:
                types[filename] = doc_type
            else:
                types.pop(filename, None)   # пустой выбор - сброс пометки
            meta["doc_types"] = types
            self._write_meta(meta)

    def has_files(self, session_id) -> bool:
        base = self.paths_of(session_id)["base_files_dir"]
        return base.is_dir() and any(
            p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES for p in base.rglob("*"))

    # ---------- лог ----------

    def append_log(self, session_id, line) -> None:
        """Лог пишем сразу на диск, а не только в память: пользователь может
        закрыть вкладку и вернуться к сессии завтра, а строки нужны и для
        разбора упавшего прогона."""
        path = self.paths_of(session_id)["log"]
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")
        except OSError:
            pass    # лог не должен ронять прогон

    def read_log(self, session_id, since=0) -> tuple:
        """Возвращает (строки начиная с номера since, номер следующей строки).

        Отдаём по смещению, а не целиком: раньше статус слал браузеру весь лог
        (до 5000 строк) каждую секунду поллинга.
        """
        path = self.paths_of(session_id)["log"]
        if not path.is_file():
            return [], 0
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return [], since
        since = max(0, int(since or 0))
        return lines[since:], len(lines)

    # ---------- отчёт ----------

    def report(self, session_id) -> dict:
        path = self.paths_of(session_id)["report"]
        if not path.is_file():
            raise SessionError("Отчёт этой сессии ещё не сформирован", 404)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise SessionError(f"Не удалось прочитать отчёт: {e}", 500)

    # ---------- подготовка к прогону ----------

    def prepare_run(self, session_id) -> dict:
        """Раскладывает песочницу сессии перед запуском и возвращает её пути.

        Копия base_analysis_scripts кладётся В САМУ сессию, а не шарится:
        промпт агента (oi_agent.py) описывает её как подпапку своей песочницы
        data/, а clear_previous_results сохраняет служебные папки по .name из
        config.paths. С копией обе вещи работают без единой правки в пайплайне,
        а в архиве сессии остаётся та версия парсеров, которой её считали.
        """
        paths = self.paths_of(session_id)
        for key in ("base_files_dir", "full_projects_dir", "helper_scripts_dir", "output_dir"):
            paths[key].mkdir(parents=True, exist_ok=True)

        if SHARED_SCRIPTS_DIR.is_dir():
            shutil.copytree(SHARED_SCRIPTS_DIR, paths["scripts_dir"],
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))

        meta = self._read_meta(session_id)
        doc_types = dict(meta.get("doc_types") or {})

        # Файлы, помеченные как альбом, переезжают в full_projects/ - там их
        # ждёт full_project.py. Пайплайн опознаёт альбом и сам (по числу
        # листов), но пометка пользователя главнее: она снимает догадку и
        # работает даже на альбоме короче порога.
        for name in [n for n, t in doc_types.items() if t == FULL_PROJECT_TYPE]:
            src = paths["base_files_dir"] / name
            if src.is_file():
                shutil.move(str(src), str(paths["full_projects_dir"] / name))
            # в .doc_types.json такая пометка не уезжает: у ingest.py нет
            # типа документа "full_project", и файл с ним осел бы в
            # skipped_files с невнятной причиной
            doc_types.pop(name, None)

        # пометки типов - туда, где их ищет ingest (data/.doc_types.json)
        sidecar = paths["data_dir"] / ".doc_types.json"
        sidecar.write_text(
            json.dumps(doc_types, ensure_ascii=False, indent=2), encoding="utf-8")
        return paths

    def cleanup_after_cancel(self, session_id) -> None:
        """После жёсткой отмены подчищает только результаты незавершённого
        прогона: output/ (недописанные report_*.json) и рабочую папку агента.
        Файлы пользователя и уже извлечённые данные документов не трогаем -
        работа над ними завершилась корректно и пригодится на следующем запуске."""
        paths = self.paths_of(session_id)
        for key in ("output_dir", "helper_scripts_dir"):
            d = paths[key]
            if not d.exists():
                continue
            for item in d.iterdir():
                try:
                    item.unlink() if item.is_file() else shutil.rmtree(item)
                except OSError:
                    pass  # файл на миг остался занят ОС после kill - не критично
