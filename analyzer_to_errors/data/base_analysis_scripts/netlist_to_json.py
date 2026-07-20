#!/usr/bin/env python3
"""
Combined script: Extracts wiring/netlist table from a GOST-style PDF 
and converts it directly into a structured JSON file for LLM analysis.

Usage:
python3 pdf_to_json.py input.pdf output.json [--meta meta.json] [--revisions revisions.json]
"""

import os
import sys
import json
import argparse
from collections import Counter
from datetime import date
import pdfplumber

# Сообщения "читаю лист N" интерфейсу. Папка скриптов копируется в каждую сессию
# и в sys.path не лежит - грузим по пути, как это делают соседние парсеры.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import progress as _progress  # noqa: E402

# =========================================================================
# БЛОК 1: ЛОГИКА ИЗВЛЕЧЕНИЯ ИЗ PDF (из extract_netlist.py)
# =========================================================================

COLUMNS = [
    "Кабель,жгут",
    "Маркировка цепи",
    "KKS",
    "Проводник",
    "Шкаф",
    "Клеммник/модуль",
    "Штифт",
    "Тип клеммы/модуля",
    "Адрес связи",
    "Примечание",
]

# x0 boundaries of the 10 data columns (constant across all sheets in this template)
COL_BOUNDS = [56.7, 209.8, 311.8, 391.2, 470.6, 583.9, 720.0, 839.1, 969.4, 1054.5, 1176.4]

def fix_text(s):
    if not s:
        return ""
    s = s.strip()
    try:
        return s.encode("latin1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s

def data_row_bands(page):
    """Return list of (top, bottom) y-bands for the real data rows of the table,
    excluding the header row and the title-block/signature-stamp area."""
    hlines = sorted(set(round(l["top"], 1) for l in page.lines
                        if abs(l["x0"] - l["x1"]) > 500))
    if len(hlines) < 3:
        return []
    bands = []
    for i in range(1, len(hlines) - 1):
        gap = hlines[i + 1] - hlines[i]
        if gap >= 20:  # jump into the title block area -> stop
            break
        bands.append((hlines[i], hlines[i + 1]))
    return bands

def extract_row(page, words, top, bottom):
    row = []
    for c0, c1 in zip(COL_BOUNDS[:-1], COL_BOUNDS[1:]):
        cell_words = [
            w["text"] for w in words
            if top - 0.5 <= w["top"] < bottom - 0.5
            and c0 - 1 <= w["x0"] < c1 - 1
        ]
        row.append(fix_text(" ".join(cell_words)))
    return row

def extract_pdf_to_dicts(pdf_path):
    """Извлекает строки из PDF и возвращает список словарей (эмуляция CSV)."""
    all_rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            _progress.page(page_num, len(pdf.pages), stage="чтение таблицы подключений")
            bands = data_row_bands(page)
            if not bands:
                continue
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            for top, bottom in bands:
                row_data = extract_row(page, words, top, bottom)
                if any(cell.strip() for cell in row_data):
                    # Формируем словарь, как если бы это была строка из CSV
                    row_dict = dict(zip(COLUMNS, row_data))
                    row_dict["Лист"] = str(page_num)  # Добавляем номер страницы
                    all_rows.append(row_dict)
    return all_rows


# =========================================================================
# БЛОК 2: ЛОГИКА КОНВЕРТАЦИИ В JSON (из csv_to_json.py)
# =========================================================================

COLUMN_MAP = {
    "Кабель,жгут": "cable_harness",
    "Кабель, жгут": "cable_harness",
    "Маркировка цепи": "circuit_marking",
    "KKS": "kks",
    "Проводник": "conductor",
    "Шкаф": "cabinet",
    "Клеммник/модуль": "terminal_block",
    "Штифт": "pin",
    "Тип клеммы/модуля": "terminal_type_or_ref",
    "Адрес связи": "connection_address",
    "Примечание": "note",
}

PAGE_COLUMN_NAMES = {"Лист", "page", "Page", "лист"}

COLUMN_LEGEND_TEMPLATE = {
    "cable_harness": {"ru": "Кабель, жгут", "en": "Cable / harness designation"},
    "circuit_marking": {"ru": "Маркировка цепи", "en": "Wire/circuit number (running wire mark)"},
    "kks": {"ru": "KKS", "en": "KKS equipment/device tag (per KKS identification system)"},
    "conductor": {"ru": "Проводник", "en": "Conductor / signal role on this KKS device "
                                           "(e.g. L, N, PE, L+, M, FB_OPEN, FB_CLOSE, FB_LOC, FB_RUN, FB_TRBL, "
                                           "C_OPEN, C_CLOSE, C_START)"},
    "cabinet": {"ru": "Шкаф", "en": "Cabinet KKS designation the terminal physically belongs to"},
    "terminal_block": {"ru": "Клеммник/модуль", "en": "Terminal block or I/O module designation "
                                                      "(e.g. XT01, XPA, XPB, XA001-XA019, RA01-RA06, XB001-XB010, RB01-RB06)"},
    "pin": {"ru": "Штифт", "en": "Terminal pin/point number within the terminal block"},
    "terminal_type_or_ref": {"ru": "Тип клеммы/модуля", "en": "Terminal/module type designation "
                                                              "OR vendor article/reference number"},
    "connection_address": {"ru": "Адрес связи", "en": "Communication/logical link address"},
    "note": {"ru": "Примечание", "en": "Free-text note"},
}

FIELD_ORDER = ["cable_harness", "circuit_marking", "kks", "conductor", "cabinet",
               "terminal_block", "pin", "terminal_type_or_ref", "connection_address", "note"]

def clean(v):
    if v is None:
        return None
    v = v.strip()
    return v if v else None

def build_connections(rows, page_col):
    connections = []
    for i, row in enumerate(rows, start=1):
        rec = {"id": i}
        if page_col:
            raw_page = clean(row.get(page_col))
            rec["page"] = int(raw_page) if raw_page and raw_page.isdigit() else raw_page
        
        for ru_col, key in COLUMN_MAP.items():
            if ru_col in row:
                rec[key] = clean(row[ru_col])
        
        for key in FIELD_ORDER:
            rec.setdefault(key, None)
        
        cabinet, tb, pin = rec.get("cabinet"), rec.get("terminal_block"), rec.get("pin")
        rec["terminal_address"] = f"{cabinet}.{tb}.{pin}" if cabinet and tb and pin else None
        connections.append(rec)
    return connections

def build_statistics(connections, page_col):
    total_rows = len(connections)
    pages = [c["page"] for c in connections if page_col and isinstance(c.get("page"), int)]
    total_pages = max(pages) if pages else None
    
    column_population_counts = {
        key: sum(1 for c in connections if c.get(key) not in (None, ""))
        for key in FIELD_ORDER
    }
    
    distinct_terminal_blocks = sorted({c["terminal_block"] for c in connections if c.get("terminal_block")})
    distinct_conductor_roles = sorted({c["conductor"] for c in connections if c.get("conductor")})
    
    reserve_rows_count = sum(
        1 for c in connections
        if any(v and "резерв" in v.lower() for v in
               (c.get("kks"), c.get("circuit_marking")) if v)
    )
    
    addr_counts = Counter(c["terminal_address"] for c in connections if c.get("terminal_address"))
    duplicate_terminal_addresses = {addr: n for addr, n in addr_counts.items() if n > 1}
    
    return {
        "total_rows": total_rows,
        "total_pages": total_pages,
        "column_population_counts": column_population_counts,
        "distinct_terminal_blocks": distinct_terminal_blocks,
        "distinct_conductor_roles": distinct_conductor_roles,
        "reserve_rows_count": reserve_rows_count,
        "duplicate_terminal_addresses": duplicate_terminal_addresses,
    }

def build_column_legend(stats):
    legend = {}
    for key in FIELD_ORDER:
        entry = dict(COLUMN_LEGEND_TEMPLATE[key])
        entry["populated"] = stats["column_population_counts"].get(key, 0) > 0
        legend[key] = entry
    return legend

def build_domain_notes(stats):
    notes = [
        "Каждая запись (connection) описывает физическую точку подключения провода: "
        "конкретный вывод (pin) конкретного клеммника/модуля (terminal_block) в шкафу "
        "(cabinet), на который заведён определённый проводник (conductor) конкретного "
        "устройства KKS.",
        "'Резерв' в поле kks/circuit_marking означает зарезервированный, физически не "
        "занятый вывод — такие строки не являются ошибкой сами по себе, но должны "
        "считаться неиспользуемыми при проверке комплектности схемы.",
        "Поле terminal_address = cabinet + '.' + terminal_block + '.' + pin — "
        "синтетический уникальный физический адрес клеммы, вычисленный при извлечении, "
        "удобен для проверки дублирования выводов (два разных провода на один и тот же "
        "физический вывод).",
    ]
    if stats["duplicate_terminal_addresses"]:
        dup_list = ", ".join(stats["duplicate_terminal_addresses"].keys())
        notes.append(
            f"Обнаружены дублирующиеся terminal_address ({dup_list}) — несколько записей "
            "ссылаются на один и тот же физический вывод клеммника. Это нужно явно "
            "проверить как потенциальную ошибку схемы или намеренное шунтирование."
        )
    else:
        notes.append("Дублирующихся terminal_address не обнаружено.")
    
    notes.append(
        f"Зарезервированных (незадействованных) выводов: {stats['reserve_rows_count']} "
        f"из {stats['total_rows']}."
    )
    return notes

def default_metadata():
    return {
        "document_number": None,
        "title_ru": None,
        "title_en": None,
        "project_ru": None,
        "project_en": None,
        "facility_ru": None,
        "organization_designer": None,
        "cabinet_kks": None,
        "sheet_format": None,
        "total_sheets": None,
        "stage": None,
        "stage_meaning_ru": None,
        "signatories": [],
        "source_file": None,
        "extraction_method": "pdfplumber word/line coordinate extraction; column "
                             "boundaries taken from PDF vertical ruling lines, "
                             "encoding corrected (cp1251 read as latin1/cp1252).",
        "extraction_date": date.today().isoformat(),
    }


# =========================================================================
# БЛОК 3: ТОЧКА ВХОДА (для пайплайна и для командной строки)
# =========================================================================

def build_document(pdf_path, meta_path=None, revisions_path=None):
    """Полное извлечение одного PDF-нетлиста в готовую JSON-структуру (dict)."""
    rows = extract_pdf_to_dicts(pdf_path)

    # Колонку страницы добавляет сам extract_pdf_to_dicts
    page_col = "Лист" if rows and "Лист" in rows[0] else None

    connections = build_connections(rows, page_col)
    stats = build_statistics(connections, page_col)

    metadata = default_metadata()
    metadata["source_file"] = str(pdf_path)

    if meta_path:
        with open(meta_path, encoding="utf-8") as f:
            metadata.update(json.load(f))

    if not metadata.get("total_sheets") and stats["total_pages"]:
        metadata["total_sheets"] = stats["total_pages"]

    revision_history = []
    if revisions_path:
        with open(revisions_path, encoding="utf-8") as f:
            revision_history = json.load(f)

    return {
        "document_metadata": metadata,
        "revision_history": revision_history,
        "column_legend": build_column_legend(stats),
        "domain_notes_for_analysis": build_domain_notes(stats),
        "statistics": stats,
        "connections": connections,
    }


def extract_to_dir(pdf_path, out_dir, meta_path=None, revisions_path=None):
    """Точка входа для пайплайна (ingest.py).

    Извлекает нетлист из PDF и кладёт connections.json в out_dir.
    Возвращает (список созданных файлов, краткая статистика) - это уходит
    в manifest.json, чтобы агент сразу видел объём данных, не открывая файлы.
    """
    os.makedirs(out_dir, exist_ok=True)
    doc = build_document(pdf_path, meta_path, revisions_path)

    out_path = os.path.join(out_dir, "connections.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    stats = doc["statistics"]
    summary = {
        "total_connections": stats["total_rows"],
        "total_pages": stats["total_pages"],
        "reserve_rows": stats["reserve_rows_count"],
        "duplicate_terminal_addresses": len(stats["duplicate_terminal_addresses"]),
        "distinct_terminal_blocks": len(stats["distinct_terminal_blocks"]),
    }
    return ["connections.json"], summary


def main():
    ap = argparse.ArgumentParser(description="Extract netlist from PDF and convert to JSON.")
    ap.add_argument("pdf_path", help="Path to the input PDF file")
    ap.add_argument("json_path", help="Path to the output JSON file")
    ap.add_argument("--meta", help="optional JSON file with document_metadata overrides")
    ap.add_argument("--revisions", help="optional JSON file with revision_history list")
    args = ap.parse_args()

    print(f"Extracting rows from {args.pdf_path}...")
    doc = build_document(args.pdf_path, args.meta, args.revisions)

    with open(args.json_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(doc['connections'])} connections -> {args.json_path}")


if __name__ == "__main__":
    main()
    