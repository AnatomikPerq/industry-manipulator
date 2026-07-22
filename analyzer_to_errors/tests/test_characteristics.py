"""
РАЗНЫЙ НОМИНАЛ У ОДНОГО ЭЛЕМЕНТА (24 В в спецификации против 36 В на чертеже).

Проверка отвечает на вопрос заказчика «а найдёт ли он, если в одном месте
20 вольт, а в другом он же на 35». Ошибка эта дорогая и молчаливая: артикулы
могут совпадать, обозначение на месте, все прочие правила довольны - а закупят
и смонтируют разное.

Здесь охраняются две вещи, обе невидимые в отчёте:
  * приведение к базовой единице (6kA и 6A - РАЗНЫЕ величины, 0,4кВ и 400В -
    одна). Без него отключающая способность автомата совпадала бы с номиналом;
  * срабатывание ТОЛЬКО на непересекающихся множествах. У реле катушка 24 В, а
    контакты 250 В, и в спецификации законно написаны оба - находки быть не
    должно.
"""

import pytest

from conftest import load_script

B = load_script("bundle_rules.py")


# ---------------------------------------------------------------- разбор

@pytest.mark.parametrize("text, expected", [
    ("Блок питания с функцией UPS и зарядного устройства, 24В, 10А, 240Вт",
     {"напряжение": {24.0}, "ток": {10.0}, "мощность": {240.0}}),
    ("Сигнальная лампа ND16-22D/2, белая - 230V AC/DC", {"напряжение": {230.0}}),
    # "24VDC" без пробела: род тока обязан входить в шаблон, иначе после "V"
    # идёт буква и совпадения нет вовсе.
    ("Реле 24VDC", {"напряжение": {24.0}}),
    ("Батарея на DIN-рейку 24В, 9.0 Ач", {"напряжение": {24.0}, "ёмкость": {9.0}}),
])
def test_characteristics_are_read(text, expected):
    assert B.characteristics(text) == expected


def test_kilo_prefix_is_a_different_value():
    """6kA (отключающая способность) не равно 6A (номинал)."""
    assert B.characteristics("OptiDin BM63 - 1P-C4 - 6kA - УХЛ3") == {"ток": {6000.0}}


def test_volts_are_comparable_across_units():
    """0,4 кВ и 400 В - одно и то же, и правило обязано это видеть."""
    assert (B.characteristics("0,4кВ")["напряжение"]
            == B.characteristics("400 В")["напряжение"])


@pytest.mark.parametrize("text", [
    "Корпус навесной ST c М/П ВхШмГ 600x600x250 мм, IP66",   # габариты - не номинал
    "Кабельный ввод, пластик V0 UL94, IP65, 25 отверстий",
    "Кронштейн прямой TST 30",
    "Автоматический выключатель ND16-22DS/2",
])
def test_non_characteristics_are_not_read(text):
    """Габариты, степень защиты и номер изделия номиналами не являются.

    "600x600x250 мм" особенно: миллиметр сознательно НЕ в списке единиц, иначе
    габарит корпуса стал бы «характеристикой» у каждой второй строки.
    """
    assert B.characteristics(text) == {}


# ---------------------------------------------------------------- правило

def bundle(spec_items, asm_elements):
    docs = {"spec": {"name": "СО", "source": "co.xlsx", "data_dir": "-"},
            "assembly": {"name": "СБ", "source": "sb.pdf", "data_dir": "-"}}
    loaded = {
        "spec": {"designation": None, "items": spec_items, "cell_errors": [],
                 "designator_index": {}, "statistics": {}},
        "assembly": {"designation": None, "elements": asm_elements,
                     "designator_index": {}, "all_texts": [], "statistics": {}},
    }
    return B.rule_characteristic_mismatch("связка", docs, loaded)


def spec_item(row, designators, name):
    return {"row": row, "designators": designators, "name": name, "code": None}


def asm_element(designator, article, label_text="", sheet=1):
    return {"designator": designator, "article": article, "sheet": sheet,
            "label_text": label_text, "pair_source": "block"}


def test_finds_different_voltage():
    """Ровно случай заказчика: в спецификации 24 В, на чертеже 36 В."""
    found = bundle([spec_item(10, ["G1"], "Блок питания 24В, 10А")],
                   [asm_element("G1", "PS-36V", "Блок питания 36В")])
    assert len(found) == 1
    assert found[0]["kind"] == "MISMATCH" and found[0]["severity"] == "high"
    assert "напряжение" in found[0]["type"]
    assert {r["found"] for r in found[0]["refs"]} == {"24 В", "36 В"}


def test_silent_when_values_agree():
    found = bundle([spec_item(10, ["G1"], "Блок питания 24В, 10А")],
                   [asm_element("G1", "PS-24V", "Блок питания 24В")])
    assert found == []


def test_silent_when_sets_intersect():
    """У реле катушка 24 В и контакты 250 В - оба номинала законны.

    Требование «значения обязаны совпадать целиком» дало бы находку на каждом
    реле связки: чертёж подписывает один номинал, спецификация перечисляет оба.
    """
    found = bundle([spec_item(11, ["K1"], "Реле 24В, контакты 250В 5А")],
                   [asm_element("K1", "RL-24", "Реле 24В")])
    assert found == []


def test_silent_on_different_kinds():
    """Ампер с вольтом не сравнивается: это разные величины, а не расхождение."""
    found = bundle([spec_item(12, ["QF1"], "Выключатель 40А")],
                   [asm_element("QF1", "NXB-63", "230В")])
    assert found == []


def test_mass_caption_on_the_drawing_is_not_a_characteristic():
    """Номинал на ТИПОВОЙ КАРТИНКЕ изделия - подпись, а не характеристика.

    Замер на КОС: '230VAC' спарен с 630 обозначениями и до этого фильтра давал
    320 ложных «разный артикул» из 322. Здесь он дал бы столько же ложных
    «разный номинал».
    """
    elements = [asm_element(f"K{i}", "230VAC", "230VAC")
                for i in range(B.MASS_CAPTION_MIN_DESIGNATORS + 5)]
    items = [spec_item(20 + i, [f"K{i}"], "Реле 24В")
             for i in range(B.MASS_CAPTION_MIN_DESIGNATORS + 5)]
    assert bundle(items, elements) == []


def test_nearest_pairing_is_not_used():
    """Подпись, притянутая по радиусу (pair_source='nearest'), не доказательство.

    На ШУ-ТМ таких пар 3266 из 3277, и артикул в них регулярно взят у соседа -
    номинал будет взят у соседа ровно так же.
    """
    el = asm_element("G1", "PS-36V", "Блок питания 36В")
    el["pair_source"] = "nearest"
    assert bundle([spec_item(10, ["G1"], "Блок питания 24В")], [el]) == []


def test_project_wide_spec_is_skipped():
    """Спецификация на весь объект описывает изделия чужих шкафов.

    Обозначения в ней пересекаются с чужими (1QF1 есть и в ЩС1, и в ШУПЧ1), и
    сравнивать их номиналы значит сравнивать разные аппараты.
    """
    docs = {"spec": {"name": "СО", "source": "co.pdf", "data_dir": "-",
                     "project_wide": True},
            "assembly": {"name": "СБ", "source": "sb.pdf", "data_dir": "-"}}
    loaded = {
        "spec": {"designation": None, "items": [spec_item(10, ["G1"], "24В")],
                 "cell_errors": [], "designator_index": {}, "statistics": {}},
        "assembly": {"designation": None,
                     "elements": [asm_element("G1", "PS-36V", "36В")],
                     "designator_index": {}, "all_texts": [], "statistics": {}},
    }
    assert B.rule_characteristic_mismatch("связка", docs, loaded) == []
