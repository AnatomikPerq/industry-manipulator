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


# ============================================================
# Нормализация
# ============================================================

# Кириллические буквы, неотличимые на вид от латинских. В российских проектах
# их мешают постоянно: 'ХТ01' кириллицей и 'XT01' латиницей выглядят
# ОДИНАКОВО, но это разные строки. Без сворачивания омоглифов сверка
# документов дала бы поток ложных "элемент не найден" на ровном месте.
HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
})

_NORM_STRIP_RE = re.compile(r"[\s]+")


def norm(s):
    """Ключ сравнения для АРТИКУЛОВ: омоглифы свёрнуты в латиницу, регистр и
    пробелы убраны. Ведущие нули НЕ трогаются: у артикулов каждая цифра
    значащая ('8000099046' и '800099046' - разные изделия)."""
    if s is None:
        return ""
    return _NORM_STRIP_RE.sub("", str(s).translate(HOMOGLYPHS).upper())


# Ведущие нули в числовой группе. В обозначениях они НЕЗНАЧАЩИЕ: спецификация
# пишет 'XA1', сборочный чертёж - 'XA001', и это одна и та же клеммная колодка.
# Без сворачивания нулей сверка ША1/ИК давала десятки ложных "изделия нет на
# чертеже" на ровном месте (XA1..XA19 против XA001..XA019 - 19 находок из воздуха).
_LEADING_ZERO_RE = re.compile(r"(?<![0-9])0+(?=[0-9])")


def norm_designator(s):
    """Ключ сравнения для ПОЗИЦИОННЫХ ОБОЗНАЧЕНИЙ: как norm(), плюс свёрнутые
    ведущие нули. Только ключ сравнения - наружу (в находку) всегда идёт
    обозначение в том виде, в каком оно написано в документе."""
    return _LEADING_ZERO_RE.sub("", norm(s))


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
# Оформление находки
# ============================================================

def _ref(document, doc_type, source_file, sheet=None, row=None, designator=None,
         article=None, name=None, quantity=None, found=None):
    return {
        "document": document,
        "doc_type": doc_type,
        "source_file": source_file,
        "sheet": sheet,
        "row": row,
        "cabinet": None,
        "terminal_block": None,
        "pin": None,
        "terminal_type": None,
        "marking": None,
        "kks": None,
        "conductor": None,
        "designator": designator,
        "article": article,
        "name": name,
        "quantity": quantity,
        "found": found,
    }


def _finding(kind, scope, severity, type_ru, refs, finding, action, evidence=None):
    return {
        "kind": kind,
        "scope": scope,
        "severity": severity,
        "type": type_ru,
        "refs": refs,
        "finding": finding,
        "action": action,
        "evidence": evidence,
    }


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
            refs.append(_ref(
                docs["scheme"]["name"], "scheme", "classified.json",
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

    findings = []
    for article, elems in sorted(by_article.items()):
        a_norm = norm(article)
        if not a_norm or a_norm in spec_articles:
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
        designators = sorted({e["designator"] for e in elems})
        sheets = sorted({e["sheet"] for e in elems})
        findings.append(_finding(
            kind="MISSING",
            scope="cross_document",
            severity="high",
            type_ru="Изделие с чертежа отсутствует в спецификации",
            refs=[
                _ref(docs["assembly"]["name"], "assembly", "assembly.json",
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

    findings = []
    for des, info in sorted(asm["designator_index"].items()):
        des_norm = norm_designator(des)
        if not des_norm or des_norm in spec_designators:
            continue
        tag = scheme["device_tags"].get(des_norm)
        if not tag:
            continue                       # на схеме нет - правило молчит

        asm_sheets = info.get("sheets") or []
        sch_sheets = tag.get("sheets") or []
        findings.append(_finding(
            kind="MISSING",
            scope="cross_document",
            severity="high",
            type_ru="Изделие с чертежа и схемы отсутствует в спецификации",
            refs=[
                _ref(docs["spec"]["name"], "spec", "specification.json",
                     designator=des,
                     found=f"обозначения {des!r} нет ни в одной из "
                           f"{spec['statistics'].get('total_rows')} строк спецификации"),
                _ref(docs["assembly"]["name"], "assembly", "assembly.json",
                     sheet=(asm_sheets[0] if asm_sheets else None), designator=des,
                     found=f"подписано на листах {asm_sheets}" if asm_sheets
                           else f"обозначение {des!r} подписано на чертеже"),
                _ref(docs["scheme"]["name"], "scheme", "classified.json",
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

    # обозначение -> артикулы, спаренные в блоке
    asm_block = defaultdict(set)
    asm_meta = {}
    for e in asm["elements"]:
        if e.get("pair_source") != "block" or not e.get("article"):
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
            _ref(docs["assembly"]["name"], "assembly", "assembly.json",
                 sheet=e.get("sheet"), designator=designator, article=asm_article,
                 found=f"лист {e.get('sheet')}: подпись {designator!r} / {asm_article!r}"),
        ]

        on_scheme = False
        if scheme:
            on_scheme = norm(asm_article) in {norm(a) for a in scheme["articles"]}
            if on_scheme and docs.get("scheme"):
                tag = scheme["device_tags"].get(des_norm)
                refs.append(_ref(
                    docs["scheme"]["name"], "scheme", "classified.json",
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


ALL_RULES = [
    rule_designation_mismatch,
    rule_article_mismatch,
    rule_article_not_in_spec,
    rule_designator_not_in_spec,
    rule_spec_element_not_on_assembly,
]

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def check_bundle(bundle, docs):
    """Главная точка входа.

    bundle: имя связки.
    docs: {"scheme"|"assembly"|"spec": {"name":..., "data_dir":..., "source":...}}
    Возвращает находки в формате schema.REPORT_SCHEMA.
    """
    loaded = {}
    loaders = {"spec": load_spec, "assembly": load_assembly, "scheme": load_scheme}
    for dtype, loader in loaders.items():
        if docs.get(dtype):
            loaded[dtype] = loader(docs[dtype]["data_dir"])

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
