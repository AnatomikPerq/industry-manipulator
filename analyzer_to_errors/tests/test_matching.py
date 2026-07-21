"""
Ключи сопоставления: нормализация обозначений и артикулов, подпись находки,
фильтр заранее известных ошибок.

Всё это - функции, ошибка в которых НЕ ВИДНА в отчёте. Сверка просто перестаёт
находить (омоглиф не свёрнут - 'ХТ01' и 'XT01' стали разными изделиями) или
начинает находить лишнее (подпись вывода принята за артикул). Ни то, ни другое
не падает и ни на что не жалуется.
"""

import pytest

from conftest import load_script

bundle_rules = load_script("bundle_rules.py")
spec_parser = load_script("specification_to_json.py")

import main as pipeline  # noqa: E402
import known_filter      # noqa: E402


# ------------------------------------------------------------ обозначения

def test_homoglyphs_folded():
    """'ХТ01' кириллицей и 'XT01' латиницей выглядят ОДИНАКОВО, но это разные
    строки. Без сворачивания сверка дала бы поток ложных «элемент не найден»."""
    assert bundle_rules.norm("ХТ01") == bundle_rules.norm("XT01")
    assert bundle_rules.norm("КМ1") == bundle_rules.norm("KM1")


def test_leading_zeros_folded_in_designators():
    """Спецификация пишет 'XA1', сборочный чертёж - 'XA001': это одна и та же
    колодка. Без сворачивания нулей связка ША1 давала 19 ложных находок."""
    assert bundle_rules.norm_designator("XA1") == bundle_rules.norm_designator("XA001")
    assert bundle_rules.norm_designator("1KL1") == bundle_rules.norm_designator("01KL01")


def test_leading_zeros_kept_in_articles():
    """У АРТИКУЛА каждая цифра значащая: '8000099046' и '800099046' - разные
    изделия, и сворачивать нули в них нельзя."""
    assert bundle_rules.norm("8000099046") != bundle_rules.norm("800099046")


def test_designator_tokens_split_from_label():
    """На чертеже в одном текстовом блоке лежит несколько обозначений разом:
    'CB-10L, FU1'. Сравнение с блоком целиком не совпало бы никогда."""
    tokens = bundle_rules.text_tokens(["CB-10L, FU1", "XA019,"])
    assert "FU1" in tokens and "XA19" in tokens


# ------------------------------------------------------------ артикулы

@pytest.mark.parametrize("token", ["1/L1", "2/T1", "13NO", "22NC", "A1", "A2",
                                   "PE", "N", "L1"])
def test_iec_terminal_is_not_an_article(token):
    """Маркировка вывода по МЭК проходит фильтр «есть цифра», но артикулом не
    является. Замер на ЩС1: две находки из трёх были ровно такими - «изделие
    '1/L1' не заказано» при позиции '13NO', то есть ОБЕ стороны пары
    оказались подписями выводов одного контактора."""
    assert bundle_rules.IEC_TERMINAL_RE.match(bundle_rules.norm(token))


@pytest.mark.parametrize("article", ["DVP16SN11T", "NDR-120-24", "8001099244",
                                     "R5ST0669"])
def test_real_articles_pass_iec_filter(article):
    assert not bundle_rules.IEC_TERMINAL_RE.match(bundle_rules.norm(article))


@pytest.mark.parametrize("token", ["230VAC", "24VDC", "~230В", "400V"])
def test_voltage_is_not_an_article(token):
    """'230VAC' на типовой картинке реле спарен на КОС с 630 обозначениями и
    до этого фильтра давал 320 ложных «разный артикул» из 322."""
    assert bundle_rules.VOLTAGE_RE.match(bundle_rules.norm(token))


def test_article_tokens_ignore_service_numbers():
    """'DVP16SN11TS 16 Point, 8DI...' -> артикул один; '16' и '8DI' в артикулы
    не годятся."""
    tokens = bundle_rules.article_tokens("DVP16SN11TS 16 Point, 8DI 24V DC")
    assert "DVP16SN11TS" in tokens
    assert "16" not in tokens


def test_mass_caption_threshold_is_above_real_articles():
    """Порог «подпись спарена со слишком многими обозначениями» стоит выше
    самого многолюдного НАСТОЯЩЕГО артикула из корпуса (19 обозначений у
    клемм) - иначе фильтр начал бы съедать настоящие сверки."""
    assert bundle_rules.MASS_CAPTION_MIN_DESIGNATORS > 19


# ------------------------------------------------------------ подпись находки

def _finding(kind, refs):
    return {"kind": kind, "refs": refs}


def test_signature_separates_bundle_findings():
    """У находок по связке нет ни клеммника, ни штифта, ни KKS - сплошные
    None. Пока в подпись не входили designator и article, ВСЕ находки по
    одному документу схлопывались в одну подпись, и разные ошибки по разным
    изделиям пропадали из отчёта."""
    a = _finding("MISSING", [{"document": "СО", "designator": "QF1"}])
    b = _finding("MISSING", [{"document": "СО", "designator": "KM2"}])
    assert pipeline._finding_signature(a) != pipeline._finding_signature(b)


def test_signature_ignores_wording():
    """Подпись обязана совпасть у находки чекера и у той же находки агента,
    описанной другими словами, - иначе инженер увидит одно и то же дважды."""
    a = _finding("MISSING", [{"document": "СО", "designator": "QF1",
                              "found": "строка 10 пуста"}])
    b = _finding("MISSING", [{"document": "СО", "designator": "QF1",
                              "found": "нет в спецификации"}])
    assert pipeline._finding_signature(a) == pipeline._finding_signature(b)


def test_signature_ignores_ref_order():
    refs = [{"document": "СО", "designator": "QF1"},
            {"document": "СБ", "designator": "QF1"}]
    assert (pipeline._finding_signature(_finding("MISSING", refs))
            == pipeline._finding_signature(_finding("MISSING", list(reversed(refs)))))


# ------------------------------------------------------------ known_errors

KNOWN = {"kind": "REVIEW", "refs": [{"document": "СО", "designator": "XP1"}]}
FINDING = {"kind": "REVIEW", "type": "Изделие не найдено на чертеже",
           "refs": [{"document": "СО", "designator": "XP1", "row": 12},
                    {"document": "СБ", "designator": "XP1"}]}


def test_known_error_matches_by_subset():
    """Запись гасит находку по НЕПОЛНОМУ совпадению: требовать переписать все
    пятнадцать полей ref'а - значит гарантировать опечатку, из-за которой
    фильтр молча не сработает."""
    assert known_filter.filter_findings([FINDING], [KNOWN]) == []


@pytest.mark.parametrize("broken", [
    {"kind": "MISSING", "refs": [{"document": "СО", "designator": "XP1"}]},
    {"kind": "REVIEW", "refs": [{"document": "ДРУГОЙ", "designator": "XP1"}]},
    {"kind": "REVIEW", "refs": [{"document": "СО", "designator": "XP2"}]},
    # у записи два ref'а, а находка описывает только один из них
    {"kind": "REVIEW", "refs": [{"document": "СО", "designator": "XP1"},
                                {"document": "Э3", "designator": "XP1"}]},
])
def test_known_error_does_not_overreach(broken):
    assert known_filter.filter_findings([FINDING], [broken]) == [FINDING]


def test_known_error_without_refs_is_ignored():
    """Запись без refs погасила бы целый класс находок разом - слишком широко,
    чтобы применять её молча."""
    assert known_filter.filter_findings([FINDING], [{"kind": "REVIEW"}]) == [FINDING]


# ------------------------------------------------------------ позиции спецификации

@pytest.mark.parametrize("raw,expected", [
    ("1KL1...1KL3", ["1KL1", "1KL2", "1KL3"]),
    ("QF1", ["QF1"]),
])
def test_position_ranges_expanded(raw, expected):
    """Диапазон в колонке «Позиция» лежит в спецификации одной строкой, а на
    чертеже и схеме - по одному обозначению. Не раскрыв его, получаем
    «изделие не заказано» на каждое промежуточное реле."""
    assert spec_parser.parse_designators(raw) == expected


def test_leading_number_range_expanded():
    """Диапазон по ВЕДУЩЕМУ числу: '1KL1 ... 50KL1' - это 50 разных реле,
    а не одно."""
    got = spec_parser.parse_designators("1KL1 ... 4KL1")
    assert got == ["1KL1", "2KL1", "3KL1", "4KL1"]


# ------------------------------------------------------------ общая таблица омоглифов

def test_homoglyph_table_is_shared_and_consistent():
    """ОДНА таблица на оба направления - ради этого normalize.py и заведён.

    Свёртывание было дважды: bundle_rules в латиницу (нужен ключ сравнения,
    направление безразлично), full_project в кириллицу (обозначение шкафа
    становится именем папки и названием связки, которое читает человек).
    Разойдись эти списки на одну букву - документы одного щита разъехались бы
    по двум связкам МОЛЧА.
    """
    import normalize

    for cyr, lat in normalize.HOMOGLYPH_PAIRS:
        assert normalize.fold(cyr) == lat, cyr
        assert normalize.to_cyrillic("Щ" + lat) == "Щ" + cyr


def test_full_project_and_bundle_rules_use_the_same_table():
    import normalize
    import full_project as fp

    assert fp._unify_layout is normalize.to_cyrillic
    assert bundle_rules.norm is normalize.fold
    assert bundle_rules.norm_designator is normalize.fold_designator


def test_latin_i_is_not_mapped_to_ukrainian():
    """Прежняя таблица full_project содержала пару 'I' -> 'І' (украинская И).
    В bundle_rules её не было вовсе, то есть свёрнутый шкаф всё равно ни с чем
    не сопоставлялся. Замерено на обоих альбомах корпуса: ни одно обозначение
    шкафа от её удаления не изменилось (0 расхождений из 57 частей).
    """
    import normalize

    assert "І" not in normalize.TO_CYRILLIC.values()
    # Щ кириллическая (свёртывание включается), I латинская, 1 цифра -
    # менять здесь нечего
    assert normalize.to_cyrillic("ЩI1") == "ЩI1"
