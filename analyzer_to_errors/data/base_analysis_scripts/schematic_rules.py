#!/usr/bin/env python3
"""
Детерминированный чекер принципиальной схемы (по данным schematic_connectivity).

Зачем. Стадия правил в пайплайне до сих пор проверяла ТОЛЬКО таблицы подключений
(netlist_rules.py), а документы типа "scheme" пропускала целиком. Из-за этого
анализ схемы без ИИ всегда давал ноль замечаний - независимо от содержимого
документа. Этот модуль закрывает дыру: находки оформляются в том же формате
schema.REPORT_SCHEMA, что и у netlist_rules и у агентов, и попадают в общую
таблицу пользователя.

ЧТО СЮДА НЕ ВОШЛО И ПОЧЕМУ (замерено на реальных файлах, см. историю проверок).
Соблазнительно навесить правил побольше, но каждое из перечисленного даёт поток
ложных срабатываний - а ложная находка в отчёте дороже пропущенной: инженер идёт
проверять её по чертежу вручную и теряет доверие ко всему отчёту.

  * "Оборванная цепь" (dangling: у цепи меньше двух клемм и меньше двух концов).
    Файл ША1 (профиль C): все 7 кандидатов - это цепи с 0 концов и 4 сегментами,
    то есть ЗАМКНУТЫЕ ПРЯМОУГОЛЬНИКИ (рамки/боксы на листе), а не провода.
    Файл ШУ-ТМ (профиль D): все 101 кандидат - ровно 1 конец и 3-4 сегмента с
    подписью "4"/"N", повторяются сотню раз одинаково - это символы (стрелки,
    земля), а не обрывы. Правило дало бы 108 ложных находок на двух файлах.

  * "Клемма участвует в нескольких цепях". Это НОРМА, а не ошибка: клемма для
    того и нужна, чтобы соединять два провода (с поля и к устройству). На ША1
    сработало бы 11 раз, и все 11 - штатное поведение (XT-AI1:1,3,5,7,9...).

  * "Одна и та же маркировка провода на разных цепях". Наша склейка цепей неидеальна:
    один физический нет, разорванный пробелом в извлечении, выглядит как две цепи
    с одинаковой маркировкой -> ложное "дублирование номера провода".

  * "Пересечение проводов с точкой соединения". В проверенных шаблонах жирных точек
    соединения в PDF нет вообще (соединение рисуется T-стыком), crossings_with_dot=0
    во всех файлах. Правило мертво по построению.

Итог: правил здесь мало и они узкие - зато срабатывают на настоящих дефектах.
Основная сила поиска ошибок по схеме - в СВЕРКЕ СО СХЕМОЙ таблицы подключений
(cross_document), а не в проверке схемы в одиночку.

Использование как библиотека:
    from schematic_rules import check_schematic_file
    findings = check_schematic_file("имя документа", "path/to/nets.json")

Как CLI (для отладки):
    python3 schematic_rules.py path/to/nets.json
"""

import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from schema import REPORT_SCHEMA  # noqa: E402

DOC_TYPE = "scheme"
SOURCE_FILE = "nets.json"


# ============================================================
# Оформление находки в формате schema.REPORT_SCHEMA
# ============================================================

def _ref(document, sheet=None, found=None, terminal_block=None, pin=None,
         marking=None, kks=None, conductor=None):
    return {
        "document": document,
        "doc_type": DOC_TYPE,
        "source_file": SOURCE_FILE,
        "sheet": sheet,
        "row": None,                 # у схемы нет строк таблицы
        "cabinet": None,
        "terminal_block": terminal_block,
        "pin": pin,
        "terminal_type": None,
        "marking": marking,
        "kks": kks,
        "conductor": conductor,
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


# ============================================================
# Правила
# ============================================================

def rule_broken_cross_sheet_link(document, nets_doc):
    """Межлистовая ссылка ведёт на лист, которого в документе нет.

    Самая твёрдая проверка по схеме: ссылка "/12.4:D" при 8 листах в документе -
    это дефект однозначно, никакой интерпретации не требует.

    ВАЖНО: target_sheet=None означает "ссылка в пределах ТОГО ЖЕ листа" (формат
    Delta "(:3D)"), это не ошибка - такие ссылки помечены target_sheet_exists=True
    самим экстрактором и сюда не попадают.
    """
    findings = []
    for r in nets_doc.get("broken_cross_sheet_links", []):
        total = nets_doc["summary"]["total_sheets"]
        findings.append(_finding(
            kind="BROKEN_LINK",
            severity="high",
            type_ru="Ссылка на несуществующий лист",
            refs=[_ref(document, sheet=r.get("from_sheet"),
                       found=f"ссылка {r.get('raw_text')} -> лист {r.get('target_sheet')}")],
            finding=f"На листе {r.get('from_sheet')} межлистовая ссылка "
                    f"{r.get('raw_text')!r} указывает на лист {r.get('target_sheet')}, "
                    f"но в документе всего {total} листов.",
            action="Исправить номер листа в ссылке либо добавить недостающий лист.",
            evidence=f"cross_sheet_link: {json.dumps(r, ensure_ascii=False)}",
        ))
    return findings


def rule_cross_ref_without_counterpart(document, nets_doc):
    """Межлистовая связь без ответной части.

    Ссылка с листа A на лист B означает, что цепь продолжается на листе B. В
    исправном комплекте на листе B есть встречная ссылка обратно на лист A. Если
    встречных ссылок НЕТ НИ ОДНОЙ - связь односторонняя, цепь на том листе
    "приходит из ниоткуда".

    Проверка сознательно грубая - на уровне пары листов, а не отдельной ссылки:
    точное сопоставление ссылка-в-ссылку требует координат зоны и даёт ложные
    срабатывания на несовершенстве извлечения. Замер на реальном файле (ША1,
    93 ссылки): все встречные пары нашлись, правило не сработало ни разу -
    то есть ложных находок оно не плодит.
    """
    links = [l for l in nets_doc.get("cross_sheet_links", [])
             if l.get("target_sheet") is not None]
    if not links:
        return []

    pair_count = defaultdict(int)
    for l in links:
        pair_count[(l["from_sheet"], l["target_sheet"])] += 1

    findings = []
    reported = set()
    for (src, dst), n in sorted(pair_count.items()):
        if pair_count.get((dst, src), 0) > 0:
            continue                      # встречные ссылки есть - всё в порядке
        if (src, dst) in reported:
            continue
        reported.add((src, dst))
        examples = [l["raw_text"] for l in links
                    if l["from_sheet"] == src and l["target_sheet"] == dst][:5]
        findings.append(_finding(
            kind="BROKEN_LINK",
            severity="medium",
            type_ru="Межлистовая связь без ответной части",
            refs=[_ref(document, sheet=src,
                       found=f"лист {src}: {n} ссылок на лист {dst}"),
                  _ref(document, sheet=dst,
                       found=f"лист {dst}: встречных ссылок на лист {src} не найдено")],
            finding=f"С листа {src} на лист {dst} ведёт {n} межлистовых ссылок "
                    f"({', '.join(examples)}), но на листе {dst} нет ни одной "
                    f"встречной ссылки обратно на лист {src}.",
            action=f"Проверить, куда приходят эти цепи на листе {dst}, и проставить "
                   f"встречные ссылки на лист {src}.",
            evidence=f"ссылок {src}->{dst}: {n}; ссылок {dst}->{src}: 0",
        ))
    return findings


ALL_RULES = [
    rule_broken_cross_sheet_link,
    rule_cross_ref_without_counterpart,
]

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def check_schematic(document, nets_doc):
    """Главная точка входа: содержимое nets.json -> список находок
    в формате schema.REPORT_SCHEMA (тот же, что у netlist_rules и агентов)."""
    findings = []
    for rule in ALL_RULES:
        findings.extend(rule(document, nets_doc))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return findings


def check_schematic_file(document, nets_path):
    with open(nets_path, encoding="utf-8") as f:
        nets_doc = json.load(f)
    return check_schematic(document, nets_doc)


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 schematic_rules.py path/to/nets.json")
        sys.exit(1)
    path = sys.argv[1]
    document = os.path.basename(os.path.dirname(os.path.abspath(path)))
    findings = check_schematic_file(document, path)

    from jsonschema import Draft7Validator
    errs = list(Draft7Validator(REPORT_SCHEMA).iter_errors({"errors": findings}))
    print(f"Найдено {len(findings)} находок; валидны по схеме: {'да' if not errs else errs}")
    print(json.dumps({"errors": findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
