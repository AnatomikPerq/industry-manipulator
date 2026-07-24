"""
Правка глобального конфига ИИ админом (web_app/config_admin.py) - без сети.

Здесь охраняется главное свойство этой правки: изменения уходят в
config.local.yaml и НЕ трогают config.yaml (там документация), переопределяя его
ПО ВЕТКАМ. Разъедься это - и либо потеряются пояснения в config.yaml, либо
чужая ветка (пути, логирование) молча уедет в локальный файл и переживёт
обновление проекта.
"""

import pytest
import yaml

import config_admin as ca


BASE = {
    "llm_servers": {
        "agent_1": {"base_url": "http://x/v1", "model": "m1", "temperature": 0.2,
                    "max_tokens": 50000, "context_window": 200000, "api_key": "not-needed"},
        "agent_2": {"base_url": "http://x/v1", "model": "m2"},
        "vision": {"use_agent": "agent_1", "model": "v1", "max_tokens": 8192},
        "merger": {"use_agent": "agent_1"},
    },
    "agents": {"count": 1, "single_agent": "agent_1"},
    "vision": {"cap_px": 18, "max_tile_pixels": 1000000},
    "paths": {"output_dir": "./output"},          # не про ИИ - трогать нельзя
}


@pytest.fixture()
def base_path(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(BASE, allow_unicode=True), encoding="utf-8")
    # существующий локальный файл с одним лишь адресом сервера
    (tmp_path / "config.local.yaml").write_text(
        yaml.safe_dump({"llm_servers": {"agent_1": {"base_url": "http://real:1234/v1"}}}),
        encoding="utf-8")
    return p


def _local(base_path):
    return yaml.safe_load((base_path.with_name("config.local.yaml")).read_text(encoding="utf-8"))


def test_apply_merges_into_local_preserving_branches(base_path):
    ca.apply_admin_config(str(base_path), {
        "llm_servers": {"agent_1": {"model": "new", "context_window": 8192}},
        "agents": {"count": 2},
    })
    local = _local(base_path)
    # прежнее локальное поле уцелело
    assert local["llm_servers"]["agent_1"]["base_url"] == "http://real:1234/v1"
    # новые - записаны
    assert local["llm_servers"]["agent_1"]["model"] == "new"
    assert local["llm_servers"]["agent_1"]["context_window"] == 8192
    assert local["agents"]["count"] == 2
    # чужая ветка (пути) в локальный файл не утекла
    assert "paths" not in local


def test_effective_view_reflects_merge(base_path):
    view = ca.apply_admin_config(str(base_path), {
        "llm_servers": {"agent_1": {"context_window": 4096}},
    })
    a1 = view["effective"]["llm_servers"]["agent_1"]
    assert a1["context_window"] == 4096          # локальная правка
    assert a1["model"] == "m1"                   # из base
    assert a1["base_url"] == "http://real:1234/v1"


def test_config_yaml_not_modified(base_path):
    before = base_path.read_text(encoding="utf-8")
    ca.apply_admin_config(str(base_path), {"agents": {"count": 2}})
    assert base_path.read_text(encoding="utf-8") == before


def test_validate_rejects_bad_values():
    with pytest.raises(ca.ConfigError):
        ca.validate_changes({"llm_servers": {"agent_1": {"temperature": 9}}})
    with pytest.raises(ca.ConfigError):
        ca.validate_changes({"agents": {"count": 5}})
    with pytest.raises(ca.ConfigError):
        ca.validate_changes({"agents": {"single_agent": "agent_9"}})
    with pytest.raises(ca.ConfigError):
        ca.validate_changes({"llm_servers": {"agent_1": {"max_tokens": -1}}})


def test_validate_ignores_unknown_fields_but_needs_something():
    # незнакомое поле молча пропускается...
    with pytest.raises(ca.ConfigError):
        ca.validate_changes({"llm_servers": {"agent_1": {"bogus": 1}}})
    # ...а знакомое рядом - проходит
    out = ca.validate_changes({"llm_servers": {"agent_1": {"bogus": 1, "model": "z"}}})
    assert out == {"llm_servers": {"agent_1": {"model": "z"}}}


def test_empty_string_clears_optional(base_path):
    # пустая строка у model/api_key = сброс (null), чтобы вернуть «как у агента»
    out = ca.validate_changes({"llm_servers": {"vision": {"model": ""}}})
    assert out["llm_servers"]["vision"]["model"] is None
