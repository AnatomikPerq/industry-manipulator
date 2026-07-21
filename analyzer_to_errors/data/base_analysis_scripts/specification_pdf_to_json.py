#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Спецификация оборудования по ГОСТ 21.110, пришедшая ЧЕРТЕЖОМ, а не книгой Excel.

Отдельным файлом спецификация приходит только в маленьком комплекте на один
шкаф. В полном проекте (альбом на 200+ листов) она - такой же лист альбома, как
схема: та же таблица ГОСТ 21.110, но нарисованная в PDF. На "24-051-ЭОМ" это 48
листов, то есть вся сверка по связке (нарисовано, но не заказано; разные
артикулы) без разбора PDF на полном проекте не работает вовсе.

Выход - тот же specification.json с тем же контрактом, что у
specification_to_json.py. Разбор строки (раскрытие диапазонов в колонке
«Позиция», отсев разделов и легенды) НЕ дублируется, а импортируется оттуда:
разъехавшись, две копии дали бы разный набор обозначений на xlsx и на PDF одной
и той же спецификации, и сверка молча поехала бы.

КАК НАХОДЯТСЯ КОЛОНКИ

По границам ЛИНОВКИ таблицы. Разделители колонок нарисованы не сплошной линией
во всю высоту, а короткими отрезками по границам каждой ячейки - длинных
вертикалей на листе нет ни одной, поэтому копится суммарная длина вертикальных
отрезков по каждой координате X: настоящая граница набирает её со всех строк
сразу и уверенно выходит из шума (на листе ЭОМ это x = 111, 479, 650, 749, 876,
933, 990, 1061).

Брать границы из заголовков (середина между центрами соседних) НЕЛЬЗЯ, хотя это
и первое, что приходит в голову: колонки резко разной ширины, «Наименование»
шире «Позиции» впятеро, и середина между их центрами проходит прямо посреди
наименования. Перенос наименования на вторую строку уезжал в «Позицию» и
становился ложным позиционным обозначением ('4шт', '248 Шайба 6.65Г.016 ГОСТ
6402-70') - то есть мусором в ГЛАВНОМ ключе сверки со схемой и чертежом.

Имя поля колонке даётся по тексту заголовка над ней, а не по порядку из ГОСТ:
бюро добавляют свои колонки (в корпусе встречались таблицы на 16 и на 9).

СЛУЖЕБНАЯ СТРОКА НУМЕРАЦИИ КОЛОНОК ("1 2 3 ... N") НЕОБЯЗАТЕЛЬНА

Первая версия парсера использовала её как ворота листа: нет строки нумерации -
лист не спецификация. На "11-463-2026-АТХ" это оказалось неверно: бюро рисует
ту же таблицу ГОСТ 21.110 БЕЗ строки нумерации, и все 7 листов спецификации
шкафа (и 5 листов объектной) молча пропускались - spec_rows: 0, а сверка связки
при этом честно работала с пустой спецификацией и выдала 16 ложных "изделие не
заказано" из 17 находок. Теперь строка нумерации - лишь один из двух способов
найти низ шапки; без неё шапка находится по самим заголовкам («Поз.»,
«Наименование...», «Кол.»), и воротами листа служит требование "нашлись колонки
«Количество» и «Наименование»" - у случайного листа таких заголовков над
линовкой нет.

КАК НАХОДЯТСЯ СТРОКИ

Якорь строки - значение в колонке «Количество»: оно стоит ровно одно на строку
и ровно у каждой строки-позиции. Просветом по вертикали строки не разделить
(шаг строки 23-30 px против высоты строки 14 - перенос текста внутри ячейки от
соседней строки таблицы так не отличить), а горизонтальных линеек извлекается
23 на ~25 строк, то есть сами по себе границами они тоже быть не могут.
Работает связка: граница между двумя соседними якорями - линейка, если она
между ними ровно одна, иначе середина. Одной серединой ячейки «Позиции»
соседних строк склеивались, когда обозначения переносились на вторую строку
('1ТА7, 1ТА8 1KL01, 1KL02, 1KL03' - это ДВЕ строки таблицы); линейки убрали
такие склейки с 26 до 4 на 837 строках.

Текст в полях листа отбрасывается по ПОВОРОТУ - см. _page_lines.
"""

import importlib.util
import json
import os
import re
import sys
from collections import Counter, defaultdict

import fitz

_HERE = os.path.dirname(os.path.abspath(__file__))


def _sibling(name, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(_HERE, name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# Разбор «Позиции», отсев разделов/легенды и числа - из xlsx-парсера, чтобы два
# формата одной и той же спецификации давали один и тот же набор данных.
_xlsx = _sibling("specification_to_json.py", "_spec_xlsx")
_schem = _sibling("schematic_diagram_to_data.py", "_spec_fontfix")
_progress = _sibling("progress.py", "_spec_progress")

parse_designators = _xlsx.parse_designators
SECTION_RE = _xlsx.SECTION_RE
LEGEND_RE = _xlsx.LEGEND_RE
COLUMN_PATTERNS = _xlsx.COLUMN_PATTERNS
_to_number = _xlsx._to_number

# Обозначение документа в штампе: "24-051-ЭОМ.СО", "026.822.13-ИПК".
DESIGNATION_RE = re.compile(r"^[0-9][0-9.\-]{3,}[A-ZА-ЯЁ0-9.\-]*$")

# Графы основной надписи. На ПОСЛЕДНЕМ листе таблица кончается выше обычного, а
# штамп сам разлинован, и его вертикали проходят по тем же X, что колонки
# таблицы - поэтому вычисленный низ таблицы съезжает на штамп. Строка, в ячейках
# которой стоят графы штампа, отбрасывается явно: это дешевле и надёжнее, чем
# отличать линовку таблицы от линовки штампа геометрически.
# Граница после образца - «дальше не буква», а не \b: половина образцов
# кончается точкой («изм.», «подп.»), а \b между точкой и пробелом не
# срабатывает вовсе - с ним "Изм. Кол.уч." штампом не опознавался.
STAMP_RE = re.compile(
    r"^(кол\.\s*уч|№\s*док|взам\.?\s*инв|инв\.\s*№|н\.\s*контр|"
    r"изм|лист|подп|дата|формат|копировал|стадия|листов|"
    r"разраб|проверил|пров|утвердил|согласовал|гип|гап)"
    r"(?![а-яёa-z])", re.I)


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _join_fragments(texts):
    """Склеить куски текста, перенесённые по строкам.

    Заголовки узких колонок набраны с переносом по дефису ("Количе-" / "ство",
    "Единица" / "измере-" / "ния"). Склеив их через пробел, получаем
    "количе- ство", которое не совпадает ни с одним образцом COLUMN_PATTERNS, и
    колонка «Количество» теряется - а она якорь строк, без неё лист не
    разбирается вообще.
    """
    out = ""
    for t in texts:
        t = _norm(t)
        if not t:
            continue
        # Склейка переноса - только когда перед дефисом БУКВА («Количе-» /
        # «ство»). Диапазон позиций, перенесённый на другую строку («1KL1 -» /
        # «1KL12»), кончается дефисом ПОСЛЕ ПРОБЕЛА - склеив его, мы съедали
        # разделитель диапазона, '1KL1 1KL12' переставал быть диапазоном, и
        # десять промежуточных реле «не были заказаны» (вал ложных MISSING
        # на связке КОС - 459 находок, почти все отсюда).
        if out.endswith("-") and len(out) >= 2 and out[-2].isalpha():
            out = out[:-1] + t
        elif out:
            out += " " + t
        else:
            out = t
    return out


def _page_lines(page, font_map):
    """Строки текста листа с координатами, с починенной кириллицей.

    ПОВЁРНУТЫЙ текст отбрасывается. В левом поле листа по ГОСТ 21.101 стоит
    блок «Инв. № подл. / Подп. и дата / Взам. инв. №», набранный вертикально.
    По координате X он попадает ЛЕВЕЕ первой линовки, то есть внутрь колонки
    «Позиция», и приезжал в неё позиционными обозначениями: строка
    '1.1QF, Взам инв. № 18.1QF, 2.1QF' давала обозначения ['1.1QF', 'Взам',
    'инв', '№', ...] - мусор в главном ключе сверки, да ещё и с ложным
    срабатыванием правила «количество меньше числа позиций» (26 находок на
    одной спецификации). Поворот - признак надёжный: содержимое таблицы
    горизонтально всегда, вертикальные надписи бывают только в полях листа.
    """
    out = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            d = line.get("dir", (1.0, 0.0))
            if abs(d[0]) < 0.99:            # не горизонтальная строка
                continue
            text = "".join(
                _schem.apply_font_fix(sp.get("text", ""), font_map.get(sp.get("font", "")))
                for sp in line.get("spans", [])
            ).strip()
            if text:
                x0, y0, x1, y1 = line["bbox"]
                out.append({"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                            "xc": (x0 + x1) / 2, "yc": (y0 + y1) / 2})
    return out


def _find_number_row(lines, page_height):
    """Служебная строка нумерации колонок по ГОСТ: "1 2 3 ... N" в верхней трети.

    Ищем набор одиночных цифр на одной высоте, идущих слева направо ПОДРЯД
    (1,2,3,...). Требование именно подряд отсекает случайные одиночные цифры в
    данных - в таблице их много (количества, массы).
    """
    by_y = defaultdict(list)
    for ln in lines:
        if ln["y0"] > page_height * 0.45:
            continue
        if re.fullmatch(r"\d{1,2}", ln["text"].strip()):
            by_y[round(ln["yc"] / 4)].append(ln)

    best = None
    for group in by_y.values():
        group.sort(key=lambda l: l["xc"])
        seq = [int(l["text"]) for l in group]
        if len(seq) >= 5 and seq == list(range(1, len(seq) + 1)):
            if best is None or len(group) > len(best):
                best = group
    return best


def _rules_of(page):
    """Линовка листа: ({x: суммарная длина вертикалей}, {y: длина горизонталей},
    {x: нижний край вертикали})."""
    vert, horiz, y_hi = defaultdict(float), defaultdict(float), defaultdict(float)
    for item in page.get_drawings():
        for el in item["items"]:
            if el[0] == "l":
                a, b = el[1], el[2]
                if abs(a.x - b.x) < 1.0 and abs(a.y - b.y) >= 10:
                    x = round((a.x + b.x) / 2)
                    vert[x] += abs(a.y - b.y)
                    y_hi[x] = max(y_hi[x], a.y, b.y)
                elif abs(a.y - b.y) < 1.0 and abs(a.x - b.x) >= 30:
                    horiz[round((a.y + b.y) / 2)] += abs(a.x - b.x)
            elif el[0] == "re":
                r = el[1]
                if r.width < 2.5 and r.height >= 10:
                    x = round(r.x0 + r.width / 2)
                    vert[x] += r.height
                    y_hi[x] = max(y_hi[x], r.y1)
                elif r.height < 2.5 and r.width >= 30:
                    horiz[round(r.y0 + r.height / 2)] += r.width
    return vert, horiz, y_hi


def _row_rules(page):
    """Горизонтальные линейки таблицы, отсортированные по Y.

    Их меньше, чем строк (23 линейки на ~25 строк), поэтому сами по себе
    границами строк они служить не могут - но там, где линейка между двумя
    соседними якорями ЕСТЬ, она точнее середины между ними. Именно из-за
    середины склеивались ячейки «Позиции» соседних строк, когда в одной из них
    обозначения переносились на вторую строку.
    """
    _, horiz, _ = _rules_of(page)
    if not horiz:
        return []
    strong = max(horiz.values())
    return sorted(y for y, total in horiz.items() if total >= strong * 0.5)


def _column_separators(page):
    """(границы колонок по X, низ таблицы по Y) из линовки таблицы."""
    acc, _, y_hi = _rules_of(page)
    if not acc:
        return [], None

    strong = max(acc.values())
    seps = sorted(x for x, total in acc.items() if total >= strong * 0.5)
    merged = []
    for x in seps:
        if merged and x - merged[-1] <= 3:   # граница нарисована в два прохода
            continue
        merged.append(x)
    bottom = max((y_hi[x] for x in merged), default=None)
    return merged, bottom


def _header_bottom_from_titles(lines, page_height):
    """Низ шапки таблицы, когда служебной строки нумерации на листе нет.

    Ищем в верхней части листа сами заголовки колонок (по образцам
    COLUMN_PATTERNS) и берём низ самого глубокого из них. Порог 0.4 высоты -
    шапка не бывает ниже, а данные («Количество 246») выше него уже есть, и
    сопоставлять образцы со всем листом нельзя.

    Заголовок узкой колонки набран в несколько строк с переносом ("Код обору-" /
    "дования," / "изделия," / "материала"), и НИЖНИЕ строки ни с одним образцом
    не совпадают. Оборвать шапку на последнем совпавшем образце - значит отдать
    эти обрывки первой строке данных ("материала" уезжало в ячейку кода первой
    позиции листа). Поэтому низ дотягивается поглощением: строка, начинающаяся
    вплотную под текущим низом шапки, - её продолжение. Данные так не
    захватываются: между шапкой и первой строкой таблицы всегда есть просвет.
    """
    bottom = None
    top_lines = [l for l in lines if l["y0"] <= page_height * 0.4]
    for l in top_lines:
        text = _norm(l["text"]).lower()
        if any(p in text for _, patterns in COLUMN_PATTERNS for p in patterns):
            bottom = l["y1"] if bottom is None else max(bottom, l["y1"])
    if bottom is None:
        return None

    changed = True
    while changed:
        changed = False
        for l in top_lines:
            # строка начинается не глубже, чем вплотную под текущим низом
            # (в т.ч. перекрывает его), а кончается ниже - шапка продолжается.
            # Зазор 4 px: в форме 7-а («Завод-изготовитель ... страна, фирма»)
            # строки шапки набраны с чуть большим межстрочным просветом.
            if l["y0"] <= bottom + 4.0 and l["y1"] > bottom:
                bottom = l["y1"]
                changed = True
    return bottom


def _map_columns(lines, header_bottom, separators, page_width, number_row=None):
    """{поле: (x_lo, x_hi)} - колонки таблицы, названные по тексту заголовка."""
    headers = [l for l in lines if l["y1"] <= header_bottom + 2]

    if separators:
        edges = [0.0] + [float(x) for x in separators] + [float(page_width)]
    elif number_row:
        # запасной путь, если линовку прочитать не удалось: середины между
        # цифрами служебной строки. Грубее, но лучше, чем ничего.
        centers = [l["xc"] for l in number_row]
        edges = [0.0] + [(a + b) / 2 for a, b in zip(centers, centers[1:])]
        edges.append(float(page_width))
    else:
        return {}

    mapping = {}
    for lo, hi in zip(edges, edges[1:]):
        title = _join_fragments(
            h["text"] for h in sorted(headers, key=lambda h: h["y0"])
            if lo <= h["xc"] <= hi
        ).lower()
        if not title:
            continue
        for field, patterns in COLUMN_PATTERNS:
            if field in mapping:
                continue
            if any(p in title for p in patterns):
                mapping[field] = (lo, hi)
                break
    return mapping


def _cell(row_lines, span):
    """Текст ячейки: все строки, чей центр попал в границы колонки."""
    if span is None:
        return ""
    lo, hi = span
    parts = [l for l in row_lines if lo <= l["xc"] <= hi]
    parts.sort(key=lambda l: (round(l["y0"]), l["x0"]))
    return _join_fragments(p["text"] for p in parts)


def _rows_from_page(lines, mapping, page_height, table_bottom, row_rules, top):
    """Разбить строки листа на строки таблицы по якорям в колонке «Количество».

    top - низ шапки (данные начинаются ниже него). Раньше он вычислялся как
    "всё, что выше 20% высоты листа, - шапка": на листах АТХ шапка кончается на
    9% высоты, данные начинаются на 13%, и первые три-четыре строки каждого
    листа молча съедались бы. Верх обязан приходить от найденной шапки.
    """
    qty_span = mapping.get("quantity")
    if qty_span is None:
        return []
    lo, hi = qty_span
    bottom = table_bottom if table_bottom else page_height * 0.88

    # Графы штампа, приклеивающиеся к последней строке листа. Вычисленный низ
    # таблицы иногда съезжает на штамп (его вертикали идут по тем же X), и
    # «Кол.уч. Лист № док.» приезжало в ячейку кода последней позиции. Сами
    # строки-штампы уже отбрасываются по STAMP_RE ниже, но здесь надо убрать
    # ОТДЕЛЬНЫЕ строки текста, попавшие в чужую полосу. Только в нижней
    # четверти листа: выше штампа не бывает, а в данных бывают слова
    # «лист»/«дата» в наименованиях.
    lines = [l for l in lines
             if l["y0"] > top and l["y1"] <= bottom + 2
             and not (l["y0"] > page_height * 0.75 and STAMP_RE.match(l["text"].strip()))]

    anchors = sorted(
        (l for l in lines
         if lo <= l["xc"] <= hi and _to_number(l["text"]) is not None),
        key=lambda l: l["yc"],
    )
    if not anchors:
        return []

    # Граница между соседними якорями: линейка таблицы, если она между ними
    # ровно одна, иначе середина. Линеек меньше, чем строк, но там, где она
    # есть, она проходит по настоящей границе ячеек, а середина - нет: при
    # переносе «Позиции» на вторую строку соседние ячейки склеивались
    # ('1ТА7, 1ТА8 1KL01, 1KL02, 1KL03' - это ДВЕ строки таблицы).
    edges = [-1e9]
    for a, b in zip(anchors, anchors[1:]):
        between = [y for y in row_rules if a["yc"] < y < b["yc"]]
        edges.append(between[0] if len(between) == 1 else (a["yc"] + b["yc"]) / 2)
    edges.append(1e9)

    rows = []
    for i, anchor in enumerate(anchors):
        band = [l for l in lines if edges[i] <= l["yc"] < edges[i + 1]]
        if band:
            rows.append({"y": anchor["yc"], "lines": band})
    return rows


def parse_pdf(path):
    font_map = _schem.analyze_fonts(path)
    doc = fitz.open(path)

    items = []
    designation, section = None, None
    pages_parsed, columns_found = 0, {}

    try:
        for page_no, page in enumerate(doc, start=1):
            _progress.page(page_no, len(doc), stage="чтение спецификации")
            lines = _page_lines(page, font_map)
            number_row = _find_number_row(lines, page.rect.height)
            if number_row:
                # По ГОСТ под шапкой стоит строка нумерации колонок: её верх -
                # низ шапки, её низ - верх данных (сами цифры "1 2 3..." в
                # данные попадать не должны: одиночная цифра в колонке
                # «Количество» - валидный якорь строки).
                header_bottom = min(l["y0"] for l in number_row)
                data_top = max(l["y1"] for l in number_row)
            else:
                # Бюро АТХ рисует ту же таблицу БЕЗ строки нумерации - шапку
                # ищем по самим заголовкам колонок.
                header_bottom = _header_bottom_from_titles(lines, page.rect.height)
                if header_bottom is None:
                    continue
                data_top = header_bottom
            separators, table_bottom = _column_separators(page)
            mapping = _map_columns(lines, header_bottom, separators,
                                   page.rect.width, number_row)
            if "quantity" not in mapping or "name" not in mapping:
                continue
            pages_parsed += 1
            columns_found = columns_found or {k: [round(v[0]), round(v[1])]
                                              for k, v in sorted(mapping.items())}

            if designation is None:
                for ln in lines:
                    if (ln["y0"] > page.rect.height * 0.7
                            and DESIGNATION_RE.match(ln["text"].strip())):
                        designation = ln["text"].strip()
                        break

            for row in _rows_from_page(lines, mapping, page.rect.height, table_bottom,
                                       _row_rules(page), data_top):
                name = _cell(row["lines"], mapping.get("name"))
                pos_raw = _cell(row["lines"], mapping.get("position"))
                code = _cell(row["lines"], mapping.get("code"))
                qty = _to_number(_cell(row["lines"], mapping.get("quantity")))

                if not name and not code and qty is None:
                    continue
                if STAMP_RE.match(name) or STAMP_RE.match(code) or STAMP_RE.match(pos_raw):
                    continue
                # Раздел («Дополнение от ...») и легенда заливок - не позиции.
                if name and qty is None and not code and not pos_raw:
                    if SECTION_RE.match(name):
                        section = name
                    continue
                if name and LEGEND_RE.match(name) and qty is None:
                    continue

                items.append({
                    "page": page_no,
                    "row": len(items) + 1,
                    "section": section,
                    "position_raw": pos_raw,
                    "designators": parse_designators(pos_raw),
                    "name": name,
                    "type_mark": _cell(row["lines"], mapping.get("type_mark")),
                    "code": code,
                    "manufacturer": _cell(row["lines"], mapping.get("manufacturer")),
                    "unit": _cell(row["lines"], mapping.get("unit")),
                    "quantity": qty,
                    "mass": _to_number(_cell(row["lines"], mapping.get("mass"))),
                    "note": _cell(row["lines"], mapping.get("note")),
                })
    finally:
        doc.close()

    by_designator = defaultdict(list)
    for it in items:
        for d in it["designators"]:
            by_designator[d].append(it["row"])
    all_designators = sorted(by_designator)
    counts = Counter(it["code"] for it in items if it["code"])

    return {
        "document_metadata": {
            "source_file": os.path.basename(path),
            "sheet_name": None,
            "designation_in_document": designation,
            "header_row": None,
            "columns_found": columns_found,
            "pages_parsed": pages_parsed,
            "source_format": "pdf",
            "extra_sheets": [],
        },
        "column_legend": {
            "position_raw": "колонка «Позиция» как есть в документе",
            "designators": "то же, развёрнутое в отдельные позиционные обозначения "
                           "(диапазоны '1KL1...1KL3' раскрыты) - ГЛАВНЫЙ КЛЮЧ СВЕРКИ "
                           "со сборочным чертежом и принципиальной схемой",
            "name": "наименование и техническая характеристика",
            "type_mark": "тип, марка, обозначение документа",
            "code": "код оборудования (артикул производителя)",
            "quantity": "количество в единицах измерения (unit)",
            "page": "лист спецификации, на котором стоит строка",
            "section": "раздел спецификации; null - основная часть",
        },
        "domain_notes_for_analysis": (
            "1) Количество НЕ обязано совпадать с числом позиционных обозначений: "
            "для клеммников в «Позиция» стоят ОБОЗНАЧЕНИЯ КЛЕММНИКОВ, а в "
            "«Количество» - число КЛЕММ в них. Количество БОЛЬШЕ числа обозначений - норма. "
            "2) Одно и то же обозначение в нескольких строках - тоже норма: у устройства "
            "есть аксессуары (реле + колодка + фиксатор + шильдик - 4 строки на одно 'KL1'). "
            "3) Строки без «Позиции» (короба, DIN-рейки, крепёж) - расходные материалы, "
            "у них нет позиционного обозначения и сверять их со схемой не с чем. "
            "4) Спецификация полного проекта охватывает ВЕСЬ объект, а не один шкаф: "
            "в ней штатно есть оборудование щитов, которых нет в разбираемой связке. "
            "Отсутствие строки спецификации на чертеже конкретного шкафа само по себе "
            "НЕ ошибка."
        ),
        "items": items,
        "designator_index": {d: by_designator[d] for d in all_designators},
        "cell_errors": [],
        "statistics": {
            "total_rows": len(items),
            "rows_with_position": sum(1 for i in items if i["designators"]),
            "unique_designators": len(all_designators),
            "total_designators": sum(len(i["designators"]) for i in items),
            "rows_without_code": sum(1 for i in items if not i["code"]),
            "duplicate_codes": {k: v for k, v in counts.items() if v > 1},
            "cell_errors": 0,
            "pages_parsed": pages_parsed,
            "sections": sorted({i["section"] for i in items if i["section"]}),
        },
    }


# ============================================================
# Контракт ingest.py
# ============================================================

def extract_to_dir(pdf_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    doc = parse_pdf(pdf_path)

    path = os.path.join(out_dir, "specification.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    st = doc["statistics"]
    return ["specification.json"], {
        "spec_rows": st["total_rows"],
        "spec_unique_designators": st["unique_designators"],
        "spec_cell_errors": st["cell_errors"],
        "spec_pages_parsed": st["pages_parsed"],
        "spec_designation": doc["document_metadata"]["designation_in_document"],
    }


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 specification_pdf_to_json.py path/to/СО.pdf [out_dir]")
        raise SystemExit(2)
    out = sys.argv[2] if len(sys.argv) > 2 else "."
    files, stats = extract_to_dir(sys.argv[1], out)
    print(json.dumps({"files": files, "stats": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
