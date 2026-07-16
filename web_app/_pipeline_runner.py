#!/usr/bin/env python3
"""
Подпроцесс, который реально исполняет пайплайн анализа.

server.py запускает пайплайн НЕ в потоке, а в отдельном процессе - только так
кнопка «Отменить анализ» может оборвать его мгновенно и гарантированно (убив
весь этот процесс и всех его потомков, эквивалент Ctrl+C во всех его окнах
разом), не дожидаясь, пока пайплайн сам заметит запрос где-то на границе
стадии. Сетевой вызов к ИИ или библиотека Open Interpreter кооперативный
сигнал вполне может проигнорировать - принудительное убийство процесса игнорировать
не может.

Аргументы: путь к JSON-файлу с параметрами запуска (см. server.py:_run_analysis).
Пишет строки лога в stdout (их построчно читает и ретранслирует в консоль
браузера родительский процесс) и результат - в файл рядом,
<args>.result.json: {"ok": true, "n_findings": N} либо
{"ok": false, "error": "..."}.
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


def main():
    args_path = Path(sys.argv[1])
    args = json.loads(args_path.read_text(encoding="utf-8"))
    result_path = args_path.with_name(args_path.name + ".result.json")

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")

    result = {"ok": False, "error": None, "n_findings": None}
    try:
        merged = pipeline.run_pipeline(
            config_path=args["config_path"],
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
