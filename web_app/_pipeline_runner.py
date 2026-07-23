#!/usr/bin/env python3
"""
Подпроцесс, который реально исполняет пайплайн анализа одной сессии.

server.py запускает пайплайн НЕ в потоке, а в отдельном процессе - только так
кнопка «Отменить анализ» может оборвать его мгновенно и гарантированно (убив
весь этот процесс и всех его потомков, эквивалент Ctrl+C во всех его окнах
разом), не дожидаясь, пока пайплайн сам заметит запрос где-то на границе
стадии. Сетевой вызов к ИИ или библиотека Open Interpreter кооперативный
сигнал вполне может проигнорировать - принудительное убийство процесса игнорировать
не может.

Здесь же собирается КОНФИГ СЕССИИ: копия базового config.yaml, в котором все
пути из раздела paths заменены на абсолютные пути папки сессии. Так пайплайн
остаётся нетронутым (run_pipeline и так резолвит абсолютные пути как есть, см.
main.resolve_path), а изоляция сессий друг от друга достигается одними лишь
путями: clear_previous_results чистит output/ и данные ЭТОЙ сессии, а не общие.
Сборка конфига живёт именно здесь, а не в server.py, потому что требует pyyaml -
а web_app по замыслу работает на одной стандартной библиотеке.

ОЧЕРЕДЬ К ИИ ЖИВЁТ ЗДЕСЬ, А НЕ ВОКРУГ ВСЕГО ПРОГОНА. Скриптовая стадия
(извлечение, чекеры, сверка связок) считает локальный процессор, и две сессии
друг другу не мешают - их незачем выстраивать в очередь вовсе. А вот к LM Studio
пропускается строго одна: сервер ИИ на всех один. Поэтому перед стадией агентов
пайплайн зовёт llm_gate (см. main.run_pipeline), а здесь этот gate реализован
самым простым надёжным способом: процесс печатает в stdout маркер LLM_WAIT_MARKER
и ЗАМИРАЕТ на чтении своего stdin. Родитель, который и так читает наш stdout
построчно, видит маркер, дожидается освобождения слота и пишет нам строку в stdin
- после чего мы идём дальше.

Почему именно так, а не через файл-семафор или сокет: канал между этими двумя
процессами уже есть и уже читается построчно, а блокировка на read() снимается
сама собой, если родитель нас убьёт (отмена) - никакого состояния на диске,
которое пришлось бы подчищать после падения сервера.

Аргументы: путь к JSON-файлу с параметрами запуска (см. queue_worker.py).
Пишет строки лога в stdout (их построчно читает и ретранслирует родительский
процесс) и результат - в файл рядом, <args>.result.json:
{"ok": true, "n_findings": N} либо {"ok": false, "error": "..."}.
"""

import json
import logging
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from paths import ANALYZER_DIR  # noqa: E402
sys.path.insert(0, str(ANALYZER_DIR))

import main as pipeline              # noqa: E402
from ingest import ExtractionError    # noqa: E402
from validation import JSONValidationError  # noqa: E402

# Маркер "скрипты отработали, жду очереди к серверу ИИ". Родитель ловит эту
# строку в нашем stdout и отвечает строкой в наш stdin, когда слот свободен.
LLM_WAIT_MARKER = "@@LLM_WAIT"


def wait_for_llm_slot():
    """llm_gate для run_pipeline: отпроситься у родителя на стадию агентов.

    Если stdin закрыт (запустили раннер руками из консоли, без веб-интерфейса) -
    идём дальше не спрашивая: очередь есть только там, где есть кому её вести.
    """
    print(LLM_WAIT_MARKER, flush=True)
    try:
        if sys.stdin is None or sys.stdin.readline() == "":
            return
    except (OSError, ValueError):
        return


def build_session_config(base_config_path, paths: dict, out_path, llm=None) -> str:
    """Пишет config.yaml сессии и возвращает путь к нему.

    paths - абсолютные пути этой сессии (SessionStore.paths_of): base_files_dir,
    scripts_dir, helper_scripts_dir, input_dir (=data), output_dir. Всё
    остальное - серверы ИИ, лимиты агента, known_errors - берётся из базового
    конфига без изменений: known_errors.json общий для всех сессий сознательно,
    это накопленное знание, а не результат прогона.

    llm - выбор моделей и числа агентов для ЭТОЙ сессии (SessionStore.set_llm):
    {"agent_1": имя модели, "agent_2": ..., "vision": ..., "agents_count": 1|2,
    "single_agent": "agent_1"|"agent_2"}. Переопределяется ТОЛЬКО названное:
    адрес сервера, лимиты, температура и context_window продолжают браться из
    общего конфига - там они выверены, и подменять их из интерфейса незачем.

    Записанный конфиг остаётся в папке сессии навсегда, поэтому по нему всегда
    видно, КАКИМИ моделями считался этот отчёт. Ради этого настройка и живёт в
    сессии, а не в общем config.yaml.
    """
    import yaml

    cfg = pipeline.load_config(base_config_path)
    cfg["paths"] = dict(cfg["paths"])
    cfg["paths"].update({
        "base_files_dir": paths["base_files_dir"],
        "full_projects_dir": paths["full_projects_dir"],
        "scripts_dir": paths["scripts_dir"],
        "helper_scripts_dir": paths["helper_scripts_dir"],
        "input_dir": paths["input_dir"],
        "output_dir": paths["output_dir"],
        # known_errors_file остаётся общим - приводим к абсолютному пути,
        # иначе он резолвился бы относительно корня пайплайна (это верно и
        # сейчас, но в конфиге сессии лучше не оставлять относительных путей)
        "known_errors_file": str(pipeline.resolve_path(cfg["paths"]["known_errors_file"])),
    })

    for key in ("agent_1", "agent_2", "vision"):
        model = (llm or {}).get(key)
        if model:
            cfg["llm_servers"][key] = dict(cfg["llm_servers"][key], model=model)

    if (llm or {}).get("agents_count"):
        cfg["agents"] = dict(cfg["agents"], count=int(llm["agents_count"]))
    if (llm or {}).get("single_agent"):
        cfg["agents"] = dict(cfg["agents"], single_agent=llm["single_agent"])

    out_path = Path(out_path)
    out_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(out_path)


def main():
    # На случай ручного запуска раннера из консоли для отладки: без этого его
    # русские логи - каша. Как подпроцесс сервера он и так получает stdout в
    # UTF-8 (родитель ставит PYTHONIOENCODING), и повторная настройка безвредна.
    from paths import setup_console_utf8
    setup_console_utf8()

    args_path = Path(sys.argv[1])
    args = json.loads(args_path.read_text(encoding="utf-8"))
    result_path = args_path.with_name(args_path.name + ".result.json")

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    result = {"ok": False, "error": None, "n_findings": None}
    try:
        config_path = build_session_config(
            args["base_config_path"], args["paths"], args["session_config_path"],
            llm=args.get("llm"))

        skip_agents = bool(args.get("skip_agents"))
        visual = bool(args.get("visual"))
        merged = pipeline.run_pipeline(
            config_path=config_path,
            doc_types=args.get("doc_types") or None,
            skip_agents=skip_agents,
            visual=visual,
            clear_previous=bool(args.get("clear_previous")),
            # Очередь нужна, если прогон вообще пойдёт к серверу ИИ. Зрению он
            # нужен ровно так же, как агентам, и сервер тот же самый - поэтому
            # режим "зрение без агентов" в очереди стоит, а "только скрипты"
            # не стоит: до модели дело не дойдёт.
            llm_gate=None if (skip_agents and not visual) else wait_for_llm_slot,
        )
        result["ok"] = True
        result["n_findings"] = len(merged.get("errors", []))
    except pipeline.LLMUnavailableError as e:
        result["error"] = str(e)
    except ExtractionError as e:
        result["error"] = "Ошибка извлечения: " + str(e)
    except JSONValidationError as e:
        result["error"] = "Модель не смогла выдать валидный JSON: " + str(e)
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
        print(traceback.format_exc(), flush=True)
    finally:
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
