"""
Общая форма находки и ссылки на место находки.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Пять чекеров (netlist_rules, schematic_rules,
spec_rules, assembly_rules, bundle_rules) складывали один и тот же словарь из
шестнадцати полей каждый у себя, пятью копиями, плюс пятая копия
SEVERITY_ORDER. Копии уже разъехались: в ref'ах нетлиста не было полей
designator/article/name/quantity, у схемы - article/name/quantity. Схема это
терпит (все доменные поля необязательны), а вот дальше по конвейеру такое
расхождение не падает НИГДЕ и всплывает пустой колонкой в таблице у инженера.

Форма описана в schema.py и здесь не дублируется - REF_FIELDS перечисляет её
поля ровно затем, чтобы новый ref нельзя было собрать наполовину.

Каждый чекер по-прежнему держит СВОЮ обёртку `_ref(...)` с удобной ему
сигнатурой (у нетлиста это запись таблицы, у чертежа - лист и обозначение):
общей делается форма, а не способ её заполнять.
"""

# Поля ref'а в порядке из schema.REF_SCHEMA. Отсутствующие в конкретном
# документе остаются None - это и означает «здесь такого поля нет».
REF_FIELDS = (
    "sheet", "row", "cabinet", "terminal_block", "pin", "terminal_type",
    "marking", "kks", "conductor", "designator", "article", "name",
    "quantity", "found",
)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def ref(document, doc_type, source_file, **fields) -> dict:
    """Одно место в одном документе, в доменных полях.

    Всё, что не передано, кладётся как None ЯВНО: ref с отсутствующим ключом и
    ref со значением None - разные словари для всякого, кто читает их через
    [] вместо .get(), и разные строки в JSON отчёта.
    """
    unknown = set(fields) - set(REF_FIELDS)
    if unknown:
        # Опечатка в имени поля иначе просто потерялась бы: схема запрещает
        # лишние ключи, но проверяются по ней только ответы моделей.
        raise ValueError(f"Неизвестные поля ref: {sorted(unknown)}")

    out = {"document": document, "doc_type": doc_type, "source_file": source_file}
    for key in REF_FIELDS:
        out[key] = fields.get(key)
    return out


def finding(kind, severity, type_ru, refs, finding, action, evidence=None,
            scope="single_document") -> dict:
    """Одна находка в формате schema.REPORT_SCHEMA.

    scope по умолчанию single_document: так устроены все четыре
    однодокументных чекера. Сверка связки передаёт cross_document явно.
    """
    return {
        "kind": kind,
        "scope": scope,
        "severity": severity,
        "type": type_ru,
        "refs": refs,
        "finding": finding,
        "action": action,
        "evidence": evidence,
    }


def sort_by_severity(findings: list) -> list:
    """Находки по убыванию важности. Сортировка устойчивая, поэтому внутри
    одной важности сохраняется порядок правил - он осмысленный (правила идут
    от самых доказательных к самым осторожным)."""
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))
    return findings
