#!/usr/bin/env python3
"""
Базовый скрипт-парсер СБОРОЧНОГО ЧЕРТЕЖА шкафа (СБ) - векторный PDF.

Чем этот документ отличается от принципиальной схемы (Э3) и почему для него
нужен ОТДЕЛЬНЫЙ парсер, а не профиль в schematic_diagram_to_data.py.

Сборочный чертёж - это РИСУНОК ШКАФА: вид спереди/сбоку, реальные габариты,
все элементы на своих местах. Геометрии в нём чудовищно много и вся она
бесполезна для поиска ошибок документации: на листе 1 файла ША1 - 127 тысяч
векторных примитивов, на листе 2 файла ШУ-ТМ - 440 тысяч. Это контуры корпуса,
перфорация коробов, штриховки. Никакой связности проводов, которую строит
schematic_connectivity.py, здесь нет и быть не может.

Поэтому парсер НЕ ЧИТАЕТ page.get_drawings() ВООБЩЕ. Ценность сборочного
чертежа - в ПОДПИСЯХ: у каждого элемента шкафа рядом написано его позиционное
обозначение и артикул/тип. Это ровно то, чем чертёж сверяется со спецификацией
и принципиальной схемой.

КАК ЛОВИТСЯ ПАРА "обозначение - артикул". Оказалось, что чертёж сам группирует
подпись элемента в ОДИН текстовый блок PDF:
    блок 59: 'DO1'(кегль 6.48) + 'DVP16SN11T'(кегль 4.53)
    блок 53: 'G1'(6.48)  + 'NDR-120-24'(3.24)
    блок 18: 'HL01'(6.48) + '828163'(4.53)
Внутри блока обозначение набрано КРУПНЕЕ артикула - это и есть правило разбора,
геометрию угадывать не нужно. Такая пара помечается pair_source="block".

Но так везёт не всегда: в ШУ-ТМ подпись разбита на два блока ('QF01'+'QF02' в
одном, 'АВР-304-3P-200А-I'+'41044DEK' в другом). Для таких обозначений артикул
ищется ближайшим (pair_source="nearest") - и это ЗАВЕДОМО менее надёжно.
bundle_rules.py учитывает эту разницу: сверку АРТИКУЛОВ он делает только по
парам из блока, а по "ближайшим" - только проверку наличия элемента. Молча
смешивать два источника нельзя: на ложном спаривании родится ложная находка
"в документах разные артикулы", которой инженер не поверит.

Текст на чертеже часто ПОВЁРНУТ на 90° (bbox тогда узкий и высокий) - для
разбора это неважно, потому что блок PDF уже собран автором.

ПОЧЕМУ У СБОРОЧНОГО ЧЕРТЕЖА НЕТ СВОЕГО ЧЕКЕРА (assembly_rules.py).
В одиночку чертёж проверять нечем: всё осмысленное - это его соответствие
спецификации и схеме, и это делает bundle_rules.py. Единственное правило,
которое напрашивалось - "одно обозначение подписано ДВУМЯ РАЗНЫМИ артикулами", -
замерено на СБ_ИК.3912-АТХ2.015 и ОТКЛОНЕНО: 9 срабатываний, все ложные.
  - 5 из них - строки ПЕРЕЧНЯ ЭЛЕМЕНТОВ, напечатанного прямо на чертеже: в
    "обозначение" попадает имя производителя ('DKC', 'Regul', 'TBloc', 'UZOLA',
    'ChipDip'), в "артикул" - его изделия, и таких пар у одного имени десятки;
  - 4 - настоящие клеммники ('XT01' -> 8000099046 + 8000099048 + 8001099244):
    клеммник ЗАКОННО собран из клемм нескольких типов, это норма, а не ошибка.
Отделить одно от другого по геометрии нельзя, а находка "у XT01 три разных
артикула" бесполезна инженеру.

Функция extract_to_dir(pdf_path, out_dir) -> (files, stats) - контракт ingest.py.
"""

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

import fitz

# Фикс кодировки шрифтов (mojibake) переиспользуем у парсера схем: чертежи тех
# же бюро набраны теми же шрифтами (GOST type B), и ломаются они одинаково.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schematic_diagram_to_data as _sdd  # noqa: E402


# ============================================================
# Классификация одной подписи
# ============================================================

# Габарит/размер: "40x80", "15x35", "2x24x1", "60x60 (ШхВ)".
DIMENSION_RE = re.compile(r"^\d+([.,]\d+)?\s*[xх×]\s*\d+", re.I)
# Измерение с единицей: "742 мм", "429,5 мм", "295 А", "3 м", "-10…+80°C".
MEASURE_RE = re.compile(
    r"^[-+]?\d+([.,]\d+)?\s*(мм|см|м|кг|г|А|A|В|V|Вт|W|°C|°|мА|Гц|Ом)\.?$", re.I)
# Номинал аппарата защиты: "C40A", "C6A/30мА", "C63A", "0.5А".
RATING_RE = re.compile(r"^C?\d+([.,]\d+)?\s*[AА](/\d+\s*мА)?$", re.I)
# Кириллица внутри -> это надпись/примечание, а не артикул.
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

# Артикул/тип: латиница+цифры (+ . - / пробел), достаточно длинный.
# 'DVP16SN11T', 'NDR-120-24', 'cMT2078X', 'plc-kvs-16-50-gray', 'FA 12.230 FD'.
ARTICLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./\-]*([ ][A-Za-z0-9./\-]+)*$")
# Чистый числовой артикул производителя: '814174', '828163', '593394'.
NUMERIC_ARTICLE_RE = re.compile(r"^\d{5,}$")

# Позиционное обозначение: '1QF1', 'DO1', 'XT-G1', 'A0', 'HL01', '2X-AC', 'KL.R'.
# Нарочно КОРОТКОЕ (<= DESIGNATOR_MAX_LEN): именно длина надёжнее всего отделяет
# обозначение от артикула ('XT-AI1' - обозначение, 'cMT2078X' - артикул).
DESIGNATOR_RE = re.compile(r"^\d{0,2}[A-Za-zА-Яа-я][A-Za-zА-Яа-я0-9]*([.\-][A-Za-zА-Яа-я0-9]+)*$")
DESIGNATOR_MAX_LEN = 7
DESIGNATOR_MIN_LEN = 2

# Подписи выводов и клемм. На сборочном чертеже их подписывают ровно так же,
# как обозначения элементов (тот же шрифт, рядом с изделием), и формально они
# неотличимы: 'A1' на ША1 - это ПЛК DVP12SA211T, а 'A1' на ШУ-ТМ - вывод катушки
# реле. Поэтому такие подписи выведены в отдельный тип: принимать их за элементы
# нельзя, иначе сверка со спецификацией завалит отчёт находками "на чертеже есть
# A1/N/COM, а в спецификации их нет".
PIN_WORDS = {
    "l", "n", "pe", "l1", "l2", "l3", "n1", "n2", "a1", "a2", "b1", "b2",
    "c1", "c2", "com", "no", "nc", "gnd", "fg", "0v", "24v", "+24", "+24v",
    "+v", "-v", "s/s", "pwr+", "pwr-", "coil", "up", "vdd", "vss", "in", "out",
    "rs-232", "rs-485", "rs485", "ethernet", "usb", "debug", "x1", "x2",
}

# Слова рамки/штампа чертежа - не подписи элементов.
FRAME_WORDS = {
    "формат", "формат а3", "формат  а3", "инв.n подл.", "инв. n подл.",
    "взам. инв. n", "подп. и дата", "подпись и дата", "копировал", "лист",
    "листов", "изм.", "кол.уч", "кол.уч.", "подп.", "подпись", "№док.",
    "№ док.", "дата", "разраб.", "разработал", "проверил", "н.контр",
    "н.контр.", "стадия", "масштаб", "масса", "согласовано", "лист № док.",
    "вид спереди", "вид справа", "вид слева", "вид сзади", "вид сверху",
}

# Обозначение документа в штампе: '026.809.01.01-ИПК  СБ', '026.808.01-ИПК'.
DOC_NUMBER_RE = re.compile(r"^\d{3}\.\d{3}(\.\d{2})*(\.\d{2})*\s*-\s*[А-ЯA-Z]{2,4}")


def classify_span(text):
    """Тип одной подписи. Порядок проверок важен: сначала то, что ТОЧНО не
    артикул и не обозначение (размеры, номиналы, кириллица)."""
    t = text.strip()
    if not t:
        return "empty"
    if t.lower() in FRAME_WORDS:
        return "frame"
    if DOC_NUMBER_RE.match(t):
        return "doc_number"
    if t.lower() in PIN_WORDS:
        return "pin_label"
    if DIMENSION_RE.match(t) or MEASURE_RE.match(t):
        return "dimension"
    if RATING_RE.match(t):
        return "rating"
    if CYRILLIC_RE.search(t):
        # кириллица есть -> надпись ("ВВОД 1 ВКЛЮЧЕН") либо смешанный
        # артикул с кириллицей ('АВР-304-3P-200А-I') - в артикулы не берём
        return "note"
    if NUMERIC_ARTICLE_RE.match(t):
        return "article"
    if t.replace(",", ".").replace(".", "").isdigit():
        # голое число: номер клеммы, количество, размер без единицы.
        # В артикулы такое брать нельзя - иначе '2' у клеммника 2X-AI
        # становится его "артикулом" и ломает сверку.
        return "number"
    if len(t) > DESIGNATOR_MAX_LEN and ARTICLE_RE.match(t):
        return "article"
    if (DESIGNATOR_MIN_LEN <= len(t) <= DESIGNATOR_MAX_LEN
            and DESIGNATOR_RE.match(t)):
        return "designator"
    if ARTICLE_RE.match(t):
        return "article"
    return "other"


# ============================================================
# Чтение текста (БЕЗ векторной геометрии)
# ============================================================

def _extract_text_blocks(pdf_path):
    """Страницы -> блоки -> подписи. get_drawings() не вызывается сознательно:
    на этих чертежах это сотни тысяч примитивов и десятки секунд на лист."""
    font_fix_map = _sdd.analyze_fonts(pdf_path)
    doc = fitz.open(pdf_path)
    pages = []
    for pno, page in enumerate(doc, 1):
        blocks = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            spans = []
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    raw = sp.get("text", "")
                    if not raw.strip():
                        continue
                    font = sp.get("font", "")
                    if font in font_fix_map:
                        text = _sdd.apply_font_fix(raw, font_fix_map[font])
                    else:
                        text = _sdd.fix_text(raw)
                    text = text.strip()
                    if not text:
                        continue
                    spans.append({
                        "text": text,
                        "bbox": [round(v, 2) for v in sp["bbox"]],
                        "size": round(sp.get("size", 0), 2),
                        "entity_type": classify_span(text),
                    })
            if spans:
                blocks.append({"bbox": [round(v, 2) for v in block["bbox"]],
                               "spans": spans})
        pages.append({
            "page_number": pno,
            "width": round(page.rect.width, 2),
            "height": round(page.rect.height, 2),
            "blocks": blocks,
        })
    doc.close()
    return pages


def _center(bbox):
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


# Радиус поиска артикула "ближайшим", в пунктах PDF. Подобран по живым файлам:
# на ША1 артикул лежит в 5-7 pt от обозначения, на ШУ-ТМ - в 7-14 pt.
# Больше 40 pt - уже соседний элемент, такому спариванию верить нельзя.
NEAREST_RADIUS = 40.0


def _pair_within_block(block):
    """Пара (обозначение, артикул) из ОДНОГО блока подписи.

    Обозначение - самая крупная по кеглю подпись типа designator; артикул -
    самая длинная подпись типа article в том же блоке. Если обозначений в блоке
    несколько (ШУ-ТМ: 'QF01'+'QF02'), возвращаем их все, а артикул делим на них:
    подпись общая, и это честнее, чем отдать артикул первому попавшемуся.
    """
    designators = [s for s in block["spans"] if s["entity_type"] == "designator"]
    articles = [s for s in block["spans"] if s["entity_type"] == "article"]
    if not designators:
        return []

    max_size = max(s["size"] for s in designators)
    designators = [s for s in designators if s["size"] >= max_size - 0.01]

    article = max(articles, key=lambda s: len(s["text"])) if articles else None
    return [(d, article) for d in designators]


def build_elements(pages):
    """Все подписи элементов на чертеже -> список элементов."""
    elements = []
    for page in pages:
        # артикулы страницы - для запасного поиска "ближайшим"
        article_spans = [s for b in page["blocks"] for s in b["spans"]
                         if s["entity_type"] == "article"]

        for block in page["blocks"]:
            for des, art in _pair_within_block(block):
                pair_source = None
                article = None
                if art is not None:
                    article = art["text"]
                    pair_source = "block"
                else:
                    cx, cy = _center(des["bbox"])
                    best, best_d = None, NEAREST_RADIUS
                    for s in article_spans:
                        ox, oy = _center(s["bbox"])
                        d = math.hypot(ox - cx, oy - cy)
                        if d < best_d:
                            best, best_d = s, d
                    if best is not None:
                        article = best["text"]
                        pair_source = "nearest"

                elements.append({
                    "designator": des["text"],
                    "article": article,
                    "pair_source": pair_source,
                    "sheet": page["page_number"],
                    "bbox": des["bbox"],
                    "label_text": " ".join(s["text"] for s in block["spans"]
                                           if s is not des),
                })
    return elements


def _document_metadata(pages, pdf_path):
    """Обозначение документа и имя шкафа из штампа. Обозначение набрано в
    штампе самым крупным кеглем - по нему и находим."""
    best = None
    for page in pages:
        for block in page["blocks"]:
            for s in block["spans"]:
                if s["entity_type"] != "doc_number":
                    continue
                if best is None or s["size"] > best["size"]:
                    best = s
    return {
        "source_file": os.path.basename(pdf_path),
        "designation_in_document": best["text"] if best else None,
        "total_sheets": len(pages),
    }


# ============================================================
# Контракт ingest.py
# ============================================================

def extract_to_dir(pdf_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    pages = _extract_text_blocks(pdf_path)
    elements = build_elements(pages)
    meta = _document_metadata(pages, pdf_path)

    by_designator = defaultdict(list)
    for e in elements:
        by_designator[e["designator"]].append(e)

    # индекс: обозначение -> где встречено и с каким артикулом
    index = {}
    for d, items in sorted(by_designator.items()):
        arts = sorted({i["article"] for i in items if i["article"]})
        index[d] = {
            "count": len(items),
            "sheets": sorted({i["sheet"] for i in items}),
            "articles": arts,
            "pair_sources": sorted({i["pair_source"] for i in items if i["pair_source"]}),
        }

    # все текстовые подписи - чтобы сверка могла проверить наличие артикула
    # спецификации на чертеже, даже если он не спарился с обозначением
    all_texts = sorted({s["text"] for p in pages for b in p["blocks"]
                        for s in b["spans"]})

    doc = {
        "document_metadata": meta,
        "column_legend": {
            "designator": "позиционное обозначение элемента, как подписано на чертеже",
            "article": "артикул/тип рядом с обозначением; null - не найден",
            "pair_source": "как найден артикул: 'block' - вместе с обозначением в одном "
                           "текстовом блоке чертежа (НАДЁЖНО); 'nearest' - ближайшая "
                           "подпись-артикул в радиусе 40 pt (НЕНАДЁЖНО, может быть чужой)",
            "label_text": "весь остальной текст подписи элемента",
            "sheet": "номер листа чертежа",
        },
        "domain_notes_for_analysis": (
            "1) Сверять артикулы со спецификацией можно ТОЛЬКО по элементам с "
            "pair_source='block'. Для 'nearest' артикул мог быть притянут от соседнего "
            "элемента - на таких парах вывод о несовпадении артикулов недопустим. "
            "2) Одно обозначение может встречаться на чертеже НЕСКОЛЬКО раз (вид спереди "
            "и вид сбоку, разрез) - count>1 сам по себе НЕ ошибка и НЕ означает, что "
            "элементов физически несколько. "
            "3) Геометрия шкафа (линии, контуры) не извлекается сознательно: её сотни "
            "тысяч примитивов и для поиска ошибок документации она бесполезна. "
            "4) На чертеже подписаны не только изделия из спецификации, но и короба, "
            "DIN-рейки и их длины - у них нет позиционного обозначения."
        ),
        "elements": elements,
        "designator_index": index,
        "all_label_texts": all_texts,
        "statistics": {
            "total_sheets": len(pages),
            "text_blocks": sum(len(p["blocks"]) for p in pages),
            "text_spans": sum(len(b["spans"]) for p in pages for b in p["blocks"]),
            "elements": len(elements),
            "unique_designators": len(by_designator),
            "paired_from_block": sum(1 for e in elements if e["pair_source"] == "block"),
            "paired_nearest": sum(1 for e in elements if e["pair_source"] == "nearest"),
            "unpaired": sum(1 for e in elements if e["pair_source"] is None),
            "entity_types": dict(Counter(s["entity_type"] for p in pages
                                         for b in p["blocks"] for s in b["spans"])),
            "designation": meta["designation_in_document"],
        },
    }

    files = []
    path = os.path.join(out_dir, "assembly.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    files.append("assembly.json")

    # сырые подписи с координатами - отдельным файлом: он крупный и нужен
    # редко (перепроверить, не потерял ли парсер подпись)
    path = os.path.join(out_dir, "assembly_raw.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"pages": pages}, f, ensure_ascii=False, indent=2)
    files.append("assembly_raw.json")

    st = doc["statistics"]
    stats = {
        "total_pages": st["total_sheets"],
        "assembly_elements": st["elements"],
        "assembly_unique_designators": st["unique_designators"],
        "assembly_paired_from_block": st["paired_from_block"],
        "assembly_paired_nearest": st["paired_nearest"],
        "assembly_designation": st["designation"],
    }
    return files, stats


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 assembly_drawing_to_data.py path/to/СБ.pdf [out_dir]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else "."
    files, stats = extract_to_dir(sys.argv[1], out)
    print(json.dumps({"files": files, "stats": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
