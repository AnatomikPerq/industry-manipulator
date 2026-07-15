"""
Тонкая обёртка над OpenAI-совместимым API (vLLM / LM Studio / etc.)
для простого текстового chat-вызова без инструментов - используется
мерджером отчётов, которому не нужно исполнять код, только рассуждать
над двумя уже готовыми JSON-отчётами.

Требует пакет `openai` (pip install openai).
"""

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
