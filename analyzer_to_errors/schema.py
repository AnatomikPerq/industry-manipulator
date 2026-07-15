"""
Единственное место, где описана структура отчёта об ошибках.
Используется:
- в промпте агентов анализа (oi_agent.py), чтобы модель знала, в каком
  формате вернуть финальный JSON;
- в validation.py для проверки (jsonschema) и для текста ошибок при
  запросе на исправление;
- в промпте мерджера (merge_reports.py).

Правьте эту схему - изменения автоматически подхватятся везде.

--------------------------------------------------------------------
МОДЕЛЬ ДАННЫХ
--------------------------------------------------------------------
Находки бывают ДВУХ РОДОВ, и обе ложатся в один и тот же список errors[]:

1. scope = "cross_document" - несостыковка МЕЖДУ документами: строка таблицы
   подключений (netlist) против того, что нарисовано на схеме (scheme).
   У такой находки ДВА ref'а: один на netlist, другой на scheme. Именно они
   рисуют в таблице пользователя левую группу колонок ("Таблица подключений")
   и правую ("Монтажная документация").

2. scope = "single_document" - ошибка ВНУТРИ одного документа: дубль
   физического адреса клеммы, обрыв межлистовой ссылки, битый KKS-тег,
   канал модуля без описания сигнала и т.п. У такой находки один ref
   (или два ref'а на ОДИН И ТОТ ЖЕ документ - например, две строки, которые
   дублируют друг друга). Правая группа колонок в таблице остаётся пустой.

Каждый ref - это одно конкретное место в одном документе, описанное в
доменных терминах (лист, строка, шкаф, клеммник, штифт, маркировка, KKS,
проводник). Набор полей одинаков для обоих типов документов: то, чего в
документе нет или что не распознано, остаётся null.
"""

# Виды замечаний. MISMATCH/MISSING/REVIEW - междокументные (как в HTML-таблице),
# DUPLICATE/BROKEN_LINK/FORMAT/INCOMPLETE - внутридокументные.
KIND_ENUM = [
    "MISMATCH",     # данные есть в обоих документах, но НЕ СОВПАДАЮТ
    "MISSING",      # есть в одном документе, во втором отсутствует полностью
    "REVIEW",       # данные распознаны неоднозначно/неполно, нужно уточнение инженером
    "DUPLICATE",    # дубль внутри документа (напр. два провода на один физический вывод)
    "BROKEN_LINK",  # оборванная связь внутри документа (межлистовая ссылка в никуда, узел без ответной части)
    "FORMAT",       # нарушение формата (битый KKS-тег, недопустимое значение поля)
    "INCOMPLETE",   # незаполненное обязательное поле, канал модуля без описания сигнала
]

SEVERITY_ENUM = ["critical", "high", "medium", "low", "info"]

DOC_TYPE_ENUM = ["netlist", "scheme"]

# Одно место в одном документе. Все доменные поля опциональны (null, если
# в этом документе такого поля нет или оно не распознано) - обязательны
# только привязка к документу и его тип.
REF_SCHEMA = {
    "type": "object",
    "properties": {
        "document": {
            "type": "string",
            "description": "Имя документа = имя папки в data/ (см. manifest.json), "
                           "например 'ИК.3912-АТХ3.115_30.06.2026'"
        },
        "doc_type": {
            "type": "string",
            "enum": DOC_TYPE_ENUM,
            "description": "netlist = таблица подключений, scheme = монтажная схема EPLAN"
        },
        "source_file": {
            "type": ["string", "null"],
            "description": "Файл с данными внутри папки документа, откуда взята находка "
                           "(connections.json / graph.json / netlist.json / classified.json / "
                           "issues_candidates.json)"
        },
        "sheet": {
            "type": ["integer", "null"],
            "description": "Номер листа документа ('лист 20'). null, если определить нельзя"
        },
        "row": {
            "type": ["integer", "null"],
            "description": "Для netlist: id строки в таблице подключений. Для scheme: null"
        },
        "cabinet": {
            "type": ["string", "null"],
            "description": "Шкаф (KKS шкафа), например '00CJF02'"
        },
        "terminal_block": {
            "type": ["string", "null"],
            "description": "Клеммник/модуль, например 'RB03', 'XT01', 'AA03'"
        },
        "pin": {
            "type": ["string", "null"],
            "description": "Штифт/вывод клеммника, например '16', 'PE'"
        },
        "terminal_type": {
            "type": ["string", "null"],
            "description": "Тип клеммы/модуля или артикул, например 'UM-DBC16M', '8002099558'"
        },
        "marking": {
            "type": ["string", "null"],
            "description": "Маркировка цепи / номер провода, например '47', '752'"
        },
        "kks": {
            "type": ["string", "null"],
            "description": "KKS-тег устройства, например '00USE23CL002XQ01'"
        },
        "conductor": {
            "type": ["string", "null"],
            "description": "Проводник/сигнал, например 'L+', 'PE', 'FB_OPEN', 'AI6'"
        },
        "found": {
            "type": ["string", "null"],
            "description": "Что фактически найдено в ЭТОМ документе в этом месте - краткой "
                           "строкой, как в колонке 'Что найдено на схеме'. Например: "
                           "'стр. 8: RB03/16; провод не требуется' или "
                           "'Данные на схеме не определены однозначно.'"
        },
    },
    "required": ["document", "doc_type"],
    "additionalProperties": False,
}

ERROR_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": KIND_ENUM,
            "description": "Вид замечания. MISMATCH/MISSING/REVIEW - междокументные; "
                           "DUPLICATE/BROKEN_LINK/FORMAT/INCOMPLETE - внутри одного документа"
        },
        "scope": {
            "type": "string",
            "enum": ["cross_document", "single_document"],
            "description": "cross_document = сравнение двух документов (тогда в refs ДВА ref'а: "
                           "netlist и scheme). single_document = ошибка внутри одного документа"
        },
        "severity": {
            "type": "string",
            "enum": SEVERITY_ENUM,
            "description": "Серьёзность: critical - монтаж по такой документации приведёт к "
                           "неработоспособности или опасности; high - явная ошибка документации; "
                           "medium - несоответствие, требующее исправления; low - мелкое "
                           "замечание; info - к сведению"
        },
        "type": {
            "type": "string",
            "description": "Короткий подтип на русском для группировки, например: "
                           "'KKS не найден на схеме', 'Дубль физического адреса клеммы', "
                           "'Маркировка провода не распознана', 'Ссылка на несуществующий лист'"
        },
        "refs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "items": REF_SCHEMA,
            "description": "Места находки. Для cross_document - РОВНО ДВА ref'а: первый с "
                           "doc_type='netlist', второй с doc_type='scheme'. Для single_document - "
                           "один ref (или два ref'а на один и тот же документ, если это дубль)"
        },
        "finding": {
            "type": "string",
            "description": "ЧТО НАЙДЕНО - констатация факта по данным, без оценки. Например: "
                           "'KKS 00USE23CL002XQ01 указан в таблице подключений (лист 20, "
                           "строка 11), но на листе 8 схемы у клеммы RB03/16 KKS отсутствует'"
        },
        "action": {
            "type": "string",
            "description": "ЧТО ТРЕБУЕТСЯ УТОЧНИТЬ ИЛИ ИСПРАВИТЬ - конкретное действие для "
                           "инженера. Например: 'Проверить, какому устройству принадлежит "
                           "клемма RB03/16, и привести KKS в схеме и таблице к одному значению'"
        },
        "evidence": {
            "type": ["string", "null"],
            "description": "Подтверждение из данных: короткая цитата/фрагмент записи, по которой "
                           "сделан вывод (опционально, но крайне желательно)"
        },
    },
    "required": ["kind", "scope", "severity", "type", "refs", "finding", "action"],
    "additionalProperties": False,
}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "errors": {
            "type": "array",
            "items": ERROR_ITEM_SCHEMA
        },
        "summary": {
            "type": "string",
            "description": "Короткое общее резюме анализа (1-3 предложения): что проверялось, "
                           "сколько и каких замечаний найдено"
        }
    },
    "required": ["errors"],
    "additionalProperties": False,
}

# Пример по одной находке каждого рода - уходит прямо в промпт модели.
# Голая JSON-Schema плохо объясняет модели, чего от неё хотят; пара живых
# примеров на реальных данных работает заметно лучше.
EXAMPLE_ERRORS = [
    {
        "kind": "MISMATCH",
        "scope": "cross_document",
        "severity": "high",
        "type": "KKS не найден на монтажной странице",
        "refs": [
            {
                "document": "ИК.3912-АТХ3.115_30.06.2026",
                "doc_type": "netlist",
                "source_file": "connections.json",
                "sheet": 20,
                "row": 11,
                "cabinet": "00CJF02",
                "terminal_block": "RB03",
                "pin": "16",
                "terminal_type": "UM-DBC16M",
                "marking": None,
                "kks": "00USE23CL002XQ01",
                "conductor": "L+",
                "found": "лист 20, строка 11: RB03/16, KKS 00USE23CL002XQ01, проводник L+",
            },
            {
                "document": "ИК.3912-АТХ2.115_02.07.2026",
                "doc_type": "scheme",
                "source_file": "graph.json",
                "sheet": 8,
                "row": None,
                "cabinet": None,
                "terminal_block": "RB03",
                "pin": "16",
                "terminal_type": None,
                "marking": None,
                "kks": None,
                "conductor": None,
                "found": "стр. 8: клемма RB03/16 присутствует, KKS рядом с ней не подписан",
            },
        ],
        "finding": "В таблице подключений клемма RB03/16 отнесена к устройству "
                   "00USE23CL002XQ01, но на листе 8 схемы у этой клеммы KKS-тег отсутствует.",
        "action": "Проверить принадлежность клеммы RB03/16 и проставить KKS "
                  "00USE23CL002XQ01 на схеме либо исправить таблицу подключений.",
        "evidence": "connections.json: {\"id\": 11, \"terminal_address\": \"00CJF02.RB03.16\", "
                    "\"kks\": \"00USE23CL002XQ01\"}",
    },
    {
        "kind": "DUPLICATE",
        "scope": "single_document",
        "severity": "medium",
        "type": "Дубль физического адреса клеммы",
        "refs": [
            {
                "document": "ИК.3912-АТХ3.115_30.06.2026",
                "doc_type": "netlist",
                "source_file": "connections.json",
                "sheet": 1,
                "row": 4,
                "cabinet": "00CJF02",
                "terminal_block": "XT01",
                "pin": "PE",
                "terminal_type": "8001099244",
                "marking": None,
                "kks": None,
                "conductor": None,
                "found": "строка 4: адрес 00CJF02.XT01.PE",
            },
            {
                "document": "ИК.3912-АТХ3.115_30.06.2026",
                "doc_type": "netlist",
                "source_file": "connections.json",
                "sheet": 1,
                "row": 9,
                "cabinet": "00CJF02",
                "terminal_block": "XT01",
                "pin": "PE",
                "terminal_type": "8001099244",
                "marking": None,
                "kks": None,
                "conductor": None,
                "found": "строка 9: тот же адрес 00CJF02.XT01.PE",
            },
        ],
        "finding": "Две строки таблицы подключений ссылаются на один и тот же физический "
                   "вывод 00CJF02.XT01.PE (строки 4 и 9).",
        "action": "Подтвердить, что это намеренное шунтирование PE, либо устранить "
                  "дублирующее подключение.",
        "evidence": "statistics.duplicate_terminal_addresses: {\"00CJF02.XT01.PE\": 2}",
    },
]
