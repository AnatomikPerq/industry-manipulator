#!/usr/bin/env python3
"""
Детерминированный чекер СПЕЦИФИКАЦИИ (по данным specification_to_json.py).

Проверки ВНУТРИ одного документа. Сверка спецификации с чертежом и схемой -
в bundle_rules.py, здесь её нет.

--------------------------------------------------------------------------
ЧТО СЮДА НЕ ВОШЛО И ПОЧЕМУ (замерено на СО связок ША1 и ШУ-ТМ)
--------------------------------------------------------------------------
  * "Количество меньше числа позиционных обозначений" БЕЗ оговорки про
    количество = 1. ОТКЛОНЕНО в таком виде: на ШУ-ТМ строка 34 - это АВР-304,
    ОДНО изделие, обслуживающее ДВА выключателя ('QF01, QF02'), количество 1.
    Правило "qty < число обозначений" срабатывало на двух файлах ровно один
    раз - и это был как раз тот случай, то есть 1 ложная находка и 0 верных.
    Оставлена версия с условием quantity > 1 (см. rule_quantity_less_than_
    designators): "одно изделие на несколько позиций" объясняет ТОЛЬКО
    количество 1, а количество 2 при пяти обозначениях так не объяснить.
    С этой оговоркой на обоих файлах правило даёт 0 срабатываний, то есть
    ложных находок не плодит.

  * "Одно обозначение в нескольких строках" = дубль. ОТКЛОНЕНО: это норма.
    У реле четыре строки на одно обозначение (само реле + колодка + фиксатор +
    шильдик), у кнопки - две (кнопка + защитная крышка). На ША1 таких
    обозначений 20+, все законные.

  * "Строка без кода оборудования". ОТКЛОНЕНО: у оргстекла, медной шины и
    стали кода нет и не будет - их не заказывают по артикулу.
"""

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import findings as _findings  # noqa: E402  (общая форма находки и ref'а)


DOC_TYPE = "spec"
SOURCE_FILE = "specification.json"


def _ref(document, row=None, designator=None, article=None, name=None,
         quantity=None, found=None):
    return _findings.ref(
        document, DOC_TYPE, SOURCE_FILE,
        row=row, designator=designator, article=article, name=name,
        quantity=quantity, found=found)


def _finding(kind, severity, type_ru, refs, finding, action, evidence=None):
    return _findings.finding(kind, severity, type_ru, refs, finding, action,
                            evidence, scope="single_document")


def rule_cell_errors(document, doc):
    """Ошибка формулы Excel (#REF!, #VALUE!, #DIV/0!) в ячейке спецификации.

    Самая твёрдая проверка по этому документу: #REF! - это не мнение, а факт,
    что формула потеряла ссылку. Значение в такой ячейке не прочитать ни
    человеку, ни программе. Обычно означает, что из книги удалили строку или
    лист, на которые ссылались итоги.
    """
    findings = []
    for err in doc.get("cell_errors", []):
        findings.append(_finding(
            kind="FORMAT",
            severity="medium",
            type_ru="Ошибка формулы в ячейке спецификации",
            refs=[_ref(document, row=err.get("row"),
                       found=f"ячейка {err.get('cell')}: {err.get('value')}")],
            finding=f"В ячейке {err.get('cell')} спецификации стоит "
                    f"{err.get('value')} - формула потеряла ссылку, значение в "
                    f"этой ячейке отсутствует.",
            action="Восстановить формулу либо вписать значение вручную: сейчас "
                   "этой ячейки в документе фактически нет.",
            evidence=f"cell_errors: {json.dumps(err, ensure_ascii=False)}",
        ))
    return findings


def rule_quantity_less_than_designators(document, doc):
    """Количество меньше числа позиционных обозначений в строке.

    Каждому позиционному обозначению нужен минимум один экземпляр изделия:
    пять обозначений и количество 2 - смету по такой строке не собрать.

    Условие quantity > 1 - НЕ перестраховка, а результат замера (см. шапку
    модуля): количество 1 при нескольких обозначениях законно означает одно
    изделие, обслуживающее несколько позиций (АВР на два выключателя), и без
    этой оговорки правило давало только ложные находки.

    Обратное (количество БОЛЬШЕ числа обозначений) не проверяется вовсе: у
    клеммников в «Позиция» стоят обозначения клеммников, а в «Количество» -
    число клемм, и там 27 против 7 - это норма.
    """
    findings = []
    for it in doc.get("items", []):
        des = it.get("designators") or []
        qty = it.get("quantity")
        if qty is None or len(des) < 2 or qty <= 1:
            continue
        if qty >= len(des):
            continue
        findings.append(_finding(
            kind="INCOMPLETE",
            severity="medium",
            type_ru="Количество меньше числа позиций в строке",
            refs=[_ref(document, row=it.get("row"),
                       designator=", ".join(des[:8]), article=it.get("code"),
                       name=it.get("name"), quantity=qty,
                       found=f"строка {it.get('row')}: позиции "
                             f"{it.get('position_raw')!r}, количество {qty:g}")],
            finding=f"В строке {it.get('row')} перечислено {len(des)} позиционных "
                    f"обозначений ({it.get('position_raw')!r}), а количество указано "
                    f"{qty:g} - на каждую позицию изделия не хватает.",
            action=f"Проверить количество в строке {it.get('row')}: должно быть не "
                   f"меньше {len(des)} либо список позиций сокращён.",
            evidence=f"position_raw={it.get('position_raw')!r} -> {len(des)} обозначений; "
                     f"quantity={qty:g}",
        ))
    return findings


def rule_duplicate_code(document, doc):
    """Один и тот же код оборудования в нескольких строках.

    Чаще всего это ревизия: строка основной части и строка раздела
    "Дополнительно от ..." с тем же артикулом и другим количеством. Формально
    это не ошибка - но по такой спецификации НЕЛЬЗЯ понять, сколько изделий
    заказывать: 21 или 19. Поэтому REVIEW, а не ошибка: вопрос инженеру, а не
    приговор документу.

    ТОЛЬКО ДЛЯ СПЕЦИФИКАЦИИ ОДНОГО ШКАФА (см. SINGLE_CABINET_ONLY_RULES). В
    спецификации полного проекта один артикул в нескольких строках - норма, а
    не ревизия: один и тот же автомат стоит в десятке разных щитов, и каждый
    щит занимает свои строки. Замер на "24-051-ЭОМ": 112 находок на 837 строк,
    все до одной ложные.
    """
    by_code = defaultdict(list)
    for it in doc.get("items", []):
        if it.get("code"):
            by_code[it["code"]].append(it)

    findings = []
    for code, rows in sorted(by_code.items()):
        if len(rows) < 2:
            continue
        qtys = [r.get("quantity") for r in rows]
        sections = [r.get("section") or "основная часть" for r in rows]
        refs = [_ref(document, row=r.get("row"),
                     designator=", ".join((r.get("designators") or [])[:4]) or None,
                     article=code, name=r.get("name"), quantity=r.get("quantity"),
                     found=f"строка {r.get('row')} ({sections[i]}): количество "
                           f"{r.get('quantity')}")
                for i, r in enumerate(rows[:2])]
        findings.append(_finding(
            kind="REVIEW",
            severity="low",
            type_ru="Один артикул в нескольких строках спецификации",
            refs=refs,
            finding=f"Код оборудования {code!r} встречается в {len(rows)} строках "
                    f"({', '.join(str(r.get('row')) for r in rows)}) с количествами "
                    f"{', '.join(f'{q:g}' if q is not None else '?' for q in qtys)} "
                    f"(разделы: {', '.join(sections)}). По документу невозможно "
                    f"однозначно определить итоговое количество к заказу.",
            action=f"Указать, заменяет ли строка из раздела «{sections[-1]}» строку "
                   f"основной части, или количества суммируются.",
            evidence=f"code={code!r}: строки {[r.get('row') for r in rows]}, "
                     f"количества {qtys}, разделы {sections}",
        ))
    return findings


ALL_RULES = [
    rule_cell_errors,
    rule_quantity_less_than_designators,
    rule_duplicate_code,
]

SEVERITY_ORDER = _findings.SEVERITY_ORDER


# Правила, которые верны только для спецификации ОДНОГО шкафа. В полном проекте
# спецификация одна на весь объект (полтора десятка щитов), и предпосылка этих
# правил там не выполняется - см. комментарий в rule_duplicate_code.
SINGLE_CABINET_ONLY_RULES = (rule_duplicate_code,)


def check_specification(document, doc, project_wide=False):
    findings = []
    for rule in ALL_RULES:
        if project_wide and rule in SINGLE_CABINET_ONLY_RULES:
            continue
        findings.extend(rule(document, doc))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return findings


def check_specification_file(document, spec_path, project_wide=False):
    """project_wide: спецификация описывает весь объект, а не один шкаф."""
    with open(spec_path, encoding="utf-8") as f:
        doc = json.load(f)
    return check_specification(document, doc, project_wide=project_wide)


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 spec_rules.py path/to/specification.json")
        sys.exit(1)
    path = sys.argv[1]
    document = os.path.basename(os.path.dirname(os.path.abspath(path)))
    findings = check_specification_file(document, path)
    print(json.dumps({"errors": findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
