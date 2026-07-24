"""
Управление моделями LM Studio (web_app/lmstudio.py) - без сети.

Здесь охраняется то, что ломается молча и дорого:
  * БЕЛЫЙ СПИСОК параметров загрузки. Сервер LM Studio отвергает ЛЮБОЙ незнакомый
    ключ с 400 (замерено на живом сервере), поэтому отфильтровать лишнее обязаны
    мы - иначе первая же опечатка в имени параметра валит всю загрузку;
  * НОРМАЛИЗАЦИЯ ответа /api/v1/models: интерфейс полагается на поля loaded,
    vision, reasoning, loaded_instances - разъедься их имена с тем, что шлёт
    сервер, и панель показала бы неверный статус.

Сеть не трогаем: подменяем lmstudio._request (единственную точку, где идёт HTTP),
ровно как в тесте очереди подменяется скрипт-раннер.
"""

import pytest

import lmstudio


# Ответ /api/v1/models в том виде, в каком его отдаёт живой сервер (урезано).
SAMPLE = {
    "models": [
        {"type": "llm", "key": "a-reasoning", "display_name": "A",
         "params_string": "27B", "max_context_length": 262144, "size_bytes": 123,
         "loaded_instances": [],
         "capabilities": {"vision": True, "trained_for_tool_use": True,
                          "reasoning": {"allowed_options": ["off", "on"], "default": "on"}}},
        {"type": "embedding", "key": "emb", "display_name": "E",
         "loaded_instances": []},
        {"type": "llm", "key": "b-loaded", "display_name": "B",
         "loaded_instances": [{"id": "b-loaded", "config": {"context_length": 8192}}],
         "capabilities": {}},
    ]
}


def test_native_root_strips_v1():
    assert lmstudio.native_root("http://h:1234/v1") == "http://h:1234"
    assert lmstudio.native_root("http://h:1234/v1/") == "http://h:1234"
    assert lmstudio.native_root("http://h:1234") == "http://h:1234"


def test_list_models_filters_embeddings_by_default(monkeypatch):
    monkeypatch.setattr(lmstudio, "_request", lambda *a, **k: SAMPLE)
    models = lmstudio.list_models({"base_url": "http://h/v1"})
    keys = [m["key"] for m in models]
    assert "emb" not in keys                      # эмбеддинг агентом быть не может
    assert set(keys) == {"a-reasoning", "b-loaded"}
    # загруженные - первыми (по ним сортировка)
    assert keys[0] == "b-loaded"


def test_list_models_include_non_llm(monkeypatch):
    monkeypatch.setattr(lmstudio, "_request", lambda *a, **k: SAMPLE)
    models = lmstudio.list_models({"base_url": "http://h/v1"}, include_non_llm=True)
    assert "emb" in [m["key"] for m in models]


def test_normalize_exposes_capabilities(monkeypatch):
    monkeypatch.setattr(lmstudio, "_request", lambda *a, **k: SAMPLE)
    by_key = {m["key"]: m for m in
              lmstudio.list_models({"base_url": "http://h/v1"}, include_non_llm=True)}

    a = by_key["a-reasoning"]
    assert a["vision"] is True and a["reasoning"] is True and a["tool_use"] is True
    assert a["loaded"] is False and a["loaded_instances"] == []

    b = by_key["b-loaded"]
    assert b["loaded"] is True and b["reasoning"] is False
    assert b["loaded_instances"] == [{"id": "b-loaded", "context_length": 8192}]


# ---------------------------------------------------------------- параметры load

def test_clean_load_params_whitelist_and_types():
    clean = lmstudio.clean_load_params({
        "context_length": "8192",          # строка -> int
        "flash_attention": True,
        "offload_kv_cache_to_gpu": 1,      # -> bool
        "eval_batch_size": 512,
        "num_experts": 4,
        "ttl_seconds": 60,
        "gpu_offload": 0.5,                # НЕ в белом списке - отбрасываем
        "unknown": "x",
    })
    assert clean == {
        "context_length": 8192, "flash_attention": True,
        "offload_kv_cache_to_gpu": True, "eval_batch_size": 512,
        "num_experts": 4, "ttl_seconds": 60,
    }


def test_clean_load_params_drops_empty():
    assert lmstudio.clean_load_params({"context_length": "", "ttl_seconds": None}) == {}
    assert lmstudio.clean_load_params(None) == {}


def test_clean_load_params_rejects_bad_ttl():
    with pytest.raises(lmstudio.LMStudioError):
        lmstudio.clean_load_params({"ttl_seconds": 0})
    with pytest.raises(lmstudio.LMStudioError):
        lmstudio.clean_load_params({"context_length": "abc"})


def test_load_model_sends_clean_body(monkeypatch):
    captured = {}

    def fake_request(method, url, api_key=None, body=None, timeout=None):
        captured.update(method=method, url=url, body=body)
        return {"instance_id": "m", "status": "loaded"}

    monkeypatch.setattr(lmstudio, "_request", fake_request)
    lmstudio.load_model({"base_url": "http://h/v1"}, "m",
                        {"context_length": 4096, "bogus": 1})
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/v1/models/load")
    assert captured["body"] == {"model": "m", "context_length": 4096}


def test_load_model_requires_model():
    with pytest.raises(lmstudio.LMStudioError):
        lmstudio.load_model({"base_url": "http://h/v1"}, "")


def test_unload_model_sends_instance_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(lmstudio, "_request",
                        lambda m, u, api_key=None, body=None, timeout=None:
                        captured.update(url=u, body=body) or {"instance_id": "x"})
    lmstudio.unload_model({"base_url": "http://h/v1"}, "x")
    assert captured["url"].endswith("/api/v1/models/unload")
    assert captured["body"] == {"instance_id": "x"}
