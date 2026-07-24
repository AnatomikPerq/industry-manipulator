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

Раскладка - У КАЖДОГО ВЛАДЕЛЬЦА СВОЯ ПАПКА:
    analyzer_to_errors/sessions/<владелец>/<id>/
        session.json    - метаданные и статус (единственный источник правды)
        config.yaml     - конфиг прогона: копия базового с путями этой сессии
        log.txt         - лог прогона (append-only, отдаётся браузеру по смещению)
        data/
            base_files/                     - файлы пользователя
            base_analysis_scripts/          - копия общей папки парсеров
            your_helping_scripts_and_files/ - песочница агента
            <имя документа>/                - извлечённые данные
        output/merged_report.json

У сессии ЕСТЬ ВЛАДЕЛЕЦ (поле owner - канонический логин из users.py). Человек
видит и трогает только свои сессии; администратор видит и трогает все. Само
разграничение делает web_app (server.py) при обращении к эндпоинтам - хранилище
лишь хранит owner и умеет отдать бесхозные сессии администратору (assign_ownerless).

ПАПКА ВЛАДЕЛЬЦА - ТОЛЬКО РАСКЛАДКА НА ДИСКЕ (чтобы в проводнике было видно, чьи
сессии), а НЕ адресация: id по-прежнему уникален и владельца в себе не несёт.
Поэтому сессию ищем по id в обеих раскладках (_locate): новые лежат вложенно, а
СТАРЫЕ, созданные до этой правки, остаются ПЛОСКО в sessions/<id>/ и не
переезжают. Переезд молча сломал бы их: manifest.json хранит пути документов
через relative_to(PROJECT_ROOT) (sessions/<id>/data/...), и после переноса
фрагменты чертежа и повторный прогон искали бы данные по старому пути. Обе
раскладки живут рядом без конфликта: имя папки владельца - это логин, а он
никогда не совпадает с форматом id (_ID_RE), см. users._reject_unsafe_login.

ПОЧЕМУ СЕССИИ ЛЕЖАТ ВНУТРИ analyzer_to_errors/ (и папка владельца тоже): ingest.py
пишет пути документов через relative_to(PROJECT_ROOT), а main.py собирает их как
PROJECT_ROOT / doc["data_dir"]. Вложенность на это не влияет - relative_to просто
даёт путь на уровень глубже, - но вынести сессии ЗА analyzer_to_errors по-прежнему
нельзя: уронит извлечение невнятным ValueError.
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

# analyzer_to_errors уже в sys.path у сервера. bundles - стдлиб-модуль
# (никакого fitz), и в нём живёт имя метки нарезанной подпапки.
import bundles
from paths import ANALYZER_DIR

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

# Имена, которые считаются «сессию ещё не назвали»: их автонейминг вправе
# заменить, а данное пользователем имя - никогда. "Новая сессия" тут на случай,
# если её так подставит интерфейс; "Без названия" ставит create по умолчанию.
DEFAULT_SESSION_NAMES = {"", "без названия", "новая сессия"}

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
        # Кэш id -> папка сессии. Сессия адресуется по id, а на диске лежит либо
        # вложенно (sessions/<владелец>/<id>), либо плоско (старые). Поиск по id
        # (_locate) обходит папки владельцев, поэтому его результат кэшируем:
        # dir_of зовётся на каждый paths_of, а тот - почти на каждый запрос.
        self._dir_cache = {}

    # ---------- пути ----------

    @staticmethod
    def _is_plain_segment(name) -> bool:
        """Годится ли строка как имя папки владельца КАК ЕСТЬ (без хэширования).

        Отсекает всё, что либо небезопасно как имя папки (обход каталога,
        зарезервированные имена устройств Windows, точки/пробелы по краям), либо
        спутало бы папку владельца с папкой сессии (формат id). Логины,
        прошедшие users._reject_unsafe_login, сюда проходят целиком - остальное
        (руками правленый owner, легаси) уедет в хэш."""
        if not name or "/" in name or "\\" in name:
            return False
        if name != name.strip() or name.startswith(".") or name.endswith("."):
            return False
        if name.strip(". ") == "" or name == "_ownerless":
            return False
        if _ID_RE.match(name):
            return False
        base = name.split(".", 1)[0].lower()
        reserved = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} \
            | {f"lpt{i}" for i in range(1, 10)}
        return base not in reserved

    @classmethod
    def _owner_dirname(cls, owner) -> str:
        """Имя папки для владельца. Читаемое для нормального логина, безопасное
        для любого: непроходное имя заменяется хэшем (папку всё равно можно
        создать, а сессия не теряется - её всё равно ищут по id)."""
        name = (owner or "").strip()
        if not name:
            return "_ownerless"
        if cls._is_plain_segment(name):
            return name
        return "user_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]

    def _locate(self, session_id) -> Path:
        """Папка существующей сессии по id либо None. Сначала плоско (старые
        сессии), затем во всех папках владельцев. Признак сессии - её
        session.json, а не просто каталог: так папку владельца (в ней лежат
        сессии, но своего session.json у неё нет) не примешь за сессию."""
        flat = self.root / session_id
        if (flat / "session.json").is_file():
            return flat
        for owner_dir in self.root.iterdir():
            if owner_dir.is_dir():
                cand = owner_dir / session_id
                if (cand / "session.json").is_file():
                    return cand
        return None

    def dir_of(self, session_id) -> Path:
        if not valid_id(session_id):
            raise SessionError(f"Некорректный идентификатор сессии: {session_id}", 400)
        with self._lock:
            # Кэшу доверяем БЕЗУСЛОВНО (без проверки, что папка уже на диске):
            # create кладёт сюда путь ещё до того, как создаст саму папку и
            # session.json, - иначе тот же dir_of, зовущийся из create через
            # paths_of, вернул бы плоский путь и сессия легла бы мимо папки
            # владельца. Устаревшая запись (сессию удалили) не опасна: её из
            # кэша вычищает delete, а промах по несуществующей упрётся в 404 при
            # чтении session.json, а не выдаст левый путь.
            cached = self._dir_cache.get(session_id)
            if cached is not None:
                return cached
            found = self._locate(session_id)
            if found is not None:
                self._dir_cache[session_id] = found
                return found
            # Не найдена нигде: плоский путь по умолчанию. Запрос к
            # несуществующей сессии всё равно упрётся в 404 при чтении её
            # session.json - невнятного пути наружу не уйдёт.
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

    def create(self, name=None, owner=None, owner_display=None) -> dict:
        """Создаёт пустую сессию в статусе draft и возвращает её метаданные.

        owner - канонический логин владельца (из users.canon), он же уходит в
        поле owner. Пусто (None) бывает у сессий, созданных до появления
        пользователей; их забирает администратор (assign_ownerless).

        owner_display - как назвать ПАПКУ владельца на диске: отображаемый логин
        читабельнее канонического ("Иванов" вместо "иванов"). На адресацию не
        влияет (сессию всё равно ищут по id), поэтому расхождение регистра
        безопасно; по умолчанию - сам owner.
        """
        with self._lock:
            # id читается глазами в проводнике и сортируется как строка;
            # хвост из uuid - на случай двух сессий в одну секунду
            session_id = "{}_{}".format(
                time.strftime("%Y-%m-%d_%H-%M-%S"), uuid.uuid4().hex[:4])
            # Папку владельца выбираем ЗДЕСЬ и сеем в кэш до paths_of: иначе
            # dir_of вернул бы плоский путь по умолчанию и сессия легла бы не в
            # папку владельца.
            self._dir_cache[session_id] = (
                self.root / self._owner_dirname(owner_display or owner) / session_id)
            paths = self.paths_of(session_id)
            for key in ("base_files_dir", "full_projects_dir", "helper_scripts_dir", "output_dir"):
                paths[key].mkdir(parents=True, exist_ok=True)
            meta = {
                "id": session_id,
                "name": (name or "").strip() or "Без названия",
                "owner": owner or None,
                "status": "draft",
                "mode": None,
                "created_at": time.time(),
                "queued_at": None,
                "started_at": None,
                "finished_at": None,
                "n_findings": None,
                "error": None,
                "doc_types": {},
                # выбранные для этой сессии модели и число агентов (см. set_llm);
                # пусто = как в общем config.yaml
                "llm": {},
                # что считается прямо сейчас: "скрипты" | "очередь к ИИ" | "ИИ"
                "stage": None,
                # какой документ и какой его лист читает парсер (см. progress.py)
                "progress": None,
                "llm_position": None,
                # скорость генерации сервера ИИ (токенов/с) за последний вызов
                "llm_tps": None,
            }
            self._write_meta(meta)
            return meta

    def get(self, session_id) -> dict:
        with self._lock:
            return self._read_meta(session_id)

    def owner_of(self, session_id):
        """Владелец сессии (канонический логин) либо None у бесхозной. Бросает
        SessionError(404), если сессии нет - это и есть проверка существования
        перед разграничением доступа в server.py."""
        return self._read_meta(session_id).get("owner")

    def assign_ownerless(self, owner) -> int:
        """Разовая передача всех бесхозных сессий владельцу (по замыслу -
        администратору). Идемпотентно: сессию с уже проставленным owner
        пропускает. Возвращает число переданных."""
        if not owner:
            return 0
        with self._lock:
            n = 0
            for meta in self.list():
                if not meta.get("owner"):
                    self.update(meta["id"], owner=owner)
                    n += 1
            return n

    def update(self, session_id, **fields) -> dict:
        """Точечно меняет поля session.json. Пишет целиком - файл крошечный."""
        with self._lock:
            meta = self._read_meta(session_id)
            meta.update(fields)
            self._write_meta(meta)
            return meta

    def list(self) -> list:
        """Все сессии, новые сверху. Битые папки просто пропускаем: сессия -
        это папка на диске, её могли скопировать/удалить руками.

        Обходим ДВА уровня: сессия лежит либо плоско (sessions/<id>, старые),
        либо в папке владельца (sessions/<владелец>/<id>). Папка владельца сама
        по себе не сессия (у неё нет session.json) - в неё спускаемся. Заодно
        освежаем кэш путей: тот же обход, что и для локатора, но разом на всех."""
        with self._lock:
            out = []
            for entry in self.root.iterdir():
                if not entry.is_dir():
                    continue
                if valid_id(entry.name) and (entry / "session.json").is_file():
                    self._collect_session(entry, out)      # плоская (старая)
                elif not valid_id(entry.name):
                    for sub in entry.iterdir():             # папка владельца
                        if (sub.is_dir() and valid_id(sub.name)
                                and (sub / "session.json").is_file()):
                            self._collect_session(sub, out)
            out.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
            return out

    def _collect_session(self, session_dir: Path, out: list) -> None:
        self._dir_cache[session_dir.name] = session_dir
        try:
            out.append(self._read_meta(session_dir.name))
        except SessionError:
            pass    # битая сессия - пропускаем, как и раньше

    def delete(self, session_id) -> None:
        with self._lock:
            meta = self._read_meta(session_id)
            if meta["status"] in ACTIVE_STATUSES:
                raise SessionError(
                    "Сессия в очереди или выполняется - сначала отмените её", 409)
            d = self.dir_of(session_id)
            shutil.rmtree(d, ignore_errors=True)
            self._dir_cache.pop(session_id, None)
            # Опустевшую папку владельца убираем, чтобы в проводнике не копились
            # пустые каталоги ушедших сессий. Плоскую (root) не трогаем.
            parent = d.parent
            if parent != self.root and parent.is_dir():
                try:
                    next(parent.iterdir())
                except StopIteration:
                    parent.rmdir()
                except OSError:
                    pass

    def rename(self, session_id, name) -> dict:
        name = (name or "").strip()
        if not name:
            raise SessionError("Пустое название сессии", 400)
        return self.update(session_id, name=name)

    def auto_name(self, session_id, mode_label=None) -> str:
        """Даёт сессии имя по загруженным файлам, ЕСЛИ пользователь её не назвал.

        Зовётся при запуске анализа. Правило (запрос заказчика): у безымянной
        сессии имя = «имя альбома» (для полного проекта) или «обозначение(я)
        шкафа» (для связок-подпапок) плюс выбранный режим анализа. Данное
        пользователем имя не трогаем; выводить не из чего - оставляем как есть.

        Возвращает новое имя либо None (ничего не поменяли).
        """
        with self._lock:
            current = (self._read_meta(session_id).get("name") or "").strip()
        if current.lower() not in DEFAULT_SESSION_NAMES:
            return None
        base = self._name_base(session_id)
        if not base:
            return None
        name = f"{base} · {mode_label}" if mode_label else base
        name = name.strip()[:120]
        self.update(session_id, name=name)
        return name

    def _name_base(self, session_id) -> str:
        """Основа имени сессии из её файлов: имя альбома либо обозначение шкафа.

        По убыванию явности:
          1. Альбом (полный проект) - имя файла. Он в full_projects/ (куда его
             перенёс прошлый прогон или перенесёт prepare_run по пометке типа).
          2. Файл, помеченный пользователем как альбом, ещё лежащий в base_files
             (переезд делает prepare_run уже в прогоне) - имя файла.
          3. Связки-подпапки base_files (кроме нарезанных из альбома - у них своя
             метка) - имена папок, которые задал пользователь.
          4. ПЛОСКИЙ комплект на один шкаф (файлы прямо в base_files, без
             подпапок) - обозначение шкафа из ИМЁН файлов ("...ЩСКЗ СБ",
             "...ЩСКЗ СО" -> ЩСКЗ). См. _cabinet_from_flat_files.
        Не нашлось ничего - None (имя не трогаем).
        """
        paths = self.paths_of(session_id)

        fp = paths["full_projects_dir"]
        if fp.is_dir():
            albums = sorted(p.stem for p in fp.iterdir()
                            if p.is_file() and p.suffix.lower() == ".pdf"
                            and not p.name.startswith((".", "~$")))
            if albums:
                return albums[0]

        for key, doc_type in self._doc_types(session_id).items():
            if doc_type == FULL_PROJECT_TYPE:
                return Path(key).stem

        base = paths["base_files_dir"]
        cabinets = []
        if base.is_dir():
            for sub in sorted(base.iterdir()):
                if (not sub.is_dir() or sub.name.startswith((".", "~$"))
                        or (sub / bundles.GENERATED_MARKER).exists()):
                    continue      # нарезанные из альбома части - не «шкаф» пользователя
                if any(p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES
                       for p in sub.rglob("*")):
                    cabinets.append(sub.name)
        if cabinets:
            head = ", ".join(cabinets[:3])
            return head + ("…" if len(cabinets) > 3 else "")

        return self._cabinet_from_flat_files(session_id)

    def _cabinet_from_flat_files(self, session_id) -> str:
        """Обозначение шкафа из имён файлов ПЛОСКОГО комплекта (одна связка на
        один шкаф, файлы прямо в base_files, без подпапок).

        Комплект грузят россыпью файлов; связка у них одна ("проект"), но в
        ИМЕНАХ шкаф назван у каждого документа ("...ЩСКЗ СБ", "...ЩСКЗ СО",
        "...ЩСКЗ ЭЗ"). Берём обозначение, общее для большинства файлов.

        Угадывать связку по имени файла в bundles.py ЗАПРЕЩЕНО - ошибка там молча
        ломает сверку. Здесь другое: не РАЗБИВАЕМ на связки, а ИМЕНУЕМ сессию.
        Неверная догадка даёт лишь неточное имя, которое пользователь
        переименует, - поэтому эвристика тут допустима. Марку вида убираем той же
        регуляркой, что и ingest (KIND_MARK_RE), чтобы «СБ»/«Э3» не спутались с
        обозначением; обозначение достаёт общий bundles.detect_cabinet.
        """
        import ingest  # ленивый импорт, как в files(): analyzer уже в sys.path
        from collections import Counter

        base = self.paths_of(session_id)["base_files_dir"]
        if not base.is_dir():
            return None
        votes = Counter()
        for p in sorted(base.iterdir()):
            if (not p.is_file() or p.suffix.lower() not in ALLOWED_SUFFIXES
                    or p.name.startswith(("~$", "."))):
                continue
            # 1) убрать марку вида (СБ/СО/Э3/СХ/NL) - её регуляркой ingest, ей
            #    нужны исходные разделители; 2) разделители имени файла (._-) ->
            #    пробелы. detect_cabinet заточена под НАИМЕНОВАНИЕ штампа и
            #    пропускает токен, прижатый к дефису (сегмент кода объекта
            #    «ТТС-БМК-48000»), - а в ИМЕНИ файла шкаф как раз и стоит через
            #    дефис («ИК.3912-АТХ2»). Разровняв разделители, отдаём ей уже
            #    «наименованиеподобную» строку и не трогаем сам album-путь.
            stem = ingest.KIND_MARK_RE.sub(" ", p.stem)
            stem = re.sub(r"[._\-]+", " ", stem)
            cabinet = bundles.detect_cabinet(stem)
            if cabinet:
                votes[cabinet] += 1
        return votes.most_common(1)[0][0] if votes else None

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
                                finished_at=time.time(), stage=None, progress=None,
                                llm_position=None, llm_tps=None,
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
        base_files/, и такие подпапки - явные связки (bundles.py).

        Поле "path" - путь относительно папки data сессии. Именно он, а не имя,
        адресует файл в эндпоинтах просмотра и удаления И служит ключом пометки
        типа: имена в разных подпапках-связках повторяются (у каждого шкафа
        альбома свой «Общий вид»), и по голому имени сервер открыл бы не тот
        файл, а пометка одного документа красила бы все одноимённые разом.
        """
        import ingest  # импорт здесь: analyzer_to_errors уже в sys.path у сервера

        paths = self.paths_of(session_id)
        data_dir = paths["data_dir"]
        base = paths["base_files_dir"]
        overrides = self._doc_types(session_id)
        parts_pages = self._album_parts(session_id)
        files = []
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in ALLOWED_SUFFIXES:
                    continue
                if p.name.startswith("~$") or p.name.startswith("."):
                    continue          # временный файл открытой книги Excel
                rel = p.relative_to(base)
                rel_data = p.relative_to(data_dir).as_posix()
                files.append({
                    "name": p.name,
                    "path": rel_data,
                    "size": p.stat().st_size,
                    "detected_type": (overrides.get(rel_data)
                                      or ingest.detect_doc_type(p.name)),
                    # связка. Обычно None: все документы прогона - один проект;
                    # имя появляется, только если файлы разложены по подпапкам
                    "bundle": rel.parts[0] if len(rel.parts) > 1 else None,
                    # часть, нарезанная из альбома, а не загруженная руками:
                    # её тип уже проставлен пометкой в имени, удалять её по
                    # одной бессмысленно (следующий прогон нарежет заново)
                    "generated": self._is_generated_part(p),
                    # для нарезанной части - из каких страниц альбома она вырезана
                    "pages": parts_pages.get(rel.as_posix()),
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
                    "path": p.relative_to(data_dir).as_posix(),
                    "size": p.stat().st_size,
                    "detected_type": FULL_PROJECT_TYPE,
                    "bundle": None,
                    "generated": False,
                })

        # Раз уж обошли диск - заодно поправим счётчик для списка сессий.
        # Это и есть гарантия схождения: как бы ни разошлось хранимое число
        # (папку правили руками, прогон оборвали посреди нарезки), открытие
        # сессии его чинит.
        if len(files) != self._read_meta(session_id).get("n_files"):
            self.refresh_file_count(session_id)
        return files

    # ---------- пометки типа документа ----------

    def file_count(self, session_id, meta=None) -> int:
        """Сколько файлов в сессии - для списка сессий.

        Число ХРАНИТСЯ в session.json, а не считается на каждый запрос. Список
        опрашивается раз в 2 с, наблюдатель завершения - раз в 3 с, и каждый
        такой опрос обходил рекурсивно base_files ВСЕХ сессий: у сессии с
        нарезанным альбомом там полсотни файлов, у десятка сессий - тысячи
        stat'ов в секунду на ровном месте.

        Пересчитывается там, где набор файлов меняется (загрузка, удаление,
        подготовка прогона, конец прогона - нарезка создаёт части уже внутри
        него) и лениво - если числа ещё нет, то есть у сессий, созданных до
        появления этого поля.
        """
        meta = meta or self._read_meta(session_id)
        n = meta.get("n_files")
        return n if isinstance(n, int) else self.refresh_file_count(session_id)

    def refresh_file_count(self, session_id) -> int:
        """Пересчитать и запомнить число файлов. Зовётся из мест, где набор
        файлов изменился."""
        n = len(self._existing_paths(session_id))
        try:
            with self._lock:
                meta = self._read_meta(session_id)
                if meta.get("n_files") != n:
                    meta["n_files"] = n
                    self._write_meta(meta)
        except SessionError:
            pass                # сессию удалили прямо сейчас - считать нечего
        return n

    def _existing_paths(self, session_id) -> list:
        """Пути всех файлов сессии относительно её data/ - без определения типа
        (в отличие от files(), который для типа импортирует ingest).

        Отбор ТОТ ЖЕ, что в files(): в base_files - документы разрешённых
        форматов, в full_projects - альбомы (только PDF). Иначе число файлов в
        списке сессий не сошлось бы с длиной списка внутри самой сессии.
        """
        paths = self.paths_of(session_id)
        data_dir = paths["data_dir"]
        out = []
        for root, pattern, suffixes in (
                (paths["base_files_dir"], "**/*", ALLOWED_SUFFIXES),
                (paths["full_projects_dir"], "*", {".pdf"})):
            if not root.is_dir():
                continue
            for p in root.glob(pattern):
                if (p.is_file() and p.suffix.lower() in suffixes
                        and not p.name.startswith(("~$", "."))):
                    out.append(p.relative_to(data_dir).as_posix())
        return out

    def _doc_types(self, session_id) -> dict:
        """Пометки типа {путь относительно data/: тип}, с миграцией старых сессий.

        До V1.7 ключом было ГОЛОЕ ИМЯ файла, и на альбоме это ломалось молча:
        у каждого шкафа своя подпапка со своим «Общий вид», имена повторяются -
        пометка одного документа применялась ко всем одноимённым, а пометка
        «полный проект» для файла в подпапке не находила его вовсе (prepare_run
        искал строго base_files/<имя>). Сессии на диске переживают обновление,
        поэтому старые ключи переводим на путь здесь, при первом же чтении:
        имя, которому нашёлся ровно один файл, переезжает на его путь;
        неоднозначное или потерявшее файл - отбрасывается (гадать нельзя, а
        оставить как есть значит навсегда сохранить неработающую пометку).
        """
        with self._lock:
            meta = self._read_meta(session_id)
            types = dict(meta.get("doc_types") or {})
            legacy = [k for k in types if "/" not in k]
            if not legacy:
                return types

            known = self._existing_paths(session_id)
            for name in legacy:
                value = types.pop(name)
                matches = [p for p in known if p.rsplit("/", 1)[-1] == name]
                if len(matches) == 1:
                    types.setdefault(matches[0], value)
            meta["doc_types"] = types
            self._write_meta(meta)
            return types

    def _album_parts(self, session_id) -> dict:
        """Карта «путь части относительно base_files -> {first_page, last_page,
        source_file}» из сайдкара нарезки (bundles.ALBUM_PARTS_FILE).

        Сайдкар пишет full_project.split_full_projects (там есть fitz и номера
        листов); здесь только читаем - на голой стдлибе. Нет файла или он битый -
        пустая карта: страницы части исчезнут из интерфейса, но список файлов
        всё равно построится."""
        sidecar = self.paths_of(session_id)["base_files_dir"] / bundles.ALBUM_PARTS_FILE
        if not sidecar.is_file():
            return {}
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _is_generated_part(path: Path) -> bool:
        """Лежит ли файл в папке, которую нарезал сплиттер альбомов.

        Имя метки берём из bundles - он про связки и подпапки и, в отличие от
        full_project, не тянет fitz, которого в web_app быть не должно."""
        parent = path.parent
        return (parent.name != "base_files"
                and (parent / bundles.GENERATED_MARKER).exists())

    def resolve_file(self, session_id, rel_path) -> Path:
        """Путь файла сессии по значению поля "path" из files().

        Проверка обязательна: значение приходит из URL. Пускаем только внутрь
        base_files и full_projects - в папке data сессии рядом лежат ещё и
        извлечённые данные, и копия скриптов, и отдавать их наружу незачем.
        """
        paths = self.paths_of(session_id)
        rel = Path(str(rel_path or ""))
        if rel.is_absolute() or ".." in rel.parts:
            raise SessionError(f"Недопустимый путь файла: {rel_path}", 400)

        target = (paths["data_dir"] / rel).resolve()
        allowed = (paths["base_files_dir"].resolve(),
                   paths["full_projects_dir"].resolve())
        if not any(target == root or root in target.parents for root in allowed):
            raise SessionError(f"Недопустимый путь файла: {rel_path}", 400)
        if not target.is_file():
            raise SessionError(f"Файл не найден: {rel.name}", 404)
        return target

    def upload_target(self, session_id, filename) -> Path:
        """Куда лечь загружаемому файлу. Проверяет сессию и имя, но НЕ пишет.

        Отдельно от записи, потому что тело запроса теперь льётся в файл
        потоком (см. web_app/multipart.py): решение «принимаем ли мы этот
        файл» надо принять ДО того, как получен первый его байт, а не после
        того, как двести мегабайт осели в памяти.
        """
        meta = self._read_meta(session_id)
        if meta["status"] in ACTIVE_STATUSES:
            raise SessionError("Сессия в очереди или выполняется - файлы менять нельзя", 409)
        name = Path(filename or "").name    # отрезаем любой путь со стороны клиента
        if not name:
            raise SessionError("пустое имя файла", 400)
        if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
            raise SessionError("неподдерживаемый формат (нужен PDF для схем и чертежей "
                               "либо .xlsx для спецификации)", 400)
        base = self.paths_of(session_id)["base_files_dir"]
        base.mkdir(parents=True, exist_ok=True)
        return base / name

    def save_upload(self, session_id, filename, data: bytes) -> str:
        """Загрузка файла целиком из памяти. Осталась для вызовов из консоли и
        тестов; интерфейс грузит потоком через upload_target."""
        target = self.upload_target(session_id, filename)
        target.write_bytes(data)
        return target.name

    def delete_file(self, session_id, rel_path) -> None:
        meta = self._read_meta(session_id)
        if meta["status"] in ACTIVE_STATUSES:
            raise SessionError("Сессия в очереди или выполняется - файлы менять нельзя", 409)
        paths = self.paths_of(session_id)
        target = self.resolve_file(session_id, rel_path)
        key = target.relative_to(paths["data_dir"]).as_posix()
        was_album = target.parent == paths["full_projects_dir"]
        target.unlink()

        # Удалили альбом - вместе с ним уходят и нарезанные из него части.
        # Иначе в base_files остались бы связки-шкафы от документа, которого в
        # сессии больше нет: следующий прогон не увидел бы ни одного альбома,
        # а значит и не запустил бы нарезку (только она чистит старые части),
        # и молча сверял бы между собой призраки удалённого проекта.
        if was_album and not self._remaining_albums(session_id):
            self._clear_generated_parts(paths["base_files_dir"])

        with self._lock:
            meta = self._read_meta(session_id)
            if meta.get("doc_types", {}).pop(key, None) is not None:
                self._write_meta(meta)
        self.refresh_file_count(session_id)

    def _remaining_albums(self, session_id) -> bool:
        fp = self.paths_of(session_id)["full_projects_dir"]
        return fp.is_dir() and any(
            p.is_file() and p.suffix.lower() == ".pdf" for p in fp.iterdir())

    @staticmethod
    def _clear_generated_parts(base_files_dir: Path) -> None:
        """То же, что full_project.clear_generated_parts, но без импорта fitz:
        web_app эту зависимость не тянет (и не должен). Метка одна и та же -
        bundles.GENERATED_MARKER, единственное место, куда дотягиваются оба."""
        if not base_files_dir.is_dir():
            return
        for item in base_files_dir.iterdir():
            if item.is_dir() and (item / bundles.GENERATED_MARKER).exists():
                shutil.rmtree(item, ignore_errors=True)

    def set_type(self, session_id, rel_path, doc_type, valid_types) -> None:
        """Пометка типа документа. Хранится в session.json, а не в общем
        data/.doc_types.json: тот был один на весь сервер и ключевался голым
        именем файла, так что две сессии с одинаковым именем файла спорили за
        одну запись. В data/.doc_types.json сессии пометки уезжают только перед
        запуском - их там ждёт ingest.

        Ключ - ПУТЬ относительно data/ (поле "path" в списке файлов), тот же,
        которым файл адресуется на просмотр и удаление. Файл обязан
        существовать: resolve_file и проверяет это, и не пускает путь наружу
        base_files/full_projects.
        """
        if doc_type and doc_type not in valid_types:
            raise SessionError(f"Недопустимый тип: {doc_type}", 400)
        # Голое имя без папки означает файл, лежащий прямо в base_files: так
        # присылает вкладка, открытая до обновления сервера, и так удобнее
        # звать метод из консоли.
        if rel_path and "/" not in str(rel_path) and "\\" not in str(rel_path):
            rel_path = "base_files/" + str(rel_path)
        target = self.resolve_file(session_id, rel_path)
        key = target.relative_to(self.paths_of(session_id)["data_dir"]).as_posix()
        self._doc_types(session_id)         # миграция старых ключей до записи
        with self._lock:
            meta = self._read_meta(session_id)
            if meta["status"] in ACTIVE_STATUSES:
                raise SessionError("Сессия в очереди или выполняется - типы менять нельзя", 409)
            types = dict(meta.get("doc_types") or {})
            if doc_type:
                types[key] = doc_type
            else:
                types.pop(key, None)        # пустой выбор - сброс пометки
            meta["doc_types"] = types
            self._write_meta(meta)

    def set_llm(self, session_id, settings) -> dict:
        """Выбор моделей и числа агентов ДЛЯ ЭТОЙ СЕССИИ.

        Хранится в session.json рядом с пометками типов и по той же причине:
        это свойство прогона, а не установки. Общий config.yaml от выбора в
        интерфейсе не меняется - иначе один пользователь молча перенастраивал бы прогоны всех остальных, а разобраться потом, чем считался
        позавчерашний отчёт, было бы нечем. Перед запуском настройки уезжают в
        config.yaml СЕССИИ (_pipeline_runner.build_session_config), который
        остаётся в её папке как часть архива прогона.

        Пустое значение (null) означает «как в общем config.yaml», а не
        «ничего»: так пользователь возвращает настройку по умолчанию, не зная,
        какая она.

        Существование модели на сервере здесь НЕ проверяется сознательно.
        Сервер бывает временно выключен, а список моделей на нём меняется;
        запретить сохранить выбор из-за того, что LM Studio сейчас не отвечает,
        значило бы связать настройку с состоянием сети. Доступность показывает
        кнопка проверки серверов, а прогон честно упадёт с понятной ошибкой.
        """
        if not isinstance(settings, dict):
            raise SessionError("Ожидался объект настроек", 400)

        clean = {}
        for key in ("agent_1", "agent_2", "vision"):
            if key not in settings:
                continue
            value = settings[key]
            if value in (None, ""):
                clean[key] = None
                continue
            if not isinstance(value, str) or len(value) > 200:
                raise SessionError(f"Недопустимое имя модели для {key}", 400)
            clean[key] = value.strip()

        if "agents_count" in settings:
            value = settings["agents_count"]
            if value in (None, ""):
                clean["agents_count"] = None
            elif value in (1, 2, "1", "2"):
                clean["agents_count"] = int(value)
            else:
                raise SessionError("Агентов может быть 1 или 2", 400)

        if "single_agent" in settings:
            value = settings["single_agent"]
            if value in (None, ""):
                clean["single_agent"] = None
            elif value in ("agent_1", "agent_2"):
                clean["single_agent"] = value
            else:
                raise SessionError("Единственный агент - agent_1 или agent_2", 400)

        with self._lock:
            meta = self._read_meta(session_id)
            if meta["status"] in ACTIVE_STATUSES:
                raise SessionError(
                    "Сессия в очереди или выполняется - настройки менять нельзя", 409)
            llm = dict(meta.get("llm") or {})
            llm.update(clean)
            llm = {k: v for k, v in llm.items() if v is not None}
            meta["llm"] = llm
            self._write_meta(meta)
            return llm

    def has_files(self, session_id) -> bool:
        """Есть ли в сессии хоть что-то для анализа.

        СЧИТАЕТ И full_projects, а не только base_files. Иначе повторный запуск
        сессии с альбомом был невозможен, и выглядело это как "файл пропал":
        альбом опознаётся уже внутри прогона (по числу листов - web_app открыть
        PDF не может) и ПЕРЕЕЗЖАЕТ из base_files в full_projects. Если прогон
        оборвать до того, как нарезка успела разложить части обратно в
        base_files - а обрывают его как раз там, потому что чтение штампов
        трёхсот листов и есть самое долгое место, - base_files оставался пуст.
        Сессия при этом полностью исправна: альбом лежит на диске в соседней
        папке, и следующий прогон нарезал бы его заново. Но enqueue отказывал,
        и единственным выходом было загрузить документ заново.
        """
        paths = self.paths_of(session_id)
        base = paths["base_files_dir"]
        if base.is_dir() and any(
                p.is_file() and p.suffix.lower() in ALLOWED_SUFFIXES
                and not p.name.startswith(("~$", "."))
                for p in base.rglob("*")):
            return True

        fp = paths["full_projects_dir"]
        return fp.is_dir() and any(
            p.is_file() and p.suffix.lower() == ".pdf"
            and not p.name.startswith(("~$", "."))
            for p in fp.iterdir())

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

    def prepare_run(self, session_id) -> tuple:
        """Раскладывает песочницу сессии перед запуском.

        Возвращает (пути сессии, пометки типов для пайплайна). Пометки отдаются
        уже в том виде, в каком их ждёт ingest - ключом относительно base_files,
        - чтобы один и тот же словарь не жил в двух форматах: в session.json он
        ключуется путём относительно data/ (как весь остальной интерфейс), а
        ingest про папку data/ ничего не знает.

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

        doc_types = self._doc_types(session_id)
        data_dir = paths["data_dir"]

        # Файлы, помеченные как альбом, переезжают в full_projects/ - там их
        # ждёт full_project.py. Пайплайн опознаёт альбом и сам (по числу
        # листов), но пометка пользователя главнее: она снимает догадку и
        # работает даже на альбоме короче порога.
        moved = []
        for key in [k for k, t in doc_types.items() if t == FULL_PROJECT_TYPE]:
            src = (data_dir / key)
            if src.is_file() and src.parent != paths["full_projects_dir"]:
                shutil.move(str(src), str(paths["full_projects_dir"] / src.name))
            moved.append(key)

        if moved:
            # Пометку убираем НАСОВСЕМ, а не только из копии: путь файла
            # изменился, и прежний ключ не совпал бы уже ни с чем, а тип
            # переехавшему файлу больше не нужен - о том, что это альбом,
            # говорит сама папка (files() метит всё в full_projects).
            with self._lock:
                meta = self._read_meta(session_id)
                types = dict(meta.get("doc_types") or {})
                for key in moved:
                    types.pop(key, None)
                meta["doc_types"] = types
                self._write_meta(meta)
            doc_types = {k: v for k, v in doc_types.items() if k not in moved}

        # Ключи для пайплайна - относительно base_files: подпапка в base_files
        # это связка, и путь внутри неё ingest видит, а вот про папку data/,
        # где base_files лежит, он ничего не знает.
        prefix = paths["base_files_dir"].relative_to(data_dir).as_posix() + "/"
        pipeline_types = {
            k[len(prefix):]: v for k, v in doc_types.items() if k.startswith(prefix)
        }

        # пометки типов - туда, где их ищет ingest (data/.doc_types.json)
        sidecar = data_dir / ".doc_types.json"
        sidecar.write_text(
            json.dumps(pipeline_types, ensure_ascii=False, indent=2), encoding="utf-8")
        self.refresh_file_count(session_id)
        return paths, pipeline_types

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
