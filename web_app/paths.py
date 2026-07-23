"""
PROJECT_ROOT и ANALYZER_DIR - общие для server.py, sessions.py, queue_worker.py
и _pipeline_runner.py.

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ. Собранный в один exe (PyInstaller) код исполняется из
_internal рядом с exe, а не из исходного дерева - config.yaml, sessions/ и
data/ пользователя должны остаться РЯДОМ С EXE, а не уехать в архив вместе с
кодом. Раньше каждый из четырёх файлов сам вычислял ANALYZER_DIR через свой
__file__ - при сборке в exe это разъехалось бы (один нашёл бы config.yaml
рядом с exe, другой - внутри _internal), а с одной точкой правки достаточно
одной.
"""

import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # sys.executable - сам exe (server.exe или runner.exe); оба лежат в одной
    # папке, поэтому .parent совпадает независимо от того, какой запущен.
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

ANALYZER_DIR = PROJECT_ROOT / "analyzer_to_errors"


def setup_console_utf8():
    """Переключает консоль Windows на UTF-8, иначе кириллица в логах и
    сообщениях превращается в кашу из "?????" и кракозябр.

    Причина: консольное окно Windows по умолчанию живёт в кодовой странице
    866/1251, а Python печатает наши русские строки в UTF-8 - без согласования
    одно с другим не читается. Правим ОБА конца: саму консоль (кодовая
    страница 65001) и потоки Python.

    Делается ЯВНЫМ вызовом из main(), а не на импорте: импортируют этот модуль
    и тесты, а менять им кодовую страницу консоли на ровном месте незачем.
    На не-Windows и там, где stdout - это pipe (подпроцесс без своего окна),
    вызовы просто тихо ничего не меняют."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    # line_buffering=True: каждая строка уходит сразу, а не копится в буфере до
    # закрытия. Без этого при запуске с перенаправлением в файл (.bat с логом)
    # консольный вывод не появлялся вовсе, пока сервер работает, - выглядело
    # как «программа молчит».
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", line_buffering=True)
        except Exception:
            pass
