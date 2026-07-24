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

from interpreter import OpenInterpreter

import llm_client
import llm_transcript
from llm_client import make_simple_ask_fn
from schema import EXAMPLE_ERRORS, REPORT_SCHEMA
from settings import PROJECT_ROOT
from validation import JSONValidationError, get_validated_json

logger = logging.getLogger(__name__)

# Промпты - обычные внешние файлы (не вкомпилированы в exe), потому что их
# правит инженер, читая свежий отчёт, и правит часто (см. заголовок файла).
PROMPTS_DIR = PROJECT_ROOT / "prompts"


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

    # ПРАВДА О КОНТЕКСТЕ - У СЕРВЕРА, А НЕ В КОНФИГЕ. context_window написан
    # человеком и неизбежно расходится с тем, как модель загрузили в LM Studio:
    # у qwythos-9b в конфиге 200000, а загружена она с 8192. Open Interpreter
    # верит числу из конфига, считает по нему бюджет истории и отправляет
    # больше, чем сервер принимает, - стадия агентов падает на первом же
    # обращении простынёй из litellm ("request (9808 tokens) exceeds the
    # available context size (8192 tokens)"), по которой причину не угадать.
    real = llm_client.loaded_context_length(server_cfg)
    if real and real < context_window:
        logger.warning(
            "Модель %s загружена на сервере с контекстом %d, а в config.yaml "
            "указано %d. Считаю по реальному: иначе Open Interpreter отправит "
            "больше, чем сервер примет, и прогон упадёт на первом же запросе.",
            server_cfg.get("model"), real, context_window)
        context_window = real

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


# Сколько символов приходится на токен. Оценка НАМЕРЕННО заниженная: считать
# токены по-настоящему значило бы тащить токенизатор конкретной модели, а
# ошибиться здесь в опасную сторону нельзя - перебор виден не как «отчёт чуть
# короче», а как отказ сервера на весь запрос. Русский текст с BPE даёт 2.5-3
# символа на токен, JSON схемы - больше; берём нижнюю границу.
CHARS_PER_TOKEN = 2.5

# Ниже этого протокол бессмысленно резать: в отчёт не попадёт ничего, кроме
# обрывка. Значит, модель с таким контекстом для стадии отчёта не годится, и
# сказать об этом надо прямо, а не слать заведомо обречённый запрос.
MIN_TRANSCRIPT_CHARS = 2000


def _transcript_budget(context_window: int, max_tokens: int, fixed_chars: int) -> int:
    """Сколько символов протокола влезет в промпт отчёта.

    Прежде протокол резался по константе в 60000 символов - числу, ни от чего
    не зависящему. На модели, загруженной с контекстом 8192, это примерно
    20000 токенов при доступных шести тысячах, и стадия отчёта падала с
    «request (14365 tokens) exceeds the available context size (8192 tokens)»
    сразу после того, как агент честно отработал пять минут и собрал факты.
    """
    room_tokens = context_window - max_tokens - 200      # 200 - служебная обвязка
    return int(room_tokens * CHARS_PER_TOKEN) - fixed_chars


def _build_interpreter(server_cfg: dict, helper_dir: str):
    """Возвращает (interpreter, context_window, max_tokens).

    Лимиты отдаются наружу, а не остаются внутри: по ним считается бюджет
    протокола для стадии отчёта, а спрашивать сервер о контексте второй раз
    ради тех же чисел незачем.
    """
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
    return interp, context_window, max_tokens


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
    interp, context_window, max_tokens = _build_interpreter(server_cfg, helper_dir)

    logger.info("Стадия 1/2: исследование данных (исполнение кода)")
    transcript = _run_investigation(interp, input_dir, max_code_turns, timeout_seconds)
    logger.info("Протокол работы агента: %d символов", len(transcript))

    # Полную беседу агента с моделью - в транскрипт LM Studio (кнопка «Лог LM
    # Studio» в сессии). Здесь она НЕ обрезается, как для контекста модели: это
    # ровно то, ради чего лог и заводится - видеть, что модель на самом деле
    # получала и отвечала.
    _log_agent_conversation(server_cfg["model"], interp)

    if not transcript.strip():
        raise JSONValidationError(
            f"Агент ({server_cfg['model']}) не собрал никаких данных: протокол пуст. "
            "Проверьте доступность модели: python main.py --check-llm")

    logger.info("Стадия 2/2: формирование отчёта (обычный chat, без исполнения кода)")
    schema_json = json.dumps(REPORT_SCHEMA, ensure_ascii=False, indent=2)
    examples_json = json.dumps({"errors": EXAMPLE_ERRORS}, ensure_ascii=False, indent=2)

    # Протокол режем по РЕАЛЬНОМУ контексту модели, а не по константе.
    fixed = len(REPORT_PROMPT_TEMPLATE) + len(schema_json) + len(examples_json)
    budget = _transcript_budget(context_window, max_tokens, fixed)
    if budget < MIN_TRANSCRIPT_CHARS:
        raise JSONValidationError(
            f"Модель {server_cfg['model']} загружена с контекстом {context_window} "
            f"токенов - на отчёт не остаётся места даже под схему находки "
            f"(нужно минимум ~{MIN_TRANSCRIPT_CHARS} символов протокола, "
            f"доступно {budget}). Увеличьте контекст модели в LM Studio или "
            f"выберите другую модель в настройках сессии.")
    if len(transcript) > budget:
        logger.warning(
            "Протокол агента (%d символов) не влезает в контекст модели "
            "(%d токенов) - оставляю последние %d символов: выводы и сводки "
            "агент печатает в конце.", len(transcript), context_window, budget)
        transcript = "... [начало протокола обрезано]\n\n" + transcript[-budget:]

    prompt = REPORT_PROMPT_TEMPLATE.format(
        transcript=transcript,
        schema=schema_json,
        examples=examples_json,
    )
    return get_validated_json(
        make_simple_ask_fn(server_cfg, label=f"агент {server_cfg['model']} — стадия отчёта"),
        prompt, REPORT_SCHEMA,
        max_attempts=max_json_repair_attempts,
    )


def _log_agent_conversation(model: str, interp: OpenInterpreter) -> None:
    """Дамп полной беседы Open Interpreter в транскрипт LM Studio.

    Стадия 1 (исследование) идёт через litellm внутри Open Interpreter, а не через
    наш llm_client, поэтому её обмен ловим не на уровне запроса, а разом - из
    накопленной истории interp.messages. Для стадии отчёта этого не нужно: она
    идёт обычным chat-вызовом и логируется в llm_client сама.
    """
    if not llm_transcript.is_active():
        return
    lines = [f"\n{'=' * 80}",
             f"АГЕНТ (Open Interpreter), модель {model}: полная беседа стадии исследования",
             "=" * 80]
    for m in interp.messages:
        role, mtype = m.get("role"), m.get("type")
        content = m.get("content")
        if not isinstance(content, str):
            content = str(content)
        lines.append(f"\n--- {role}/{mtype} ---\n{content}")
    llm_transcript.raw("\n".join(lines))
