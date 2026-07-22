#!/usr/bin/env python3
"""
Детерминированный чекер СВЯЗКИ документов одного шкафа:
принципиальная схема (Э3) + сборочный чертёж (СБ) + спецификация (СО).

Зачем отдельный чекер. netlist_rules.py и schematic_rules.py проверяют ОДИН
документ каждый. Но самые дорогие ошибки комплекта лежат МЕЖДУ документами:
элемент нарисован в шкафу, но не заказан; в спецификации один артикул, на
чертеже другой; комплект собран из документов разных проектов. Ни один
однодокументный чекер такого не увидит по построению.

Ключ сверки - ПОЗИЦИОННОЕ ОБОЗНАЧЕНИЕ элемента (designator): '1QF1', 'DO1',
'G1'. Оно есть во всех трёх документах и означает в них одно и то же изделие.

СОСТАВ СВЯЗКИ НЕ ФИКСИРОВАН: схема + чертёж + спецификация + нетлист, или
только чертёж со спецификацией, или одна схема - как пользователь загрузил.
Каждое правило само проверяет, есть ли нужные ему документы, и молча
пропускает связку, если их нет.

--------------------------------------------------------------------------
ЧТО СЮДА НЕ ВОШЛО И ПОЧЕМУ (замерено на связках ША1 и ШУ-ТМ)
--------------------------------------------------------------------------
Правила здесь отбирались тем же способом, что и в schematic_rules.py: сначала
замер на реальных файлах, потом решение. Ложная находка дороже пропущенной -
инженер идёт сверять её по чертежу вручную и теряет доверие ко всему отчёту.

  * "Обозначение есть на чертеже, но его нет в спецификации" (по обозначениям,
    ТОЛЬКО по чертежу). ОТКЛОНЕНО. На сборочном чертеже теми же буквами подписаны
    ВЫВОДЫ изделий: 'A1'/'A2' у катушки реле, 'L1','N','COM','NO','NC'. Формально
    они неотличимы от обозначений элементов ('A1' на ША1 - это как раз ПЛК
    DVP12SA211T). На ШУ-ТМ таких подписей сотни -> отчёт превратился бы в
    мусор. Вместо этого сделаны две проверки, каждая со своей защитой от подписей
    выводов: ПО АРТИКУЛАМ (rule_article_not_in_spec - артикул строка однозначная,
    выводы артикулов не имеют) и ПО ДВУМ ДОКУМЕНТАМ СРАЗУ
    (rule_designator_not_in_spec - обозначение должно найтись И на чертеже,
    И на схеме; см. его шапку).

  * "Количество в спецификации не равно числу экземпляров на чертеже".
    ОТКЛОНЕНО. Одно обозначение законно встречается на чертеже несколько раз
    (вид спереди + вид сбоку + разрез), а у клеммников в «Количество» стоит
    число КЛЕММ, а не число клеммников. Правило меряло бы не то, что нужно.

  * "Элемент спецификации не найден на принципиальной схеме" - ОТДЕЛЬНЫМ
    правилом. ОТКЛОНЕНО. На Э3 законно нет половины спецификации: корпус,
    короба, DIN-рейки, крепёж, вентилятор с фильтром, оргстекло. Отделить "нет,
    потому что не рисуют" от "нет, потому что забыли" детерминированно нельзя.
    Схема используется как ТРЕТИЙ ГОЛОС в rule_spec_element_not_on_assembly
    (изделия нет ни там, ни там - сигнал сильнее), а решение "должно ли это
    вообще быть на схеме" оставлено агентам-нейросетям: у них есть наименование
    изделия, и они понимают, рисуется такое на схеме или нет.

  * Сверка артикулов по парам pair_source='nearest'. ОТКЛОНЕНО: на ШУ-ТМ таких
    пар 3266 из 3277, и артикул там сплошь и рядом притянут от соседнего
    элемента. Сверка по ним дала бы тысячи ложных "разные артикулы". Поэтому
    rule_article_mismatch работает только по парам из одного блока подписи
    (на ША1 их 109 из 116, на ШУ-ТМ - 11).

  * "В связке не хватает документа" (был scheme+assembly, нет spec -> замечание).
    УДАЛЕНО. Состав комплекта определяет пользователь: он грузит те документы,
    которые проверяет. Правило требовало полный комплект из трёх документов и
    ругалось на связку "сборочный чертёж + спецификация" или "схема + нетлист",
    хотя это совершенно законные наборы. Отсутствие документа - это не свойство
    документации, а свойство того, что загрузили; оценивать его нельзя.

Итог: правил немного, зато каждое опирается на однозначные строки (артикул,
обозначение документа), а не на догадки о том, что и где должно быть нарисовано.
Там, где догадка неизбежна, находка оформляется как REVIEW (вопрос инженеру),
а не как утверждение об ошибке.
"""

import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import findings as _findings  # noqa: E402  (общая форма находки и ref'а)
import normalize              # noqa: E402  (омоглифы и ведущие нули - см. ниже)



# ============================================================
# Нормализация
# ============================================================

# Свёртывание омоглифов и ведущих нулей живёт в normalize.py - ОДНОЙ таблицей
# на весь проект. Второй её пользователь, full_project.detect_cabinet,
# сворачивает в другую сторону (обозначение шкафа читает человек, и оно должно
# остаться кириллическим), и разойдись эти таблицы на одну букву - документы
# одного шкафа разъехались бы по двум связкам молча. Здесь оставлены короткие
# имена: ими пестрит весь модуль.
norm = normalize.fold
norm_designator = normalize.fold_designator


# Разделители внутри одной подписи. На чертеже в ОДНОМ текстовом span'е часто
# лежит несколько обозначений разом: 'CB-10L, FU1', 'XA019,'. Сравнивать
# обозначение с таким span'ом целиком нельзя - 'CB-1L' никогда не совпадёт с
# 'CB-1L,' и родится ложное "изделия нет на чертеже". Поэтому подписи режутся
# на токены.
_TOKEN_SPLIT_RE = re.compile(r"[\s,;]+")
_TOKEN_STRIP = " \t.,;:()[]«»\"'"


def text_tokens(texts):
    """Множество нормализованных ОБОЗНАЧЕНИЙ из произвольных подписей."""
    out = set()
    for t in texts:
        if not t:
            continue
        for tok in _TOKEN_SPLIT_RE.split(str(t)):
            tok = tok.strip(_TOKEN_STRIP)
            if tok:
                out.add(norm_designator(tok))
    out.discard("")
    return out


# Артикулоподобные токены внутри наименования из спецификации.
# 'DVP16SN11TS 16 Point, 8DI...' -> {'DVP16SN11TS'}; служебные '16', '8DI' в
# артикулы не годятся - отсюда требование длины и наличия и букв, и цифр.
_ARTICLE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9./\-]{4,}|[A-Za-z0-9./\-]{5,}")

# Маркировка вывода аппарата по МЭК 60947: силовые выводы '1/L1'...'6/T3',
# вспомогательные контакты '13NO'/'14NO'/'21NC'/'22NC'/'13'/'14', катушка
# 'A1'/'A2', сеть 'L1'/'L2'/'L3'/'N'/'PE'. Всё это подписи НА изделии, а не
# номера изделий, и в спецификации им взяться неоткуда.
IEC_TERMINAL_RE = re.compile(
    r"^(?:\d{1,2}/[LT]\d|[LT]\d|A[12]|N|PE|PEN|\d{1,2}(?:NO|NC))$", re.I)

# Подпись КАНАЛА модуля ПЛК: 'DI1'..'DI8', 'DO5', 'AI3', 'AO1', 'COM1', 'GND'.
# Это вывод изделия, а не изделие: заказывают модуль целиком, и строки на
# отдельный его канал в спецификации не будет никогда.
#
# От подписей вывода по МЭК выше отличается тем, что живёт и на чертеже, и на
# схеме сразу - канал подписан и на картинке модуля, и в месте подключения, -
# поэтому защита «обозначение найдено в ДВУХ документах» его не отсеивает.
# Замер на «24-051-АК»: 19 находок «нарисовано, но не заказано» из 56 были
# ровно такими (DI1..DI8, DO1..DO8, COM1, COM2).
#
# Буквенные коды по ГОСТ 2.710 сюда не попадают: устройство обозначается 'A',
# реле 'K', выключатель 'Q', предохранитель 'F' - двухбуквенных 'DI'/'DO'/'AO'
# среди кодов изделий нет.
PLC_CHANNEL_RE = re.compile(r"^(?:DI|DO|AI|AO|COM|GND|VCC|VDC|\+V|0V)\d{0,3}$", re.I)

# Номинал напряжения ('230VAC', '24VDC', '~230В') - подпись характеристики на
# картинке изделия, а не артикул. Цифры в нём есть, и фильтр цифры его
# пропускает - отсекаем по форме.
VOLTAGE_RE = re.compile(r"^~?\d{1,4}\s*(?:V|В|B)\s*(?:AC|DC)?$", re.I)

# Подпись, спаренная с ПОДОЗРИТЕЛЬНО МНОГИМИ обозначениями, - не артикул.
# Настоящий артикул стоит у одного изделия или у небольшой группы одинаковых
# (замер: на ША1/ШУ-ТМ/ЩСКЗ максимум - 19 обозначений у одного артикула клемм),
# а вот надпись '230VAC' на типовой картинке реле спарена на КОС с 630
# обозначениями - и до этого фильтра давала 320 ложных «разный артикул» из 322.
MASS_CAPTION_MIN_DESIGNATORS = 25

# Доля обозначений связки, которую обязана содержать одолженная спецификация
# всего объекта, чтобы считаться описывающей ЭТОТ шкаф. Замер по трём альбомам:
# подходящая спецификация - 55.6..100%, спецификация чужого раздела - ровно 0.0%
# (двенадцать случаев из двенадцати). См. rule_designator_not_in_spec.
PROJECT_WIDE_MIN_MATCH = 0.05


# ============================================================
# ХАРАКТЕРИСТИКИ ИЗДЕЛИЯ (номиналы)
# ============================================================
#
# Единица измерения -> (вид характеристики, множитель к базовой единице).
# Порядок в регулярке ниже - от ДЛИННОЙ к короткой: иначе "Вт" совпадёт как
# "В", а "кА" как "А", и мощность 240 Вт станет напряжением 240 В.
#
# Раскладки перемешаны сознательно: бюро пишет "24В" кириллицей, "24V"
# латиницей и "24B" латинской B - в одной и той же спецификации ЩСКЗ
# встречаются все три ("24В, 10А, 240Вт" и "230V AC/DC" в соседних строках).
CHARACTERISTIC_UNITS = [
    ("кВт", "мощность", 1000.0), ("kW", "мощность", 1000.0),
    ("мВт", "мощность", 0.001),
    ("Вт", "мощность", 1.0), ("W", "мощность", 1.0),
    ("кВА", "мощность", 1000.0), ("kVA", "мощность", 1000.0),
    ("кА", "ток", 1000.0), ("kA", "ток", 1000.0),
    ("мА", "ток", 0.001), ("mA", "ток", 0.001),
    ("Ач", "ёмкость", 1.0), ("Ah", "ёмкость", 1.0),
    ("кВ", "напряжение", 1000.0), ("kV", "напряжение", 1000.0),
    ("мм2", "сечение", 1.0), ("мм²", "сечение", 1.0), ("mm2", "сечение", 1.0),
    ("Гц", "частота", 1.0), ("Hz", "частота", 1.0),
    ("А", "ток", 1.0), ("A", "ток", 1.0),
    ("В", "напряжение", 1.0), ("V", "напряжение", 1.0), ("B", "напряжение", 1.0),
]

# Число, единица и необязательный род тока. "AC/DC" обязан входить в шаблон:
# без него "24VDC" не совпадает вовсе (после "V" сразу идёт буква, и граница
# слова не срабатывает), а именно так подписаны лампы на чертеже ЩСКЗ.
CHARACTERISTIC_RE = re.compile(
    r"(?<![\w,.])(\d{1,4}(?:[.,]\d{1,3})?)\s*("
    + "|".join(re.escape(u) for u, _, _ in CHARACTERISTIC_UNITS)
    + r")\s*(?:AC/DC|DC/AC|AC|DC)?(?![\wА-Яа-я])", re.I | re.U)

_UNIT_LOOKUP = {u.lower(): (kind, mul) for u, kind, mul in CHARACTERISTIC_UNITS}


def characteristics(*texts):
    """{вид характеристики: множество значений} из произвольных подписей.

    "Блок питания с функцией UPS, 24В, 10А, 240Вт" ->
        {"напряжение": {24.0}, "ток": {10.0}, "мощность": {240.0}}

    Значения приводятся к базовой единице, поэтому "6kA" и "6A" - РАЗНЫЕ
    величины (6000 и 6), а "0,4кВ" и "400В" - одна и та же. Без приведения
    отключающая способность автомата совпала бы с его номиналом.
    """
    out = defaultdict(set)
    for text in texts:
        for m in CHARACTERISTIC_RE.finditer(text or ""):
            kind, mul = _UNIT_LOOKUP[m.group(2).lower()]
            out[kind].add(round(float(m.group(1).replace(",", ".")) * mul, 4))
    return dict(out)


def _caption_articles(asm):
    """Артикулы чертежа, которые на деле - типовые подписи на картинках
    (спарены с массой разных обозначений)."""
    by_article = defaultdict(set)
    for e in asm["elements"]:
        if e.get("pair_source") == "block" and e.get("article"):
            by_article[e["article"]].add(norm_designator(e.get("designator")))
    return {a for a, des in by_article.items()
            if len(des) >= MASS_CAPTION_MIN_DESIGNATORS}


def article_tokens(*texts):
    """Множество артикулоподобных токенов из произвольных текстов."""
    out = set()
    for t in texts:
        if not t:
            continue
        for m in _ARTICLE_TOKEN_RE.finditer(str(t)):
            tok = m.group().strip(".-/")
            if len(tok) < 4:
                continue
            has_digit = any(c.isdigit() for c in tok)
            has_alpha = any(c.isalpha() for c in tok)
            if has_digit and (has_alpha or len(tok) >= 5):
                out.add(norm(tok))
    return out


# Цифровое обозначение проекта в начале обозначения документа:
# '026.809.01.01-ИПК  ША1 СО_25.03.26' -> '026.809.01.01'
DECIMAL_NUMBER_RE = re.compile(r"^\s*(\d{2,3}(?:\.\d{2,4})+)")


def decimal_number(designation):
    if not designation:
        return None
    m = DECIMAL_NUMBER_RE.match(str(designation))
    return m.group(1) if m else None


# ============================================================
# Загрузка данных документов связки
# ============================================================

def _load(data_dir, filename):
    path = os.path.join(data_dir, filename)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_spec(data_dir):
    doc = _load(data_dir, "specification.json")
    if doc is None:
        return None
    return {
        "designation": doc["document_metadata"].get("designation_in_document"),
        "items": doc.get("items", []),
        "cell_errors": doc.get("cell_errors", []),
        "designator_index": doc.get("designator_index", {}),
        "statistics": doc.get("statistics", {}),
    }


def load_assembly(data_dir):
    doc = _load(data_dir, "assembly.json")
    if doc is None:
        return None
    return {
        "designation": doc["document_metadata"].get("designation_in_document"),
        "elements": doc.get("elements", []),
        "designator_index": doc.get("designator_index", {}),
        "all_texts": doc.get("all_label_texts", []),
        "statistics": doc.get("statistics", {}),
    }


def _doc_entries(docs, dtype):
    """Главный документ типа + документы того же типа из "extra".

    В связке-альбоме у шкафа несколько схем (принципиальная + однолинейная +
    внешних соединений) и несколько частей чертежа ("Общий вид" + "Вид
    спереди"). Сверка обязана видеть обозначения ИЗ ВСЕХ: изделие, подписанное
    только на однолинейной, - всё равно нарисованное изделие. Главный документ
    идёт первым: при совпадении обозначений в нескольких документах ссылка в
    находке ведёт на него.
    """
    entry = docs.get(dtype)
    if not entry:
        return []
    return [entry] + list(entry.get("extra") or [])


def load_scheme(data_dir):
    """Факты со схемы: обозначения приборов, артикулы модулей, весь текст."""
    pages = _load(data_dir, "classified.json")
    if pages is None:
        return None

    device_tags = {}
    articles = set()
    all_texts = set()
    designation = None
    for page in pages:
        pno = page.get("page_number")
        for s in page.get("text_spans", []):
            t = (s.get("text") or "").strip()
            if not t:
                continue
            all_texts.add(t)
            et = s.get("entity_type")
            if et in ("device_tag", "instrument_tag"):
                key = norm_designator(t)
                device_tags.setdefault(key, {"text": t, "sheets": set()})
                device_tags[key]["sheets"].add(pno)
            elif et == "module_partno":
                articles.add(t)
            elif et == "doc_number" and designation is None:
                designation = t
    for v in device_tags.values():
        v["sheets"] = sorted(x for x in v["sheets"] if x is not None)

    return {
        "designation": designation,
        "device_tags": device_tags,
        "articles": articles,
        "all_texts": all_texts,
        "norm_texts": text_tokens(all_texts),
    }


# ============================================================
# Слияние нескольких документов одного типа (главный + extra)
# ============================================================

def load_scheme_bundle(entries):
    """Все схемы связки одним словарём. Каждый device_tag помнит, из какого
    документа он пришёл ("doc") - ссылка в находке обязана вести на документ,
    где обозначение реально подписано, а не на главный по умолчанию. При
    совпадении тега в нескольких схемах остаётся главный (entries[0] - он)."""
    merged = None
    for e in entries:
        data = load_scheme(e["data_dir"])
        if data is None:
            continue
        for tag in data["device_tags"].values():
            tag["doc"] = e["name"]
        if merged is None:
            merged = data
            continue
        for key, tag in data["device_tags"].items():
            merged["device_tags"].setdefault(key, tag)
        merged["articles"] |= data["articles"]
        merged["all_texts"] |= data["all_texts"]
        merged["norm_texts"] |= data["norm_texts"]
        if not merged.get("designation"):
            merged["designation"] = data.get("designation")
    return merged


def load_assembly_bundle(entries):
    """Все части чертежа связки одним словарём, с пометкой документа у каждого
    элемента и обозначения (см. load_scheme_bundle - причина та же)."""
    merged = None
    for e in entries:
        data = load_assembly(e["data_dir"])
        if data is None:
            continue
        for el in data["elements"]:
            el["doc"] = e["name"]
        for info in data["designator_index"].values():
            info["doc"] = e["name"]
        if merged is None:
            merged = data
            continue
        merged["elements"] = list(merged["elements"]) + list(data["elements"])
        for des, info in data["designator_index"].items():
            merged["designator_index"].setdefault(des, info)
        merged["all_texts"] = list(merged["all_texts"]) + list(data["all_texts"])
        st, add = merged["statistics"], data["statistics"]
        st["total_sheets"] = (st.get("total_sheets") or 0) + (add.get("total_sheets") or 0)
        if not merged.get("designation"):
            merged["designation"] = data.get("designation")
    return merged


def _is_empty(dtype, data):
    """Документ, из которого не извлеклось НИЧЕГО. Это провал парсера, а не
    пустой шкаф, и участвовать в сверке такой документ не должен: пустая
    спецификация читается правилами как «ничего не заказано» и выдаёт вал
    ложных MISSING (16 из 17 находок на связке КОС - ровно этот случай),
    пустой чертёж - как «ничего не нарисовано»."""
    if data is None:
        return True
    if dtype == "spec":
        return not data["items"]
    if dtype == "assembly":
        return not data["elements"] and not data["all_texts"]
    if dtype == "scheme":
        return not data["device_tags"] and not data["all_texts"]
    return False


# ============================================================
# Оформление находки
# ============================================================

def _ref(document, doc_type, source_file, sheet=None, row=None, designator=None,
         article=None, name=None, quantity=None, found=None):
    """Место находки по связке. Проводных полей (клеммник, штифт, KKS) здесь
    нет по построению: связка сверяется по ПОЗИЦИОННЫМ ОБОЗНАЧЕНИЯМ изделий,
    а не по проводам."""
    return _findings.ref(document, doc_type, source_file, sheet=sheet, row=row,
                         designator=designator, article=article, name=name,
                         quantity=quantity, found=found)


def _finding(kind, scope, severity, type_ru, refs, finding, action, evidence=None):
    return _findings.finding(kind, severity, type_ru, refs, finding, action,
                             evidence, scope=scope)


# ============================================================
# Правила связки
# ============================================================

def rule_designation_mismatch(bundle, docs, loaded):
    """Внутри документов связки стоят РАЗНЫЕ цифровые обозначения проекта.

    Самая твёрдая междокументная проверка: обозначение - это строка в штампе,
    её не надо интерпретировать. Все документы одного шкафа обязаны иметь одно
    и то же цифровое обозначение (различаются только марки видов: Э3/СБ/СО).
    Расхождение означает, что документ скопирован из другого проекта и в нём
    забыли поменять штамп - ошибка, которая тянет за собой весь комплект.
    """
    seen = {}
    for dtype in ("spec", "assembly", "scheme"):
        data = loaded.get(dtype)
        if not data:
            continue
        # Общая спецификация объекта носит обозначение АЛЬБОМА, а не шкафа
        # ("24-051-ЭОМ.СО" против "24-051-ЭОМ"), и она одна на все связки.
        # Не исключив её, получаем одно и то же расхождение штампов на каждом
        # из полутора десятков шкафов - пятнадцать копий одной неошибки.
        if dtype == "spec" and docs.get("spec", {}).get("project_wide"):
            continue
        num = decimal_number(data.get("designation"))
        if num:
            seen[dtype] = (num, data.get("designation"))

    if len(seen) < 2:
        return []
    numbers = {v[0] for v in seen.values()}
    if len(numbers) < 2:
        return []

    # большинство побеждает: обозначение, встретившееся чаще, считаем верным
    freq = defaultdict(int)
    for num, _ in seen.values():
        freq[num] += 1
    majority = max(freq, key=lambda n: freq[n])
    odd = [t for t, (num, _) in seen.items() if num != majority]

    ru = {"scheme": "принципиальная схема", "assembly": "сборочный чертёж",
          "spec": "спецификация"}
    refs = []
    for dtype in ("spec", "assembly", "scheme"):
        if dtype not in seen:
            continue
        d = docs[dtype]
        num, full = seen[dtype]
        refs.append(_ref(d["name"], dtype, d["source"],
                         found=f"обозначение в документе: {full!r} -> {num}"))

    odd_ru = ", ".join(f"{ru[t]} ({seen[t][0]})" for t in odd)
    return [_finding(
        kind="MISMATCH",
        scope="cross_document",
        severity="high",
        type_ru="Разное обозначение проекта в документах связки",
        refs=refs[:3],
        finding=f"Документы связки «{bundle}» имеют разные цифровые обозначения: "
                f"{'; '.join(f'{ru[t]} - {v[0]}' for t, v in sorted(seen.items()))}. "
                f"Выбивается: {odd_ru}.",
        action=f"Проверить штамп: привести обозначение к {majority} либо подтвердить, "
               f"что документ относится к другому проекту и попал в связку по ошибке.",
        evidence="; ".join(f"{t}: {v[1]!r}" for t, v in sorted(seen.items())),
    )]


def _spec_rows_by_designator(spec):
    """{нормализованное обозначение: [строки спецификации]}"""
    out = defaultdict(list)
    for it in spec["items"]:
        for d in it.get("designators", []):
            out[norm(d)].append(it)
    return out


def rule_spec_element_not_on_assembly(bundle, docs, loaded):
    """Изделие из спецификации не найдено на сборочном чертеже.

    Направление сверки выбрано именно так (спецификация -> чертёж), потому что
    СПИСОК ОБОЗНАЧЕНИЙ БЕРЁТСЯ ИЗ СПЕЦИФИКАЦИИ - гадать, что на чертеже является
    обозначением, а что подписью вывода, не требуется. Ищем обозначение среди
    ВСЕГО текста чертежа, а не только среди спаренных подписей: даже если
    парсер не смог связать обозначение с артикулом, сам факт наличия надписи
    на чертеже доказывает, что элемент размещён.

    Проверяются только строки, у которых ЕСТЬ позиционное обозначение: строки
    без него (короба, DIN-рейки, крепёж, оргстекло) - расходные материалы, их
    на чертеже по обозначению искать бессмысленно.

    ПОЧЕМУ REVIEW, А НЕ ОШИБКА. Замер на двух связках: из 454 обозначений
    спецификации на чертеже не нашлось 7 (1.5%). Разбор всех семи вручную:
      - QS1 'Выключатель нагрузки' и R1 'Потенциометр' (ША1) - настоящие
        расхождения (на чертеже выключатель подписан 'QS01', а не 'QS1');
      - K01 'Коробка испытательная' (ШУ-ТМ) - похоже на настоящий пропуск;
      - XP1 'Гнездо DB-9', 1E1 'Патч-корд', R1/Rc 'Резисторы' - изделия,
        которые на виде шкафа ЗАКОННО не рисуют (кабельная мелочёвка и
        компоненты на клеммах).
    То есть примерно половина - не ошибки документа, а норма оформления.
    Отличить "не нарисовано, потому что не рисуют" от "забыли" детерминированно
    нельзя: для этого надо понимать, ЧТО ЭТО ЗА ИЗДЕЛИЕ. Поэтому правило
    констатирует ФАКТ и задаёт вопрос (REVIEW), а не выносит приговор (MISSING).

    Важность зависит от третьего документа связки: если обозначения нет и на
    принципиальной схеме, изделие числится ТОЛЬКО в спецификации - сигнал
    заметно сильнее, чем "есть на схеме, но не нарисовано в шкафу".
    """
    spec, asm = loaded.get("spec"), loaded.get("assembly")
    if not spec or not asm:
        return []
    # Спецификация ВСЕГО ОБЪЕКТА (полный проект, см. _lend_project_wide_docs в
    # main.py) описывает полтора десятка шкафов сразу. В этом направлении она
    # даёт вал заведомо ложных находок: оборудование двенадцати чужих шкафов
    # закономерно отсутствует на чертеже разбираемого. Обратное направление
    # (rule_designator_not_in_spec) на такой спецификации остаётся верным.
    if docs.get("spec", {}).get("project_wide"):
        return []
    scheme = loaded.get("scheme")

    asm_norm_texts = {norm(t) for t in asm["all_texts"]}
    asm_norm_texts |= {norm(d) for d in asm["designator_index"]}

    findings = []
    for des_norm, rows in sorted(_spec_rows_by_designator(spec).items()):
        if not des_norm or des_norm in asm_norm_texts:
            continue
        row = rows[0]
        shown = row.get("position_raw") or des_norm
        original = next((d for d in row.get("designators", []) if norm(d) == des_norm),
                        des_norm)

        on_scheme = None
        if scheme:
            on_scheme = (des_norm in scheme["device_tags"]
                         or des_norm in scheme["norm_texts"])

        refs = [
            _ref(docs["spec"]["name"], "spec", "specification.json",
                 row=row.get("row"), designator=original, article=row.get("code"),
                 name=row.get("name"), quantity=row.get("quantity"),
                 found=f"строка {row.get('row')}: позиция {shown!r}, "
                       f"{row.get('name')!r}, кол-во {row.get('quantity')}"),
            _ref(docs["assembly"]["name"], "assembly", "assembly.json",
                 designator=original,
                 found=f"обозначение {original!r} не найдено ни в одной подписи "
                       f"на {asm['statistics'].get('total_sheets')} листах чертежа"),
        ]
        if scheme and docs.get("scheme"):
            tag = scheme["device_tags"].get(des_norm)
            # тег мог прийти не с главной схемы связки, а с однолинейной или
            # схемы внешних соединений - ссылка ведёт туда, где он подписан
            refs.append(_ref(
                (tag or {}).get("doc") or docs["scheme"]["name"],
                "scheme", "classified.json",
                sheet=(tag["sheets"][0] if tag and tag["sheets"] else None),
                designator=original,
                found=(f"обозначение {original!r} на схеме есть"
                       + (f" (лист {tag['sheets'][0]})" if tag and tag["sheets"] else "")
                       if on_scheme else
                       f"обозначение {original!r} на схеме тоже не найдено")))

        if on_scheme is False:
            severity = "medium"
            where = ("Ни на сборочном чертеже, ни на принципиальной схеме этого "
                     "обозначения нет - изделие числится только в спецификации.")
            action = (f"Проверить, нужно ли {original!r} вообще: изделие заказано, но "
                      f"не появляется ни в одном другом документе связки. Если нужно - "
                      f"нанести его на чертёж и схему; если нет - убрать из спецификации.")
        else:
            severity = "low"
            where = ("На принципиальной схеме обозначение есть, то есть электрически "
                     "изделие в проекте присутствует, а в шкафу не размещено.")
            action = (f"Проверить, размещается ли {original!r} в шкафу. Если это "
                      f"кабельная мелочёвка или компонент на клемме, которые на вид "
                      f"шкафа не наносят, - замечание можно закрыть.")

        findings.append(_finding(
            kind="REVIEW",
            scope="cross_document",
            severity=severity,
            type_ru="Изделие спецификации не найдено на сборочном чертеже",
            refs=refs[:3],
            finding=f"Изделие {original!r} ({row.get('name')!r}) заказано в спецификации "
                    f"(строка {row.get('row')}, кол-во {row.get('quantity')}), но его "
                    f"позиционное обозначение не подписано ни на одном листе сборочного "
                    f"чертежа. {where}",
            action=action,
            evidence=f"spec row {row.get('row')}: position={shown!r}, "
                     f"code={row.get('code')!r}; на схеме: {on_scheme}",
        ))
    return findings


def rule_article_not_in_spec(bundle, docs, loaded):
    """Артикул подписан на сборочном чертеже, но такого изделия нет в спецификации.

    Проверка идёт ПО АРТИКУЛАМ, а не по обозначениям, сознательно: артикул -
    строка однозначная ('DVP16SN11T', '814174'), а обозначения на чертеже
    невозможно отличить от подписей выводов (см. шапку модуля).

    Берутся только артикулы, спаренные с обозначением В ОДНОМ БЛОКЕ ПОДПИСИ
    (pair_source='block'): такая подпись заведомо относится к изделию. Артикулы,
    притянутые "ближайшим", сюда не попадают - они могли прилететь от соседа.

    Смысл находки: всё, что стоит в шкафу, должно быть закуплено. Изделие на
    чертеже, которого нет в спецификации, - это либо незаказанное железо, либо
    забытая строка сметы.
    """
    spec, asm = loaded.get("spec"), loaded.get("assembly")
    if not spec or not asm:
        return []

    # весь артикульный словарь спецификации: коды, тип-марки и то, что написано
    # в наименовании (в наименовании тип часто дублируется)
    spec_articles = set()
    for it in spec["items"]:
        spec_articles |= article_tokens(it.get("code"), it.get("type_mark"),
                                        it.get("name"), it.get("note"))
        if it.get("code"):
            spec_articles.add(norm(it["code"]))
        if it.get("type_mark"):
            spec_articles.add(norm(it["type_mark"]))

    by_article = defaultdict(list)
    for e in asm["elements"]:
        if e.get("pair_source") != "block" or not e.get("article"):
            continue
        by_article[e["article"]].append(e)

    captions = _caption_articles(asm)
    spec_des = {norm_designator(d)
                for it in spec["items"] for d in it.get("designators", [])}

    findings = []
    for article, elems in sorted(by_article.items()):
        a_norm = norm(article)
        if not a_norm or a_norm in spec_articles:
            continue
        # типовая подпись на картинке изделия, а не артикул (см. _caption_articles)
        if article in captions or VOLTAGE_RE.match(a_norm):
            continue
        # Артикул без единой цифры - не артикул. У изделия в каталоге номер
        # обязательно содержит цифры ('DVP16SN11T', '260511', 'ND16-22DS/2',
        # 'R5ST0669'), а вот подписи на КАРТИНКЕ изделия - нет. На чертеже ЩСКЗ
        # на рисунке блока питания подписаны его лампочки и кнопки: 'Status',
        # 'Communication', 'Force Button', 'Battery', 'Charger'. Классификатор
        # берёт их в артикулы по длине (длиннее обозначения), и правило заявляло
        # "изделие Communication не заказано" - две ложные находки из двух на
        # этом комплекте. Требование цифры убирает их и не трогает ни один
        # настоящий артикул из проверенных спецификаций.
        if not any(c.isdigit() for c in a_norm):
            continue
        # Маркировка ВЫВОДА по МЭК - тоже не артикул, хотя цифры в ней есть и
        # фильтр выше её пропускает. На силовых аппаратах выводы подписаны
        # '1/L1', '2/T1', '3/L2', '4/T2', на вспомогательных контактах -
        # '13NO', '14NO', '21NC', '22NC', на катушке - 'A1', 'A2'. Замер на
        # ЩС1 (полный проект): из трёх находок этого правила две были ровно
        # такими - "изделие '1/L1' не заказано" при позиции '13NO', то есть
        # обе стороны пары оказались подписями выводов одного контактора.
        # Это тот же капкан, что описан в шапке модуля: на сборочном чертеже
        # обозначение изделия неотличимо от подписи вывода.
        if IEC_TERMINAL_RE.match(a_norm):
            continue
        if article_tokens(article) & spec_articles:
            continue
        # Все обозначения этого артикула в спецификации ЕСТЬ - значит изделие
        # заказано, а расходится только артикул. Это случай
        # rule_article_mismatch, и он его уже покажет; вторая находка о том же
        # ('изделие не заказано' при заказанном изделии) - шум и неправда.
        if all(norm_designator(e["designator"]) in spec_des for e in elems):
            continue
        designators = sorted({e["designator"] for e in elems})
        sheets = sorted({e["sheet"] for e in elems})
        findings.append(_finding(
            kind="MISSING",
            scope="cross_document",
            severity="high",
            type_ru="Изделие с чертежа отсутствует в спецификации",
            refs=[
                _ref(elems[0].get("doc") or docs["assembly"]["name"],
                     "assembly", "assembly.json",
                     sheet=sheets[0], designator=designators[0], article=article,
                     quantity=len(elems),
                     found=f"лист {sheets[0]}: подпись {designators[0]!r} / {article!r}"
                           + (f" (и ещё {len(elems) - 1} экз.)" if len(elems) > 1 else "")),
                _ref(docs["spec"]["name"], "spec", "specification.json",
                     article=article,
                     found=f"артикул {article!r} не найден ни в одной строке "
                           f"спецификации ({spec['statistics'].get('total_rows')} строк)"),
            ],
            finding=f"На сборочном чертеже подписано изделие {article!r} "
                    f"(позиции: {', '.join(designators[:6])}), но такого артикула нет "
                    f"ни в одной строке спецификации - изделие не заказано.",
            action=f"Добавить {article!r} в спецификацию либо убрать его с чертежа, "
                   f"если изделие не устанавливается.",
            evidence=f"assembly: article={article!r}, designators={designators[:6]}, "
                     f"листы={sheets}",
        ))
    return findings


def rule_designator_not_in_spec(bundle, docs, loaded):
    """Изделие есть И на чертеже, И на схеме, но его нет в спецификации.

    Смысл: изделие нарисовано в шкафу и заведено в электрическую схему, то есть
    оно точно нужно, - а строки в спецификации на него нет. Значит, его никто не
    закупит. Ровно так в комплекте ЩСКЗ потеряли автомат QF1: он есть на общем
    виде и на листе 10.2 схемы, а в спецификации на его месте (строка 10) пустая
    строка.

    ПОЧЕМУ ЭТО НЕ ПОВТОРЯЕТ ОТКЛОНЁННОЕ ПРАВИЛО (см. шапку модуля). Отклонена
    была сверка ТОЛЬКО ПО ЧЕРТЕЖУ: на чертеже подпись вывода ('A1', 'L1', 'COM')
    неотличима от обозначения элемента, и такая проверка топила отчёт. Здесь
    обязательное условие - обозначение найдено В ДВУХ документах сразу: на
    чертеже И на схеме, причём на схеме именно как device_tag/instrument_tag.
    Подпись вывода, чтобы дать ложную находку, должна совпасть в обоих
    документах и там и там опознаться как обозначение прибора - на порядок
    менее вероятно, чем каждое из событий по отдельности. Это тот же приём
    "третьего голоса", что и в rule_spec_element_not_on_assembly, только
    в другую сторону.

    Замер на связке ЩСКЗ (41 обозначение в спецификации, 71 на чертеже,
    77 на схеме): правило дало РОВНО ОДНУ находку - QF1, настоящую ошибку.

    Расходники (короба, DIN-рейки, крепёж) сюда не попадают по построению: у них
    нет позиционного обозначения, значит их нечем искать на чертеже и схеме.
    """
    spec, asm, scheme = loaded.get("spec"), loaded.get("assembly"), loaded.get("scheme")
    if not spec or not asm or not scheme:
        return []

    spec_designators = {norm_designator(d)
                        for it in spec["items"] for d in it.get("designators", [])}
    # Обозначения, названные в ТЕКСТЕ наименования, а не в графе «Позиция»
    # (см. ниже, in_spec_text).
    spec_name_tokens = text_tokens(it.get("name") for it in spec["items"])

    # Спецификация ДРУГОГО РАЗДЕЛА проекта, одолженная связке по ошибке.
    # У «24-051-АК» спецификаций всего объекта четыре - тепломеханических
    # решений, газоснабжения, жидкого топливоснабжения и автоматизации, - и
    # шкафу одалживается та, в которой больше строк. Ею оказалась
    # тепломеханическая: в ней насосы и задвижки, а внутренностей щита
    # (реле 14K2, каналов ПЛК DO5) нет и быть не может. Замер: 56 находок
    # «нарисовано, но не заказано», все до одной ложные.
    #
    # Отличается такая спецификация не тем, что чего-то не хватает, а тем, что
    # не совпадает НИЧЕГО. Замер по трём альбомам разделяет случаи начисто:
    # у подходящей спецификации совпадает 55.6-100% обозначений связки, у
    # чужой - РОВНО 0.0%, во всех двенадцати случаях. Порог стоит между
    # группами с одиннадцатикратным запасом снизу.
    if docs.get("spec", {}).get("project_wide"):
        both = ({norm_designator(d) for d in asm["designator_index"]}
                & set(scheme["device_tags"]))
        if both and len(both & spec_designators) < PROJECT_WIDE_MIN_MATCH * len(both):
            return []

    findings = []
    for des, info in sorted(asm["designator_index"].items()):
        des_norm = norm_designator(des)
        if not des_norm or des_norm in spec_designators:
            continue
        # По ГОСТ 2.710 позиционное обозначение оканчивается порядковым НОМЕРОМ
        # ('HL3', '1SB01'). Токен без номера на конце ('3HL', '17SA') - это
        # обрезанная подпись (номер уехал в соседний фрагмент) или буквенный
        # код без экземпляра; замер на ЭОМ: 14 из 21 находки по ПЭСПЗ были
        # ровно такими. Судить по обрывку нельзя - пропускаем.
        if not des_norm[-1:].isdigit():
            continue
        # Маркировка вывода по МЭК ('1NO', 'A1') - подпись контакта, не изделие;
        # тот же капкан, что в rule_article_not_in_spec.
        if IEC_TERMINAL_RE.match(des_norm):
            continue
        # Канал модуля ПЛК ('DI3', 'DO5', 'COM1') - тоже вывод, а не изделие,
        # и защиту «нашлось в двух документах» он проходит: канал подписан и на
        # чертеже, и на схеме. См. PLC_CHANNEL_RE.
        if PLC_CHANNEL_RE.match(des_norm):
            continue
        # Подпись-диапазон ('FU1-FU3'): спецификация хранит концы по одному
        # (FU1, FU2, FU3), и целиком такой ключ в ней не найдётся никогда.
        # Если ВСЕ части диапазона в спецификации есть - изделия заказаны.
        if "-" in des_norm:
            parts = [p for p in des_norm.split("-") if p]
            if parts and all(p in spec_designators for p in parts):
                continue
        # Обозначение НАЗВАНО в спецификации, но не в графе «Позиция», а внутри
        # наименования: у «24-051-АК» есть строка «14K2, 15K2 Фиксатор SR20T,
        # пластик, чёрный...» - позиции затекли в соседнюю графу, и по колонке
        # «Позиция» строка выглядит безымянной. Изделие при этом ЗАКАЗАНО, и
        # утверждать обратное нельзя.
        #
        # Молча гасить такое тоже нельзя: разбор столбцов мог ошибиться, и тогда
        # заказано на самом деле другое. Поэтому находка остаётся, но как ВОПРОС
        # инженеру - ровно тот случай, ради которого заведён REVIEW.
        in_spec_text = des_norm in spec_name_tokens
        tag = scheme["device_tags"].get(des_norm)
        if not tag:
            continue                       # на схеме нет - правило молчит

        asm_sheets = info.get("sheets") or []
        sch_sheets = tag.get("sheets") or []
        findings.append(_finding(
            kind="REVIEW" if in_spec_text else "MISSING",
            scope="cross_document",
            severity="low" if in_spec_text else "high",
            type_ru=("Изделие названо в спецификации не в своей графе"
                     if in_spec_text
                     else "Изделие с чертежа и схемы отсутствует в спецификации"),
            refs=[
                _ref(docs["spec"]["name"], "spec", "specification.json",
                     designator=des,
                     found=f"обозначения {des!r} нет ни в одной из "
                           f"{spec['statistics'].get('total_rows')} строк спецификации"),
                _ref(info.get("doc") or docs["assembly"]["name"],
                     "assembly", "assembly.json",
                     sheet=(asm_sheets[0] if asm_sheets else None), designator=des,
                     found=f"подписано на листах {asm_sheets}" if asm_sheets
                           else f"обозначение {des!r} подписано на чертеже"),
                _ref(tag.get("doc") or docs["scheme"]["name"],
                     "scheme", "classified.json",
                     sheet=(sch_sheets[0] if sch_sheets else None), designator=des,
                     found=f"подписано на листах {sch_sheets}" if sch_sheets
                           else f"обозначение {des!r} подписано на схеме"),
            ],
            finding=f"Изделие {des!r} нарисовано на сборочном чертеже "
                    f"(лист{'ы' if len(asm_sheets) > 1 else ''} "
                    f"{', '.join(map(str, asm_sheets)) or '-'}) и заведено в "
                    f"принципиальную схему (лист "
                    f"{', '.join(map(str, sch_sheets)) or '-'}), но строки на него "
                    f"в спецификации нет - изделие не будет заказано.",
            action=f"Добавить {des!r} в спецификацию либо убрать его с чертежа и схемы, "
                   f"если изделие не устанавливается.",
            evidence=f"assembly: {des!r} листы {asm_sheets}; scheme: листы {sch_sheets}; "
                     f"spec: обозначения нет среди {len(spec_designators)} обозначений",
        ))
    return findings


def rule_article_mismatch(bundle, docs, loaded):
    """У одного и того же элемента в спецификации и на чертеже РАЗНЫЕ артикулы.

    Это и есть "сверка характеристик элемента между документами". Условия
    срабатывания жёсткие, чтобы находка была доказательной:
      - обозначение есть и в спецификации, и на чертеже;
      - артикул на чертеже спарен с обозначением В БЛОКЕ (pair_source='block'),
        то есть подпись заведомо принадлежит этому элементу;
      - в строке спецификации есть код или тип-марка;
      - НИ ОДИН артикульный токен строки спецификации (код, тип-марка,
        наименование) не совпадает с артикулом на чертеже.
    Сравнение токенами, а не подстрокой: 'DVP16SN11T' ЯВЛЯЕТСЯ подстрокой
    'DVP16SN11TS', и проверка "входит ли" молча пропустила бы ровно ту ошибку,
    ради которой правило написано.

    Если расхождение уже описано в примечании строки ("Замена на DVP16SN11T"),
    находка понижается до REVIEW/low: это не ошибка-сюрприз, а известная замена,
    у которой не обновили колонку кода. Инженеру она всё равно нужна - заказ
    пойдёт по коду, - но кричать о ней высоким приоритетом нечестно.

    Третьим ref'ом добавляется принципиальная схема, если тот же артикул
    подписан и на ней: тогда видно, что спецификация расходится с ДВУМЯ
    документами сразу, а не с одним.
    """
    spec, asm = loaded.get("spec"), loaded.get("assembly")
    if not spec or not asm:
        return []
    scheme = loaded.get("scheme")

    spec_rows = _spec_rows_by_designator(spec)

    # обозначение -> артикулы, спаренные в блоке. Типовые подписи на картинках
    # ('230VAC' у каждого реле) и номиналы напряжения артикулами не считаются:
    # без этого фильтра сверка КОС дала 320 ложных «разный артикул» из 322 -
    # каждая строка спецификации реле «расходилась» с надписью 230VAC.
    captions = _caption_articles(asm)
    asm_block = defaultdict(set)
    asm_meta = {}
    for e in asm["elements"]:
        if e.get("pair_source") != "block" or not e.get("article"):
            continue
        if e["article"] in captions or VOLTAGE_RE.match(norm(e["article"])):
            continue
        asm_block[norm(e["designator"])].add(e["article"])
        asm_meta.setdefault(norm(e["designator"]), e)

    findings = []
    for des_norm, articles in sorted(asm_block.items()):
        rows = spec_rows.get(des_norm)
        if not rows:
            continue                       # нет в спецификации - это другое правило
        if len(articles) != 1:
            continue                       # на чертеже несколько разных артикулов
                                           # у одного обозначения - парсер не уверен
        asm_article = next(iter(articles))
        asm_tokens = article_tokens(asm_article) or {norm(asm_article)}

        # все артикульные токены ВСЕХ строк спецификации с этим обозначением:
        # у изделия бывают строки-аксессуары (реле + колодка + фиксатор), и
        # артикул чертежа может относиться к любой из них
        spec_tokens = set()
        coded_rows = []
        for it in rows:
            if it.get("code") or it.get("type_mark"):
                coded_rows.append(it)
            spec_tokens |= article_tokens(it.get("code"), it.get("type_mark"),
                                          it.get("name"))
            if it.get("code"):
                spec_tokens.add(norm(it["code"]))
            if it.get("type_mark"):
                spec_tokens.add(norm(it["type_mark"]))
        if not coded_rows:
            continue
        if asm_tokens & spec_tokens or norm(asm_article) in spec_tokens:
            continue                       # совпало - всё в порядке

        row = coded_rows[0]
        e = asm_meta[des_norm]
        designator = e["designator"]

        # известная замена, описанная в примечании самой спецификации?
        note = row.get("note") or ""
        known_swap = bool(article_tokens(note) & asm_tokens)

        refs = [
            _ref(docs["spec"]["name"], "spec", "specification.json",
                 row=row.get("row"), designator=designator, article=row.get("code"),
                 name=row.get("name"), quantity=row.get("quantity"),
                 found=f"строка {row.get('row')}: код {row.get('code')!r}"
                       + (f", примечание {note!r}" if note else "")),
            _ref(e.get("doc") or docs["assembly"]["name"],
                 "assembly", "assembly.json",
                 sheet=e.get("sheet"), designator=designator, article=asm_article,
                 found=f"лист {e.get('sheet')}: подпись {designator!r} / {asm_article!r}"),
        ]

        on_scheme = False
        if scheme:
            on_scheme = norm(asm_article) in {norm(a) for a in scheme["articles"]}
            if on_scheme and docs.get("scheme"):
                tag = scheme["device_tags"].get(des_norm)
                refs.append(_ref(
                    (tag or {}).get("doc") or docs["scheme"]["name"],
                    "scheme", "classified.json",
                    sheet=(tag["sheets"][0] if tag and tag["sheets"] else None),
                    designator=designator, article=asm_article,
                    found=f"на схеме подписан модуль {asm_article!r}"))

        if known_swap:
            kind, severity = "REVIEW", "low"
            finding = (f"У элемента {designator!r} в спецификации код "
                       f"{row.get('code')!r}, а на сборочном чертеже подписан "
                       f"{asm_article!r}. Примечание строки {row.get('row')} "
                       f"({note!r}) описывает эту замену, но колонка кода осталась "
                       f"прежней - заказ пойдёт по старому коду.")
            action = (f"Обновить код оборудования в строке {row.get('row')} на "
                      f"{asm_article!r} либо подтвердить, что закупается "
                      f"{row.get('code')!r}.")
        else:
            kind = "MISMATCH"
            severity = "high" if on_scheme else "medium"
            also = (" Тот же артикул подписан и на принципиальной схеме - "
                    "спецификация расходится с двумя документами связки." if on_scheme else "")
            finding = (f"У элемента {designator!r} в спецификации указан "
                       f"{row.get('code') or row.get('type_mark')!r} "
                       f"(строка {row.get('row')}), а на сборочном чертеже подписан "
                       f"{asm_article!r}.{also}")
            action = (f"Определить, какое изделие устанавливается фактически, и "
                      f"привести спецификацию и чертёж к одному артикулу.")

        findings.append(_finding(
            kind=kind, scope="cross_document", severity=severity,
            type_ru="Разный артикул элемента в документах связки",
            refs=refs[:3], finding=finding, action=action,
            evidence=f"spec row {row.get('row')}: code={row.get('code')!r}, "
                     f"type_mark={row.get('type_mark')!r}, note={note!r}; "
                     f"assembly: {designator!r} -> {asm_article!r} (pair_source=block)",
        ))
    return findings


def _mass_caption_texts(asm):
    """Подписи чертежа, спаренные с массой обозначений.

    Тот же признак и тот же порог, что у _caption_articles, но по ЛЮБОЙ подписи
    элемента, а не только по колонке артикула: номинал на типовой картинке
    изделия ("230VAC" у каждого реле КОС - 630 обозначений) сюда попадает как
    раз через label_text, и без этого фильтра каждое такое реле давало бы
    «на чертеже 230 В, в спецификации 24 В».
    """
    by_text = defaultdict(set)
    for el in asm["elements"]:
        if el.get("pair_source") != "block":
            continue
        for text in (el.get("article"), el.get("label_text")):
            if text:
                by_text[text].add(norm_designator(el.get("designator")))
    return {t for t, des in by_text.items()
            if len(des) >= MASS_CAPTION_MIN_DESIGNATORS}


def rule_characteristic_mismatch(bundle, docs, loaded):
    """У ОДНОГО элемента в спецификации и на чертеже РАЗНЫЕ номиналы.

    Это заказ не того изделия: в спецификации блок питания на 24 В, а на
    чертеже у той же позиции подписано 36 В - значит либо закажут не то, либо
    смонтируют не то. От rule_article_mismatch отличается тем, ЧТО сравнивается:
    там номер изделия по каталогу, здесь - его электрическая характеристика.
    Артикулы могут совпадать (одна серия), а номиналы разойтись, и наоборот.

    Сравниваются только ОДНОИМЁННЫЕ величины и только приведённые к базовой
    единице (см. characteristics): вольты с вольтами, амперы с амперами.

    СРАБАТЫВАЕТ ТОЛЬКО НА НЕПЕРЕСЕКАЮЩИХСЯ МНОЖЕСТВАХ, а не на любом различии.
    У изделия законно несколько номиналов одного вида: у реле катушка 24 В, а
    контакты 250 В, и в спецификации написаны оба. Пока хоть одно значение
    общее - расхождения нет; находка выдаётся, когда общих нет НИ ОДНОГО.

    Источники - только спецификация и чертёж, и только подписи, спаренные с
    обозначением В БЛОКЕ (pair_source='block'). Принципиальная схема сюда НЕ
    входит, хотя номиналы на ней есть: привязать надпись к обозначению на схеме
    можно только по радиусу, а это ровно тот механизм, на котором проверка
    маркировки проводов дала 250 ложных срабатываний (см. schematic_rules,
    заголовок «не вошло»). Для схемы этот вопрос решает стадия зрения.
    """
    spec, asm = loaded.get("spec"), loaded.get("assembly")
    if not spec or not asm:
        return []
    if docs.get("spec", {}).get("project_wide"):
        # Спецификация всего объекта описывает изделия десятка чужих шкафов;
        # совпадение обозначений в ней случайно (1QF1 есть и в ЩС1, и в ШУПЧ1),
        # и сравнивать их номиналы значит сравнивать разные аппараты.
        return []

    captions = _mass_caption_texts(asm)

    by_designator = defaultdict(list)
    for el in asm["elements"]:
        if el.get("pair_source") != "block":
            continue
        texts = [t for t in (el.get("article"), el.get("label_text"))
                 if t and t not in captions]
        if texts:
            # norm, а НЕ norm_designator: ключ обязан совпадать с тем, которым
            # проиндексирована спецификация в _spec_rows_by_designator, иначе
            # поиск строки молча не находит ничего.
            by_designator[norm(el.get("designator"))].append((el, texts))

    spec_rows = _spec_rows_by_designator(spec)

    findings = []
    for designator, entries in sorted(by_designator.items()):
        rows = spec_rows.get(designator)
        if not rows:
            continue
        row = rows[0]
        spec_values = characteristics(row.get("name"), row.get("code"))
        asm_values = characteristics(*[t for _, texts in entries for t in texts])

        for kind in sorted(set(spec_values) & set(asm_values)):
            here, there = spec_values[kind], asm_values[kind]
            if here & there:
                continue
            el = entries[0][0]
            findings.append(_finding(
                "MISMATCH", "cross_document", "high",
                f"разный номинал ({kind}) у одного элемента",
                [_ref(docs["spec"]["name"], "spec", docs["spec"]["source"],
                      row=row.get("row"), designator=designator,
                      name=row.get("name"), found=_fmt_values(here, kind)),
                 _ref(el.get("doc") or docs["assembly"]["name"], "assembly",
                      docs["assembly"]["source"], sheet=el.get("sheet"),
                      designator=designator, article=el.get("article"),
                      found=_fmt_values(there, kind))],
                f"У элемента {designator} {kind} в спецификации и на сборочном "
                f"чертеже не совпадает: {_fmt_values(here, kind)} против "
                f"{_fmt_values(there, kind)}.",
                "Сверить с проектным решением, какой номинал верен, и привести "
                "документы к одному: по спецификации изделие закупают, по "
                "чертежу монтируют.",
                f"спецификация, строка {row.get('row')}: {row.get('name')!r}; "
                f"чертёж: {'; '.join(t for _, ts in entries for t in ts)!r}"))
    return findings


def _fmt_values(values, kind):
    unit = {"напряжение": "В", "ток": "А", "мощность": "Вт",
            "сечение": "мм²", "ёмкость": "А·ч", "частота": "Гц"}.get(kind, "")
    return ", ".join(f"{v:g} {unit}".strip() for v in sorted(values))


ALL_RULES = [
    rule_designation_mismatch,
    rule_article_mismatch,
    rule_characteristic_mismatch,
    rule_article_not_in_spec,
    rule_designator_not_in_spec,
    rule_spec_element_not_on_assembly,
]

SEVERITY_ORDER = _findings.SEVERITY_ORDER


def _pick_project_wide_spec(docs, loaded):
    """Из нескольких спецификаций объекта выбрать ту, что описывает ЭТОТ шкаф.

    В альбоме «24-051-АК» спецификаций всего объекта четыре, по разделам
    проекта: тепломеханических решений (441 строка), газоснабжения, жидкого
    топливоснабжения и автоматизации. Главной по общему правилу (_doc_quality -
    больше строк) становится тепломеханическая, а внутренности щитов лежат в
    той, что про автоматизацию. Сверять щит с чужим разделом бессмысленно и
    громко: замер - 56 находок «нарисовано, но не заказано», все ложные.

    Выбираем по ОБОЗНАЧЕНИЯМ, а не по наименованию раздела: наименование -
    привычка бюро, ровно та причина, по которой связку нельзя угадывать по
    имени файла. Разделение по обозначениям при этом полное: подходящая
    спецификация содержит 55.6-100% обозначений связки, чужая - ровно 0.0%.
    """
    entries = _doc_entries(docs, "spec")
    if len(entries) < 2 or not docs["spec"].get("project_wide"):
        return docs
    asm, scheme = loaded.get("assembly"), loaded.get("scheme")
    if not asm or not scheme:
        return docs
    # Ключи designator_index - СЫРЫЕ обозначения, а device_tags - уже
    # свёрнутые (см. load_scheme). Без приведения пересечение пустое, и выбор
    # молча не срабатывает.
    both = ({norm_designator(d) for d in asm["designator_index"]}
            & set(scheme["device_tags"]))
    if not both:
        return docs

    best, best_hits = None, -1
    for entry in entries:
        spec = load_spec(entry["data_dir"])
        if not spec:
            continue
        found = {norm_designator(d) for it in spec["items"]
                 for d in it.get("designators", [])}
        hits = len(both & found)
        if hits > best_hits:
            best, best_hits = entry, hits
    if best is None or best is entries[0]:
        return docs

    docs = dict(docs)
    docs["spec"] = dict(best, project_wide=True,
                        extra=[e for e in entries if e is not best])
    return docs


def check_bundle(bundle, docs):
    """Главная точка входа.

    bundle: имя связки.
    docs: {"scheme"|"assembly"|"spec": {"name":..., "data_dir":..., "source":...}}
    Каждое значение может нести "extra" - список ДРУГИХ документов того же
    типа этой связки (несколько схем шкафа, чертёж из нескольких частей). Их
    данные объединяются с главным документом, см. load_*_bundle.
    Возвращает находки в формате schema.REPORT_SCHEMA.
    """
    loaded = {}
    if docs.get("assembly"):
        loaded["assembly"] = load_assembly_bundle(_doc_entries(docs, "assembly"))
    if docs.get("scheme"):
        loaded["scheme"] = load_scheme_bundle(_doc_entries(docs, "scheme"))
    if docs.get("spec"):
        # Спецификация выбирается ДО загрузки, но ПОСЛЕ чертежа и схемы: у
        # полного проекта спецификаций объекта бывает несколько, по разделам,
        # и какая из них описывает этот шкаф, видно только по обозначениям.
        docs = _pick_project_wide_spec(docs, loaded)
        # спецификации не объединяются: у связки она одна, а строки двух
        # спецификаций с одинаковыми номерами перемешались бы в ссылках
        loaded["spec"] = load_spec(docs["spec"]["data_dir"])

    # Пустое извлечение - не данные: правила читали бы его как «в документе
    # ничего нет» и штамповали ложные MISSING. Документ выбывает из сверки.
    for dtype in list(loaded):
        if _is_empty(dtype, loaded[dtype]):
            loaded[dtype] = None

    findings = []
    for rule in ALL_RULES:
        findings.extend(rule(bundle, docs, loaded))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return findings


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 bundle_rules.py <data_dir> [имя связки]\n"
              "  data_dir - папка data/ с manifest.json")
        sys.exit(1)

    data_dir = sys.argv[1]
    with open(os.path.join(data_dir, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)

    root = os.path.dirname(os.path.abspath(data_dir))
    groups = defaultdict(dict)
    for d in manifest.get("documents", []):
        groups[d.get("bundle") or "без связки"][d["doc_type"]] = {
            "name": d["name"],
            "data_dir": os.path.join(root, d["data_dir"]),
            "source": d.get("source_file"),
        }

    all_findings = []
    for bundle, docs in groups.items():
        if len(sys.argv) > 2 and bundle != sys.argv[2]:
            continue
        f = check_bundle(bundle, docs)
        print(f"связка {bundle!r}: {len(f)} находок", file=sys.stderr)
        all_findings.extend(f)

    print(json.dumps({"errors": all_findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
