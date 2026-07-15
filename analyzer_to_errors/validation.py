"""
Общий слой валидации структурированных JSON-ответов модели.

Идея: не доверяем модели "с первого раза" - вытаскиваем JSON из её ответа,
валидируем по JSON-Schema (см. schema.py). Если невалидно - отправляем
модели текст ошибки валидации и просим переисправить (перегенерация).
Если после нескольких попыток валидного JSON так и не получено -
кидаем исключение, которое main.py превращает в ошибку выполнения
(в реальном сервисе - в ошибку/алерт на сервер).
"""

import json
import logging
import re

from jsonschema import Draft7Validator

logger = logging.getLogger(__name__)


class JSONValidationError(Exception):
    """Модель не смогла выдать валидный JSON за отведённое число попыток."""
    pass


def _extract_json_candidate(text: str) -> str:
    """Пытается вытащить JSON-текст из ответа модели:
    сначала ищет ```json ... ``` блок, потом ``` ... ```,
    иначе - самую внешнюю пару { ... } в тексте."""
    fenced = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()

    fenced_plain = re.search(r"```\s*(.*?)```", text, re.DOTALL)
    if fenced_plain:
        return fenced_plain.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]

    return text.strip()


def _validate(data: dict, schema: dict) -> list:
    """Возвращает список текстовых описаний ошибок валидации (пустой = валидно)."""
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]


def get_validated_json(ask_fn, initial_prompt: str, schema: dict,
                        max_attempts: int = 3) -> dict:
    """
    ask_fn: функция (str) -> str, отправляющая сообщение модели и
            возвращающая её текстовый ответ (обёртка над Open Interpreter
            или над обычным chat-вызовом - см. oi_agent.py / llm_client.py).
    initial_prompt: первый промпт, отправляемый модели.
    schema: JSON-Schema, которой должен соответствовать результат.
    max_attempts: сколько раз пытаться перегенерировать при невалидном JSON.
    """
    prompt = initial_prompt
    last_errors = []

    for attempt in range(1, max_attempts + 1):
        raw_response = ask_fn(prompt)
        candidate = _extract_json_candidate(raw_response)

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as e:
            last_errors = [f"невалидный JSON (parse error): {e}"]
            logger.warning("Попытка %d/%d: модель вернула не-JSON: %s",
                            attempt, max_attempts, e)
            prompt = (
                "Твой предыдущий ответ не является валидным JSON "
                f"(ошибка парсинга: {e}). Пришли ИСПРАВЛЕННЫЙ ответ - "
                "ТОЛЬКО валидный JSON-объект, без пояснений и без markdown-обёртки."
            )
            continue

        schema_errors = _validate(data, schema)
        if not schema_errors:
            logger.info("Валидный JSON получен с попытки %d/%d", attempt, max_attempts)
            return data

        last_errors = schema_errors
        logger.warning("Попытка %d/%d: JSON не проходит схему: %s",
                        attempt, max_attempts, schema_errors)
        prompt = (
            "Твой предыдущий JSON не соответствует требуемой схеме. Ошибки:\n"
            + "\n".join(f"- {e}" for e in schema_errors) +
            "\n\nПришли ИСПРАВЛЕННЫЙ полный JSON-объект, устранив все перечисленные "
            "ошибки. Только JSON, без пояснений и без markdown-обёртки."
        )

    raise JSONValidationError(
        f"Не удалось получить валидный JSON за {max_attempts} попыток. "
        f"Последние ошибки: {last_errors}"
    )
