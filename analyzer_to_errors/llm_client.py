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

import llm_stats
import llm_transcript

logger = logging.getLogger(__name__)


def _completion_tokens(resp, text=None):
    """Сколько токенов в ответе модели. Сначала спрашиваем сервер (usage), а если
    он их не прислал - грубо оцениваем по длине ответа (~4 символа на токен),
    чтобы счётчик скорости в интерфейсе не пропадал на серверах без usage.
    Оценка приблизительна и только для показа скорости, не для биллинга."""
    usage = getattr(resp, "usage", None)
    ct = getattr(usage, "completion_tokens", None) if usage else None
    if ct:
        return ct
    if text:
        return max(1, round(len(text) / 4))
    return None


def _usage_meta(resp) -> str:
    """Короткая пометка о токенах для транскрипта, если сервер их вернул."""
    usage = getattr(resp, "usage", None)
    if not usage:
        return None
    ct = getattr(usage, "completion_tokens", None)
    pt = getattr(usage, "prompt_tokens", None)
    bits = []
    if pt is not None:
        bits.append(f"промпт {pt}")
    if ct is not None:
        bits.append(f"ответ {ct} токенов")
    return ", ".join(bits) or None

# Коды, при которых запрос имеет смысл повторить. 503 LM Studio отдаёт, когда
# модель ещё грузится или на секунду отвалилась, - на прогоне в сотни тайлов
# такое случается, и терять из-за этого лист (а на альбоме - половину находок)
# нельзя. 4xx не повторяем: это ошибка запроса, второй раз выйдет то же самое.
RETRYABLE_STATUS = (500, 502, 503, 504)
VISION_RETRIES = 3
RETRY_DELAY = 5.0


def native_models_url(base_url: str) -> str:
    """Нативный эндпоинт LM Studio: там видно, С КАКИМ контекстом модель
    реально загружена. У OpenAI-совместимого /v1/models этого нет."""
    root = str(base_url).rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root + "/api/v1/models"


def loaded_context_length(server_cfg: dict, timeout: float = 5.0):
    """С каким контекстом модель ЗАГРУЖЕНА на сервере, или None.

    ЗАЧЕМ. context_window в config.yaml - число, написанное человеком, и оно
    неизбежно расходится с тем, как модель на самом деле загрузили в LM Studio.
    Замер: у qwythos-9b в конфиге стоит 200000, а загружена она с 8192, и
    стадия агентов умирала на первом же обращении - Open Interpreter верит
    конфигу, считает по нему бюджет истории и отправляет 9808 токенов туда, где
    принимают 8192. Ошибка при этом прилетает простынёй из litellm, по которой
    догадаться о причине нельзя.

    Спрашиваем сервер: он единственный знает правду. Не ответил - возвращаем
    None и работаем по конфигу, как раньше: проверка доступности не должна
    становиться условием запуска.
    """
    import json
    import urllib.request

    try:
        req = urllib.request.Request(native_models_url(server_cfg["base_url"]))
        if server_cfg.get("api_key") and server_cfg["api_key"] != "not-needed":
            req.add_header("Authorization", f"Bearer {server_cfg['api_key']}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 - сервер недоступен, это не наше дело
        logger.debug("Не удалось спросить контекст у сервера: %s", e)
        return None

    for m in data.get("models", []):
        if m.get("key") != server_cfg.get("model"):
            continue
        for inst in m.get("loaded_instances") or []:
            ctx = (inst.get("config") or {}).get("context_length")
            if ctx:
                return int(ctx)
        return None
    return None


def make_client(server_cfg: dict) -> OpenAI:
    return OpenAI(
        base_url=server_cfg["base_url"],
        api_key=server_cfg.get("api_key") or "not-needed",
    )


def make_simple_ask_fn(server_cfg: dict, label: str = "LLM (chat)"):
    """Возвращает функцию (str) -> str поверх обычного chat.completions,
    с накоплением истории диалога (нужно для цикла авторемонта JSON:
    модель должна помнить свой предыдущий невалидный ответ).

    label - как назвать эти обращения в транскрипте LM Studio (мерджер, стадия
    отчёта агента, проверка серверов): в один прогон их несколько разных.
    """
    client = make_client(server_cfg)
    history = []

    def ask(message: str) -> str:
        history.append({"role": "user", "content": message})
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=server_cfg["model"],
                messages=history,
                temperature=server_cfg.get("temperature", 0.0),
                max_tokens=server_cfg.get("max_tokens", 4096),
            )
        except Exception as e:  # noqa: BLE001 - логируем отказ и пробрасываем
            llm_transcript.record(label, model=server_cfg.get("model"),
                                  request=message, seconds=time.time() - t0,
                                  error=f"{type(e).__name__}: {e}")
            raise
        text = resp.choices[0].message.content or ""
        elapsed = time.time() - t0
        history.append({"role": "assistant", "content": text})
        llm_transcript.record(label, model=server_cfg["model"], request=message,
                              response=text, seconds=elapsed,
                              meta=_usage_meta(resp))
        llm_stats.record(model=server_cfg["model"],
                         tokens=_completion_tokens(resp, text), seconds=elapsed)
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


def make_vision_ask_fn(server_cfg: dict, system: str = None, label: str = "зрение"):
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

        n_img = len(images or ())
        req_text = message + (f"\n[+ {n_img} изображени(я/й)]" if n_img else "")
        t0 = time.time()
        try:
            resp = _with_retries(
                lambda: client.chat.completions.create(
                    model=server_cfg["model"],
                    messages=messages,
                    temperature=server_cfg.get("temperature", 0.1),
                    max_tokens=max_tokens,
                ))
        except Exception as e:  # noqa: BLE001 - логируем отказ и пробрасываем
            llm_transcript.record(label, model=server_cfg.get("model"),
                                  request=req_text, seconds=time.time() - t0,
                                  error=f"{type(e).__name__}: {e}")
            raise
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
                err = VisionAnswerError(
                    f"модель израсходовала весь лимит ответа ({max_tokens} токенов) на "
                    "рассуждение и до ответа не дошла. Так она ведёт себя на густой "
                    "графике; поднимать лимит бесполезно - она растянет рассуждение и "
                    "на него (замерено). Смотрите настройки модели в LM Studio")
            else:
                err = VisionAnswerError(
                    f"модель вернула пустой ответ (finish_reason={reason})")
            llm_transcript.record(label, model=server_cfg.get("model"),
                                  request=req_text, seconds=time.time() - t0,
                                  error=str(err))
            raise err

        elapsed = time.time() - t0
        llm_transcript.record(label, model=server_cfg["model"], request=req_text,
                              response=text, seconds=elapsed,
                              meta=_usage_meta(resp))
        llm_stats.record(model=server_cfg["model"],
                         tokens=_completion_tokens(resp, text), seconds=elapsed)
        if keep_history:
            history.append({"role": "user", "content": content})
            history.append({"role": "assistant", "content": text})
        return text

    return ask
