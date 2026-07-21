"""
Пути и конфигурация пайплайна - общая база для всех его модулей.

Выделено из main.py, потому что этим пользуются ВСЕ: стадии (stages.py),
раннер веб-интерфейса, сервер, отчёт в PDF. Пока это лежало в main.py,
любой модуль, которому нужен был resolve_path, тянул за собой весь
оркестратор целиком - вместе с Open Interpreter и агентами.
"""

import logging
from pathlib import Path

import yaml

logger = logging.getLogger("error_analyzer")

PROJECT_ROOT = Path(__file__).resolve().parent

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# Локальные настройки этой машины поверх config.yaml. В репозиторий не едут.
LOCAL_CONFIG_SUFFIX = ".local.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Слияние настроек по веткам, а не целыми разделами.

    Иначе, чтобы поменять один base_url, в локальный файл пришлось бы
    скопировать весь llm_servers - и он тихо разъехался бы с config.yaml при
    первой же правке модели или лимитов.
    """
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str) -> dict:
    """config.yaml плюс, если он есть рядом, config.local.yaml поверх него.

    ЗАЧЕМ РАЗДЕЛЕНИЕ. Адрес сервера ИИ - настройка КОНКРЕТНОЙ УСТАНОВКИ, а не
    проекта: у одного он на localhost (LM Studio на той же машине), у другого
    на машине в серверной. В общем config.yaml он неизбежно оказывался чьим-то
    личным - и уезжал в репозиторий вместе с ним. Плюс это ровно то поле, по
    которому расходились README («полностью офлайн») и то, что стояло в
    конфиге на самом деле.

    Локальный файл ПЕРЕОПРЕДЕЛЯЕТ только названные в нём ветки: остальное
    берётся из config.yaml, и обновление проекта не требует его переписывать.
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    local = path.with_name(path.stem + LOCAL_CONFIG_SUFFIX)
    if local.is_file():
        with open(local, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
        logger.debug("Локальные настройки применены: %s", local)
    return cfg


def resolve_path(p) -> Path:
    """Пути из config.yaml - относительно корня проекта, а не cwd:
    пайплайн должен работать одинаково, откуда бы его ни запустили
    (в т.ч. из бэкенда сайта с произвольной рабочей директорией)."""
    path = Path(p)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
