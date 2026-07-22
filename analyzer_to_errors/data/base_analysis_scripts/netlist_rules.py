#!/usr/bin/env python3
"""
Детерминированный чекер таблицы подключений (нетлиста).

Зачем он нужен. Разбор отчёта "умной" нейросети (та, которой PDF загрузили целиком)
показал: почти все её твёрдые находки лежат ВНУТРИ одной таблицы и берутся простыми
правилами - дубль физического адреса, тег в неправильной колонке, лишний суффикс в
KKS, незаполненные поля. Проверять это нейросетью дорого и ненадёжно (модель галлю-
цинирует и пропускает очевидное), тогда как правило либо находит ошибку, либо нет -
за миллисекунды и бесплатно. Поэтому такие находки снимаем кодом ДО запуска агентов,
а нейросети оставляем то, что правилами не берётся (сверка со схемой, формулировки).

ВАЖНО: находки этого чекера оформляются РОВНО в том же формате, что и находки
агентов (schema.REPORT_SCHEMA - kind/scope/severity/refs/finding/action/...), чтобы
они сливались в общий список и показывались пользователю в единой таблице, без
отдельной ветки в рендере.

Сюда НЕ входят спорные проверки. Например "смещение колонок" у резервных каналов
(kks='Резерв', тег в колонке conductor) "умная" модель насчитала 125 раз и записала
в критические - но у резервного канала устройства-владельца попросту нет, и куда
писать имя канала это вопрос оформления, а не ошибка. Такое либо не трогаем, либо
сводим в одну справочную запись уровня info (см. rule_reserve_tag_placement).

Использование как библиотека:
    from netlist_rules import check_connections
    findings = check_connections("ИК.3912-АТХ3.115_30.06.2026", connections_list)

Как CLI (для отладки на одном файле):
    python3 netlist_rules.py path/to/connections.json
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict

# schema.py лежит в корне проекта (на уровень выше data/base_analysis_scripts).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import findings as _findings  # noqa: E402  (общая форма находки и ref'а)

from schema import REPORT_SCHEMA  # noqa: E402

DOC_TYPE = "netlist"
SOURCE_FILE = "connections.json"

# Канонический KKS-тег этого проекта: 00 + 2-4 буквы + 2 цифры + 2 буквы + 3 цифры,
# например 00USE23CL002. Всё, что идёт ПОСЛЕ - подозрительный хвост (XQ01 и т.п.).
KKS_CANONICAL_RE = re.compile(r'^(00[A-Z]{2,4}\d{2}[A-Z]{2}\d{3})(.+)$')

# Имя канала модуля (00CJF02AI010P_01, 00CJF02BO215P_35) - это НЕ проводник и не
# KKS-тег устройства. По этому шаблону отличаем "тег уехал в колонку conductor".
CHANNEL_TAG_RE = re.compile(r'^00[A-Z0-9]{4,}[A-Z]\d{2,3}[A-Z]?(_\d+)?$')


# ============================================================
# Оформление находки в формате schema.REPORT_SCHEMA
# ============================================================

def _ref(document, conn):
    """Одна запись нетлиста -> ref в терминах общей схемы."""
    return _findings.ref(
        document, DOC_TYPE, SOURCE_FILE,
        sheet=conn.get("page") if isinstance(conn.get("page"), int) else None,
        row=conn.get("id"),
        cabinet=conn.get("cabinet"),
        terminal_block=conn.get("terminal_block"),
        pin=conn.get("pin"),
        terminal_type=conn.get("terminal_type_or_ref"),
        marking=conn.get("circuit_marking"),
        kks=conn.get("kks"),
        conductor=conn.get("conductor"),
        found=f"строка {conn.get('id')}: адрес {conn.get('terminal_address')}",
    )


def _finding(kind, severity, type_ru, refs, finding, action, evidence=None):
    return _findings.finding(kind, severity, type_ru, refs, finding, action,
                            evidence, scope="single_document")


# ============================================================
# Правила
# ============================================================

def rule_duplicate_address(document, conns):
    """Несколько записей на один физический вывод (cabinet.terminal_block.pin)."""
    by_addr = defaultdict(list)
    for c in conns:
        addr = c.get("terminal_address")
        if addr:
            by_addr[addr].append(c)

    findings = []
    for addr, group in by_addr.items():
        if len(group) < 2:
            continue
        rows = [g.get("id") for g in group]
        is_pe = str(group[0].get("pin")).upper() == "PE"
        findings.append(_finding(
            kind="DUPLICATE",
            severity="medium" if is_pe else "high",
            type_ru="Дубль физического адреса клеммы",
            refs=[_ref(document, g) for g in group[:2]],
            finding=f"Несколько записей ({len(group)}) ссылаются на один физический вывод "
                    f"{addr} (строки {', '.join(map(str, rows))}).",
            action=("Подтвердить, что это намеренное шунтирование PE, либо убрать "
                    "дублирующую запись." if is_pe else
                    "Проверить, не подключены ли к одному выводу два разных провода, "
                    "и устранить дубль."),
            evidence=f"terminal_address={addr} встречается {len(group)} раз",
        ))
    return findings


def rule_tag_duplicated_in_conductor(document, conns):
    """Один и тот же тег стоит и в KKS, и в conductor - тег продублирован в поле
    проводника, хотя тег канала это не проводник."""
    findings = []
    for c in conns:
        kks, cond = c.get("kks"), c.get("conductor")
        if kks and cond and kks == cond:
            findings.append(_finding(
                kind="FORMAT",
                severity="medium",
                type_ru="Тег продублирован в поле проводника",
                refs=[_ref(document, c)],
                finding=f"В строке {c.get('id')} тег '{kks}' указан и в колонке KKS, и в "
                        f"колонке 'Проводник'. Тег канала не является проводником.",
                action="В поле 'Проводник' указать тип сигнала (BO, дискретный выход и т.п.) "
                       "или оставить пустым.",
                evidence=f"kks == conductor == '{kks}'",
            ))
    return findings


def rule_kks_suffix_artifact(document, conns):
    """KKS-тег с лишним хвостом после канонической части (напр. ...XQ01).

    Факт (лишний суффикс) устанавливается детерминированно, но ОКОНЧАТЕЛЬНЫЙ вердикт
    требует сверки со схемой (там суффикса может не быть) - поэтому severity=medium
    и в action прямо сказано свериться со схемой. Это НЕ вранье про схему: сам чекер
    в схему не смотрит, он лишь помечает подозрительный тег.
    """
    findings = []
    for c in conns:
        kks = c.get("kks")
        if not kks:
            continue
        m = KKS_CANONICAL_RE.match(kks)
        if m and m.group(2):
            base, suffix = m.group(1), m.group(2)
            findings.append(_finding(
                kind="FORMAT",
                severity="medium",
                type_ru="Лишний суффикс в KKS-теге",
                refs=[_ref(document, c)],
                finding=f"В строке {c.get('id')} KKS-тег '{kks}' содержит хвост '{suffix}' "
                        f"после канонической части '{base}'.",
                action=f"Сверить со схемой: если там указан '{base}' без '{suffix}', "
                       f"привести тег в таблице к '{base}'.",
                evidence=f"KKS='{kks}', каноническая часть='{base}', лишнее='{suffix}'",
            ))
    return findings


def rule_missing_terminal(document, conns):
    """У НЕрезервной записи не указан клеммник или штифт - неполная спецификация
    точки подключения (её нельзя смонтировать по такой строке)."""
    findings = []
    for c in conns:
        if str(c.get("kks")).strip().lower() == "резерв":
            continue  # у резерва отсутствие точки подключения ожидаемо
        has_owner = c.get("kks") or c.get("circuit_marking")
        if has_owner and (not c.get("terminal_block") or not c.get("pin")):
            findings.append(_finding(
                kind="INCOMPLETE",
                severity="low",
                type_ru="Не указана точка подключения",
                refs=[_ref(document, c)],
                finding=f"В строке {c.get('id')} не заполнен клеммник и/или штифт "
                        f"(terminal_block={c.get('terminal_block')!r}, pin={c.get('pin')!r}), "
                        f"хотя запись не помечена как резерв.",
                action="Указать конкретный клеммник и штифт точки подключения.",
                evidence=f"terminal_block={c.get('terminal_block')!r}, pin={c.get('pin')!r}",
            ))
    return findings


def rule_reserve_tag_placement(document, conns):
    """СВОДНАЯ справочная запись (не ошибка) про резервные каналы, у которых имя
    канала лежит в колонке conductor. "Умная" модель насчитала таких 125 и записала
    в критические - но у резерва устройства-владельца нет, так что это вопрос
    оформления. Сводим в ОДНУ запись уровня info, чтобы инженер знал о паттерне,
    но не тонул в сотне ложных 'критических'."""
    hits = [c for c in conns
            if str(c.get("kks")).strip().lower() == "резерв"
            and c.get("conductor") and CHANNEL_TAG_RE.match(c["conductor"])]
    if not hits:
        return []
    rows = [c.get("id") for c in hits]
    return [_finding(
        kind="FORMAT",
        severity="info",
        type_ru="Имя канала в поле проводника у резервных каналов",
        refs=[_ref(document, hits[0])],
        finding=f"У {len(hits)} резервных каналов имя канала записано в колонку 'Проводник' "
                f"(kks='Резерв'). Это единый стиль оформления резерва, а не ошибка подключения. "
                f"Строки: {', '.join(map(str, rows[:15]))}{'...' if len(rows) > 15 else ''}.",
        action="Проверить, что такое оформление резервных каналов допустимо по стандарту "
               "проекта. Отдельного исправления по каждой строке не требуется.",
        evidence=f"{len(hits)} записей kks='Резерв' с тегом канала в поле conductor",
    )]


def rule_duplicate_signal_channel(document, conns):
    """ПЕРЕЧЕНЬ СИГНАЛОВ: один адрес канала ПЛК в двух строках.

    Канал контроллера существует в единственном экземпляре, и перечень
    перечисляет каналы по одному на строку - второй '1DO7' означает, что один
    из двух сигналов останется неподключённым. Проверка точная по построению
    (сравниваются готовые строки адресов); замер на обоих перечнях КОС
    (177 + 280 каналов) - ноль ложных срабатываний.
    """
    by_addr = defaultdict(list)
    for c in conns:
        addr = c.get("connection_address")
        if addr:
            by_addr[addr].append(c)
    findings = []
    for addr, group in sorted(by_addr.items()):
        if len(group) < 2:
            continue
        rows = [g.get("id") for g in group]
        descriptions = [g.get("note") or "" for g in group]
        findings.append(_finding(
            kind="DUPLICATE",
            severity="high",
            type_ru="Один канал ПЛК в двух строках перечня сигналов",
            refs=[_ref(document, g) for g in group[:2]],
            finding=f"Адрес канала {addr!r} стоит в {len(group)} строках перечня "
                    f"(строки {', '.join(map(str, rows))}), а канал контроллера "
                    f"существует один: один из сигналов останется неподключённым. "
                    f"Сигналы: {'; '.join(d[:60] for d in descriptions[:2])}.",
            action=f"Проверить адресацию: одному из сигналов назначить свободный "
                   f"канал вместо {addr!r}.",
            evidence=f"connection_address={addr!r} встречается {len(group)} раз",
        ))
    return findings


def rule_duplicate_cable(document, conns):
    """КАБЕЛЬНЫЙ ЖУРНАЛ: одно обозначение кабеля в двух строках.

    Журнал ведётся по одному кабелю на строку; повторное обозначение - это
    либо два кабеля под одним именем (не смонтировать), либо задвоенная
    строка. Замер на журнале КОС (153 кабеля) - ноль ложных срабатываний.
    """
    by_cable = defaultdict(list)
    for c in conns:
        cable = c.get("cable_harness")
        if cable:
            by_cable[cable].append(c)
    findings = []
    for cable, group in sorted(by_cable.items()):
        if len(group) < 2:
            continue
        rows = [g.get("id") for g in group]
        routes = [g.get("note") or "" for g in group]
        findings.append(_finding(
            kind="DUPLICATE",
            severity="high",
            type_ru="Одно обозначение кабеля в двух строках журнала",
            refs=[_ref(document, g) for g in group[:2]],
            finding=f"Обозначение кабеля {cable!r} стоит в {len(group)} строках "
                    f"журнала (строки {', '.join(map(str, rows))}). Трассы: "
                    f"{'; '.join(r[:70] for r in routes[:2])}.",
            action=f"Проверить журнал: если это разные кабели - развести обозначения, "
                   f"если одна строка задвоена - убрать дубль.",
            evidence=f"cable_harness={cable!r} встречается {len(group)} раз",
        ))
    return findings


ALL_RULES = [
    rule_duplicate_address,
    rule_tag_duplicated_in_conductor,
    rule_kks_suffix_artifact,
    rule_missing_terminal,
    rule_reserve_tag_placement,
]

# Какие правила осмыслены для какого ВИДА таблицы (metadata.table_kind в
# connections.json). Перечень сигналов и кабельный журнал - не ГОСТ-таблицы
# подключений: у них нет клеммников, штифтов и KKS по построению, и правила
# про них выдавали бы по INCOMPLETE на каждую строку. Отсутствие table_kind
# (данные старых прогонов) означает ГОСТ-таблицу.
RULES_BY_KIND = {
    "gost_connections": ALL_RULES,
    "signal_list": [rule_duplicate_signal_channel],
    "cable_journal": [rule_duplicate_cable],
    # ПЕРЕЧЕНЬ ПАРАМЕТРОВ: своих правил пока нет СОЗНАТЕЛЬНО. Просившееся
    # «одна позиция в двух строках» отвергнуто без замера как заведомо ложное:
    # один прибор законно даёт несколько сигналов и занимает несколько строк
    # («NSL101-1» и «NSL101-2» - положения одного переключателя, «PZS PGmax» -
    # уставка того же датчика). Клеммников и адресов каналов у этой таблицы
    # нет по построению, так что прочие правила ей тоже не подходят.
    #
    # Извлечение при этом не напрасно: графа «Позиция по схеме» - ключ сшивки с
    # ФСА и кабельным журналом, и её видят агенты. Правило по ней появится,
    # когда будет на чём замерить ложные (нужен второй альбом с перечнем).
    "param_list": [],
}

SEVERITY_ORDER = _findings.SEVERITY_ORDER


def check_connections(document, connections, table_kind="gost_connections"):
    """Главная точка входа: список записей нетлиста -> список находок в формате
    schema.REPORT_SCHEMA (тот же, что у агентов)."""
    findings = []
    for rule in RULES_BY_KIND.get(table_kind, ALL_RULES):
        findings.extend(rule(document, connections))
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return findings


def check_connections_file(document, connections_path):
    with open(connections_path, encoding="utf-8") as f:
        data = json.load(f)
    kind = ((data.get("statistics") or {}).get("table_kind")
            or (data.get("document_metadata") or {}).get("table_kind")
            or "gost_connections")
    return check_connections(document, data.get("connections", []), kind)


def main():
    if len(sys.argv) < 2:
        print("Использование: python3 netlist_rules.py path/to/connections.json")
        sys.exit(1)
    path = sys.argv[1]
    document = os.path.basename(os.path.dirname(os.path.abspath(path)))
    findings = check_connections_file(document, path)

    # проверим, что находки валидны по общей схеме
    from jsonschema import Draft7Validator
    errs = list(Draft7Validator(REPORT_SCHEMA).iter_errors({"errors": findings}))
    print(f"Найдено {len(findings)} находок; валидны по схеме: {'да' if not errs else errs}")
    by_kind = Counter(f["kind"] for f in findings)
    by_sev = Counter(f["severity"] for f in findings)
    print("по видам:", dict(by_kind))
    print("по важности:", dict(by_sev))
    print(json.dumps({"errors": findings}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
