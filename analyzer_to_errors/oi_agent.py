"""
Агент анализа данных на базе Open Interpreter (пакет `open-interpreter`,
модуль импортируется как `interpreter`).

Open Interpreter сам решает, какой код написать и исполнить (Python/shell),
чтобы обследовать папку data/ - он не пытается затолкать содержимое файлов
в контекст (они на порядки больше контекстного окна), а пишет код для
чтения/агрегации и видит только вывод этого кода.

Финальный ответ модели прогоняется через validation.get_validated_json,
который вытаскивает JSON, валидирует по REPORT_SCHEMA и при необходимости
просит модель переисправить ответ (либо роняет исключение, если модель
так и не смогла выдать валидный JSON).

САМИ ПРОМПТЫ ЛЕЖАТ В prompts/, а не в этом файле. Их почти три сотни строк -
описание того, что означает каждое поле каждого извлечённого JSON, чем связка
отличается от прогона и какие находки НЕ надо выдавать. Это текст предметной
области: его правит инженер, читая свежий отчёт, и правит часто. Пока он лежал
строковой константой посреди кода, каждая такая правка была правкой .py-файла
с риском сломать экранирование, а посмотреть промпт целиком (или сравнить две
его версии в истории) означало листать код.
"""

import json
import logging
import time
from pathlib import Path

from interpreter import OpenInterpreter

from llm_client import make_simple_ask_fn
from schema import EXAMPLE_ERRORS, REPORT_SCHEMA
from validation import JSONValidationError, get_validated_json

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _prompt(name: str) -> str:
    """Текст промпта из prompts/. Читается на импорте: файл обязан быть на
    месте, и падать из-за него надо сразу, а не посреди сорокаминутного
    прогона, когда дело дошло до агентов."""
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


SYSTEM_INSTRUCTIONS = _prompt("agent_system.md")

ANALYSIS_PROMPT_TEMPLATE = _prompt("agent_analysis.md")


# Доля контекста, которую разрешено отдать под ОТВЕТ модели. Остальное - под
# историю диалога.
#
# Зачем это нужно. Open Interpreter считает бюджет на историю как
#     context_window - max_tokens - 25
# и обрезает по нему сообщения (tokentrim). Если в конфиге max_tokens >=
# context_window (например, оба 512000 - легко поставить по невнимательности),
# бюджет становится нулевым или отрицательным, обрезалка выбрасывает ВСЕ
# сообщения, включая промпт пользователя, и на сервер уходит один system.
# LM Studio на такое отвечает "No user query found in messages" - ошибка,
# по тексту которой ни за что не догадаешься, что виноваты числа в конфиге.
# Собственная защита OI ловит только строгое max_tokens > context_window,
# равенство проходит мимо неё. Поэтому зажимаем сами.
MAX_TOKENS_SHARE_OF_CONTEXT = 0.25


def _sane_token_limits(server_cfg: dict) -> tuple:
    context_window = server_cfg.get("context_window", 32000)
    max_tokens = server_cfg.get("max_tokens", 4096)

    cap = int(context_window * MAX_TOKENS_SHARE_OF_CONTEXT)
    if max_tokens > cap:
        logger.warning(
            "max_tokens=%d слишком велик при context_window=%d (не осталось места под "
            "историю диалога - Open Interpreter обрежет промпт целиком). Уменьшаю "
            "max_tokens до %d. Поправьте config.yaml: max_tokens - это лимит на ДЛИНУ "
            "ОТВЕТА модели, а не размер контекста.",
            max_tokens, context_window, cap)
        max_tokens = cap

    return context_window, max_tokens


def _build_interpreter(server_cfg: dict, helper_dir: str) -> OpenInterpreter:
    context_window, max_tokens = _sane_token_limits(server_cfg)

    interp = OpenInterpreter()
    interp.offline = True                 # не дёргаем облачные сервисы Open Interpreter
    interp.auto_run = True                # не спрашивать подтверждение перед запуском кода (нужно для скрипта)
    interp.custom_instructions = SYSTEM_INSTRUCTIONS.format(helper_dir=helper_dir)
    interp.llm.api_base = server_cfg["base_url"]
    interp.llm.api_key = server_cfg.get("api_key") or "not-needed"
    # litellm (на котором работает OI) требует префикс провайдера для
    # кастомных OpenAI-совместимых эндпоинтов
    interp.llm.model = "openai/" + server_cfg["model"]
    interp.llm.temperature = server_cfg.get("temperature", 0.2)
    interp.llm.max_tokens = max_tokens
    interp.llm.context_window = context_window
    return interp


# Промпт стадии 2 (отчёт). Уходит ОБЫЧНЫМ chat-вызовом, без Open Interpreter.
#
# Почему отчёт не просят у самого Open Interpreter. OI устроен так, чтобы модель
# писала и исполняла код: его системный промпт и шаблоны сообщений постоянно
# подталкивают к следующему блоку кода. Просьба "верни JSON" внутри OI приводит к
# тому, что модель отвечает очередным скриптом, а не отчётом. На практике: qwen
# выдавала валидный JSON лишь с 3-й попытки из 3, а 9b-модель не выдавала вообще
# (её финальный текст был пуст - она снова уходила в код).
# При этом обе модели без проблем отдают валидный по схеме JSON обычным chat-вызовом
# (проверяется через `python main.py --check-llm`).
# Поэтому роли разведены: OI СОБИРАЕТ ФАКТЫ (стадия 1), отчёт по протоколу его работы
# формирует обычный chat-вызов (стадия 2).
REPORT_PROMPT_TEMPLATE = _prompt("agent_report.md")


def _chat_capped(interp: OpenInterpreter, message: str,
                 max_code_turns: int, deadline: float):
    """interp.chat() с жёсткими ограничителями.

    Open Interpreter крутит цикл "модель пишет код -> код исполняется -> модель видит
    вывод" до тех пор, пока модель САМА не решит, что закончила, и не ответит текстом.
    Слабая модель может не решить этого никогда: наблюдал 190+ ходов за 40 минут без
    финального ответа. Поэтому считаем ходы (запуски кода) и следим за часами; при
    превышении - прерываем поток и возвращаем причину остановки.

    Возвращает (текст последнего сообщения ассистента, причина остановки, ходов).
    """
    code_turns = 0
    messages_text = []
    stopped = None

    for chunk in interp.chat(message, display=False, stream=True):
        if not isinstance(chunk, dict):
            continue

        role, ctype = chunk.get("role"), chunk.get("type")

        if role == "assistant" and ctype == "message":
            if chunk.get("start"):
                messages_text.append("")
            content = chunk.get("content")
            if content and not chunk.get("start") and not chunk.get("end"):
                if not messages_text:
                    messages_text.append("")
                messages_text[-1] += content

        elif role == "assistant" and ctype == "code" and chunk.get("start"):
            code_turns += 1

        if code_turns > max_code_turns:
            stopped = "turns"
            break
        if time.monotonic() > deadline:
            stopped = "timeout"
            break

    text = next((t for t in reversed(messages_text) if t.strip()), "")
    return text, stopped, code_turns


def _build_transcript(interp: OpenInterpreter, max_chars_per_message: int = 1500,
                      max_total_chars: int = 60000) -> str:
    """Протокол работы агента: что он делал и что увидел.

    Берём из истории OI рассуждения ассистента, исполненный код и вывод этого кода.
    Вывод кода обрезаем: агент мог напечатать простыню на сотни килобайт, а в контекст
    модели она не влезет. Если протокол целиком не помещается - оставляем ХВОСТ:
    выводы и итоговые сводки агент печатает в конце, а начало - это осмотр папки,
    самая бесполезная для отчёта часть.
    """
    parts = []
    for m in interp.messages:
        role, mtype = m.get("role"), m.get("type")
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        if len(content) > max_chars_per_message:
            content = (content[:max_chars_per_message]
                       + f"\n... [обрезано, всего {len(content)} символов]")

        if role == "assistant" and mtype == "message":
            parts.append(f"[рассуждение агента]\n{content}")
        elif role == "assistant" and mtype == "code":
            parts.append(f"[исполненный код]\n{content}")
        elif role == "computer":
            parts.append(f"[вывод кода]\n{content}")

    transcript = "\n\n".join(parts)
    if len(transcript) > max_total_chars:
        transcript = ("... [начало протокола обрезано]\n\n"
                      + transcript[-max_total_chars:])
    return transcript


def _run_investigation(interp: OpenInterpreter, input_dir: str,
                       max_code_turns: int, timeout_seconds: int) -> str:
    """Стадия 1: агент исследует данные, исполняя код. Возвращает протокол работы."""
    deadline = time.monotonic() + timeout_seconds
    _, stopped, turns = _chat_capped(
        interp, build_analysis_prompt(input_dir), max_code_turns, deadline)

    if stopped == "turns":
        logger.warning("Агент остановлен: исчерпан лимит ходов (%d). "
                       "Отчёт будет построен по тому, что он успел собрать.",
                       max_code_turns)
    elif stopped == "timeout":
        logger.warning("Агент остановлен: вышло время (%d c), сделано ходов: %d. "
                       "Отчёт будет построен по тому, что он успел собрать.",
                       timeout_seconds, turns)
    else:
        logger.info("Агент завершил исследование сам, ходов: %d", turns)

    return _build_transcript(interp)


def build_analysis_prompt(input_dir: str) -> str:
    return ANALYSIS_PROMPT_TEMPLATE.format(
        input_dir=input_dir,
        schema=json.dumps(REPORT_SCHEMA, ensure_ascii=False, indent=2),
        examples=json.dumps({"errors": EXAMPLE_ERRORS}, ensure_ascii=False, indent=2),
    )


def run_analysis_agent(server_cfg: dict, input_dir: str, helper_dir: str,
                       max_json_repair_attempts: int = 3,
                       max_code_turns: int = 25,
                       timeout_seconds: int = 1200) -> dict:
    """Агент анализа в две стадии:

    1. ИССЛЕДОВАНИЕ (Open Interpreter): модель пишет и исполняет код, изучая данные.
       Ограничено лимитом ходов и таймаутом - иначе слабая модель крутится вечно.
    2. ОТЧЁТ (обычный chat-вызов): по протоколу первой стадии модель оформляет
       валидный по схеме JSON. Здесь нет исполнения кода, и модель не срывается
       обратно в написание скриптов - именно на этом ломался прежний вариант,
       требовавший JSON прямо от Open Interpreter.
    """
    logger.info("Инициализация Open Interpreter для модели %s "
                "(лимит ходов: %d, таймаут: %d c)",
                server_cfg["model"], max_code_turns, timeout_seconds)
    interp = _build_interpreter(server_cfg, helper_dir)

    logger.info("Стадия 1/2: исследование данных (исполнение кода)")
    transcript = _run_investigation(interp, input_dir, max_code_turns, timeout_seconds)
    logger.info("Протокол работы агента: %d символов", len(transcript))

    if not transcript.strip():
        raise JSONValidationError(
            f"Агент ({server_cfg['model']}) не собрал никаких данных: протокол пуст. "
            "Проверьте доступность модели: python main.py --check-llm")

    logger.info("Стадия 2/2: формирование отчёта (обычный chat, без исполнения кода)")
    prompt = REPORT_PROMPT_TEMPLATE.format(
        transcript=transcript,
        schema=json.dumps(REPORT_SCHEMA, ensure_ascii=False, indent=2),
        examples=json.dumps({"errors": EXAMPLE_ERRORS}, ensure_ascii=False, indent=2),
    )
    return get_validated_json(
        make_simple_ask_fn(server_cfg), prompt, REPORT_SCHEMA,
        max_attempts=max_json_repair_attempts,
    )
