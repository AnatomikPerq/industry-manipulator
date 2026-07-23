#!/usr/bin/env python3
"""
Сборка портативной версии: PyInstaller (package.spec) + раскладка
пользовательских файлов рядом с exe.

Почему раскладка - отдельным шагом, а не через datas в спеке: PyInstaller
кладёт datas внутрь _internal/, а config.yaml/prompts/base_analysis_scripts
должны остаться СНАРУЖИ, рядом с exe, - это же ветка кода settings.py и
web_app/paths.py (getattr(sys, "frozen", False)), которая ищет
analyzer_to_errors/ именно рядом с sys.executable.

Запуск:
    python build_dist.py
Результат:
    dist/IndustryManipulator/IndustryManipulator.exe
    dist/IndustryManipulator/runner.exe
    dist/IndustryManipulator/analyzer_to_errors/{config.yaml, ...}
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "IndustryManipulator"
AN = ROOT / "analyzer_to_errors"


def run_pyinstaller():
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "package.spec", "--noconfirm"],
        cwd=ROOT, check=True,
    )


def lay_out_user_files():
    dst_an = DIST / "analyzer_to_errors"
    dst_an.mkdir(parents=True, exist_ok=True)

    # Только сами скрипты-парсеры/чекеры - НЕ всю data/ (там в рабочем
    # дереве репозитория лежат чужие PDF разработчика, manifest.json и т.п.
    # от прошлых прогонов, которым в поставке не место).
    scripts_dst = dst_an / "data" / "base_analysis_scripts"
    if scripts_dst.exists():
        shutil.rmtree(scripts_dst)
    shutil.copytree(
        AN / "data" / "base_analysis_scripts", scripts_dst,
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    shutil.copytree(AN / "prompts", dst_an / "prompts", dirs_exist_ok=True)
    shutil.copy2(AN / "config.yaml", dst_an / "config.yaml")
    shutil.copy2(AN / "config.local.example.yaml", dst_an / "config.local.example.yaml")
    shutil.copy2(AN / "known_errors.json", dst_an / "known_errors.json")

    (dst_an / "sessions").mkdir(exist_ok=True)
    (dst_an / "output").mkdir(exist_ok=True)
    (dst_an / "data" / "base_files").mkdir(parents=True, exist_ok=True)
    (dst_an / "data" / "full_projects").mkdir(parents=True, exist_ok=True)
    (dst_an / "data" / "your_helping_scripts_and_files").mkdir(parents=True, exist_ok=True)

    # Запуск с открытым доступом из сети - двойным кликом. Аргументы exe при
    # двойном клике не передашь, поэтому .bat выставляет переменную окружения
    # IM_HOST (её читает server.main). Внутри .bat только латиница: batch
    # читается в кодовой странице консоли, и русский текст в нём пришлось бы
    # хранить в cp866 - предупреждение о рисках exe печатает сам, по-русски.
    bat = DIST / "Открытый доступ из сети.bat"
    bat.write_text(
        "@echo off\r\n"
        "rem Zapusk s dostupom po seti (0.0.0.0). Bez avtorizacii - tolko v\r\n"
        "rem doverennoy seti. Podrobnee - v PROCHTI_MENYA.txt.\r\n"
        "set IM_HOST=0.0.0.0\r\n"
        "\"%~dp0IndustryManipulator.exe\"\r\n"
        "pause\r\n",
        encoding="cp866",
    )

    readme = DIST / "ПРОЧТИ_МЕНЯ.txt"
    readme.write_text(
        "Индустрия манипулятор - офлайн анализатор проектной документации\n"
        "===================================================================\n\n"
        "Запуск: IndustryManipulator.exe - откроется браузер на http://localhost:8000\n\n"
        "Перед первым запуском:\n"
        "  1. Настройте адрес сервера ИИ (LM Studio) в analyzer_to_errors\\config.local.yaml.\n"
        "     Образец - analyzer_to_errors\\config.local.example.yaml, скопируйте его в\n"
        "     config.local.yaml и впишите свой адрес (по умолчанию localhost:1234).\n"
        "     Без этого файла используется адрес по умолчанию из config.yaml.\n"
        "  2. Модели и их лимиты - в analyzer_to_errors\\config.yaml (можно также\n"
        "     выбрать модели прямо в интерфейсе, для каждой сессии).\n\n"
        "ДОСТУП ИЗ СЕТИ (несколько человек с одного сервера):\n"
        "  Запустите \"Открытый доступ из сети.bat\" вместо exe - сервер станет\n"
        "  слушать 0.0.0.0, и коллеги смогут открыть http://<IP этого\n"
        "  компьютера>:8000 в своём браузере. То же самое из командной строки:\n"
        "     IndustryManipulator.exe --host 0.0.0.0\n"
        "  или переменными окружения IM_HOST / IM_PORT.\n"
        "  ВНИМАНИЕ: авторизации в интерфейсе нет - любой, кто откроет адрес,\n"
        "  видит и может удалять ЧУЖИЕ сессии. Открывайте доступ только в\n"
        "  доверенной локальной сети.\n\n"
        "Все ваши сессии и загруженные документы хранятся в\n"
        "analyzer_to_errors\\sessions\\ - эта папка и есть архив прогонов.\n"
        "Ничего в системе не устанавливается и не меняется; чтобы удалить\n"
        "программу, просто удалите эту папку целиком.\n",
        encoding="utf-8",
    )


def main():
    run_pyinstaller()
    lay_out_user_files()
    print(f"\nГотово: {DIST}\\IndustryManipulator.exe")


if __name__ == "__main__":
    main()
