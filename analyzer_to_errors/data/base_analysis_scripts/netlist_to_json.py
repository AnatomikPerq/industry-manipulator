#!/usr/bin/env python3
"""
Combined script: Extracts wiring/netlist table from a GOST-style PDF 
and converts it directly into a structured JSON file for LLM analysis.

Usage:
python3 pdf_to_json.py input.pdf output.json [--meta meta.json] [--revisions revisions.json]
"""

import os
import re
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
# БЛОК 1а: ДРУГИЕ ВИДЫ ТАБЛИЦ ТИПА "НЕТЛИСТ"
#
# Нарезка полного проекта отдаёт этому парсеру не только ГОСТ-таблицу
# подключений, но и «Перечень входных/выходных сигналов» и «Кабельный журнал»
# (см. TITLE_TO_TYPE в full_project.py). Это ДРУГИЕ таблицы: у перечня сигналов
# колонки "№ п/п / Обозначение / Адрес PLC / Тип / Описание", у журнала -
# "Обозначение кабеля / Начало / Конец / Марка / сечение / длина". Жёсткие
# COL_BOUNDS ГОСТ-шаблона на них дают НОЛЬ строк, и до этого блока все три
# документа АТХ молча извлекались пустыми (total_connections: 0 при статусе ok).
#
# Раскладка узнаётся по заголовкам первого листа, колонки берутся из линовки
# (суммарная длина вертикальных отрезков по X - тот же приём, что в
# specification_pdf_to_json.py, и по той же причине: длинных вертикалей нет,
# границы нарисованы по-ячеечно). Выход - те же connections.json записи;
# что за таблица была - в metadata.table_kind, и правила netlist_rules
# применяются только те, что осмыслены для этого вида (см. check_connections_file).
# =========================================================================

def _norm_ws(s):
    return " ".join(str(s or "").split()).lower()


def _page_words(page):
    return [
        {"text": fix_text(w["text"]), "x0": w["x0"], "x1": w["x1"],
         "top": w["top"], "bottom": w["bottom"], "xc": (w["x0"] + w["x1"]) / 2}
        for w in page.extract_words(use_text_flow=False, keep_blank_chars=False)
    ]


def detect_table_kind(pdf_path):
    """"gost_connections" | "signal_list" | "cable_journal" - по заголовкам
    первого листа. ГОСТ-таблица отдаётся штатному извлечению."""
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return "gost_connections"
        page = pdf.pages[0]
        head = _norm_ws(" ".join(w["text"] for w in _page_words(page)
                                 if w["top"] < page.height * 0.25))
    # «Перечень входных/выходных параметров контроля, регулирования, управления»
    # (опросная таблица ГОСТ 21.408). Тоже перечень сигналов, но подписан
    # иначе и, главное, несёт ГРАФУ «Позиция по схеме» - позиции приборов
    # (HL101, NSF001, PT206), которыми он сшивается с ФСА и кабельным журналом.
    # На «24-051-АК» это 27 листов, два документа.
    if "позиция" in head and "схеме" in head and (
            "сигнализация" in head or "пределы измерений" in head
            or "резервирование" in head):
        return "param_list"
    if "описание сигнала" in head and (
            "адрес plc" in head or "номер входа" in head or "номер выхода" in head):
        return "signal_list"
    # «Начало/конец» - подписи ГОСТ 21.110, «откуда/куда» - обиходные, и второй
    # парой пользуются ОБА бюро объекта 24-051. Пока их здесь не было, кабельные
    # журналы АК (10 л.) и ЭОМ (16 л.) уезжали в ветку ГОСТ-таблицы подключений,
    # где жёсткий COL_BOUNDS даёт НОЛЬ строк при статусе ok - тот же молчаливый
    # отказ, ради которого этот детектор и появился.
    if "кабел" in head and (("начало" in head and "конец" in head)
                            or ("откуда" in head and "куда" in head)):
        return "cable_journal"
    return "gost_connections"


def _vertical_separators(page, min_share=0.35):
    """Границы колонок по линовке: X, набравшие достаточную суммарную длину
    вертикальных отрезков. Поля листа (x < 50) отбрасываются - там рамка
    формата и вертикальные надписи «Инв. № подл.»."""
    from collections import defaultdict
    acc = defaultdict(float)
    for l in page.lines:
        if abs(l["x0"] - l["x1"]) < 1.0 and abs(l["top"] - l["bottom"]) >= 5:
            acc[round(l["x0"])] += abs(l["top"] - l["bottom"])
    for r in page.rects:
        if r["width"] < 2.5 and r["height"] >= 5:
            acc[round(r["x0"])] += r["height"]
    if not acc:
        return []
    strong = max(acc.values())
    seps, merged = sorted(x for x, v in acc.items()
                          if v >= strong * min_share and x >= 50), []
    for x in seps:
        if merged and x - merged[-1] <= 3:
            continue
        merged.append(x)
    return merged


def _row_bands(page, min_width, gap_lo=8, gap_hi=45):
    """Полосы строк таблицы между соседними горизонтальными линейками.

    В отличие от data_row_bands ГОСТ-шаблона, ширина линейки - параметр
    (перечень сигналов набран на A4, его линейки короче 500), а полосы штампа
    внизу листа отсеиваются не по скачку шага, а содержимым ячеек в вызывающем
    коде (в колонке обозначения у штампа пусто).

    Линейка бывает и ПРЯМОУГОЛЬНИКОМ, а не отрезком - ровно как вертикальные
    разделители в _vertical_separators, и по той же причине: чем нарисована
    таблица, решает экспорт CAD, а не бюро. У кабельных журналов АК и ЭОМ
    горизонтальных отрезков на листе НОЛЬ при 994 и 761 прямоугольнике, и
    журналы извлекались пустыми уже после того, как их научились опознавать.

    И линейка НЕ ОБЯЗАНА быть цельной: у АК она нарисована по-ячеечно, кусками
    ровно в ширину графы (74, 148, 120, 419 px при таблице шириной 1114), так
    что ни один кусок не проходит порог сам по себе. Поэтому по каждому Y
    копится СУММАРНАЯ длина - тот же приём и та же причина, что у вертикалей в
    specification_pdf_to_json; min_width из «какой длины отрезок» становится
    «сколько всего прочерчено на этой высоте».
    """
    from collections import defaultdict
    acc = defaultdict(float)
    for line in page.lines:
        if abs(line["top"] - line["bottom"]) < 1.0:
            acc[round(line["top"])] += abs(line["x1"] - line["x0"])
    for rect in page.rects:
        if rect["height"] < 2.5:
            acc[round(rect["top"])] += rect["width"]

    # Соседние Y - одна и та же линейка: границы соседних ячеек ставятся с
    # разбросом в доли пикселя, а округление до целого разносит их по двум
    # ключам и делит сумму пополам.
    ys = []
    for y in sorted(acc):
        if ys and y - ys[-1] <= 2:
            acc[ys[-1]] += acc[y]
            continue
        ys.append(y)

    ys = [y for y in ys if acc[y] >= min_width]
    return [(a, b) for a, b in zip(ys, ys[1:]) if gap_lo <= b - a <= gap_hi]


def _cells(words, seps, page_width, top, bottom):
    """Тексты ячеек полосы, по границам колонок (центр слова решает)."""
    edges = [0.0] + [float(x) for x in seps] + [float(page_width)]
    row = [w for w in words if top - 0.5 <= w["top"] < bottom - 0.5]
    out = []
    for lo, hi in zip(edges, edges[1:]):
        cell = sorted((w for w in row if lo <= w["xc"] < hi),
                      key=lambda w: (round(w["top"]), w["x0"]))
        out.append(" ".join(w["text"] for w in cell).strip())
    return out


def _map_alt_columns(words, seps, page_width, page_height, patterns):
    """{поле: индекс колонки} по заголовкам над таблицей + низ шапки.

    Для СОПОСТАВЛЕНИЯ заголовков берётся верхняя четверть листа (шапки бывают
    глубокими), а вот низ шапки считается ТОЛЬКО по верхней десятой: в четверть
    листа уже попадают первые строки данных, и низ, посчитанный по ним, съедал
    первые четыре канала каждого перечня (замерено: перечень выходных сигналов
    начинался с 1DO5)."""
    edges = [0.0] + [float(x) for x in seps] + [float(page_width)]
    header_words = [w for w in words if w["top"] < page_height * 0.25]
    header_bottom = 0.0
    mapping = {}
    for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
        col_words = [w for w in header_words if lo <= w["xc"] < hi]
        title = _norm_ws(" ".join(
            w["text"] for w in sorted(col_words, key=lambda w: (w["top"], w["x0"]))))
        if not title:
            continue
        for field, pats in patterns:
            if field in mapping:
                continue
            if any(p in title for p in pats):
                mapping[field] = i
                header_bottom = max(
                    [header_bottom] + [w["bottom"] for w in col_words
                                       if w["top"] < page_height * 0.10])
                break
    return mapping, header_bottom


def _same_layout(page_seps, seps, tol=3.0):
    """Одна ли это таблица: совпадает ли линовка листа с запомненной.

    Раскладка колонок запоминается с листа, где нашлась шапка, и дальше
    применяется к листам-продолжениям (у них шапки нет). Проверять при этом
    ЛИНОВКУ обязательно: часть альбома, нарезанная по наименованию штампа,
    регулярно содержит подшитые в конец листы ЧУЖОЙ таблицы - лист без
    заполненной графы наименования наследует наименование предыдущего.

    Замер: в «Кабельный журнал» ЭОМ так попали листы расчёта электрических
    нагрузок. Их разбирали колонками журнала, и в графу «обозначение кабеля»
    ложились обрывки названий оборудования («Горелочное устройство», «Насос
    воды котлового») - по одному на каждый агрегат. Итог: 18 ложных находок
    «одно обозначение кабеля в двух строках журнала» из 18.
    """
    if not page_seps or not seps or len(page_seps) != len(seps):
        return False
    return all(abs(a - b) <= tol for a, b in zip(page_seps, seps))


def _leftmost_titled_column(words, seps, page_width, page_height):
    """Индекс самой левой колонки с непустым заголовком, иначе None.

    Служит запасным ответом на вопрос «где обозначение кабеля». Подписью эту
    графу не опознать: бюро называют её "Обозначение кабеля, провода" (Енисей),
    "№ кабеля" (ЭОМ) и "SB№ кабеля" (АК), причём у АК перенос режет слово на
    "ка-" и "беля", так что не выживает даже корень. Зато МЕСТО у неё одно и то
    же во всех трёх журналах - крайняя слева графа таблицы; нулевая колонка
    (поле листа до первой вертикали) заголовка не имеет и потому отсеивается.

    Ответ запасной: он берётся, только если подписи не опознали графу, и
    перебить правильное сопоставление не может.
    """
    edges = [0.0] + [float(x) for x in seps] + [float(page_width)]
    header_words = [w for w in words if w["top"] < page_height * 0.25]
    for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
        if any(lo <= w["xc"] < hi for w in header_words):
            return i
    return None


# Содержимое штампа, просочившееся в полосу строки: подписи граф («Дата»,
# «Подп.», «Копировал») и голые даты «04.25». Такая "запись" - не канал и не
# кабель, отбрасываем по значению ключевой ячейки.
_ALT_JUNK_RE = re.compile(
    r"^(изм|лист|№\s*док|подп|дата|формат|копировал|стадия|"
    r"разраб|провер|н\.\s*контр|утв|соглас|гип|гап)(?![а-яёa-z])"
    r"|^[\d.,/\s-]+$", re.I)


def _alt_record(rec_id, page_no, **fields):
    rec = {"id": rec_id, "page": page_no}
    for key in FIELD_ORDER:
        rec[key] = None
    rec["terminal_address"] = None
    rec.update(fields)
    return rec


SIGNAL_COLUMNS = [
    ("kks",                  ["обозначение"]),
    ("connection_address",   ["адрес plc", "номер входа", "номер выхода", "адрес"]),
    ("terminal_type_or_ref", ["тип"]),
    ("note",                 ["описание"]),
]

JOURNAL_COLUMNS = [
    ("cable",   ["обозначение"]),
    ("from",    ["начало", "откуда"]),
    ("to",      ["конец", "куда"]),
    ("segment", ["участок"]),
    ("brand",   ["марка"]),
    ("section", ["сечение", "количество кабелей"]),
    ("length",  ["длина"]),
]


PARAM_COLUMNS = [
    ("position",  ["позиция"]),
    ("note",      ["наименование"]),
    ("sheet_ref", ["лист схемы", "схемы"]),
]

# Род сигнала и его номинал в строке перечня: «СК НР =24В», «СК НЗ =24В».
# Ищется по ВСЕЙ строке, а не по своей графе, сознательно: графы «Вход» и
# «Выход» разбиты на десяток подколонок, шапка над ними набрана ПОВЁРНУТЫМ
# текстом (pdfplumber отдаёт его задом наперёд - «еинавориврезеР»), и
# сопоставлять их по заголовку значит гадать. Само значение при этом
# однозначно: другой такой записи в строке нет.
SIGNAL_SPEC_RE = re.compile(r"(СК\s*[НH][РPЗ3]?)?\s*([=~]\s*\d{1,3}\s*[ВVB])", re.I)

# Шапка таблицы, повторённая на каждом листе, и графы штампа. Подзаголовком
# раздела не являются, но графу «Позиция» тоже оставляют пустой.
_PARAM_HEADER_RE = re.compile(
    r"пределы\s+измерений|шкала\s+прибора|сигнализация\s+вход|примечание|"
    r"разраб|провер|утверд|инв\.|\.внИ|теплотранссервис|"
    r"стадия\s+лист|лист\s+листов", re.I)


def extract_param_list(pdf_path):
    """«Перечень входных/выходных параметров контроля, регулирования, управления».

    Опросная таблица ГОСТ 21.408: № п/п, ПОЗИЦИЯ ПО СХЕМЕ, наименование
    параметра, пределы измерений, функция, сигнализация, вход/выход, класс
    точности, ЛИСТ СХЕМЫ, примечание.

    Ценна она графой «Позиция по схеме»: там стоят позиции приборов (HL101,
    NSF001, PZS PGmax) - тот же ключ, которым подписаны кабели в кабельном
    журнале и приборы на ФСА. Другого документа, связывающего прибор с его
    сигналом и листом схемы, в альбоме нет.

    Строки-подзаголовки («Горелочное устройство», «Насосы воды котлового
    контура HPA001») занимают всю ширину таблицы и позиции не имеют - они
    запоминаются в section, как в extract_signal_list.
    """
    connections, section = [], None
    with pdfplumber.open(pdf_path) as pdf:
        seps = mapping = None
        for page_no, page in enumerate(pdf.pages, start=1):
            _progress.page(page_no, len(pdf.pages),
                           stage="чтение перечня параметров")
            words = _page_words(page)
            page_seps = _vertical_separators(page)
            if page_seps:
                m, header_bottom = _map_alt_columns(
                    words, page_seps, page.width, page.height, PARAM_COLUMNS)
                if "position" in m:
                    seps, mapping = page_seps, m
                else:
                    header_bottom = 40.0
            else:
                header_bottom = 40.0
            if not seps or not mapping:
                continue
            # Лист чужой таблицы, подшитый в этот же документ (см. _same_layout).
            if not _same_layout(page_seps, seps):
                continue
            for top, bottom in _row_bands(page, min_width=250):
                if bottom <= header_bottom + 2:
                    continue
                cells = _cells(words, seps, page.width, top, bottom)
                get = lambda f: (cells[mapping[f]] if f in mapping        # noqa: E731
                                 and mapping[f] < len(cells) else "")
                position = clean(get("position"))
                if not position:
                    # Строка без позиции - подзаголовок раздела: он один на всю
                    # ширину таблицы, поэтому и опознаётся по пустой графе.
                    # Нулевая колонка (№ п/п) в набор НЕ входит: в неё стекает
                    # повёрнутый текст поля листа - обозначение документа задом
                    # наперёд («1-1-2В1В-КА-150-42»), и оно приклеивалось к
                    # названию раздела.
                    text = clean(" ".join(c for c in cells[1:] if c))
                    if (text and len(text) < 120 and not _ALT_JUNK_RE.match(text)
                            and not _PARAM_HEADER_RE.search(text)):
                        section = text
                    continue
                if _ALT_JUNK_RE.match(position):
                    continue
                spec = SIGNAL_SPEC_RE.search(" ".join(cells))
                connections.append(_alt_record(
                    len(connections) + 1, page_no,
                    kks=position,
                    note=clean(get("note")),
                    section=section,
                    sheet_ref=clean(get("sheet_ref")) or None,
                    signal_spec=clean(spec.group(0)) if spec else None))
    return connections


def extract_signal_list(pdf_path):
    """«Перечень входных/выходных сигналов»: № п/п, обозначение сигнала,
    адрес канала ПЛК (1DI1/1DO1), тип данных, описание. Подзаголовки разделов
    («Дискретные сигналы») запоминаются в поле section."""
    connections, section = [], None
    with pdfplumber.open(pdf_path) as pdf:
        seps = mapping = None
        for page_no, page in enumerate(pdf.pages, start=1):
            _progress.page(page_no, len(pdf.pages), stage="чтение перечня сигналов")
            words = _page_words(page)
            page_seps = _vertical_separators(page)
            if page_seps:
                m, header_bottom = _map_alt_columns(
                    words, page_seps, page.width, page.height, SIGNAL_COLUMNS)
                if "connection_address" in m:
                    seps, mapping = page_seps, m
                else:
                    header_bottom = 40.0
            else:
                header_bottom = 40.0
            if not seps or not mapping:
                continue
            # Лист чужой таблицы, подшитый в этот же документ (см. _same_layout).
            if not _same_layout(page_seps, seps):
                continue
            for top, bottom in _row_bands(page, min_width=250):
                if bottom <= header_bottom + 2:
                    continue
                cells = _cells(words, seps, page.width, top, bottom)
                get = lambda f: (cells[mapping[f]] if f in mapping
                                 and mapping[f] < len(cells) else "")  # noqa: E731
                addr = clean(get("connection_address"))
                tag = clean(get("kks"))
                if not addr and not tag:
                    continue
                if addr and _ALT_JUNK_RE.match(addr):
                    continue        # графы штампа/даты, попавшие в полосу
                if not addr and tag:
                    if _ALT_JUNK_RE.match(tag):
                        continue
                    # строка-подзаголовок раздела («Дискретные сигналы»)
                    if not any(ch.isdigit() for ch in tag):
                        section = tag
                        continue
                connections.append(_alt_record(
                    len(connections) + 1, page_no,
                    kks=tag, connection_address=addr,
                    terminal_type_or_ref=clean(get("terminal_type_or_ref")),
                    note=clean(get("note")), section=section))
    return connections


def extract_cable_journal(pdf_path):
    """«Кабельный журнал»: обозначение кабеля, откуда и куда он идёт, марка,
    сечение и длина по проекту. Поля начала/конца и марки кладутся в
    отдельные ключи (from_point/to_point/...) - у ГОСТ-таблицы подключений
    таких понятий нет, и втискивать их в чужие колонки значило бы врать."""
    connections = []
    with pdfplumber.open(pdf_path) as pdf:
        seps = mapping = None
        for page_no, page in enumerate(pdf.pages, start=1):
            _progress.page(page_no, len(pdf.pages), stage="чтение кабельного журнала")
            words = _page_words(page)
            page_seps = _vertical_separators(page)
            if page_seps:
                m, header_bottom = _map_alt_columns(
                    words, page_seps, page.width, page.height, JOURNAL_COLUMNS)
                if "cable" not in m and ("from" in m or "to" in m):
                    first = _leftmost_titled_column(
                        words, page_seps, page.width, page.height)
                    if first is not None and first not in m.values():
                        m["cable"] = first
                if "cable" in m and ("from" in m or "to" in m):
                    seps, mapping = page_seps, m
                else:
                    header_bottom = 40.0
            else:
                header_bottom = 40.0
            if not seps or not mapping:
                continue
            # Лист чужой таблицы, подшитый в этот же документ (см. _same_layout).
            if not _same_layout(page_seps, seps):
                continue
            for top, bottom in _row_bands(page, min_width=600):
                if bottom <= header_bottom + 2:
                    continue
                cells = _cells(words, seps, page.width, top, bottom)
                get = lambda f: (cells[mapping[f]] if f in mapping
                                 and mapping[f] < len(cells) else "")  # noqa: E731
                cable = clean(get("cable"))
                src, dst = clean(get("from")), clean(get("to"))
                if not cable or (not src and not dst):
                    continue        # штамп и подзаголовки: обозначения там нет
                if _ALT_JUNK_RE.match(cable):
                    continue
                connections.append(_alt_record(
                    len(connections) + 1, page_no,
                    cable_harness=cable,
                    note=f"{src or '?'} -> {dst or '?'}",
                    from_point=src, to_point=dst,
                    segment=clean(get("segment")),
                    cable_brand=clean(get("brand")),
                    cable_section=clean(get("section")),
                    cable_length=clean(get("length"))))
    return connections


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
    table_kind = detect_table_kind(pdf_path)

    if table_kind == "signal_list":
        connections = extract_signal_list(pdf_path)
    elif table_kind == "param_list":
        connections = extract_param_list(pdf_path)
    elif table_kind == "cable_journal":
        connections = extract_cable_journal(pdf_path)
    else:
        rows = extract_pdf_to_dicts(pdf_path)
        connections = build_connections(rows, "Лист" if rows and "Лист" in rows[0]
                                        else None)
    stats = build_statistics(connections, True)
    stats["table_kind"] = table_kind

    metadata = default_metadata()
    metadata["source_file"] = str(pdf_path)
    metadata["table_kind"] = table_kind

    if meta_path:
        with open(meta_path, encoding="utf-8") as f:
            metadata.update(json.load(f))

    if not metadata.get("total_sheets") and stats["total_pages"]:
        metadata["total_sheets"] = stats["total_pages"]

    revision_history = []
    if revisions_path:
        with open(revisions_path, encoding="utf-8") as f:
            revision_history = json.load(f)

    legend = build_column_legend(stats)
    if table_kind == "signal_list":
        legend["connection_address"] = {
            "ru": "Адрес/номер канала ПЛК", "en": "PLC channel address (1DI1, 1DO5...)",
            "populated": True}
        legend["section"] = {"ru": "Раздел перечня (Дискретные/Аналоговые сигналы)",
                             "en": "List section", "populated": True}
        notes = [
            "Это ПЕРЕЧЕНЬ СИГНАЛОВ ПЛК, а не таблица подключений: каждая запись - "
            "один канал контроллера (connection_address), его тип данных "
            "(terminal_type_or_ref) и назначение (note). Клеммников и штифтов в "
            "таком документе нет по построению - их пустота не ошибка.",
            "Полезная сверка: каналы из перечня должны существовать на "
            "принципиальной схеме (io_channels в graph.json схемы) и наоборот.",
        ]
    elif table_kind == "cable_journal":
        for key, ru in (("from_point", "Начало трассы (откуда идёт кабель)"),
                        ("to_point", "Конец трассы (куда приходит кабель)"),
                        ("segment", "Участок трассы"),
                        ("cable_brand", "Марка кабеля по проекту"),
                        ("cable_section", "Число и сечение жил"),
                        ("cable_length", "Длина по проекту")):
            legend[key] = {"ru": ru, "en": None, "populated": True}
        notes = [
            "Это КАБЕЛЬНЫЙ ЖУРНАЛ, а не таблица подключений: каждая запись - один "
            "кабель (cable_harness), его начало и конец (from_point/to_point), марка "
            "и сечение. Клеммников, штифтов и KKS здесь нет по построению.",
            "Полезная сверка: обозначения шкафов в начале/конце трассы против "
            "комплекта документов; марка и сечение - против однолинейных схем.",
        ]
    else:
        notes = build_domain_notes(stats)

    return {
        "document_metadata": metadata,
        "revision_history": revision_history,
        "column_legend": legend,
        "domain_notes_for_analysis": notes,
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
        "table_kind": stats.get("table_kind", "gost_connections"),
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
    