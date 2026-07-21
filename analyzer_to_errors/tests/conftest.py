"""
Общая обвязка тестов.

ПОЧЕМУ ТЕСТЫ ЛЕЖАТ ВНУТРИ analyzer_to_errors, А НЕ В КОРНЕ РЕПОЗИТОРИЯ.
По той же причине, по которой там же обязаны лежать папки сессий: ingest.py
пишет пути документов в манифест через relative_to(PROJECT_ROOT), а
main.run_rules_stage собирает их обратно как PROJECT_ROOT / doc["data_dir"].
Фикстура - это готовый манифест с такими путями, и снаружи analyzer_to_errors
он бы попросту не собрался.
"""

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
SCRIPTS_DIR = PROJECT_ROOT / "data" / "base_analysis_scripts"

# Пайплайн не пакет: модули лежат плоско и импортируются по имени.
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT.parent / "web_app"))


@pytest.fixture(scope="session")
def scripts_dir() -> Path:
    return SCRIPTS_DIR


def fixture_cfg(name: str) -> dict:
    """Минимальный config для стадий правил и связок по фикстуре.

    Больше стадиям ничего не нужно: ни серверов ИИ, ни known_errors, ни
    output - они читают manifest.json и запускают чекеры из scripts_dir.
    """
    return {
        "paths": {
            "scripts_dir": str(SCRIPTS_DIR),
            "input_dir": str(FIXTURES / name),
        }
    }


def fixture_data_dir(name: str) -> Path:
    return FIXTURES / name


def load_script(name: str):
    """Скрипт из base_analysis_scripts по имени файла (та же загрузка по пути,
    какой их грузит пайплайн: папка копируется в сессии и в sys.path не лежит)."""
    import importlib.util

    path = SCRIPTS_DIR / name
    spec = importlib.util.spec_from_file_location(f"_test_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
