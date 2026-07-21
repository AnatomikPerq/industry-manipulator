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

  * "У ОДНОЙ цепи две разных маркировки" (обратная предыдущей: цепь несёт номер 5,
    а один её отрезок подписан 6 - настоящая ошибка, такая в файле ЩСКЗ есть).
    ОТКЛОНЕНО ПО ЗАМЕРУ: правило даёт 250 находок на трёх файлах (ЩСКЗ 35,
    ША1 78, ШУ-ТМ 137), а настоящих среди них 2. Причина не в склейке цепей -
    её починили (см. drop_glyph_hairlines в schematic_connectivity.py, самая
    большая цепь листа упала с 2837 отрезков до 39), - а в ПРИВЯЗКЕ МАРКИРОВКИ
    К ЦЕПИ: маркировка ищется по расстоянию до провода (MARKING_MAX_DIST=9pt), и
    на плотном листе номер соседнего параллельного провода лежит к чужой цепи
    ближе, чем к своей. Отсюда "цепи" с markings=['1','2','3','4','5'] - это не
    ошибки чертежа, а собранные с полулиста чужие номера. Чтобы правило стало
    честным, маркировку надо привязывать не по радиусу, а по принадлежности
    отрезку (проекция на сам провод, а не на цепь) - это отдельная работа по
    извлечению, до неё правило не имеет смысла.
    Ошибки такого рода пока ищут агенты-нейросети.

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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import findings as _findings  # noqa: E402  (общая форма находки и ref'а)

from schema import REPORT_SCHEMA  # noqa: E402

DOC_TYPE = "scheme"
SOURCE_FILE = "nets.json"


# ============================================================
# Оформление находки в формате schema.REPORT_SCHEMA
# ============================================================

def _ref(document, sheet=None, found=None, terminal_block=None, pin=None,
         marking=None, kks=None, conductor=None, designator=None):
    # row у схемы нет по построению: строк таблицы в ней не существует
    return _findings.ref(
        document, DOC_TYPE, SOURCE_FILE,
        sheet=sheet, terminal_block=terminal_block, pin=pin, marking=marking,
        kks=kks, conductor=conductor, designator=designator, found=found)


def _finding(kind, severity, type_ru, refs, finding, action, evidence=None):
    return _findings.finding(kind, severity, type_ru, refs, finding, action,
                            evidence, scope="single_document")


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


def rule_duplicate_terminal_address(document, nets_doc):
    """Один и тот же адрес клеммы подписан на листе дважды.

    Вывод клеммника - одна физическая точка. Два места на листе с адресом
    '1XT5:3' означают, что монтажнику некуда садить провод: либо соседний вывод
    забыли перенумеровать (сплошь и рядом при правке подписи руками), либо две
    разные клеммы получили один адрес.

    Проверка не зависит от склейки цепей - только от подписей, - поэтому
    работает даже там, где геометрия распозналась плохо. Факты собирает
    schematic_connectivity.find_duplicate_terminal_addresses.

    Замер: ЩСКЗ - 2 находки, обе настоящие (проверены по чертежу: на листе 10.4
    ряд '1XT1:3->1XT1:4 ... 1XT4:3->1XT4:4' заканчивается '1XT5:3->1XT5:3';
    на листе 10.5 две соседние клеммы, '+' и '-' одного прибора, обе '5XT1:1').
    ША1 - 0, ШУ-ТМ - 0.
    """
    # Сколько РАЗНЫХ адресов одного клеммника задублировано на листе. Одиночный
    # дубль ('1XT5:3' дважды при уникальных соседях :1,:2,:4) - опечатка
    # перенумерации, ровно те настоящие ошибки ЩСКЗ. А вот когда у клеммника
    # задублирован ЦЕЛЫЙ РЯД адресов ('7X1:1', ':2', ':3', ':4' - все по два
    # раза), это не четыре опечатки разом, а повторно изображённый клеммник:
    # на ЩОВ (ЭОМ) типовая обвязка двух вентагрегатов нарисована на одном листе
    # дважды, и в каждой показан клеммник ЧУЖОГО шкафа ШУК - все 26 «дублей»
    # листа были такими. Групповые дубли пропускаем.
    dup_addrs_per_block = defaultdict(set)
    for d in nets_doc.get("duplicate_terminal_addresses", []):
        block, _, pin = d["address"].partition(":")
        dup_addrs_per_block[(d.get("sheet"), block)].add(d["address"])

    findings = []
    for d in nets_doc.get("duplicate_terminal_addresses", []):
        addr = d["address"]
        block, _, pin = addr.partition(":")
        # Клемма шины N/PE - не «одна физическая точка»: на однолинейных и
        # принципиальных щитах освещения (ЭОМ) нулевая и защитная шины
        # подписаны у КАЖДОГО присоединения ('X1:N' пять раз на листе - это
        # пять посадочных мест шины, а не пять клемм с одним адресом).
        # Замер на ЭОМ: все 6 находок по N/PE - ровно такие; числовые
        # адреса (как настоящие ошибки ЩСКЗ 1XT5:3 и 5XT1:1) не трогаем.
        if pin.strip().upper() in ("N", "PE", "PEN"):
            continue
        if len(dup_addrs_per_block[(d.get("sheet"), block)]) >= 2:
            continue        # повторно изображённый клеммник, см. выше
        places = ", ".join(f"({p['x']}, {p['y']})" for p in d["positions"])
        findings.append(_finding(
            kind="DUPLICATE",
            severity="high",
            type_ru="Дубль адреса клеммы на листе",
            refs=[_ref(document, sheet=d["sheet"], terminal_block=block, pin=pin,
                       found=f"лист {d['sheet']}: подпись {addr!r} в позиции "
                             f"({p['x']}, {p['y']})")
                  for p in d["positions"][:2]],
            finding=f"На листе {d['sheet']} адрес клеммы {addr!r} подписан "
                    f"{d['count']} раза в разных местах листа. Вывод клеммника - "
                    f"одна физическая точка, двух клемм с одним адресом быть не может.",
            action=f"Сверить нумерацию выводов клеммника {block!r} на листе "
                   f"{d['sheet']}: один из выводов должен получить свой номер.",
            evidence=f"адрес {addr!r}, лист {d['sheet']}, позиции: {places}",
        ))
    return findings


def rule_duplicate_relay_coil(document, nets_doc):
    """Две КАТУШКИ реле с одним обозначением - значит, у двух реле одно имя.

    Обратите внимание, чего здесь НЕТ: правила "обозначение встречается на схеме
    дважды". Это НЕ ошибка - у реле одна катушка и сколько угодно контактов, все
    подписаны одинаково, и на нормальной схеме '1KL1' встречается по пять раз.
    Ошибка - именно две КАТУШКИ: катушка у реле одна, две катушки с одним именем
    означают, что соседнее реле осталось без обозначения.

    Катушки опознаёт schematic_connectivity.find_relay_coils - по выводам A1/A2
    (МЭК), а не по картинке символа.

    Замер: ЩСКЗ - 1 находка (лист 10.3: катушка '2KL2' нарисована дважды, левая
    должна быть '2KL1' - её контакт на листе 10.4 есть, а катушки нет).
    ША1 - 16 катушек, 0 находок. ШУ-ТМ - 181 катушка, 0 находок.
    """
    findings = []
    for d in nets_doc.get("duplicate_relay_coils", []):
        des = d["designator"]
        places = d["places"]
        sheets = sorted({p["sheet"] for p in places})
        where = "; ".join(f"лист {p['sheet']}, позиция ({p['x']}, {p['y']})"
                          for p in places)
        findings.append(_finding(
            kind="DUPLICATE",
            severity="high",
            type_ru="Две катушки реле с одним обозначением",
            refs=[_ref(document, sheet=p["sheet"], designator=des,
                       found=f"лист {p['sheet']}: катушка {des!r} (выводы A1/A2) "
                             f"в позиции ({p['x']}, {p['y']})")
                  for p in places[:2]],
            finding=f"Обозначение {des!r} стоит у {d['count']} РАЗНЫХ катушек реле "
                    f"(лист{'ы' if len(sheets) > 1 else ''} "
                    f"{', '.join(map(str, sheets))}). У реле катушка одна, значит это "
                    f"два разных реле с одним именем: соседнее реле осталось без "
                    f"своего обозначения.",
            action=f"Проверить обозначения реле: одной из катушек {des!r} нужно "
                   f"вернуть её собственное обозначение (сверить с контактами этого "
                   f"реле на других листах и со спецификацией).",
            evidence=f"катушки {des!r}: {where}",
        ))
    return findings


ALL_RULES = [
    rule_broken_cross_sheet_link,
    rule_cross_ref_without_counterpart,
    rule_duplicate_terminal_address,
    rule_duplicate_relay_coil,
]

SEVERITY_ORDER = _findings.SEVERITY_ORDER


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
