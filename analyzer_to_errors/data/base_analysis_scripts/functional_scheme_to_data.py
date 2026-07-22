"""
ФУНКЦИОНАЛЬНАЯ СХЕМА АВТОМАТИЗАЦИИ (ФСА, ГОСТ 21.408): позиции приборов.

ЗАЧЕМ ОТДЕЛЬНЫЙ ТИП. ФСА - не электрическая схема. На ней нарисован
технологический процесс: трубопроводы, аппараты, а приборы обозначены кружками
по ГОСТ 21.208. Проводов, цепей, клемм и катушек реле на ней нет вовсе, и
парсеру принципиальных схем там делать нечего. До V2.0 ФСА попадала именно к
нему - по наименованию она подходила под общее «функциональная схема» в
TITLE_TO_TYPE, - и разбиралась как Э3.

Вреда от этого было два. Первый: время. Плотность ФСА в разы выше настоящей
схемы - замер по «24-051-АК»: 70095, 15042 и 14974 примитива на листах 27, 29 и
31 против 2799 и 3046 на листах принципиальной схемы того же альбома (и против
4885 у самого густого настоящего листа корпуса). Второй, хуже: порог
DENSE_GRAPHIC_LINES=20000 проходит ПОСРЕДИ этой группы, поэтому лист 27
объявлялся картинкой и пропускался, а листы 30 и 32 (8114 и 9195) честно
разбирались как схема - в пределах ОДНОГО документа судьба листа решалась
жребием.

ЧТО ИЗВЛЕКАЕТСЯ. Только позиции приборов и только из ТЕКСТА. Геометрию не
трогаем совсем - ровно по той же причине, по которой её не трогает
assembly_drawing_to_data.py: распознавать кружки ГОСТ 21.208 значит писать
машинное зрение ради данных, которые полностью лежат в текстовом слое.

Позиция - ключ, которым ФСА сшивается с остальным альбомом: в кабельном журнале
АК графа «SB№ кабеля» заполнена ровно этими позициями (PT206, TT309, PS215), а в
перечне входных/выходных параметров есть графа «Позиция по схеме». Ни один
другой документ комплекта такого ключа не даёт.

ПОЗИЦИЯ ПИШЕТСЯ ДВУМЯ СПОСОБАМИ, и оба замерены на «24-051-АК»:
  * одним куском - "FT-401", "PG2007", "ASA601" (163 штуки на л.27);
  * буквами НАД цифрами - две надписи в одном кружке (76 штук на л.31,
    "MOV"+"1201"). Сшиваем их по геометрии, как schematic_rules сшивает
    разорванные обозначения в _adjacent_fragment, и по той же причине: разрыв
    сделал экспорт CAD, а не проектировщик.
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
    """Соседний скрипт по ПУТИ, а не по имени модуля.

    Папка base_analysis_scripts копируется в каждую сессию и на sys.path не
    попадает, поэтому обычный import соседа работает только случайно. Идиома
    та же, что в specification_pdf_to_json.py.
    """
    spec = importlib.util.spec_from_file_location(mod_name, os.path.join(_HERE, name))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# Починка кириллицы в CAD-экспорте: она уже написана и выверена для схем,
# второй раз её писать незачем (второй раз неизбежно разъедется с первым).
_schem = _sibling("schematic_diagram_to_data.py", "_functional_fontfix")
_progress = _sibling("progress.py", "_functional_progress")

analyze_fonts = _schem.analyze_fonts
apply_font_fix = _schem.apply_font_fix

# Позиция целым куском: буквенный код по ГОСТ 21.208 и номер контура.
WHOLE_TAG_RE = re.compile(r"^([A-Z]{1,5})-?(\d{2,4})$")
LETTERS_RE = re.compile(r"^[A-Z]{1,5}$")
DIGITS_RE = re.compile(r"^\d{2,4}$")

# Буквенные коды, похожие на прибор по форме и НЕ являющиеся им. Список
# короткий и весь замерен на «24-051-АК»: длинного перечня исключений тут быть
# не должно, иначе он превратится в фильтр «оставить только то, что мы уже
# видели», и первый же прибор нового бюро потеряется молча.
NOT_INSTRUMENT = {
    # Условный диаметр трубопровода. По форме неотличим от прибора ("DN100"),
    # по смыслу - размер трубы; на одном листе 27 их восемь разных.
    "DN",
    # Номинальное давление и типоразмер по тому же ГОСТ на трубопроводную
    # арматуру, стоят рядом с DN.
    "PN", "DU",
}

# Кружок прибора рисуется диаметром примерно в две строки текста, поэтому
# допуски на сшивку «буквы над цифрами» задаются В ВЫСОТАХ НАДПИСИ, а не в
# пунктах: форматы листов альбома гуляют от A4 до 5054x2384, и любой допуск,
# подобранный на одном, промахивается на другом.
PAIR_DX_IN_HEIGHTS = 1.2
PAIR_DY_IN_HEIGHTS = 1.4


def _spans(pdf_path):
    """Текстовые надписи всех листов: текст, рамка, номер листа."""
    font_map = analyze_fonts(pdf_path)
    doc = fitz.open(pdf_path)
    pages = []
    try:
        for i, page in enumerate(doc):
            _progress.page(i + 1, len(doc), stage="чтение функциональной схемы")
            out = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = apply_font_fix(span.get("text", ""),
                                              font_map.get(span.get("font", ""))).strip()
                        if text:
                            out.append({"text": text, "bbox": list(span["bbox"])})
            pages.append(out)
    finally:
        doc.close()
    return pages


def _is_instrument(letters):
    return letters not in NOT_INSTRUMENT


def find_instruments(spans, page_no):
    """Позиции приборов на одном листе."""
    found = []
    for span in spans:
        m = WHOLE_TAG_RE.match(span["text"])
        if m and _is_instrument(m.group(1)):
            found.append({"tag": m.group(1) + m.group(2),
                          "letters": m.group(1), "number": m.group(2),
                          "sheet": page_no, "raw": span["text"], "split": False})

    # Буквы над цифрами в одном кружке.
    letters = [s for s in spans if LETTERS_RE.match(s["text"])]
    digits = [s for s in spans if DIGITS_RE.match(s["text"])]
    for lo in letters:
        if not _is_instrument(lo["text"]):
            continue
        height = max(lo["bbox"][3] - lo["bbox"][1], 1.0)
        lx = (lo["bbox"][0] + lo["bbox"][2]) / 2
        best = None
        for hi in digits:
            dx = abs((hi["bbox"][0] + hi["bbox"][2]) / 2 - lx)
            dy = hi["bbox"][1] - lo["bbox"][3]
            if dx <= height * PAIR_DX_IN_HEIGHTS and -height <= dy <= height * PAIR_DY_IN_HEIGHTS:
                if best is None or dy < best[0]:
                    best = (dy, hi)
        if best:
            found.append({"tag": lo["text"] + best[1]["text"],
                          "letters": lo["text"], "number": best[1]["text"],
                          "sheet": page_no,
                          "raw": f'{lo["text"]} {best[1]["text"]}', "split": True})
    return found


def extract_to_dir(pdf_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    pages = _spans(pdf_path)

    instruments = []
    for page_no, spans in enumerate(pages, start=1):
        instruments.extend(find_instruments(spans, page_no))

    by_tag = defaultdict(list)
    for item in instruments:
        by_tag[item["tag"]].append(item)

    index = {
        tag: {"count": len(items),
              "sheets": sorted({i["sheet"] for i in items}),
              "letters": items[0]["letters"], "number": items[0]["number"]}
        for tag, items in sorted(by_tag.items())
    }

    doc = {
        "document_metadata": {"source_file": os.path.basename(pdf_path),
                              "total_sheets": len(pages)},
        "column_legend": {
            "tag": "позиция прибора по ГОСТ 21.208 без дефиса (PT206, TE303)",
            "letters": "буквенный код: первая буква - измеряемая величина, "
                       "остальные - функция прибора",
            "number": "номер контура регулирования",
            "split": "true - позиция была подписана двумя надписями (буквы над "
                     "цифрами внутри кружка) и сшита при разборе",
        },
        "domain_notes_for_analysis": (
            "1) ФСА не электрическая схема: проводов, клемм и катушек реле на ней "
            "нет, искать их здесь бессмысленно. "
            "2) Позиция прибора - ключ сшивки с кабельным журналом (графа "
            "«№ кабеля» заполнена позициями) и с перечнем входных/выходных "
            "параметров (графа «Позиция по схеме»). "
            "3) Отсутствие позиции на ФСА при наличии её в журнале - вопрос "
            "инженеру, а не утверждение об ошибке: ФСА бывает разбита по "
            "технологическим узлам, и прибор может быть на другом листе альбома."
        ),
        "instruments": instruments,
        "index": index,
        "all_texts": sorted({s["text"] for p in pages for s in p}),
    }

    path = os.path.join(out_dir, "functional.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    letters = Counter(i["letters"] for i in instruments)
    stats = {
        "total_pages": len(pages),
        "functional_instruments": len(instruments),
        "functional_unique_tags": len(index),
        "functional_split_tags": sum(1 for i in instruments if i["split"]),
        "functional_letter_codes": len(letters),
    }
    return [path], stats
