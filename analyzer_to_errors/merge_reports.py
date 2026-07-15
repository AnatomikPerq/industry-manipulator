"""
Слияние двух JSON-отчётов об ошибках в один:
- убирает повторяющиеся находки между двумя отчётами;
- убирает ошибки, совпадающие с "заранее известными" (known_errors.json),
  которые передаются модели прямо в промпте.

В качестве модели-"сшивателя" переиспользуется одна из уже работающих
моделей-агентов (см. merger.use_agent в config.yaml) - отдельный сервер
для этого не поднимается. Здесь не нужен Open Interpreter / исполнение
кода - модель просто рассуждает над двумя уже готовыми JSON и текстом
известных ошибок, поэтому используется обычный chat-вызов.

Итоговый ответ так же прогоняется через validation.get_validated_json
с авторемонтом при невалидном JSON.
"""

import json
import logging

from llm_client import make_simple_ask_fn
from schema import REPORT_SCHEMA
from validation import get_validated_json

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = """Ты объединяешь два отчёта о найденных ошибках в проектной документации
АСУ ТП, полученных от двух разных нейросетей-аналитиков, которые независимо
анализировали одни и те же документы, в один итоговый отчёт для инженера.

Правила объединения:
1. Одна и та же находка в обоих отчётах = ОДНА запись в итоге. Считай находки
   одинаковыми, если они указывают на одно и то же место (совпадают document +
   клеммник/штифт, либо document + row, либо тот же KKS-тег) и говорят об одном
   и том же по сути - даже если сформулированы разными словами.
2. При слиянии дублей бери НАИБОЛЕЕ ПОЛНУЮ версию: в refs сохраняй все поля,
   которые заполнены хотя бы в одном из двух отчётов (не теряй данные!);
   finding и action бери более конкретные и информативные.
3. Если два отчёта противоречат друг другу по одному и тому же месту (например,
   разные kind или severity) - оставь одну запись, выбери более обоснованную
   версию и снизь severity на одну ступень: согласия между моделями нет.
4. Из результата ПОЛНОСТЬЮ ИСКЛЮЧИ находки, которые совпадают по сути (не
   обязательно дословно) с "заранее известными ошибками" ниже - это не баги,
   а ожидаемое/уже задокументированное поведение, пользователь их видеть не должен.
5. НЕ ПРИДУМЫВАЙ новых находок, которых не было ни в одном из двух отчётов, и не
   меняй фактическую суть находок - ты только объединяешь и чистишь.
6. Сохраняй оба рода находок: и междокументные (scope="cross_document"), и
   внутридокументные (scope="single_document").
7. Отсортируй итоговый список по severity: critical -> high -> medium -> low -> info.

Заранее известные ошибки (игнорировать, не включать в итог):
{known_errors_block}

Отчёт от нейросети №1:
{report_1}

Отчёт от нейросети №2:
{report_2}

Выведи итоговый результат СТРОГО в виде JSON-объекта, соответствующего
следующей JSON-Schema (внутри markdown-блока ```json ... ```, без
дополнительного текста после него):

{schema}
"""


def merge_reports(server_cfg: dict, report_1: dict, report_2: dict,
                   known_errors: list, max_json_repair_attempts: int = 3) -> dict:
    ask_fn = make_simple_ask_fn(server_cfg)

    known_errors_block = json.dumps(known_errors, ensure_ascii=False, indent=2) \
        if known_errors else "(список пуст)"

    prompt = PROMPT_TEMPLATE.format(
        known_errors_block=known_errors_block,
        report_1=json.dumps(report_1, ensure_ascii=False, indent=2),
        report_2=json.dumps(report_2, ensure_ascii=False, indent=2),
        schema=json.dumps(REPORT_SCHEMA, ensure_ascii=False, indent=2),
    )

    return get_validated_json(
        ask_fn, prompt, REPORT_SCHEMA,
        max_attempts=max_json_repair_attempts,
    )
