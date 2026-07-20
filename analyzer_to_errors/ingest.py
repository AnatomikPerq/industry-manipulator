"""
Стадия извлечения: PDF из data/base_files -> папки с данными в data/<имя файла>/.

Что здесь происходит:
1) сканируем data/base_files;
2) по пометке в начале имени файла определяем ТИП документа:
       "(scheme)ИК.3912-АТХ2.115.pdf"  -> scheme  (принципиальная схема EPLAN)
       "(netlist)ИК.3912-АТХ3.115.pdf" -> netlist (таблица соединений)
   Файл без пометки или с неизвестной пометкой НЕ обрабатывается -
   он попадает в manifest со статусом "skipped" и причиной. Это и есть
   требование "скрипты обрабатывают только файлы с правильной пометкой":
   ни один парсер не запускается на документе не своего типа.
3) для каждого типа вызываем ЕГО базовый скрипт-парсер из
   data/base_analysis_scripts (см. DOC_TYPES / config.yaml);
4) результат каждого документа кладём в отдельную папку data/<имя файла>/;
5) пишем data/manifest.json - оглавление для агента-нейросети: какой документ
   какого типа, где лежат его данные, какие файлы получились и сколько в них
   чего. Агент читает манифест первым и дальше идёт по нему, не сканируя
   папку вслепую.

Позже, когда появится сайт, тип документа будет приходить из формы загрузки -
тогда достаточно передать overrides={"имя файла.pdf": "scheme"} в discover_documents(),
и пометка в имени станет необязательной.
"""

import importlib.util
import json
import logging
import re
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

import bundles

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent

# Пометка типа в начале имени файла: "(scheme)...", "(netlist)..."
TYPE_MARKER_RE = re.compile(r"^\s*\(\s*([^)]+?)\s*\)")

# Синонимы пометок -> канонический тип документа.
# Пользователь может писать пометку по-русски или по-английски.
TYPE_ALIASES = {
    "scheme": "scheme",
    "schema": "scheme",
    "schematic": "scheme",
    "схема": "scheme",
    "netlist": "netlist",
    "нетлист": "netlist",
    "таблица": "netlist",
    "таблица соединений": "netlist",
    "assembly": "assembly",
    "сборочный": "assembly",
    "сборочный чертеж": "assembly",
    "сборочный чертёж": "assembly",
    "спецификация": "spec",
    "spec": "spec",
    "specification": "spec",
}

# Марка вида документа в имени файла -> тип документа.
# Реальные файлы бюро уже названы с маркой ("...ША1 Э3_10.04.26.pdf",
# "СБ_ИК.3912-АТХ2.015_01.06.2026.pdf"), поэтому требовать от пользователя
# дописывать вручную пометку "(scheme)" незачем - марка вида и есть пометка
# типа, просто отраслевая.
#
# Марка встречается и В КОНЦЕ имени ("... ША1 СБ_08.05.26"), и В НАЧАЛЕ
# ("СБ_ИК.3912-..."), поэтому ищется как отдельное "слово" в любом месте.
# Кроме ГОСТ-марок (Э3, СБ, СО) поддержаны сокращения, которыми бюро называет
# файлы на практике: "СХ" - схема, "NL"/"НЛ" - нетлист внешних подключений.
KIND_MARK_TO_TYPE = {
    "Э3": "scheme",
    "Э4": "scheme",
    "Э5": "scheme",
    "Э6": "scheme",
    "СХ": "scheme",
    "СБ": "assembly",
    "СО": "spec",
    "NL": "netlist",
    "НЛ": "netlist",
}

# Марка как отдельное "слово": перед ней начало имени или пробел/подчёркивание,
# после - пробел, "_", "." или конец. Без этих границ "СО" находилось бы внутри
# "СОЕДИНЕНИЙ", "СХ" - внутри "схема", а "ТМ" - внутри "ШУ-ТМ-14082" (это часть
# обозначения шкафа, а не марка вида).
KIND_MARK_RE = re.compile(
    r"(?:^|(?<=[\s_]))(" + "|".join(re.escape(m) for m in KIND_MARK_TO_TYPE)
    + r")(?=[\s_.]|$)", re.IGNORECASE)


def detect_kind_mark(stem: str):
    """Марка вида документа из имени файла ("Э3", "СБ", "СО", "СХ", "NL").

    Если марок несколько, берём САМУЮ ПРАВУЮ: в "СБ_ИК.3912-АТХ2.015" марка одна,
    а вот в "026_812_..._АТХ2_015_СО_29_04_26" левее могло попасться что-то
    похожее из обозначения проекта.
    """
    best = None
    for m in KIND_MARK_RE.finditer(stem):
        if best is None or m.start() > best.start():
            best = m
    return best.group(1).upper() if best else None

# Какие базовые скрипты-парсеры обслуживают какой тип документа. На один документ
# может работать НЕСКОЛЬКО скриптов - каждый достаёт свой срез данных и кладёт свои
# файлы в общую папку документа.
# Каждый скрипт обязан иметь функцию extract_to_dir(pdf_path, out_dir)
# -> (список созданных файлов, dict со статистикой).
DOC_TYPES = {
    "scheme": {
        "scripts": [
            "schematic_diagram_to_data.py",   # текст, линии, граф, каналы ввода/вывода
            "schematic_connectivity.py",       # настоящие цепи (с учётом T-стыков) + индекс клемм
        ],
        "description": "Принципиальная схема EPLAN (векторный PDF): текст, линии, "
                       "цепи проводов и клеммы, netlist по каналам ввода/вывода, "
                       "межлистовые ссылки, геометрические кандидаты на ошибку.",
    },
    "netlist": {
        "scripts": ["netlist_to_json.py"],
        "description": "Таблица соединений (нетлист) по ГОСТ: построчный перечень "
                       "физических точек подключения проводов.",
    },
    "assembly": {
        "scripts": ["assembly_drawing_to_data.py"],
        "description": "Сборочный чертёж шкафа (СБ): вид шкафа с размещением "
                       "изделий. Извлекаются ПОДПИСИ элементов (позиционное "
                       "обозначение + артикул), геометрия шкафа сознательно нет - "
                       "её сотни тысяч примитивов и для поиска ошибок она бесполезна.",
    },
    "spec": {
        # Парсер выбирается по расширению (см. _scripts_for): книга Excel и
        # лист альбома - один и тот же документ по ГОСТ 21.110, но читаются
        # они принципиально разным кодом (openpyxl против разбора линовки
        # таблицы в PDF). Запускать оба на одном файле бессмысленно: тот, чей
        # формат не совпал, просто упадёт.
        "scripts": ["specification_to_json.py"],
        "scripts_by_suffix": {
            ".pdf": ["specification_pdf_to_json.py"],
        },
        "description": "Спецификация оборудования (СО) по ГОСТ 21.110: построчный "
                       "перечень заказываемых изделий - позиция, наименование, код "
                       "оборудования, количество. Приходит книгой Excel (отдельный "
                       "комплект на шкаф) либо листом PDF (в составе полного проекта).",
    },
}


def _scripts_for(doc_type: str, source: Path) -> list:
    """Какими парсерами читать документ этого типа в этом формате."""
    entry = DOC_TYPES[doc_type]
    by_suffix = entry.get("scripts_by_suffix") or {}
    return by_suffix.get(source.suffix.lower(), entry["scripts"])

# .xlsx нужен спецификации: это единственный документ связки, который приходит
# не чертежом, а книгой Excel.
SUPPORTED_SUFFIXES = {".pdf", ".xlsx", ".xlsm"}

# Какие расширения допустимы для какого типа документа. Без этой проверки
# спецификацию (.xlsx) можно было бы пометить как "scheme", и PDF-парсер упал
# бы на ней с невнятной ошибкой fitz вместо понятного сообщения.
TYPE_SUFFIXES = {
    "scheme": {".pdf"},
    "netlist": {".pdf"},
    "assembly": {".pdf"},
    # .pdf у спецификации появился вместе с полными проектами: в альбоме она
    # такой же лист, как схема, и книги Excel к ней не прилагается.
    "spec": {".xlsx", ".xlsm", ".pdf"},
}


class ExtractionError(Exception):
    """Базовый скрипт-парсер упал на конкретном документе."""


def detect_doc_type(filename: str):
    """Тип документа по имени файла. None, если определить нельзя.

    Два способа, в порядке приоритета:
    1) явная пометка в начале имени: "(scheme)ИК.3912-АТХ2.115.pdf";
    2) марка вида документа в имени - в конце ("...ША1 Э3_10.04.26.pdf" ->
       scheme) или в начале ("СБ_ИК.3912-АТХ2.015_01.06.2026.pdf" -> assembly).
       Файлы бюро уже названы так, и это делает ручную пометку ненужной
       в обычном случае.
    """
    m = TYPE_MARKER_RE.match(filename)
    if m:
        return TYPE_ALIASES.get(m.group(1).strip().lower())

    mark = detect_kind_mark(Path(filename).stem)
    return KIND_MARK_TO_TYPE.get(mark) if mark else None


def document_name(path: Path) -> str:
    """Имя папки с данными: имя файла без расширения и без пометки типа,
    очищенное от символов, недопустимых в имени папки Windows."""
    stem = TYPE_MARKER_RE.sub("", path.stem).strip()
    stem = re.sub(r'[<>:"/\\|?*]', "_", stem)
    return stem or path.stem


def discover_documents(base_files_dir: Path, overrides: dict = None) -> list:
    """Список документов из base_files с определённым типом.

    Обходит base_files РЕКУРСИВНО: пользователь раскладывает комплекты по
    подпапкам ("base_files/связка 1/..."), и подпапка - один из способов
    задать связку (см. bundles.py). Плоский список файлов тоже работает.

    overrides: {"имя файла.pdf": "scheme"} - явная пометка типа, важнее
    имени файла (сюда приходит выбор пользователя с сайта).
    """
    overrides = overrides or {}
    docs = []

    if not base_files_dir.is_dir():
        raise FileNotFoundError(f"Папка с исходными файлами не найдена: {base_files_dir}")

    for path in sorted(base_files_dir.rglob("*")):
        if not path.is_file():
            continue
        # временные файлы Excel: пользователь открыл спецификацию, и рядом
        # появился "~$026.809...xlsx" - это не документ
        if path.name.startswith("~$") or path.name.startswith("."):
            continue

        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            docs.append({"source": path, "doc_type": None, "name": document_name(path),
                         "skip_reason": f"неподдерживаемое расширение {path.suffix!r} "
                                        f"(поддерживаются: "
                                        f"{', '.join(sorted(SUPPORTED_SUFFIXES))})"})
            continue

        doc_type = overrides.get(path.name) or detect_doc_type(path.name)
        if doc_type is None:
            docs.append({"source": path, "doc_type": None, "name": document_name(path),
                         "skip_reason": "не удалось определить тип документа. Ожидается "
                                        "марка вида в имени файла - в начале или в конце: "
                                        "Э3/СХ (принципиальная схема), СБ (сборочный "
                                        "чертёж), СО (спецификация), NL/НЛ (нетлист). "
                                        "Например 'СБ_ИК.3912-АТХ2.015_01.06.2026.pdf' "
                                        "или '026.809.01.01-ИПК ША1 Э3_10.04.26.pdf'. "
                                        "Либо укажите тип в списке файлов на сайте, либо "
                                        "поставьте пометку в начале имени: "
                                        "'(scheme)ИК.3912-АТХ2.115.pdf'"})
            continue

        allowed = TYPE_SUFFIXES.get(doc_type, SUPPORTED_SUFFIXES)
        if path.suffix.lower() not in allowed:
            docs.append({"source": path, "doc_type": None, "name": document_name(path),
                         "skip_reason": f"тип документа {doc_type!r} ожидает "
                                        f"{', '.join(sorted(allowed))}, а файл имеет "
                                        f"расширение {path.suffix!r}"})
            continue

        docs.append({"source": path, "doc_type": doc_type,
                     "name": document_name(path), "skip_reason": None})

    return docs


def _load_parser(scripts_dir: Path, script_name: str):
    """Импортирует базовый скрипт-парсер по пути к файлу.

    Через importlib, а не обычным import: скрипты лежат в data/, это не
    Python-пакет, и в их именах нет гарантии валидного идентификатора.
    """
    script_path = scripts_dir / script_name
    if not script_path.is_file():
        raise ExtractionError(f"Базовый скрипт-парсер не найден: {script_path}")

    module_name = f"_base_parser_{script_path.stem}"
    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, script_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "extract_to_dir"):
        raise ExtractionError(
            f"{script_name} не имеет функции extract_to_dir(pdf_path, out_dir) - "
            "пайплайн не может его вызвать")
    return module


def _extraction_warnings(script: str, stats: dict, cumulative_stats: dict) -> list:
    """Грубые сигналы "экстракция почти ничего не дала" по статистике скрипта.

    Не измерено на корпусе файлов (в отличие от rule-чекеров в schematic_rules.py/
    netlist_rules.py) - это просто отлов заведомо пустых/почти пустых результатов,
    которые иначе тихо проезжают в логе строкой "готово: {...}" и легко
    остаются незамеченными. Порог нарочно грубый (ноль или близко к нулю): часть
    метрик легитимно бывает нулевой для конкретного документа (напр. щит без ПЛК
    честно даёт io_channels=0 - см. профили E/F), поэтому такие метрики сюда не
    включены вовсе, чтобы не плодить ложных предупреждений.
    """
    warnings = []
    total_pages = stats.get("total_pages") or cumulative_stats.get("total_pages")

    if script == "schematic_diagram_to_data.py":
        nodes = stats.get("graph_nodes", 0)
        if nodes == 0:
            warnings.append("графических узлов (текст/линии) не найдено вообще - "
                            "экстракция, вероятно, не сработала")
        elif total_pages and nodes / total_pages < 20:
            warnings.append(f"мало графических данных: {nodes / total_pages:.1f} "
                            "узлов/лист (на нормально извлечённой схеме - сотни) - "
                            "проверьте, векторный ли это PDF и опознан ли профиль бюро")

    elif script == "schematic_connectivity.py":
        nets = stats.get("nets", 0)
        terminals = stats.get("terminals_on_scheme", 0)
        if nets == 0:
            warnings.append("не построено ни одной цепи проводов - линии в PDF "
                            "не распознаны как провода")
        else:
            if terminals == 0:
                warnings.append("ни одна клемма/вывод не привязаны к цепям - сверка "
                                "с таблицей подключений по клеммам будет невозможна")
            # Порог "мало" - не 0, а меньше 2% от числа цепей (не ниже 3): чистый
            # ноль ловится веткой выше, а вот "1 клемма на 52 цепи" (как оказалось
            # на реальном файле бюро Schneider) выше неё не проходит, хотя это
            # тоже фактически провал привязки клемм, просто не абсолютный.
            elif nets >= 20 and terminals < max(3, round(nets * 0.02)):
                warnings.append(f"клемм привязано подозрительно мало ({terminals} "
                                f"на {nets} цепей) - большинство выводов на схеме, "
                                "похоже, не распознаются текущим профилем бюро")
            if nets > 5 and stats.get("wire_markings_on_scheme", 0) == 0:
                warnings.append("маркировки проводов на схеме не найдены - сверка "
                                "номеров проводов с таблицей подключений будет невозможна")

    elif script == "netlist_to_json.py":
        if stats.get("total_connections", 0) == 0:
            warnings.append("в таблице подключений не найдено ни одной строки "
                            "соединения - проверьте формат/качество PDF")

    elif script in ("specification_to_json.py", "specification_pdf_to_json.py"):
        if stats.get("spec_rows", 0) == 0:
            warnings.append("в спецификации не найдено ни одной позиции - проверьте, "
                            "та ли это книга и не пуст ли первый лист")
        elif stats.get("spec_unique_designators", 0) == 0:
            warnings.append("в спецификации не распознано ни одного позиционного "
                            "обозначения (колонка «Позиция») - сверить её со схемой "
                            "и чертежом будет не по чему")
        if not stats.get("spec_designation"):
            warnings.append("в спецификации не найдено обозначение документа - "
                            "сверка обозначений по связке будет неполной")

    elif script == "assembly_drawing_to_data.py":
        if stats.get("assembly_elements", 0) == 0:
            warnings.append("на сборочном чертеже не найдено ни одной подписи элемента - "
                            "вероятно, это скан (растр), а не векторный PDF")
        elif stats.get("assembly_paired_from_block", 0) == 0:
            warnings.append("на сборочном чертеже не удалось надёжно связать ни одного "
                            "обозначения с артикулом - сверка АРТИКУЛОВ со спецификацией "
                            "по этому чертежу выполняться не будет (останется только "
                            "проверка наличия элементов)")
        if not stats.get("assembly_designation"):
            warnings.append("на сборочном чертеже не найдено обозначение документа - "
                            "сверка обозначений по связке будет неполной")

    return warnings


def extract_document(doc: dict, scripts_dir: Path, data_dir: Path,
                     overwrite: bool = True) -> dict:
    """Прогоняет ОДИН документ через его базовый парсер. Возвращает запись
    для манифеста. Исключение парсера не роняет весь пайплайн - оно
    записывается в статус документа."""
    out_dir = data_dir / doc["name"]

    scripts = _scripts_for(doc["doc_type"], doc["source"])

    record = {
        "name": doc["name"],
        "doc_type": doc["doc_type"],
        "source_file": str(doc["source"].relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "data_dir": str(out_dir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "parsers": list(scripts),
        "doc_type_description": DOC_TYPES[doc["doc_type"]]["description"],
        "status": "ok",
        "files": [],
        "stats": {},
        "errors": [],
    }

    if out_dir.exists() and overwrite:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Извлечение [%s] %s -> %s",
                doc["doc_type"], doc["source"].name, out_dir)

    for script in scripts:
        try:
            parser = _load_parser(scripts_dir, script)
            files, stats = parser.extract_to_dir(str(doc["source"]), str(out_dir))
            record["files"].extend(files)
            record["stats"].update(stats)
            logger.info("  [%s] готово: %s", script, stats)
            for msg in _extraction_warnings(script, stats, record["stats"]):
                logger.warning("  [%s] %s: %s", script, doc["name"], msg)
        except Exception as e:  # noqa: BLE001 - падение парсера не должно ронять весь прогон
            record["errors"].append(f"{script}: {type(e).__name__}: {e}")
            logger.error("  [%s] ОШИБКА: %s", script, e)
            logger.debug(traceback.format_exc())

    # Документ считается извлечённым, если сработал ХОТЯ БЫ ОДИН парсер: данные
    # одного скрипта уже пригодны для анализа, и терять их из-за падения второго
    # (например, нового, экспериментального) незачем.
    if not record["files"]:
        record["status"] = "failed"
    elif record["errors"]:
        record["status"] = "partial"

    return record


def run_extraction(base_files_dir, scripts_dir, data_dir, overrides: dict = None,
                   overwrite: bool = True, bundle_overrides: dict = None) -> dict:
    """Полная стадия извлечения: все файлы из base_files -> data/<имя>/ + manifest.json.

    Возвращает манифест (dict). Бросает ExtractionError, только если не
    удалось извлечь НИ ОДИН документ - анализировать в этом случае нечего.
    """
    base_files_dir = Path(base_files_dir)
    scripts_dir = Path(scripts_dir)
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    docs = discover_documents(base_files_dir, overrides)
    if not docs:
        raise ExtractionError(f"В {base_files_dir} нет файлов для анализа")

    # Сообщения о ходе разбора интерфейсу. Модуль лежит в папке скриптов (она
    # копируется в каждую сессию), поэтому грузим по пути. Не нашёлся - работаем
    # молча: прогресс-бар не повод ронять извлечение.
    try:
        import importlib.util as _ilu
        _gspec = _ilu.spec_from_file_location("_progress", scripts_dir / "progress.py")
        _progress = _ilu.module_from_spec(_gspec)
        _gspec.loader.exec_module(_progress)
    except Exception:  # noqa: BLE001
        _progress = None

    to_extract = [d for d in docs if d["doc_type"] is not None]
    documents, skipped = [], []
    done = 0
    for doc in docs:
        if doc["doc_type"] is None:
            logger.warning("Пропущен %s: %s", doc["source"].name, doc["skip_reason"])
            skipped.append({
                "source_file": doc["source"].name,
                "reason": doc["skip_reason"],
            })
            continue
        done += 1
        if _progress:
            _progress.document(done, len(to_extract), doc["name"], doc["doc_type"])
        documents.append(extract_document(doc, scripts_dir, data_dir, overwrite))
    if _progress:
        _progress.done()

    ok = [d for d in documents if d["status"] in ("ok", "partial")]
    if not ok:
        raise ExtractionError(
            "Ни один документ не удалось извлечь. Проверьте пометки типов в именах "
            f"файлов в {base_files_dir} и ошибки парсеров выше.")

    # Разбивка на связки: какие документы описывают один и тот же шкаф.
    # Делается ПОСЛЕ извлечения, по именам файлов/подпапкам (см. bundles.py).
    bundles.assign_bundles(documents, bundle_overrides)
    bundle_info = bundles.bundle_summary(documents)
    for b in bundle_info:
        if b["cross_checkable"]:
            logger.info("Связка %r: %d документов (%s) - сверка между документами возможна",
                        b["bundle"], len(b["documents"]), ", ".join(sorted(b["doc_types"])))
        else:
            logger.info("Связка %r: %d документов (%s) - сверять не с чем, нужен "
                        "минимум ещё один документ другого вида",
                        b["bundle"], len(b["documents"]),
                        ", ".join(sorted(b["doc_types"])) or "тип не определён")

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_files_dir": str(base_files_dir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "doc_types": {k: v["description"] for k, v in DOC_TYPES.items()},
        "bundles_explained": (
            "СВЯЗКА - документы ОДНОГО проекта (шкафа). За один прогон анализатора "
            "загружаются документы одного проекта, поэтому по умолчанию ВСЕ документы - "
            "одна связка; несколько связок бывает, только если пользователь сам разложил "
            "файлы по подпапкам. Состав связки НЕ ФИКСИРОВАН: это может быть схема + "
            "сборочный чертёж + спецификация + нетлист, а может быть только чертёж со "
            "спецификацией. Отсутствие какого-то вида документа - НЕ ошибка, не сообщай "
            "о нём. Ключ сверки элементов внутри связки - позиционное обозначение "
            "('1QF1', 'DO1', 'G1')."
        ),
        "bundles": bundle_info,
        "documents": documents,
        "skipped_files": skipped,
        "summary": {
            "total_documents": len(documents),
            "extracted_ok": len(ok),
            "failed": len(documents) - len(ok),
            "skipped": len(skipped),
            "bundles": len(bundle_info),
            # по скольким связкам вообще возможна сверка документов между собой
            # (нужно минимум два вида документов); "неполных" связок не бывает -
            # состав комплекта определяет пользователь, см. bundles.py
            "cross_checkable_bundles": sum(1 for b in bundle_info if b["cross_checkable"]),
        },
    }

    manifest_path = data_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Манифест сохранён: %s (документов: %d, ошибок: %d, пропущено: %d)",
                manifest_path, len(ok), len(documents) - len(ok), len(skipped))

    return manifest
