#!/usr/bin/env python3
"""
Детерминированный чекер СБОРОЧНОГО ЧЕРТЕЖА (СБ) - по данным assembly.json.

Зачем. Считалось, что в одиночку сборочный чертёж проверять нечем: всё, что на
нём есть, надо сверять со спецификацией и схемой (этим занят bundle_rules.py).
Это оказалось неверно. Чертёж - МНОГОЛИСТОВЫЙ документ, и одно и то же изделие
показано на нескольких листах: на общем виде (где оно стоит в шкафу), на виде
двери, в таблице надписей. Изделие, выпавшее с ОДНОГО из этих листов, - дефект,
который виден внутри самого чертежа, без всякой спецификации.

Именно так выглядели две ошибки в комплекте ЩСКЗ: батарея GB1 подписана на
листе 3, но на общем виде (лист 1) её нет - в шкафу нарисована только GB2;
лампа HL5 есть в таблице ламп двери (лист 5), но на самой двери (лист 1) её не
нарисовали. Обе ошибки монтажные: по такому чертежу изделие просто не поставят.

--------------------------------------------------------------------------
ЧТО СЮДА НЕ ВОШЛО И ПОЧЕМУ
--------------------------------------------------------------------------
  * "Обозначение подписано на листе дважды". ОТКЛОНЕНО: это НОРМА. Одно изделие
    законно показано на чертеже по нескольку раз (вид спереди + вид сбоку +
    разрез + выноска), и на одном листе тоже.

  * "Одно обозначение подписано двумя разными артикулами" - замерено и отклонено
    ещё раньше, см. шапку assembly_drawing_to_data.py.

  * Сверка со спецификацией и схемой - НЕ здесь: это другой документ, значит
    другая стадия (bundle_rules.py). Здесь только то, что доказывается ОДНИМ
    документом.

Использование как библиотека:
    from assembly_rules import check_assembly_file
    findings = check_assembly_file("имя документа", "path/to/assembly.json")
"""

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

DOC_TYPE = "assembly"
SOURCE_FILE = "assembly.json"

# Насколько плотно листы должны пересекаться, чтобы считать их показывающими
# ОДИН И ТОТ ЖЕ набор изделий. См. rule_element_missing_from_peer_sheet.
PEER_SHEET_COVERAGE = 0.90
# Сколько общих обозначений нужно, чтобы пересечение вообще о чём-то говорило.
PEER_SHEET_MIN_SHARED = 8
# Больше скольких изделий может "потерять" лист, оставаясь парой другому.
PEER_SHEET_MAX_MISSING = 3


def _ref(document, sheet=None, designator=None, found=None):
    return {
        "document": document,
        "doc_type": DOC_TYPE,
        "source_file": SOURCE_FILE,
        "sheet": sheet,
        "row": None,
        "cabinet": None,
        "terminal_block": None,
        "pin": None,
        "terminal_type": None,
        "marking": None,
        "kks": None,
        "conductor": None,
        "designator": designator,
        "article": None,
        "name": None,
        "quantity": None,
        "found": found,
    }


def _finding(kind, severity, type_ru, refs, finding, action, evidence=None):
    return {
        "kind": kind,
        "scope": "single_document",
        "severity": severity,
        "type": type_ru,
        "refs": refs,
        "finding": finding,
        "action": action,
        "evidence": evidence,
    }


def rule_element_missing_from_peer_sheet(document, asm):
    """Изделие есть на одном листе чертежа и пропало с ПАРНОГО ему листа.

    КАК ОПРЕДЕЛЯЕТСЯ "ПАРНЫЙ ЛИСТ" - и почему не по названию вида. Соблазнительно
    искать лист со штампом "Вид общий" и требовать, чтобы на нём было всё. Но
    название вида - привычка бюро (та же ошибка, что с именами файлов, см.
    CLAUDE.md): "Вид общий", "Вид спереди", "Компоновка" - и на каждом чертеже
    по-своему. Поэтому парность выводится ИЗ САМИХ ДАННЫХ: если почти все
    обозначения листа B встречаются и на листе A, значит эти листы показывают
    один и тот же набор изделий, и те немногие, что на A не попали, - выпали.

    Порог намеренно высокий (>=90% обозначений листа и не больше 3 пропавших):
    правило должно срабатывать на "лист А - копия листа B минус одно изделие", а
    не на любых двух листах с общими элементами. Замер на чертеже ЩСКЗ (6 листов):
      лист 3 -> лист 1: покрытие 98%, не хватает ровно ['GB1']   <- ошибка
      лист 5 -> лист 1: покрытие 92%, не хватает ровно ['HL5']   <- ошибка
      лист 1 -> лист 3: покрытие 80%  -> порог не пройден, молчим (на листе 1
                        законно есть дверь, которой нет на листе 3)
      лист 1 -> лист 5: покрытие 17%, лист 5 -> лист 3: 0%  -> молчим
    То есть на реальном чертеже правило дало ровно две находки, и обе настоящие.

    Почему REVIEW, а не MISSING: доказано, что обозначения нет на парном листе, -
    но не то, что оно там ОБЯЗАНО быть. Изделие могло законно не попасть на вид
    (стоит с обратной стороны панели, показано на другом разрезе). Это вопрос
    инженеру, а не приговор.
    """
    per_sheet = defaultdict(set)
    for e in asm.get("elements", []):
        if e.get("designator") and e.get("sheet") is not None:
            per_sheet[e["sheet"]].add(e["designator"])
    if len(per_sheet) < 2:
        return []

    findings = []
    for src in sorted(per_sheet):
        for dst in sorted(per_sheet):
            if src == dst:
                continue
            shared = per_sheet[src] & per_sheet[dst]
            missing = per_sheet[src] - per_sheet[dst]
            if len(shared) < PEER_SHEET_MIN_SHARED or not missing:
                continue
            if len(missing) > PEER_SHEET_MAX_MISSING:
                continue
            coverage = len(shared) / len(per_sheet[src])
            if coverage < PEER_SHEET_COVERAGE:
                continue

            for des in sorted(missing):
                findings.append(_finding(
                    kind="REVIEW",
                    severity="medium",
                    type_ru="Изделие пропало с парного листа чертежа",
                    refs=[
                        _ref(document, sheet=src, designator=des,
                             found=f"лист {src}: обозначение {des!r} подписано"),
                        _ref(document, sheet=dst, designator=des,
                             found=f"лист {dst}: обозначения {des!r} нет, хотя "
                                   f"{len(shared)} из {len(per_sheet[src])} изделий "
                                   f"листа {src} на нём есть"),
                    ],
                    finding=f"Изделие {des!r} подписано на листе {src}, но на листе "
                            f"{dst} его нет. При этом листы {src} и {dst} показывают "
                            f"один и тот же набор изделий: {len(shared)} из "
                            f"{len(per_sheet[src])} обозначений листа {src} "
                            f"({coverage:.0%}) на листе {dst} присутствуют. "
                            f"Похоже, изделие забыли нанести.",
                    action=f"Проверить лист {dst}: нанести {des!r} либо подтвердить, "
                           f"что на этом виде изделие не показывается.",
                    evidence=f"лист {src}: {len(per_sheet[src])} обозначений; "
                             f"лист {dst}: {len(per_sheet[dst])}; общих {len(shared)} "
                             f"({coverage:.0%}); нет на листе {dst}: {sorted(missing)}",
                ))
    return findings


ALL_RULES = [
    rule_element_missing_from_peer_sheet,
]

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def check_assembly(document, asm):
    """Главная точка входа: содержимое assembly.json -> список находок
    в формате schema.REPORT_SCHEMA."""
    findings = []
    for rule in ALL_RULES:
        findings.extend(rule(document, asm))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return findings


def check_assembly_file(document, assembly_path):
    with open(assembly_path, encoding="utf-8") as f:
        asm = json.load(f)
    return check_assembly(document, asm)


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 assembly_rules.py path/to/assembly.json")
        sys.exit(1)
    path = sys.argv[1]
    document = os.path.basename(os.path.dirname(os.path.abspath(path)))
    findings = check_assembly_file(document, path)

    from schema import REPORT_SCHEMA
    from jsonschema import Draft7Validator
    errs = list(Draft7Validator(REPORT_SCHEMA).iter_errors({"errors": findings}))
    print(f"Найдено {len(findings)} находок; валидны по схеме: {'да' if not errs else errs}")
    print(json.dumps({"errors": findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
