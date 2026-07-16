#!/usr/bin/env python3
"""
Оркестратор всего пайплайна.

CLI:
    python main.py                    # весь пайплайн: извлечение -> агенты -> merged_report.json
    python main.py --extract-only     # только базовые парсеры (LLM не нужны) - для отладки
    python main.py --skip-extract     # только агенты, на уже извлечённых данных в data/
    python main.py --input ./data --config config.yaml

Как библиотека (позже - из бэкенда сайта):
    from main import run_pipeline
    merged = run_pipeline()                                  # -> dict со списком ошибок
    merged = run_pipeline(doc_types={"файл.pdf": "scheme"})  # тип пришёл из формы загрузки

Пайплайн:
0) ИЗВЛЕЧЕНИЕ (ingest.py): PDF из data/base_files -> базовый скрипт-парсер по
   пометке типа в имени файла -> data/<имя документа>/*.json + data/manifest.json
1) Агент 1 (llm_servers.agent_1, через Open Interpreter) анализирует data/ -> report_1.json
2) Агент 2 (llm_servers.agent_2, через Open Interpreter) анализирует data/ -> report_2.json
3) Модель-сшиватель (llm_servers[merger.use_agent]) объединяет report_1 + report_2,
   убирает дубли и заранее известные ошибки (known_errors.json) -> merged_report.json
4) Каждый JSON-результат проходит валидацию по схеме (schema.py) с
   автоматическим циклом исправления. Если модель так и не выдала
   валидный JSON - поднимается validation.JSONValidationError.
"""

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

import yaml

from ingest import ExtractionError, run_extraction
from llm_check import check_server_alive, run_checks
from oi_agent import run_analysis_agent
from merge_reports import merge_reports
from schema import SEVERITY_ENUM
from validation import JSONValidationError

logger = logging.getLogger("error_analyzer")

PROJECT_ROOT = Path(__file__).resolve().parent

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(p) -> Path:
    """Пути из config.yaml - относительно корня проекта, а не cwd:
    пайплайн должен работать одинаково, откуда бы его ни запустили
    (в т.ч. из бэкенда сайта с произвольной рабочей директорией)."""
    path = Path(p)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def load_known_errors(path) -> list:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("errors", data) if isinstance(data, dict) else data


def resolve_merger_cfg(cfg: dict) -> dict:
    merger_key = cfg["llm_servers"]["merger"]["use_agent"]
    return cfg["llm_servers"][merger_key]


def _format_ref(ref: dict) -> str:
    """Одно место находки в человекочитаемом виде - примерно те же поля,
    что станут колонками таблицы на сайте."""
    where = []
    if ref.get("sheet") is not None:
        where.append(f"лист {ref['sheet']}")
    if ref.get("row") is not None:
        where.append(f"строка {ref['row']}")

    what = []
    if ref.get("cabinet"):
        what.append(f"шкаф {ref['cabinet']}")
    if ref.get("terminal_block") or ref.get("pin"):
        what.append(f"клемма {ref.get('terminal_block') or '?'}/{ref.get('pin') or '?'}")
    if ref.get("marking"):
        what.append(f"маркировка {ref['marking']}")
    if ref.get("kks"):
        what.append(f"KKS {ref['kks']}")
    if ref.get("conductor"):
        what.append(f"проводник {ref['conductor']}")

    head = f"[{ref.get('doc_type')}] {ref.get('document')}"
    if where:
        head += " (" + ", ".join(where) + ")"

    out = [f"      {head}"]
    if what:
        out.append(f"        {'; '.join(what)}")
    if ref.get("found"):
        out.append(f"        найдено: {ref['found']}")
    return "\n".join(out)


def format_text_report(merged: dict) -> str:
    errors = merged.get("errors", [])
    lines = []
    summary = merged.get("summary")
    if summary:
        lines.append(f"Резюме: {summary}")
        lines.append("")

    by_kind = Counter(e.get("kind") for e in errors)
    by_severity = Counter(e.get("severity") for e in errors)
    lines.append(f"Найдено замечаний: {len(errors)}")
    if errors:
        lines.append("  по видам: " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
        lines.append("  по важности: " + ", ".join(
            f"{s}={by_severity[s]}" for s in SEVERITY_ENUM if by_severity[s]))
    lines.append("=" * 70)

    for i, err in enumerate(errors, 1):
        lines.append(f"[{i}] {err.get('kind')} | {err.get('severity')} | {err.get('type')}")
        lines.append(f"    ({err.get('scope')})")
        for ref in err.get("refs", []):
            lines.append(_format_ref(ref))
        lines.append(f"    что найдено: {err.get('finding')}")
        lines.append(f"    что делать:  {err.get('action')}")
        if err.get("evidence"):
            lines.append(f"    подтверждение: {err.get('evidence')}")
        lines.append("-" * 70)
    return "\n".join(lines)


def format_extraction_report(manifest: dict) -> str:
    lines = ["Извлечение данных из PDF:", "=" * 60]
    for doc in manifest["documents"]:
        mark = {"ok": "OK  ", "partial": "ЧАСТЬ", "failed": "FAIL"}[doc["status"]]
        lines.append(f"[{mark}] ({doc['doc_type']}) {doc['name']}")
        lines.append(f"    парсеры: {', '.join(doc['parsers'])}")
        lines.append(f"    данные: {doc['data_dir']}/")
        if doc["files"]:
            lines.append(f"    файлы: {', '.join(doc['files'])}")
            for k, v in doc["stats"].items():
                lines.append(f"      {k}: {v}")
        for err in doc["errors"]:
            lines.append(f"    ОШИБКА: {err}")
        lines.append("-" * 60)
    for sk in manifest["skipped_files"]:
        lines.append(f"[SKIP] {sk['source_file']}")
        lines.append(f"    причина: {sk['reason']}")
        lines.append("-" * 60)
    s = manifest["summary"]
    lines.append(f"Документов: {s['total_documents']}, извлечено: {s['extracted_ok']}, "
                 f"ошибок: {s['failed']}, пропущено: {s['skipped']}")
    return "\n".join(lines)


def run_extraction_stage(cfg: dict, doc_types: dict = None) -> dict:
    """Стадия 0: PDF -> data/<имя документа>/*.json + data/manifest.json."""
    paths = cfg["paths"]
    return run_extraction(
        base_files_dir=resolve_path(paths["base_files_dir"]),
        scripts_dir=resolve_path(paths["scripts_dir"]),
        data_dir=resolve_path(paths["input_dir"]),
        overrides=doc_types,
        overwrite=not cfg.get("extraction", {}).get("reuse_existing", False),
    )


def _finding_signature(finding: dict) -> tuple:
    """Подпись находки для дедупликации: вид + множество физических точек, к которым
    она относится (документ + клеммник + штифт + KKS). По ней детерминированные
    находки чекера сопоставляются с находками агентов, чтобы не задваивать."""
    points = frozenset(
        (r.get("document"), r.get("terminal_block"), r.get("pin"), r.get("kks"))
        for r in finding.get("refs", [])
    )
    return (finding.get("kind"), points)


def run_rules_stage(cfg: dict, data_dir: Path) -> list:
    """Стадия правил: детерминированный чекер по таблицам подключений (нетлистам).

    Читает manifest.json, находит документы типа netlist и прогоняет по их
    connections.json правила из netlist_rules. Возвращает список находок в том же
    формате schema.REPORT_SCHEMA, что и у агентов. LLM здесь не участвует.
    """
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        logger.warning("manifest.json не найден в %s - стадия правил пропущена", data_dir)
        return []

    scripts_dir = resolve_path(cfg["paths"]["scripts_dir"])
    rules = _load_parser_module(scripts_dir, "netlist_rules.py")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings = []
    for doc in manifest.get("documents", []):
        if doc.get("doc_type") != "netlist":
            continue
        conn_path = PROJECT_ROOT / doc["data_dir"] / "connections.json"
        if not conn_path.exists():
            continue
        doc_findings = rules.check_connections_file(doc["name"], str(conn_path))
        logger.info("  правила по %s: %d находок", doc["name"], len(doc_findings))
        findings.extend(doc_findings)
    return findings


def _load_parser_module(scripts_dir: Path, script_name: str):
    """Импорт скрипта из data/base_analysis_scripts по пути к файлу (как в ingest)."""
    import importlib.util
    import sys as _sys
    path = scripts_dir / script_name
    mod_name = f"_stage_{path.stem}"
    if mod_name in _sys.modules:
        return _sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    _sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def combine_rule_and_agent_findings(rule_findings: list, merged: dict) -> dict:
    """Соединяет находки чекера и итог агентов. Находки чекера - ground truth: они
    ВСЕГДА в итоге. Из находок агентов выбрасываем те, что дублируют находку чекера
    (по подписи), чтобы одно и то же не выводилось дважды. Результат сортируем по
    важности."""
    rule_sigs = {_finding_signature(f) for f in rule_findings}
    agent_errors = [e for e in merged.get("errors", [])
                    if _finding_signature(e) not in rule_sigs]
    dropped = len(merged.get("errors", [])) - len(agent_errors)
    if dropped:
        logger.info("Из находок агентов убрано %d дублей находок чекера", dropped)

    combined = rule_findings + agent_errors
    combined.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))
    merged["errors"] = combined
    return merged


class LLMUnavailableError(RuntimeError):
    """Сервер ИИ недоступен - полный анализ (с агентами) невозможен."""


def preflight_llm(cfg: dict) -> None:
    """Быстрая проверка доступности серверов ИИ ПЕРЕД запуском агентов.

    Нужна, чтобы при недоступном сервере анализ падал сразу с понятной ошибкой
    в консоль, а не висел минутами и не выдавал криптическую ошибку соединения
    litellm где-то в середине. Проверяются серверы обоих агентов из config.
    """
    checked = {}
    for key in ("agent_1", "agent_2"):
        scfg = cfg["llm_servers"][key]
        base = scfg["base_url"]
        if base in checked:
            ok = checked[base]
        else:
            ok = check_server_alive(scfg)["ok"]
            checked[base] = ok
        if not ok:
            raise LLMUnavailableError(
                f"Сервер ИИ недоступен: {base} (модель {scfg['model']}). "
                f"Проверьте, запущен ли LM Studio, кнопкой «Проверить серверы и модели ИИ». "
                f"Для анализа без ИИ выберите режим «Без ИИ — только скрипты».")
    logger.info("Серверы ИИ доступны, запускаю агентов")


def clear_previous_results(cfg: dict) -> None:
    """Стирает результаты прошлого анализа перед новым прогоном: папку output
    целиком и извлечённые данные в data/ (папки документов + manifest.json).

    НЕ трогает base_files (исходные файлы пользователя), base_analysis_scripts
    (скрипты-парсеры) и your_helping_scripts_and_files - это не результаты, а вход
    и инструментарий. Их имена берём из config.paths, чтобы не удалить лишнего.
    """
    import shutil

    out_dir = resolve_path(cfg["paths"]["output_dir"])
    if out_dir.exists():
        for item in out_dir.iterdir():
            item.unlink() if item.is_file() else shutil.rmtree(item)
    out_dir.mkdir(parents=True, exist_ok=True)

    data_dir = resolve_path(cfg["paths"]["input_dir"])
    keep = {
        resolve_path(cfg["paths"]["base_files_dir"]).name,
        resolve_path(cfg["paths"]["scripts_dir"]).name,
        resolve_path(cfg["paths"]["helper_scripts_dir"]).name,
    }
    if data_dir.exists():
        for item in data_dir.iterdir():
            if item.name in keep:
                continue
            item.unlink() if item.is_file() else shutil.rmtree(item)

    # рабочую папку агента чистим ОТ содержимого, но саму папку оставляем
    helper = resolve_path(cfg["paths"]["helper_scripts_dir"])
    if helper.exists():
        for item in helper.iterdir():
            item.unlink() if item.is_file() else shutil.rmtree(item)
    logger.info("Результаты прошлого анализа очищены (output/ и data/<документы>/)")


def run_pipeline(input_dir: str = None, known_errors_path: str = None,
                 output_dir: str = None, config_path: str = None,
                 doc_types: dict = None, skip_extract: bool = False,
                 skip_agents: bool = False, clear_previous: bool = False) -> dict:
    """Главная точка входа для использования из другого скрипта (без CLI).

    doc_types: {"имя файла.pdf": "scheme"|"netlist"} - явная пометка типа
        документа (приходит из формы загрузки на сайте). Если не передано,
        тип берётся из пометки в начале имени файла: "(scheme)...", "(netlist)...".
    skip_extract: не перезапускать базовые парсеры, работать по тому,
        что уже лежит в data/.
    skip_agents: НЕ запускать LLM-агентов и мерджер - отчёт собирается только из
        находок детерминированного чекера (режим "без ИИ, только скрипты").
    clear_previous: стереть результаты прошлого анализа перед запуском.

    Возвращает итоговый merged-отчёт (dict с ключами 'errors' и 'summary').
    Бросает ingest.ExtractionError, если не удалось извлечь ни один документ,
    и validation.JSONValidationError, если одна из моделей так и не смогла
    выдать валидный JSON - вызывающий код сам решает, что делать (retry,
    алерт на сервер и т.д.).
    """
    cfg = load_config(config_path or str(PROJECT_ROOT / "config.yaml"))

    if clear_previous:
        clear_previous_results(cfg)

    data_dir = resolve_path(input_dir or cfg["paths"]["input_dir"])
    known_errors_path = resolve_path(known_errors_path or cfg["paths"]["known_errors_file"])
    out_dir = resolve_path(output_dir or cfg["paths"]["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    if skip_extract:
        logger.info("Стадия извлечения пропущена (--skip-extract), "
                    "анализируем то, что уже лежит в %s", data_dir)
    else:
        logger.info("Стадия 0: извлечение данных из PDF базовыми скриптами-парсерами")
        manifest = run_extraction_stage(cfg, doc_types)
        logger.info("Извлечено документов: %d из %d",
                    manifest["summary"]["extracted_ok"],
                    manifest["summary"]["total_documents"])

    known_errors = load_known_errors(known_errors_path)
    logger.info("Загружено %d заранее известных ошибок из %s",
                len(known_errors), known_errors_path)

    logger.info("Стадия правил: детерминированный чекер таблиц подключений")
    rule_findings = run_rules_stage(cfg, data_dir)
    logger.info("Чекер нашёл %d находок (до запуска нейросетей)", len(rule_findings))

    if skip_agents:
        logger.info("Режим без ИИ: агенты и мерджер пропущены, отчёт только из находок чекера")
        merged = {
            "errors": sorted(rule_findings,
                             key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9)),
            "summary": (f"Анализ без ИИ (только скрипты): найдено {len(rule_findings)} "
                        f"замечаний детерминированным чекером таблиц подключений."),
        }
        merged_path = out_dir / "merged_report.json"
        merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        logger.info("Итоговый отчёт сохранён: %s", merged_path)
        return merged

    # Перед запуском агентов проверяем, что серверы ИИ вообще доступны -
    # иначе сразу понятная ошибка в консоль, а не зависание на минуты.
    preflight_llm(cfg)

    agent_cfg = cfg["agent"]
    max_repair = agent_cfg["max_json_repair_attempts"]
    limits = {
        "max_code_turns": agent_cfg.get("max_code_turns", 25),
        "timeout_seconds": agent_cfg.get("timeout_seconds", 1200),
    }
    helper_dir = resolve_path(cfg["paths"]["helper_scripts_dir"])
    helper_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Запуск агента анализа №1 (%s)", cfg["llm_servers"]["agent_1"]["model"])
    report_1 = run_analysis_agent(cfg["llm_servers"]["agent_1"], str(data_dir),
                                  helper_dir=str(helper_dir),
                                  max_json_repair_attempts=max_repair, **limits)

    logger.info("Запуск агента анализа №2 (%s)", cfg["llm_servers"]["agent_2"]["model"])
    report_2 = run_analysis_agent(cfg["llm_servers"]["agent_2"], str(data_dir),
                                  helper_dir=str(helper_dir),
                                  max_json_repair_attempts=max_repair, **limits)

    if cfg.get("logging", {}).get("save_raw_agent_json", True):
        (out_dir / "report_1.json").write_text(
            json.dumps(report_1, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "report_2.json").write_text(
            json.dumps(report_2, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info("Слияние отчётов моделью-сшивателем")
    merger_cfg = resolve_merger_cfg(cfg)
    merged = merge_reports(merger_cfg, report_1, report_2, known_errors,
                           max_json_repair_attempts=max_repair)

    # Находки чекера добавляем ПОСЛЕ LLM-слияния и детерминированно: они ground truth
    # и не должны потеряться на слиянии (слабая модель-сшиватель уже роняла находки).
    merged = combine_rule_and_agent_findings(rule_findings, merged)
    logger.info("Итоговых находок: %d (из них от чекера: %d)",
                len(merged["errors"]), len(rule_findings))

    merged_path = out_dir / "merged_report.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Итоговый отчёт сохранён: %s", merged_path)

    return merged


def main():
    parser = argparse.ArgumentParser(
        description="Анализ EPLAN-схем и таблиц соединений на ошибки через две LLM")
    parser.add_argument("--input", default=None,
                        help="папка-песочница с извлечёнными данными (по умолчанию ./data)")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--known-errors", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--extract-only", action="store_true",
                        help="только базовые парсеры PDF, без запуска LLM-агентов")
    parser.add_argument("--rules-only", action="store_true",
                        help="только детерминированный чекер таблиц подключений, без LLM")
    parser.add_argument("--skip-extract", action="store_true",
                        help="не перезапускать парсеры, анализировать готовые данные в data/")
    parser.add_argument("--check-llm", action="store_true",
                        help="проверить ТОЛЬКО нейросети (сервер, модель, формат JSON) - "
                             "без PDF, парсеров и папки data")
    parser.add_argument("--quick", action="store_true",
                        help="с --check-llm: пропустить проверку формата JSON (только пинг моделей)")
    args = parser.parse_args()

    logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")

    if args.check_llm:
        ok = run_checks(load_config(args.config), skip_contract=args.quick)
        sys.exit(0 if ok else 1)

    if args.extract_only:
        try:
            manifest = run_extraction_stage(load_config(args.config))
        except (ExtractionError, FileNotFoundError) as e:
            logger.error("Извлечение не удалось: %s", e)
            sys.exit(1)
        print(format_extraction_report(manifest))
        return

    if args.rules_only:
        cfg = load_config(args.config)
        data_dir = resolve_path(args.input or cfg["paths"]["input_dir"])
        findings = run_rules_stage(cfg, data_dir)
        print(format_text_report({"errors": sorted(
            findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9)),
            "summary": f"Детерминированный чекер: найдено {len(findings)} замечаний."}))
        return

    try:
        merged = run_pipeline(args.input, args.known_errors, args.output_dir,
                              args.config, skip_extract=args.skip_extract)
    except (ExtractionError, FileNotFoundError) as e:
        logger.error("Пайплайн остановлен на стадии извлечения: %s", e)
        sys.exit(1)
    except LLMUnavailableError as e:
        logger.error("%s", e)
        sys.exit(1)
    except JSONValidationError as e:
        logger.error("Пайплайн остановлен: модель не смогла выдать валидный JSON: %s", e)
        sys.exit(1)

    print(format_text_report(merged))


if __name__ == "__main__":
    main()
