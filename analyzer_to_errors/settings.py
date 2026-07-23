"""
Пути и конфигурация пайплайна - общая база для всех его модулей.

Выделено из main.py, потому что этим пользуются ВСЕ: стадии (stages.py),
раннер веб-интерфейса, сервер, отчёт в PDF. Пока это лежало в main.py,
любой модуль, которому нужен был resolve_path, тянул за собой весь
оркестратор целиком - вместе с Open Interpreter и агентами.
"""

import logging
import sys
from pathlib import Path

import yaml

logger = logging.getLogger("error_analyzer")

# Собранный в один exe (PyInstaller) код лежит в _internal рядом с exe, а не
# там, откуда его удобно редактировать - config.yaml и остальные пользовательские
# файлы должны остаться РЯДОМ С EXE, а не внутри архива. sys.executable там -
# это сам exe (server.exe или runner.exe), оба лежат в одной папке, поэтому
# .parent совпадает независимо от того, какой из двух сейчас запущен.
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(sys.executable).resolve().parent / "analyzer_to_errors"
else:
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


def resolve_vision_cfg(cfg: dict) -> dict:
    """Настройки сервера для модели зрения.

    Берём сервер названного агента ЦЕЛИКОМ (в том числе base_url, который
    переопределён в config.local.yaml) и накрываем полями ветки vision - тем,
    что у зрения своё: другая модель на том же сервере, своя температура, свой
    лимит ответа. Тот же приём, что у merger.use_agent, и по той же причине:
    второй адрес сервера в конфиге - это второй адрес, который однажды
    разъедется с первым.
    """
    servers = cfg.get("llm_servers", {})
    vision = dict(servers.get("vision") or {})
    base = dict(servers.get(vision.get("use_agent", "agent_1"), {}))
    for key, value in vision.items():
        if key == "use_agent" or value is None:
            continue          # null у model означает «та же, что у агента»
        base[key] = value
    return base


def resolve_path(p) -> Path:
    """Пути из config.yaml - относительно корня проекта, а не cwd:
    пайплайн должен работать одинаково, откуда бы его ни запустили
    (в т.ч. из бэкенда сайта с произвольной рабочей директорией)."""
    path = Path(p)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()
