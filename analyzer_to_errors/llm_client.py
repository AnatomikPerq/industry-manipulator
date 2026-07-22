"""
Тонкая обёртка над OpenAI-совместимым API (vLLM / LM Studio / etc.)
для простого текстового chat-вызова без инструментов - используется
мерджером отчётов, которому не нужно исполнять код, только рассуждать
над двумя уже готовыми JSON-отчётами.

Здесь же вызов модели ЗРЕНИЯ (make_vision_ask_fn): тот же самый эндпоинт,
только в content уходит не строка, а список частей, среди которых картинки.
Open Interpreter для зрения не нужен и вреден - исполнять модели нечего, она
смотрит на растр и отвечает текстом.

Требует пакет `openai` (pip install openai).
"""

import base64
import logging
import time

from openai import APIConnectionError, APIStatusError, OpenAI

logger = logging.getLogger(__name__)

# Коды, при которых запрос имеет смысл повторить. 503 LM Studio отдаёт, когда
# модель ещё грузится или на секунду отвалилась, - на прогоне в сотни тайлов
# такое случается, и терять из-за этого лист (а на альбоме - половину находок)
# нельзя. 4xx не повторяем: это ошибка запроса, второй раз выйдет то же самое.
RETRYABLE_STATUS = (500, 502, 503, 504)
VISION_RETRIES = 3
RETRY_DELAY = 5.0


def make_client(server_cfg: dict) -> OpenAI:
    return OpenAI(
        base_url=server_cfg["base_url"],
        api_key=server_cfg.get("api_key") or "not-needed",
    )


def make_simple_ask_fn(server_cfg: dict):
    """Возвращает функцию (str) -> str поверх обычного chat.completions,
    с накоплением истории диалога (нужно для цикла авторемонта JSON:
    модель должна помнить свой предыдущий невалидный ответ)."""
    client = make_client(server_cfg)
    history = []

    def ask(message: str) -> str:
        history.append({"role": "user", "content": message})
        resp = client.chat.completions.create(
            model=server_cfg["model"],
            messages=history,
            temperature=server_cfg.get("temperature", 0.0),
            max_tokens=server_cfg.get("max_tokens", 4096),
        )
        text = resp.choices[0].message.content or ""
        history.append({"role": "assistant", "content": text})
        return text

    return ask


def _with_retries(call, retries: int = VISION_RETRIES, delay: float = RETRY_DELAY):
    """Повтор запроса при временном отказе сервера.

    Прогон зрения - это сотни вызовов подряд, и один 503 посреди альбома не
    повод терять лист. Повторяем только то, что имеет смысл повторять:
    обрыв соединения и 5xx.
    """
    for attempt in range(1, retries + 1):
        try:
            return call()
        except APIConnectionError as e:
            last = e
        except APIStatusError as e:
            if e.status_code not in RETRYABLE_STATUS:
                raise
            last = e
        if attempt < retries:
            logger.warning("Сервер ИИ не ответил (%s), повтор %d из %d через %.0f c",
                           type(last).__name__, attempt, retries - 1, delay * attempt)
            time.sleep(delay * attempt)
    raise last


class VisionAnswerError(RuntimeError):
    """Модель зрения не дала ответа. Причина - в тексте: её видно и в логе
    прогона, и в выводе калибровки."""


def _image_part(png: bytes) -> dict:
    """Картинка в том виде, в каком её принимает OpenAI-совместимый эндпоинт.

    Именно data-URL, а не путь к файлу: сервер ИИ может стоять на другой машине
    (адрес задаётся в config.local.yaml), и файловой системы этой он не видит.
    """
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
    }


# ПОТОЛОК ЛИМИТА ОТВЕТА ДЛЯ ЗРЕНИЯ. Не «сколько разрешить», а «сколько ждать».
#
# У агента max_tokens - это запас на длинный отчёт, и там чем больше, тем лучше.
# У зрения ровно наоборот: ответ - десяток чисел, а вот РАССУЖДЕНИЕ думающей
# модели расходует тот же бюджет и растягивается ровно настолько, насколько ему
# позволили. Замер: со значением 65536 из общего конфига один тайл считался
# 22-25 минут (~45 токенов/с), и прогон на 12 тайлов уходил в четыре часа - при
# том, что осмысленный ответ занимает 300-2500 токенов.
#
# Поэтому лимит зажимается здесь, а не только в config.yaml: конфиг правят
# целиком (все max_tokens разом), и зрение не должно от этого впадать в кому.
# Уткнувшись в потолок, модель вернёт пустой ответ - и это станет громкой
# ошибкой через минуту, а не тихим зависанием на полдня.
VISION_MAX_TOKENS_CEILING = 4096


def make_vision_ask_fn(server_cfg: dict, system: str = None):
    """Возвращает функцию (текст, картинки) -> ответ модели.

    БЕЗ ИСТОРИИ ПО УМОЛЧАНИЮ, и это принципиально. Единица работы зрения - один
    тайл листа: вопрос про него не зависит от того, что модель видела на
    предыдущем, а накопленная история из десятка картинок съела бы контекст и
    потащила бы за собой чужие подписи («на прошлом тайле было 11, значит и
    здесь 11»). Историю просит только цикл авторемонта JSON - для него
    keep_history=True.
    """
    client = make_client(server_cfg)
    history = []

    max_tokens = server_cfg.get("max_tokens", 2048)
    if max_tokens > VISION_MAX_TOKENS_CEILING:
        logger.warning(
            "max_tokens=%d для модели зрения слишком велик: думающая модель растянет "
            "рассуждение на весь лимит (замер: 65536 -> 22 минуты на ОДИН тайл). "
            "Зажимаю до %d - ответ зрения это десяток чисел.",
            max_tokens, VISION_MAX_TOKENS_CEILING)
        max_tokens = VISION_MAX_TOKENS_CEILING

    def ask(message: str, images=(), keep_history: bool = False) -> str:
        content = [{"type": "text", "text": message}]
        content += [_image_part(png) for png in images or ()]

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages += history if keep_history else []
        messages.append({"role": "user", "content": content})

        resp = _with_retries(
            lambda: client.chat.completions.create(
                model=server_cfg["model"],
                messages=messages,
                temperature=server_cfg.get("temperature", 0.1),
                max_tokens=max_tokens,
            ))
        choice = resp.choices[0]
        text = choice.message.content or ""

        # ПУСТОЙ ОТВЕТ - ЭТО ОТКАЗ, А НЕ «НИЧЕГО НЕ НАЙДЕНО».
        #
        # Замеренный случай: «думающая» модель (Qwen3) израсходовала ВЕСЬ лимит
        # ответа на рассуждение - finish_reason='length', reasoning_tokens=2048,
        # content пуст. Наверх это уезжало пустой строкой, разбиралось как
        # «подписей на тайле нет» и давало ровный, правдоподобный и полностью
        # ложный результат: «прочитано 4 из 54». Целый прогон калибровки ушёл
        # на то, чтобы это заметить.
        #
        # Поэтому молчание модели - исключение с причиной. Стадия ловит его
        # потайлово и пишет в лог, лист из-за одного тайла не теряется.
        if not text.strip():
            reason = choice.finish_reason
            if reason == "length":
                raise VisionAnswerError(
                    f"модель израсходовала весь лимит ответа ({max_tokens} токенов) на "
                    "рассуждение и до ответа не дошла. Так она ведёт себя на густой "
                    "графике; поднимать лимит бесполезно - она растянет рассуждение и "
                    "на него (замерено). Смотрите настройки модели в LM Studio")
            raise VisionAnswerError(f"модель вернула пустой ответ (finish_reason={reason})")

        if keep_history:
            history.append({"role": "user", "content": content})
            history.append({"role": "assistant", "content": text})
        return text

    return ask
