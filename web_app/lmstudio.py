#!/usr/bin/env python3
"""
Управление сервером LM Studio через его НАТИВНЫЙ REST API (`/api/v1/`).

ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ. Раньше веб-часть умела у LM Studio только СПРАШИВАТЬ
список моделей (server.py::_server_models), а менять что-либо приходилось в самом
LM Studio руками: загрузить модель, задать ей контекст, выгрузить. При этом главная
боль проекта (см. llm_client.loaded_context_length) - что модель загружена НЕ с тем
контекстом, что в конфиге, - лечилась только походом к серверу. Теперь админ делает
это из интерфейса.

Проверено на живом сервере, что нативный API это умеет чистым HTTP, без SDK и CLI:
  * GET  /api/v1/models          - список; у модели есть capabilities.reasoning
    (по нему видно «думающую» модель), loaded_instances (id + config.context_length),
    max_context_length, params_string, size_bytes, type (llm/embedding), vision.
  * POST /api/v1/models/load     - {model, ...параметры загрузки}. Сервер СТРОГО
    валидирует ключи и отвергает незнакомые, поэтому шлём только из белого списка
    (LOAD_PARAM_SPEC): context_length, ttl_seconds(>=1), flash_attention,
    eval_batch_size, offload_kv_cache_to_gpu, num_experts. GPU-offload через REST
    задать нельзя - все имена ключей сервер отверг (замер).
  * POST /api/v1/models/unload   - {instance_id}.

Только стандартная библиотека (urllib), как и весь остальной web_app-слой:
ни openai, ни requests сюда не тянем.
"""

import json
import urllib.error
import urllib.request

# Белый список параметров загрузки и их типы. Сервер отвергает любой незнакомый
# ключ с 400 - поэтому фильтруем на нашей стороне, а не надеемся, что «лишнее он
# проигнорирует». Значения приводим к типу; ttl_seconds сервер требует >= 1.
LOAD_PARAM_SPEC = {
    "context_length": int,
    "ttl_seconds": int,
    "flash_attention": bool,
    "eval_batch_size": int,
    "offload_kv_cache_to_gpu": bool,
    "num_experts": int,
}

# Загрузка большой модели идёт долго (сервер держит соединение открытым, пока не
# загрузит), поэтому таймаут щедрый. Список моделей и выгрузка - быстрые.
LOAD_TIMEOUT = 900.0
LIST_TIMEOUT = 8.0
UNLOAD_TIMEOUT = 60.0


class LMStudioError(Exception):
    """Ошибка обращения к LM Studio, пригодная для показа пользователю.

    В сообщении - текст, который вернул сам сервер (у нативного API он внятный:
    «Missing required field 'model'», «Model not found» и т.п.), а не простыня
    трассировки urllib.
    """

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def native_root(base_url: str) -> str:
    """Из base_url вида http://host:1234/v1 делаем http://host:1234 - корень,
    от которого идут нативные эндпоинты /api/v1/... (у них свой префикс, не /v1)."""
    root = str(base_url or "").rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root


def _request(method: str, url: str, api_key=None, body=None, timeout=LIST_TIMEOUT):
    """Один HTTP-запрос к LM Studio. Возвращает разобранный JSON.

    Ошибку сервера (4xx/5xx) поднимает LMStudioError с текстом из тела ответа -
    у нативного API он человекочитаемый и его-то и надо показать.
    """
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key and api_key != "not-needed":
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise LMStudioError(_error_text(e), status=502) from e
    except urllib.error.URLError as e:
        raise LMStudioError(f"сервер ИИ недоступен: {e.reason}", status=502) from e
    except OSError as e:
        raise LMStudioError(f"сервер ИИ недоступен: {e}", status=502) from e
    try:
        return json.loads(raw or b"null")
    except json.JSONDecodeError as e:
        raise LMStudioError(f"сервер вернул не JSON: {e}", status=502) from e


def _error_text(e: "urllib.error.HTTPError") -> str:
    """Достаёт человекочитаемое сообщение из тела ошибки LM Studio."""
    try:
        payload = json.loads(e.read() or b"{}")
    except (json.JSONDecodeError, OSError):
        return f"HTTP {e.code} {e.reason}"
    err = payload.get("error")
    if isinstance(err, dict):
        return err.get("message") or f"HTTP {e.code}"
    if isinstance(err, str):
        return err
    return f"HTTP {e.code} {e.reason}"


def _normalize(m: dict) -> dict:
    """Одна модель из ответа /api/v1/models -> плоская запись для интерфейса.

    reasoning - признак «думающей» модели: у неё в capabilities есть ветка
    reasoning (allowed_options/default). По нему интерфейс решает, показывать ли
    сворачиваемый блок раздумий в чате и ждать ли долгого молчания у зрения.
    """
    caps = m.get("capabilities") or {}
    instances = []
    for inst in m.get("loaded_instances") or []:
        instances.append({
            "id": inst.get("id") or inst.get("instance_id") or m.get("key"),
            "context_length": (inst.get("config") or {}).get("context_length"),
        })
    return {
        "key": m.get("key"),
        "display_name": m.get("display_name") or m.get("key"),
        "type": m.get("type"),                       # llm | embedding
        "params": m.get("params_string"),
        "max_context": m.get("max_context_length"),
        "size_bytes": m.get("size_bytes"),
        "loaded": bool(m.get("loaded_instances")),
        "loaded_instances": instances,
        "vision": bool(caps.get("vision")),
        "tool_use": bool(caps.get("trained_for_tool_use")),
        "reasoning": bool(caps.get("reasoning")),
    }


def list_models(server_cfg: dict, include_non_llm: bool = False) -> list:
    """Модели сервера: имя, статус загрузки, зрение, раздумья, контекст.

    Берём НАТИВНЫЙ эндпоинт (/api/v1/models), а не OpenAI-совместимый /v1/models:
    последний отдаёт голый список имён без статуса загрузки и возможностей модели.

    include_non_llm=False (по умолчанию) отбрасывает эмбеддинги - агентом или
    моделью чата они быть не могут. Админ-панель управления зовёт с True: там
    показать надо всё, что на сервере есть.
    """
    url = native_root(server_cfg["base_url"]) + "/api/v1/models"
    data = _request("GET", url, server_cfg.get("api_key"), timeout=LIST_TIMEOUT)
    out = []
    for m in (data or {}).get("models", []):
        if not include_non_llm and m.get("type") != "llm":
            continue
        out.append(_normalize(m))
    out.sort(key=lambda m: (not m["loaded"], (m["key"] or "").lower()))
    return out


def clean_load_params(params) -> dict:
    """Оставляет из присланных параметров только знакомые серверу, приводит к типу.

    Пустые/None-значения отбрасываем: их отсутствие означает «как решит LM Studio».
    ttl_seconds < 1 сервер отвергает - поднимаем понятную ошибку заранее.
    """
    clean = {}
    for key, caster in LOAD_PARAM_SPEC.items():
        if key not in (params or {}):
            continue
        value = params[key]
        if value in (None, ""):
            continue
        try:
            if caster is bool:
                clean[key] = bool(value)
            elif caster is int:
                clean[key] = int(value)
        except (TypeError, ValueError):
            raise LMStudioError(f"Недопустимое значение параметра {key}: {value!r}", 400)
    if "ttl_seconds" in clean and clean["ttl_seconds"] < 1:
        raise LMStudioError("ttl_seconds должен быть не меньше 1 (или пусто)", 400)
    if "context_length" in clean and clean["context_length"] < 1:
        raise LMStudioError("context_length должен быть положительным", 400)
    return clean


def load_model(server_cfg: dict, model: str, params=None) -> dict:
    """Загрузить модель в память сервера. Возвращает ответ сервера
    ({type, instance_id, load_time_seconds, status})."""
    if not model or not isinstance(model, str):
        raise LMStudioError("Не указана модель для загрузки", 400)
    body = {"model": model, **clean_load_params(params)}
    url = native_root(server_cfg["base_url"]) + "/api/v1/models/load"
    return _request("POST", url, server_cfg.get("api_key"), body=body,
                    timeout=LOAD_TIMEOUT)


def unload_model(server_cfg: dict, instance_id: str) -> dict:
    """Выгрузить модель из памяти по её instance_id (см. loaded_instances)."""
    if not instance_id or not isinstance(instance_id, str):
        raise LMStudioError("Не указан instance_id для выгрузки", 400)
    url = native_root(server_cfg["base_url"]) + "/api/v1/models/unload"
    return _request("POST", url, server_cfg.get("api_key"),
                    body={"instance_id": instance_id}, timeout=UNLOAD_TIMEOUT)
