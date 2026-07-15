"""
Проверка ТОЛЬКО нейросетей - без PDF, без парсеров, без папки data.
Отладочный режим: `python main.py --check-llm`.

Три проверки по каждому серверу из config.yaml (agent_1, agent_2 и модель-сшиватель):

1. СЕРВЕР ЖИВ      - GET {base_url}/models. Заодно печатает список моделей, которые
                     сервер реально отдаёт: опечатка в имени модели видна сразу.
2. МОДЕЛЬ ОТВЕЧАЕТ - крошечный chat-запрос. Здесь всплывают проблемы уровня сервера:
                     модель не загружается (не хватает памяти под вторую большую
                     модель), неверный api_key, недоступный порт.
3. ФОРМАТ JSON     - модели даётся МАЛЕНЬКИЙ фрагмент данных прямо в промпте (никаких
                     файлов) и просится вернуть отчёт по REPORT_SCHEMA. Ответ гоняется
                     через тот же validation.get_validated_json, что и в бою. Так видно
                     главное: способна ли модель вообще выдать валидный по схеме JSON,
                     ДО того как вы потратите час на анализ 87-листовой схемы.

Здесь намеренно НЕ используется Open Interpreter: он добавляет исполнение кода и свой
системный промпт. Задача этого модуля - проверить связку "сервер + модель + схема",
а не поведение агента.
"""

import json
import logging
import time
import urllib.error
import urllib.request

from llm_client import make_simple_ask_fn
from oi_agent import MAX_TOKENS_SHARE_OF_CONTEXT
from schema import EXAMPLE_ERRORS, REPORT_SCHEMA
from validation import JSONValidationError, get_validated_json

logger = logging.getLogger(__name__)

# Крошечный «документ» прямо в промпте: две строки таблицы подключений с очевидным
# дублем физического адреса. Правильный ответ - одна находка kind=DUPLICATE.
FAKE_DATA = {
    "document": "TEST_DOC",
    "connections": [
        {"id": 1, "page": 1, "cabinet": "00CJF02", "terminal_block": "XT01", "pin": "5",
         "kks": "00ABC01CT001", "conductor": "L+", "terminal_address": "00CJF02.XT01.5"},
        {"id": 2, "page": 1, "cabinet": "00CJF02", "terminal_block": "XT01", "pin": "5",
         "kks": "00ABC01CT002", "conductor": "L+", "terminal_address": "00CJF02.XT01.5"},
    ],
}

CONTRACT_PROMPT = """Ниже - крошечный фрагмент таблицы подключений (документ "TEST_DOC",
doc_type="netlist"). Найди в нём ошибку и верни отчёт.

Данные:
{data}

Верни результат СТРОГО в виде JSON-объекта по этой JSON-Schema, внутри блока
```json ... ```, без текста после него:

{schema}

Пример правильно заполненной находки:
{example}
"""


def check_server_alive(server_cfg: dict, timeout: float = 10.0) -> dict:
    """GET {base_url}/models - сервер поднят и какие модели отдаёт."""
    url = server_cfg["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(url)
    if server_cfg.get("api_key") and server_cfg["api_key"] != "not-needed":
        req.add_header("Authorization", f"Bearer {server_cfg['api_key']}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        models = [m.get("id") for m in data.get("data", [])]
        return {"ok": True, "models": models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "models": []}


def check_model_responds(server_cfg: dict) -> dict:
    """Минимальный chat-запрос: модель реально грузится и отвечает."""
    ask = make_simple_ask_fn({**server_cfg, "max_tokens": 32})
    t0 = time.time()
    try:
        text = ask("Ответь одним словом: ок")
        return {"ok": True, "seconds": round(time.time() - t0, 1),
                "reply": (text or "").strip()[:60]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.time() - t0, 1),
                "error": _short_error(e)}


def check_json_contract(server_cfg: dict, max_attempts: int = 3) -> dict:
    """Способна ли модель выдать валидный по REPORT_SCHEMA JSON на маленькой задаче."""
    prompt = CONTRACT_PROMPT.format(
        data=json.dumps(FAKE_DATA, ensure_ascii=False, indent=2),
        schema=json.dumps(REPORT_SCHEMA, ensure_ascii=False, indent=2),
        example=json.dumps({"errors": [EXAMPLE_ERRORS[1]]}, ensure_ascii=False, indent=2),
    )
    ask = make_simple_ask_fn(server_cfg)
    t0 = time.time()
    try:
        report = get_validated_json(ask, prompt, REPORT_SCHEMA, max_attempts=max_attempts)
    except JSONValidationError as e:
        return {"ok": False, "seconds": round(time.time() - t0, 1), "error": str(e)[:300]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.time() - t0, 1), "error": _short_error(e)}

    errors = report.get("errors", [])
    return {
        "ok": True,
        "seconds": round(time.time() - t0, 1),
        "n_errors": len(errors),
        "kinds": [e.get("kind") for e in errors],
        "found_duplicate": any(e.get("kind") == "DUPLICATE" for e in errors),
    }


def _short_error(e: Exception) -> str:
    """Из простыни litellm/openai вытащить главное - сообщение сервера."""
    msg = str(e)
    for marker in ("Failed to load model", "message\":", "Connection error"):
        i = msg.find(marker)
        if i != -1:
            return msg[i:i + 220]
    return f"{type(e).__name__}: {msg[:220]}"


def run_checks(cfg: dict, skip_contract: bool = False) -> bool:
    """Прогоняет проверки по всем моделям из config.yaml.
    Возвращает True, если всё, что нужно пайплайну, работает."""
    merger_key = cfg["llm_servers"]["merger"]["use_agent"]
    targets = [
        ("agent_1  (анализ)", cfg["llm_servers"]["agent_1"]),
        ("agent_2  (анализ)", cfg["llm_servers"]["agent_2"]),
    ]
    if merger_key in ("agent_1", "agent_2"):
        print(f"\nМодель-сшиватель: merger.use_agent = {merger_key} "
              f"(переиспользуется, отдельно не проверяется)")

    all_ok = True
    for label, server_cfg in targets:
        print("\n" + "=" * 70)
        print(f"{label}: {server_cfg['model']}")
        print(f"  base_url: {server_cfg['base_url']}")
        print("=" * 70)

        ctx = server_cfg.get("context_window", 32000)
        mt = server_cfg.get("max_tokens", 4096)
        if mt > ctx * MAX_TOKENS_SHARE_OF_CONTEXT:
            print(f"  [!]    max_tokens={mt} слишком велик при context_window={ctx}: "
                  "не остаётся места под историю диалога.")
            print("         Open Interpreter обрежет промпт целиком, и LM Studio ответит "
                  "'No user query found in messages'.")
            print(f"         Пайплайн сам понизит max_tokens до "
                  f"{int(ctx * MAX_TOKENS_SHARE_OF_CONTEXT)}, но лучше поправить config.yaml: "
                  "max_tokens - это лимит на ДЛИНУ ОТВЕТА, а не размер контекста.")

        alive = check_server_alive(server_cfg)
        if not alive["ok"]:
            print(f"  [FAIL] сервер недоступен: {alive['error']}")
            print("         -> проверьте, запущен ли LM Studio и верен ли base_url/порт")
            all_ok = False
            continue
        print(f"  [OK]   сервер отвечает, моделей на сервере: {len(alive['models'])}")

        if server_cfg["model"] not in alive["models"]:
            print(f"  [FAIL] модели '{server_cfg['model']}' НЕТ в списке сервера.")
            print("         Сервер отдаёт: " + ", ".join(alive["models"]))
            all_ok = False
            continue
        print("  [OK]   модель есть в списке сервера")

        resp = check_model_responds(server_cfg)
        if not resp["ok"]:
            print(f"  [FAIL] модель не отвечает ({resp['seconds']} c): {resp['error']}")
            print("         -> модель числится на сервере, но не загружается/не работает.")
            print("            Частая причина: не хватает памяти под вторую большую модель.")
            all_ok = False
            continue
        print(f"  [OK]   модель отвечает за {resp['seconds']} c: {resp['reply']!r}")

        if skip_contract:
            continue

        contract = check_json_contract(
            server_cfg, cfg.get("agent", {}).get("max_json_repair_attempts", 3))
        if not contract["ok"]:
            print(f"  [FAIL] модель не смогла выдать валидный по схеме JSON "
                  f"({contract['seconds']} c):")
            print(f"         {contract['error']}")
            all_ok = False
            continue
        print(f"  [OK]   валидный по схеме JSON получен за {contract['seconds']} c: "
              f"находок {contract['n_errors']} {contract['kinds']}")
        if not contract["found_duplicate"]:
            print("  [!]    но ожидаемый дубль (kind=DUPLICATE) модель не нашла - "
                  "формат осилила, качество анализа под вопросом")

    print("\n" + "=" * 70)
    print("ИТОГ: всё готово к запуску пайплайна" if all_ok
          else "ИТОГ: есть проблемы - пайплайн упадёт на отмеченных [FAIL] шагах")
    print("=" * 70)
    return all_ok
