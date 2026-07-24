#!/usr/bin/env python3
"""
Транскрипт обмена с сервером ИИ (LM Studio) за один прогон - «полный лог LM Studio»,
который потом можно открыть кнопкой в интерфейсе сессии.

ЗАЧЕМ. Сам LM Studio свои консольные логи по API не отдаёт, а разобраться, ПОЧЕМУ
модель выдала то, что выдала, без её ответа нельзя. Поэтому лог собираем на СВОЕЙ
стороне - из всех обращений пайплайна к серверу ИИ: агенты, зрение, мерджер, стадия
отчёта. Пишем в один файл на прогон (main.run_pipeline настраивает путь в output/
сессии перед стадиями ИИ; clear_previous_results в начале прогона чистит output/,
поэтому файл всегда про последний прогон).

Устройство - ГЛОБАЛЬНЫЙ СТОК на процесс. Прогон исполняется отдельным процессом
(web_app/_pipeline_runner.py), по одному на сессию, поэтому глобальное состояние
здесь ничем не грозит: перепутать транскрипты двух сессий невозможно - у них разные
процессы. Точки записи (llm_client, oi_agent) зовут модульные функции record/raw,
не таская путь за собой.

Только стандартная библиотека: этот модуль импортируют из llm_client, который в
свою очередь тянут и стадия зрения, и мерджер, и проверка серверов.
"""

import threading
import time
from pathlib import Path

# Каждый блок запроса/ответа обрезаем сверху: беседа агента с повторяющейся схемой
# в промпте может весить сотни килобайт, а файл лога открывают в браузере. Предел
# щедрый - это «полный транскрипт», а не сводка, - но не бесконечный.
_MAX_BLOCK_CHARS = 400_000


class _Transcript:
    def __init__(self):
        self._lock = threading.Lock()
        self._path = None

    def configure(self, path) -> None:
        """Назначить файл транскрипта и начать его заново (пустым).

        Пустым - потому что файл про ОДИН прогон: остатки прошлого прогона в нём
        читались бы как часть нынешнего. Шапку пишет вызывающий (main) через raw().
        """
        with self._lock:
            self._path = Path(path) if path else None
            if self._path is None:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                self._path.write_text("", encoding="utf-8")
            except OSError:
                self._path = None      # некуда писать - молча выключаемся

    def is_active(self) -> bool:
        return self._path is not None

    def close(self) -> None:
        with self._lock:
            self._path = None

    def _append(self, text: str) -> None:
        with self._lock:
            if self._path is None:
                return
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass               # лог не должен ронять прогон

    def raw(self, text: str) -> None:
        """Записать произвольный текст (шапка прогона, дамп беседы агента)."""
        if self.is_active():
            self._append(text.rstrip("\n") + "\n")

    def record(self, label, model=None, request=None, response=None,
               seconds=None, error=None, meta=None) -> None:
        """Один обмен с моделью: что отправили, что получили, сколько заняло."""
        if not self.is_active():
            return
        head = f"\n{'=' * 80}\n[{time.strftime('%H:%M:%S')}] {label}"
        tail = []
        if model:
            tail.append(f"модель: {model}")
        if seconds is not None:
            tail.append(f"{seconds:.1f} c")
        if meta:
            tail.append(meta)
        if tail:
            head += "  (" + ", ".join(tail) + ")"
        parts = [head]
        if request is not None:
            parts.append("--- ЗАПРОС ---\n" + _clip(request))
        if response is not None:
            parts.append("--- ОТВЕТ ---\n" + _clip(response))
        if error is not None:
            parts.append("--- ОШИБКА ---\n" + str(error))
        self._append("\n".join(parts) + "\n")


def _clip(text) -> str:
    s = str(text)
    if len(s) <= _MAX_BLOCK_CHARS:
        return s
    return (s[:_MAX_BLOCK_CHARS]
            + f"\n…[обрезано, показано {_MAX_BLOCK_CHARS} из {len(s)} символов]")


# Единственный на процесс сток и модульные обёртки над ним.
_SINK = _Transcript()

configure = _SINK.configure
is_active = _SINK.is_active
close = _SINK.close
raw = _SINK.raw
record = _SINK.record


def messages_to_text(messages) -> str:
    """Сообщения OpenAI-формата -> читаемый текст для транскрипта.

    content бывает строкой или списком частей (текст + картинки): картинку в лог
    целиком (data-URL на мегабайт) не кладём - помечаем «[изображение]»."""
    lines = []
    for msg in messages or []:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, list):
            chunks = []
            for part in content:
                if not isinstance(part, dict):
                    chunks.append(str(part))
                elif part.get("type") == "text":
                    chunks.append(part.get("text", ""))
                elif part.get("type") == "image_url":
                    chunks.append("[изображение]")
                else:
                    chunks.append(f"[{part.get('type', 'часть')}]")
            content = "\n".join(chunks)
        lines.append(f"[{role}]\n{content}")
    return "\n\n".join(lines)
