#!/usr/bin/env python3
"""
Редактирование ГЛОБАЛЬНОГО конфига нейросетей из интерфейса (только админ).

КУДА ПИШЕМ И ПОЧЕМУ. Правки уходят в config.local.yaml, а НЕ в config.yaml.
config.yaml - это документированный по самое горло файл-образец (десятки строк
пояснений, зачем какой лимит), он в репозитории и общий для всех установок.
Перезаписать его через YAML-дампер значило бы стереть всю эту документацию.
config.local.yaml - файл КОНКРЕТНОЙ УСТАНОВКИ (gitignored), и слияние по веткам
уже устроено так, что он переопределяет только названные поля (settings._deep_merge,
settings.load_config). Поэтому админ-правка модели/лимита ложится сюда и
переопределяет config.yaml, ничего в нём не ломая. Действует со СЛЕДУЮЩЕГО
прогона: каждый прогон зовёт load_config, а тот подмешивает local (перезапуск
сервера не нужен).

ЧТО МОЖНО МЕНЯТЬ - строго по белому списку (_FIELDS): модели агентов/зрения/
мерджера, адрес и ключ сервера, лимиты, температура, число агентов и параметры
стадии зрения. Всё остальное в конфиге (пути, логирование, extraction) из
интерфейса не трогается - это не про нейросети.

Модуль тянет только yaml (он и так есть у пайплайна) и стандартную библиотеку.
"""

from pathlib import Path

import yaml

# То же имя-суффикс, что и у settings.LOCAL_CONFIG_SUFFIX. Держим свою копию
# сознательно: config_admin импортируют веб-сервер и тесты, и тащить ради одной
# строки весь settings (а с ним - половину пайплайна) незачем.
LOCAL_SUFFIX = ".local.yaml"


class ConfigError(Exception):
    """Недопустимое значение в присланных настройках - показываем пользователю."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---- валидаторы отдельных полей ----

def _as_str(value, name, maxlen=300):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maxlen:
        raise ConfigError(f"{name}: ожидалась строка до {maxlen} символов")
    return value.strip()


def _as_pos_int(value, name):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: ожидалось целое число")
    if n <= 0:
        raise ConfigError(f"{name}: число должно быть положительным")
    return n


def _as_temp(value, name):
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name}: ожидалось число (температура)")
    if not (0.0 <= t <= 2.0):
        raise ConfigError(f"{name}: температура вне диапазона 0..2")
    return t


def _as_agent_key(value, name):
    if value not in ("agent_1", "agent_2"):
        raise ConfigError(f"{name}: допустимо только agent_1 или agent_2")
    return value


# Белый список редактируемых полей по веткам конфига. Значение - валидатор.
# base_url/api_key/model/temperature/max_tokens/context_window у agent_1/agent_2;
# у vision - своя модель, температура и лимит (сервер берётся у агента через
# use_agent); merger - только use_agent; agents - число и единственный агент;
# vision (ветка верхнего уровня) - параметры рендера стадии зрения.
_SERVER_FIELDS = {
    "model": lambda v: _as_str(v, "модель"),
    "base_url": lambda v: _as_str(v, "base_url"),
    "api_key": lambda v: _as_str(v, "api_key"),
    "temperature": lambda v: _as_temp(v, "температура"),
    "max_tokens": lambda v: _as_pos_int(v, "max_tokens"),
    "context_window": lambda v: _as_pos_int(v, "context_window"),
}
_VISION_SERVER_FIELDS = {
    "use_agent": lambda v: _as_agent_key(v, "vision.use_agent"),
    "model": lambda v: _as_str(v, "модель зрения"),   # null = как у агента
    "temperature": lambda v: _as_temp(v, "температура зрения"),
    "max_tokens": lambda v: _as_pos_int(v, "max_tokens зрения"),
}
_MERGER_FIELDS = {
    "use_agent": lambda v: _as_agent_key(v, "merger.use_agent"),
}
_VISION_STAGE_FIELDS = {
    "cap_px": lambda v: _as_pos_int(v, "cap_px"),
    "max_tile_pixels": lambda v: _as_pos_int(v, "max_tile_pixels"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base or {})
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _local_path(base_config_path) -> Path:
    base = Path(base_config_path)
    return base.with_name(base.stem + LOCAL_SUFFIX)


def _validate_branch(fields: dict, data: dict, branch: str) -> dict:
    """Провалидировать один узел настроек по своему белому списку. Незнакомые
    ключи молча пропускаем: интерфейс мог прислать поле, которого мы не правим
    (например, служебное), и это не повод ронять всю форму."""
    out = {}
    for key, value in (data or {}).items():
        validator = fields.get(key)
        if validator is None:
            continue
        # Пустая строка для необязательных строковых полей = «сбросить» (null):
        # так админ убирает переопределение модели зрения, возвращая её к «как у
        # агента». Для остального пустых значений не шлём.
        if value == "" and key in ("model", "api_key"):
            out[key] = None
            continue
        out[key] = validator(value)
    return out


def validate_changes(changes: dict) -> dict:
    """Собирает из присланного объекта чистый override для config.local.yaml.

    Возвращает вложенный словарь только с разрешёнными и проверенными полями -
    ровно то, что уйдёт в _deep_merge с текущим локальным конфигом.
    """
    if not isinstance(changes, dict):
        raise ConfigError("Ожидался объект настроек")

    override = {}
    servers_in = changes.get("llm_servers") or {}
    servers_out = {}
    for key in ("agent_1", "agent_2"):
        if key in servers_in:
            branch = _validate_branch(_SERVER_FIELDS, servers_in[key], key)
            if branch:
                servers_out[key] = branch
    if "vision" in servers_in:
        branch = _validate_branch(_VISION_SERVER_FIELDS, servers_in["vision"], "vision")
        if branch:
            servers_out["vision"] = branch
    if "merger" in servers_in:
        branch = _validate_branch(_MERGER_FIELDS, servers_in["merger"], "merger")
        if branch:
            servers_out["merger"] = branch
    if servers_out:
        override["llm_servers"] = servers_out

    agents_in = changes.get("agents") or {}
    agents_out = {}
    if "count" in agents_in:
        count = agents_in["count"]
        if count not in (1, 2, "1", "2"):
            raise ConfigError("agents.count: допустимо 1 или 2")
        agents_out["count"] = int(count)
    if "single_agent" in agents_in:
        agents_out["single_agent"] = _as_agent_key(
            agents_in["single_agent"], "agents.single_agent")
    if agents_out:
        override["agents"] = agents_out

    if "vision" in changes:
        branch = _validate_branch(_VISION_STAGE_FIELDS, changes["vision"], "vision")
        if branch:
            override["vision"] = branch

    if not override:
        raise ConfigError("Нет ни одного распознанного поля для сохранения")
    return override


# Шапка, которой начинается перезаписываемый config.local.yaml. Инлайновые
# комментарии YAML-дампер стирает, поэтому оставляем хотя бы этот блок - чтобы
# заглянувший в файл понял, откуда он и что правится из интерфейса.
_LOCAL_HEADER = (
    "# Настройки ЭТОЙ установки. В репозиторий не едут (см. .gitignore).\n"
    "#\n"
    "# Этот файл РЕДАКТИРУЕТСЯ ИЗ ИНТЕРФЕЙСА (админ -> «Настройки ИИ»). Правки\n"
    "# отсюда переопределяют config.yaml по веткам (settings._deep_merge), не\n"
    "# трогая его. Можно править и руками, но учтите: сохранение из интерфейса\n"
    "# перезапишет файл целиком, и рукописные комментарии пропадут.\n"
    "#\n"
    "# Образец с пояснениями - config.local.example.yaml.\n\n"
)


def apply_admin_config(base_config_path, changes: dict) -> dict:
    """Валидирует правки и сливает их в config.local.yaml. Возвращает
    admin_view нового состояния (см. admin_view)."""
    override = validate_changes(changes)
    local_path = _local_path(base_config_path)
    current_local = _load_yaml(local_path)
    merged_local = _deep_merge(current_local, override)

    text = _LOCAL_HEADER + yaml.safe_dump(
        merged_local, allow_unicode=True, sort_keys=False)
    local_path.write_text(text, encoding="utf-8")
    return admin_view(base_config_path)


def admin_view(base_config_path) -> dict:
    """Данные для формы: эффективный конфиг ИИ + что переопределено локально.

    effective - что реально пойдёт в прогон (config.yaml + config.local.yaml).
    local - только локальные переопределения: по нему интерфейс помечает поля,
    отличающиеся от образца, чтобы админ видел, что именно он уже менял.
    """
    base = _load_yaml(Path(base_config_path))
    local = _load_yaml(_local_path(base_config_path))
    effective = _deep_merge(base, local)

    def branch(cfg, *keys):
        node = cfg
        for k in keys:
            node = (node or {}).get(k) if isinstance(node, dict) else None
        return node or {}

    servers = effective.get("llm_servers", {})
    return {
        "effective": {
            "llm_servers": {
                "agent_1": _server_view(servers.get("agent_1", {})),
                "agent_2": _server_view(servers.get("agent_2", {})),
                "vision": _vision_view(servers.get("vision", {})),
                "merger": {"use_agent": (servers.get("merger", {}) or {}).get("use_agent")},
            },
            "agents": {
                "count": effective.get("agents", {}).get("count", 1),
                "single_agent": effective.get("agents", {}).get("single_agent", "agent_1"),
            },
            "vision": {
                "cap_px": effective.get("vision", {}).get("cap_px"),
                "max_tile_pixels": effective.get("vision", {}).get("max_tile_pixels"),
            },
        },
        # какие ветки/поля заданы именно локально - для пометок в интерфейсе
        "local": local,
    }


def _server_view(s: dict) -> dict:
    return {
        "model": s.get("model"),
        "base_url": s.get("base_url"),
        "api_key": s.get("api_key"),
        "temperature": s.get("temperature"),
        "max_tokens": s.get("max_tokens"),
        "context_window": s.get("context_window"),
    }


def _vision_view(s: dict) -> dict:
    return {
        "use_agent": s.get("use_agent", "agent_1"),
        "model": s.get("model"),
        "temperature": s.get("temperature"),
        "max_tokens": s.get("max_tokens"),
    }
