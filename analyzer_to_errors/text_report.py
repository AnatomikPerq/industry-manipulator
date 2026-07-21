"""
Текстовый отчёт для консоли: находки и итог извлечения.

Выделено из main.py: это ПРЕДСТАВЛЕНИЕ, а не пайплайн. Им пользуется только
CLI - у веб-интерфейса своя таблица (static/app.js) и свой PDF
(report_pdf.py), собираемые из того же merged_report.json.
"""

from collections import Counter

from schema import SEVERITY_ENUM


def _format_ref(ref: dict) -> str:
    """Одно место находки в человекочитаемом виде - примерно те же поля,
    что станут колонками таблицы на сайте."""
    where = []
    if ref.get("sheet") is not None:
        where.append(f"лист {ref['sheet']}")
    if ref.get("row") is not None:
        where.append(f"строка {ref['row']}")

    what = []
    if ref.get("cabinet"):
        what.append(f"шкаф {ref['cabinet']}")
    if ref.get("terminal_block") or ref.get("pin"):
        what.append(f"клемма {ref.get('terminal_block') or '?'}/{ref.get('pin') or '?'}")
    if ref.get("marking"):
        what.append(f"маркировка {ref['marking']}")
    if ref.get("kks"):
        what.append(f"KKS {ref['kks']}")
    if ref.get("conductor"):
        what.append(f"проводник {ref['conductor']}")

    head = f"[{ref.get('doc_type')}] {ref.get('document')}"
    if where:
        head += " (" + ", ".join(where) + ")"

    out = [f"      {head}"]
    if what:
        out.append(f"        {'; '.join(what)}")
    if ref.get("found"):
        out.append(f"        найдено: {ref['found']}")
    return "\n".join(out)


def format_text_report(merged: dict) -> str:
    errors = merged.get("errors", [])
    lines = []
    summary = merged.get("summary")
    if summary:
        lines.append(f"Резюме: {summary}")
        lines.append("")

    by_kind = Counter(e.get("kind") for e in errors)
    by_severity = Counter(e.get("severity") for e in errors)
    lines.append(f"Найдено замечаний: {len(errors)}")
    if errors:
        lines.append("  по видам: " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
        lines.append("  по важности: " + ", ".join(
            f"{s}={by_severity[s]}" for s in SEVERITY_ENUM if by_severity[s]))
    lines.append("=" * 70)

    for i, err in enumerate(errors, 1):
        lines.append(f"[{i}] {err.get('kind')} | {err.get('severity')} | {err.get('type')}")
        lines.append(f"    ({err.get('scope')})")
        for ref in err.get("refs", []):
            lines.append(_format_ref(ref))
        lines.append(f"    что найдено: {err.get('finding')}")
        lines.append(f"    что делать:  {err.get('action')}")
        if err.get("evidence"):
            lines.append(f"    подтверждение: {err.get('evidence')}")
        lines.append("-" * 70)
    return "\n".join(lines)


def format_extraction_report(manifest: dict) -> str:
    lines = ["Извлечение данных из PDF:", "=" * 60]
    for doc in manifest["documents"]:
        mark = {"ok": "OK  ", "partial": "ЧАСТЬ", "failed": "FAIL"}[doc["status"]]
        lines.append(f"[{mark}] ({doc['doc_type']}) {doc['name']}")
        lines.append(f"    парсеры: {', '.join(doc['parsers'])}")
        lines.append(f"    данные: {doc['data_dir']}/")
        if doc["files"]:
            lines.append(f"    файлы: {', '.join(doc['files'])}")
            for k, v in doc["stats"].items():
                lines.append(f"      {k}: {v}")
        for err in doc["errors"]:
            lines.append(f"    ОШИБКА: {err}")
        lines.append("-" * 60)
    for sk in manifest["skipped_files"]:
        lines.append(f"[SKIP] {sk['source_file']}")
        lines.append(f"    причина: {sk['reason']}")
        lines.append("-" * 60)
    s = manifest["summary"]
    lines.append(f"Документов: {s['total_documents']}, извлечено: {s['extracted_ok']}, "
                 f"ошибок: {s['failed']}, пропущено: {s['skipped']}")
    return "\n".join(lines)
