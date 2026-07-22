"""
ЗОЛОТОЙ БАЗЛАЙН НАРЕЗКИ АЛЬБОМА на трёх реальных проектах.

Фикстура - НАИМЕНОВАНИЯ ЛИСТОВ, снятые из штампов трёх настоящих альбомов
(184, 237 и 309 листов, tests/fixtures/album_titles.json). Самих PDF в
репозитории нет и быть не может - это документация заказчика, - но вся логика
нарезки работает именно со списком наименований: границы документа, тип части и
обозначение шкафа выводятся только из них. Чтение штампов (единственное, что
требует PDF и fitz) остаётся за пределами теста; пересобирается фикстура
отдельной командой `python tests/record_album_titles.py`.

Третий альбом (АК) добавлен в V2.0 и охраняет ровно то, на чём чтение штампов
до него отказывало молча: у него ЧЕТВЁРТАЯ форма основной надписи - штамп с
расширенной левой частью для перечней, где меток "Изм./Кол.уч./Дата" на листе
два блока, и ловился не тот. 27 листов перечня входных/выходных параметров
пропадали целиком.

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
        # 49, а не 48: на л.222 лежит «Кабельный журнал», и до V2.0 его
        # наименование не читалось - лист наследовал наименование предыдущего и
        # весь журнал молча уезжал внутрь спецификации.
        "parts": 49,
        "types": {"scheme": 31, "assembly": 9, "spec": 1, "netlist": 1, None: 7},
        "cabinets": {"БУЗО", "ВРУ", "ПЭСПЗ", "РП1", "РП2", "ШУПЧ1", "ШУПЧ2",
                     "ШУПЧ3", "ШУПЧ4", "ЩАО", "ЩОВ", "ЩРО", "ЩС1", "ЩС2",
                     fp.COMMON_BUNDLE_DIR},
    },
    "ак": {
        "sheets": 309,
        "parts": 67,
        # netlist: 3 - кабельный журнал и ДВА перечня входных/выходных
        # параметров (27 листов, до V2.0 пропадали целиком).
        # functional: 5 - это пять ФСА (тепломеханика, газоснабжение, жидкое
        # топливо, отопление-вентиляция, парк хранения). «Схема структурная»
        # остаётся scheme и это верно: она блок-схема связей щитов, а не ФСА.
        # None: 35 - 34 сознательных пропуска (планы расположения, установочные
        # чертежи, отборные устройства) плюс ОДИН неопознанный: вендорский лист
        # «РИЗУР-2030 ... Чертёж общего вида ООО "НПО РИЗУР"». Он и должен
        # остаться неопознанным - это паспорт покупного изделия, а не документ
        # комплекта, - но пропуск его СОЗНАТЕЛЕН только на словах, поэтому
        # число здесь и зафиксировано: вырастет - значит анализ снова начал
        # молча терять документы.
        "types": {"scheme": 15, "functional": 5, "assembly": 5, "spec": 4,
                  "netlist": 3, None: 35},
        # ЩА здесь НЕТ сознательно: «Щит общекотельной автоматики ША (ШУК)
        # OS001» и «... (ЩА) OS001» - один щит, сведённый по функциональному
        # коду (см. cabinet_aliases).
        "cabinets": {"ШУК", "ЩАК1", "ЩАК2", "ЩАК3", "ЩРТХ", "ЩСКЗ", "ЩУУТЭ",
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
    parts = parts_of(album)
    aliases = fp.cabinet_aliases(parts)
    cabinets = set()
    prev = None
    for part in parts:
        doc_type, _ = fp.classify(part["title"])
        if doc_type is None:
            continue
        cabinet = fp.detect_cabinet(part["title"])
        # то же сведение обозначений одного щита, что в split_full_project
        if cabinet:
            cabinet = aliases.get(cabinet, cabinet)
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


def test_cabinet_aliases_join_one_cabinet_written_two_ways():
    """«ША (ШУК) OS001» и «(ЩА) OS001» - один щит, и сводить их обязательно.

    Иначе принципиальная схема щита общекотельной автоматики лежит в одной
    связке, а его же схема внешних проводок - в другой, и они не сверятся друг
    с другом молча.
    """
    aliases = fp.cabinet_aliases(parts_of("ак"))
    assert aliases == {"ЩА": "ШУК"}


@pytest.mark.parametrize("album", ["эом", "енисей"])
def test_cabinet_aliases_do_not_merge_distinct_cabinets(album):
    """А вот ЩАК1/ЩАК2/ЩАК3 сводить НЕЛЬЗЯ - это разные щиты.

    Ровно поэтому ключом сведения взят функциональный код (CC001/CC002/CC003
    у них разные), а не описательная часть наименования, одинаковая у всех
    трёх («Щит автоматики котла»). Слив по описанию дал бы вал ложных «разный
    артикул у одного обозначения» - то самое, ради чего связка делится по
    шкафам. На альбомах без кодов механизм обязан молчать.
    """
    assert fp.cabinet_aliases(parts_of(album)) == {}


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
