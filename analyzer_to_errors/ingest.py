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
}

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
}

SUPPORTED_SUFFIXES = {".pdf"}


class ExtractionError(Exception):
    """Базовый скрипт-парсер упал на конкретном документе."""


def detect_doc_type(filename: str):
    """Тип документа по пометке в начале имени. None, если пометки нет
    или она неизвестна."""
    m = TYPE_MARKER_RE.match(filename)
    if not m:
        return None
    return TYPE_ALIASES.get(m.group(1).strip().lower())


def document_name(path: Path) -> str:
    """Имя папки с данными: имя файла без расширения и без пометки типа,
    очищенное от символов, недопустимых в имени папки Windows."""
    stem = TYPE_MARKER_RE.sub("", path.stem).strip()
    stem = re.sub(r'[<>:"/\\|?*]', "_", stem)
    return stem or path.stem


def discover_documents(base_files_dir: Path, overrides: dict = None) -> list:
    """Список документов из base_files с определённым типом.

    overrides: {"имя файла.pdf": "scheme"} - явная пометка типа, важнее
    пометки в имени файла (сюда будет приходить выбор пользователя с сайта).
    """
    overrides = overrides or {}
    docs = []

    if not base_files_dir.is_dir():
        raise FileNotFoundError(f"Папка с исходными файлами не найдена: {base_files_dir}")

    for path in sorted(base_files_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            docs.append({"source": path, "doc_type": None, "name": document_name(path),
                         "skip_reason": f"неподдерживаемое расширение {path.suffix!r} (нужен PDF)"})
            continue

        doc_type = overrides.get(path.name) or detect_doc_type(path.name)
        if doc_type is None:
            docs.append({"source": path, "doc_type": None, "name": document_name(path),
                         "skip_reason": "нет пометки типа в имени файла. Ожидается префикс "
                                        "(scheme) или (netlist), например: "
                                        "'(scheme)ИК.3912-АТХ2.115.pdf'"})
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


def extract_document(doc: dict, scripts_dir: Path, data_dir: Path,
                     overwrite: bool = True) -> dict:
    """Прогоняет ОДИН документ через его базовый парсер. Возвращает запись
    для манифеста. Исключение парсера не роняет весь пайплайн - оно
    записывается в статус документа."""
    out_dir = data_dir / doc["name"]

    scripts = DOC_TYPES[doc["doc_type"]]["scripts"]

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


def run_extraction(base_files_dir, scripts_dir, data_dir,
                   overrides: dict = None, overwrite: bool = True) -> dict:
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

    documents, skipped = [], []
    for doc in docs:
        if doc["doc_type"] is None:
            logger.warning("Пропущен %s: %s", doc["source"].name, doc["skip_reason"])
            skipped.append({
                "source_file": doc["source"].name,
                "reason": doc["skip_reason"],
            })
            continue
        documents.append(extract_document(doc, scripts_dir, data_dir, overwrite))

    ok = [d for d in documents if d["status"] in ("ok", "partial")]
    if not ok:
        raise ExtractionError(
            "Ни один документ не удалось извлечь. Проверьте пометки типов в именах "
            f"файлов в {base_files_dir} и ошибки парсеров выше.")

    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "base_files_dir": str(base_files_dir.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "doc_types": {k: v["description"] for k, v in DOC_TYPES.items()},
        "documents": documents,
        "skipped_files": skipped,
        "summary": {
            "total_documents": len(documents),
            "extracted_ok": len(ok),
            "failed": len(documents) - len(ok),
            "skipped": len(skipped),
        },
    }

    manifest_path = data_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Манифест сохранён: %s (документов: %d, ошибок: %d, пропущено: %d)",
                manifest_path, len(ok), len(documents) - len(ok), len(skipped))

    return manifest
