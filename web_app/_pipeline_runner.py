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
ANALYZER_DIR = HERE.parent / "analyzer_to_errors"
sys.path.insert(0, str(ANALYZER_DIR))

import main as pipeline              # noqa: E402
from ingest import ExtractionError    # noqa: E402
from validation import JSONValidationError  # noqa: E402


def build_session_config(base_config_path, paths: dict, out_path) -> str:
    """Пишет config.yaml сессии и возвращает путь к нему.

    paths - абсолютные пути этой сессии (SessionStore.paths_of): base_files_dir,
    scripts_dir, helper_scripts_dir, input_dir (=data), output_dir. Всё
    остальное - серверы ИИ, лимиты агента, known_errors - берётся из базового
    конфига без изменений: known_errors.json общий для всех сессий сознательно,
    это накопленное знание, а не результат прогона.
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

    out_path = Path(out_path)
    out_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(out_path)


def main():
    args_path = Path(sys.argv[1])
    args = json.loads(args_path.read_text(encoding="utf-8"))
    result_path = args_path.with_name(args_path.name + ".result.json")

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    result = {"ok": False, "error": None, "n_findings": None}
    try:
        config_path = build_session_config(
            args["base_config_path"], args["paths"], args["session_config_path"])

        merged = pipeline.run_pipeline(
            config_path=config_path,
            doc_types=args.get("doc_types") or None,
            skip_agents=bool(args.get("skip_agents")),
            clear_previous=bool(args.get("clear_previous")),
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
