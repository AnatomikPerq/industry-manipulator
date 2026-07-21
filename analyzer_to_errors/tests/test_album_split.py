"""
ЗОЛОТОЙ БАЗЛАЙН НАРЕЗКИ АЛЬБОМА на двух реальных проектах.

Фикстура - НАИМЕНОВАНИЯ ЛИСТОВ, снятые из штампов двух настоящих альбомов
(184 и 237 листов, tests/fixtures/album_titles.json, 8 КБ). Самих PDF в
репозитории нет и быть не может - это документация заказчика, - но вся логика
нарезки работает именно со списком наименований: границы документа, тип части и
обозначение шкафа выводятся только из них. Чтение штампов (единственное, что
требует PDF и fitz) остаётся за пределами теста.

ЧТО ЭТИМ ЗАЩИЩЕНО. Отказы нарезки почти все тихие и дорогие:
  * часть уехала не в тот шкаф - сверка идёт между документами РАЗНЫХ щитов,
    где обозначения законно пересекаются, и даёт вал ложных «разный артикул»;
  * шкаф не опознан - документы одного щита разъехались по двум связкам и не
    сверились вовсе, без единого сообщения;
  * граница документа определена по номеру листа или форме штампа (оба сигнала
    замерены и отвергнуты - см. шапку full_project.py) - альбом рассыпается на
    полсотни однолистовых «документов».
"""

import json
from collections import Counter

import pytest

import full_project as fp
from conftest import FIXTURES

TITLES = json.loads((FIXTURES / "album_titles.json").read_text(encoding="utf-8"))

# Замерено на корпусе (см. CLAUDE.md): у Енисея один шкаф ЛСУ КОС и общие
# документы на весь объект, у ЭОМ - тринадцать щитов плюс общие.
EXPECTED = {
    "енисей": {
        "sheets": 184,
        "parts": 9,
        "types": {"scheme": 2, "netlist": 3, "spec": 2, "assembly": 1, None: 1},
        "cabinets": {"КОС", fp.COMMON_BUNDLE_DIR},
    },
    "эом": {
        "sheets": 237,
        "parts": 48,
        "types": {"scheme": 31, "assembly": 9, "spec": 1, None: 7},
        "cabinets": {"БУЗО", "ВРУ", "ПЭСПЗ", "РП1", "РП2", "ШУПЧ1", "ШУПЧ2",
                     "ШУПЧ3", "ШУПЧ4", "ЩАО", "ЩОВ", "ЩРО", "ЩС1", "ЩС2",
                     fp.COMMON_BUNDLE_DIR},
    },
}


def parts_of(album):
    return fp.split_into_parts(TITLES[album])


@pytest.mark.parametrize("album", sorted(EXPECTED))
def test_sheet_count(album):
    assert len(TITLES[album]) == EXPECTED[album]["sheets"]


@pytest.mark.parametrize("album", sorted(EXPECTED))
def test_part_count(album):
    """Число документов в альбоме. Резкий рост означает, что граница снова
    ловится не по наименованию: на ЭОМ форма штампа давала 48 -> 94 части,
    половина по одной странице."""
    assert len(parts_of(album)) == EXPECTED[album]["parts"]


@pytest.mark.parametrize("album", sorted(EXPECTED))
def test_part_types(album):
    """Раскладка частей по типам. Сюда попадает и число НЕОПОЗНАННЫХ (None):
    их рост - это документы, которые перестали анализироваться молча."""
    got = Counter(fp.classify(p["title"])[0] for p in parts_of(album))
    assert dict(got) == EXPECTED[album]["types"]


@pytest.mark.parametrize("album", sorted(EXPECTED))
def test_cabinets(album):
    """Набор связок-шкафов. Лишний шкаф - это разъехавшийся комплект (часть
    документов щита ушла в собственную связку и ни с чем не сверится),
    пропавший - наоборот, слипшиеся щиты."""
    cabinets = set()
    prev = None
    for part in parts_of(album):
        doc_type, _ = fp.classify(part["title"])
        if doc_type is None:
            continue
        cabinet = fp.detect_cabinet(part["title"])
        # то же наследование, что в split_full_project: лист-продолжение
        # чертежа своего шкафа в наименовании не называет
        if cabinet is None and doc_type == "assembly" and prev:
            cabinet = prev
        cabinet = cabinet or fp.COMMON_BUNDLE_DIR
        prev = cabinet if cabinet != fp.COMMON_BUNDLE_DIR else prev
        cabinets.add(cabinet)
    assert cabinets == EXPECTED[album]["cabinets"]


@pytest.mark.parametrize("album", sorted(EXPECTED))
def test_no_numeric_junk_titles(album):
    """Наименование, состоящее из одних цифр и разделителей, - это не документ,
    а значение из соседней графы штампа (масштаб, формат, номер листа)."""
    import re
    junk = [p["title"] for p in parts_of(album)
            if re.fullmatch(r"[\d.,:/\s×xXхХ-]+", p["title"])]
    assert junk == []


def test_object_wide_spec_goes_to_common_bundle():
    """Спецификация НА ВЕСЬ ОБЪЕКТ обязана уехать в общую связку.

    Без неё у связок-шкафов спецификации нет вообще, и вся сверка «нарисовано,
    но не заказано» молча не выполняется - ровно ради этого в main.py заведён
    _lend_project_wide_docs, который одалживает её каждому шкафу.

    Обеих спецификаций Енисея тест НЕ требует безшкафными, и это не поблажка:
    в альбоме их две - общая («Спецификация оборудования, изделий и
    материалов») и на конкретный щит («Спецификация шкафа ЛСУ КОС»). Вторая
    шкаф называет ЗАКОННО и должна лежать именно в его связке.
    """
    specs = [p["title"] for p in parts_of("енисей")
             if fp.classify(p["title"])[0] == "spec"]
    assert len(specs) == 2, specs
    without_cabinet = [t for t in specs if fp.detect_cabinet(t) is None]
    assert len(without_cabinet) == 1, specs
