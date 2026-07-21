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

from openai import OpenAI


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


def _image_part(png: bytes) -> dict:
    """Картинка в том виде, в каком её принимает OpenAI-совместимый эндпоинт.

    Именно data-URL, а не путь к файлу: сервер ИИ может стоять на другой машине
    (адрес задаётся в config.local.yaml), и файловой системы этой он не видит.
    """
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
    }


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

    def ask(message: str, images=(), keep_history: bool = False) -> str:
        content = [{"type": "text", "text": message}]
        content += [_image_part(png) for png in images or ()]

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages += history if keep_history else []
        messages.append({"role": "user", "content": content})

        resp = client.chat.completions.create(
            model=server_cfg["model"],
            messages=messages,
            temperature=server_cfg.get("temperature", 0.1),
            max_tokens=server_cfg.get("max_tokens", 2048),
        )
        text = resp.choices[0].message.content or ""
        if keep_history:
            history.append({"role": "user", "content": content})
            history.append({"role": "assistant", "content": text})
        return text

    return ask
