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

ГДЕ ЧТО ЛЕЖИТ. Здесь остался ПОРЯДОК стадий и всё, что связано с агентами;
сами стадии вынесены, потому что занимали больше половины файла и заслоняли
run_pipeline - то единственное, ради чего main.py зовут снаружи:

    settings.py     - пути, config.yaml (+ config.local.yaml), resolve_path
    stages.py       - извлечение, правила по документам, сверка связок
    text_report.py  - текстовый отчёт для консоли
    known_filter.py - гашение заранее известных ошибок

Имена стадий и настроек ПЕРЕЭКСПОРТИРУЮТСЯ отсюда: main - фасад пайплайна, и
снаружи его зовут как pipeline.load_config / pipeline.run_rules_stage.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import known_filter
from ingest import ExtractionError
from llm_check import check_server_alive, run_checks
from oi_agent import run_analysis_agent
from merge_reports import merge_reports
from text_report import format_extraction_report, format_text_report
from validation import JSONValidationError

# Пути и конфиг живут в settings.py, стадии - в stages.py. Здесь они
# ПЕРЕЭКСПОРТИРУЮТСЯ, потому что main - это фасад пайплайна: снаружи его зовут
# как pipeline.load_config / pipeline.resolve_path / pipeline.run_rules_stage
# (web_app/server.py, web_app/_pipeline_runner.py, report_pdf.py, тесты), и
# ломать эти вызовы ради перестановки файлов незачем.
from settings import (PROJECT_ROOT, SEVERITY_ORDER, load_config,  # noqa: F401
                      resolve_path, resolve_vision_cfg)
from stages import (load_type_overrides, run_bundle_stage,  # noqa: F401
                    run_extraction_stage, run_rules_stage)

logger = logging.getLogger("error_analyzer")


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


def _finding_signature(finding: dict) -> tuple:
    """Подпись находки для дедупликации: вид + множество точек, к которым она
    относится. По ней детерминированные находки чекера сопоставляются с
    находками агентов, чтобы не задваивать.

    В подпись входят И "проводные" поля (клеммник/штифт/KKS), И "элементные"
    (позиционное обозначение/артикул). Без последних ВСЕ находки по связке
    схлопывались бы в одну подпись (клеммника и KKS у них нет - там сплошные
    None), и из отчёта пропадали бы разные ошибки по разным элементам.
    """
    points = frozenset(
        (r.get("document"), r.get("terminal_block"), r.get("pin"), r.get("kks"),
         r.get("designator"), r.get("article"))
        for r in finding.get("refs", [])
    )
    return (finding.get("kind"), points)


def combine_rule_and_agent_findings(rule_findings: list, merged: dict,
                                    known_errors: list = None) -> dict:
    """Соединяет находки чекера и итог агентов. Находки чекера - ground truth: они
    ВСЕГДА в итоге. Из находок агентов выбрасываем те, что дублируют находку чекера
    (по подписи), чтобы одно и то же не выводилось дважды. Результат сортируем по
    важности.

    known_errors применяются ЗДЕСЬ, а не только в промпте мерджера. Мерджер видит
    лишь отчёты агентов, а находки чекера приходят мимо него - и в режиме «без ИИ»
    (как и при agents.count = 1) его нет вовсе. Пока фильтр жил только в промпте,
    погасить ложное срабатывание чекера было нечем, хотя ровно ради этого файл и
    заведён. Сопоставление - по частичному совпадению полей, см. known_filter.
    """
    rule_sigs = {_finding_signature(f) for f in rule_findings}
    agent_errors = [e for e in merged.get("errors", [])
                    if _finding_signature(e) not in rule_sigs]
    dropped = len(merged.get("errors", [])) - len(agent_errors)
    if dropped:
        logger.info("Из находок агентов убрано %d дублей находок чекера", dropped)

    combined = known_filter.filter_findings(rule_findings + agent_errors,
                                            known_errors or [])
    combined.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9))
    merged["errors"] = combined
    return merged


class LLMUnavailableError(RuntimeError):
    """Сервер ИИ недоступен - полный анализ (с агентами) невозможен."""


def preflight_llm(cfg: dict, agents: bool = True, vision: bool = False) -> None:
    """Быстрая проверка доступности серверов ИИ ПЕРЕД запуском агентов.

    Нужна, чтобы при недоступном сервере анализ падал сразу с понятной ошибкой
    в консоль, а не висел минутами и не выдавал криптическую ошибку соединения
    litellm где-то в середине. Проверяется сервер каждого агента, который
    реально будет запущен (при agents.count: 1 - только выбранный single_agent),
    и, если включено зрение, сервер модели зрения.
    """
    agents_cfg = cfg.get("agents", {})
    servers = []
    if agents:
        if agents_cfg.get("count", 2) == 1:
            keys = (agents_cfg.get("single_agent", "agent_1"),)
        else:
            keys = ("agent_1", "agent_2")
        servers += [cfg["llm_servers"][key] for key in keys]
    if vision:
        servers.append(resolve_vision_cfg(cfg))

    checked = {}
    for scfg in servers:
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


def _remove_path(path: Path, retries: int = 5, delay: float = 0.3) -> None:
    """Удаляет файл или папку с повторными попытками.

    На Windows файл иногда на миг остаётся занят сторонним процессом (антивирус,
    индексатор Проводника, недавно закрытый PDF-парсер) - первая попытка падает с
    WinError 32. Если так и не получилось за все попытки - не роняем весь пайплайн
    из-за одной неубранной папки прошлого прогона, а лишь предупреждаем: она
    останется и будет убрана на следующей очистке."""
    import shutil
    import time as _time

    last_err = None
    for _ in range(retries):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as e:
            last_err = e
            _time.sleep(delay)
    logger.warning("Не удалось удалить %s (файл занят другим процессом): %s", path, last_err)


def clear_previous_results(cfg: dict) -> None:
    """Стирает результаты прошлого анализа перед новым прогоном: папку output
    целиком и извлечённые данные в data/ (папки документов + manifest.json).

    НЕ трогает base_files (исходные файлы пользователя), base_analysis_scripts
    (скрипты-парсеры) и your_helping_scripts_and_files - это не результаты, а вход
    и инструментарий. Их имена берём из config.paths, чтобы не удалить лишнего.
    """
    out_dir = resolve_path(cfg["paths"]["output_dir"])
    if out_dir.exists():
        for item in out_dir.iterdir():
            _remove_path(item)
    out_dir.mkdir(parents=True, exist_ok=True)

    data_dir = resolve_path(cfg["paths"]["input_dir"])
    keep = {
        resolve_path(cfg["paths"]["base_files_dir"]).name,
        resolve_path(cfg["paths"]["scripts_dir"]).name,
        resolve_path(cfg["paths"]["helper_scripts_dir"]).name,
        # альбомы целиком - тоже вход пользователя, а не результат прогона
        resolve_path(cfg["paths"].get("full_projects_dir")
                     or "./data/full_projects").name,
    }
    if data_dir.exists():
        for item in data_dir.iterdir():
            if item.name in keep:
                continue
            _remove_path(item)

    # рабочую папку агента чистим ОТ содержимого, но саму папку оставляем
    helper = resolve_path(cfg["paths"]["helper_scripts_dir"])
    if helper.exists():
        for item in helper.iterdir():
            _remove_path(item)
    logger.info("Результаты прошлого анализа очищены (output/ и data/<документы>/)")


def run_pipeline(input_dir: str = None, known_errors_path: str = None,
                 output_dir: str = None, config_path: str = None,
                 doc_types: dict = None, skip_extract: bool = False,
                 skip_agents: bool = False, visual: bool = False,
                 clear_previous: bool = False,
                 bundles: dict = None, llm_gate=None) -> dict:
    """Главная точка входа для использования из другого скрипта (без CLI).

    doc_types: {"имя файла.pdf": "scheme"|"assembly"|"spec"|"netlist"} - явная
        пометка типа документа (приходит из формы загрузки на сайте). Если не
        передано, тип берётся из имени файла: марка вида по ГОСТ ("Э3", "СБ",
        "СО") либо пометка в начале имени ("(scheme)...", "(netlist)...").
    bundles: {"имя файла.pdf": "связка 1"} - явная привязка документа к связке
        (комплекту одного шкафа). Если не передано, связка определяется по
        подпапке в base_files или по общему префиксу имени файла.
    skip_extract: не перезапускать базовые парсеры, работать по тому,
        что уже лежит в data/.
    skip_agents: НЕ запускать LLM-агентов и мерджер - отчёт собирается только из
        находок детерминированного чекера (режим "без ИИ, только скрипты").
    visual: включить СТАДИЮ ЗРЕНИЯ (visual_stage.py) - модель со зрением смотрит
        на растр листов схемы и восстанавливает привязку подписей к линиям, по
        которой судит детерминированный чекер. Сочетается с любым значением
        skip_agents: "зрение без текстовых агентов" - осмысленный режим.
    clear_previous: стереть результаты прошлого анализа перед запуском.
    llm_gate: вызывается ОДИН РАЗ прямо перед первой стадией, которой нужен
        сервер ИИ (зрение, а если его нет - агенты), и может блокировать
        сколько угодно долго. Через него веб-интерфейс держит очередь: скрипты
        (извлечение, чекеры, сверка связок) считаются сразу и параллельно с
        другими сессиями - они грузят локальный процессор и друг другу не мешают,
        - а вот к серверу ИИ пропускается строго одна сессия за раз, потому что
        LM Studio на всех один. Ставить в очередь ВЕСЬ прогон было неверно:
        пользователь ждал чужой сорокаминутной работы с моделью, чтобы получить
        находки чекера, которые считаются за секунды и никакого ИИ не требуют.
        В CLI не используется (там прогон и так один).

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
        logger.info("Стадия 0: извлечение данных базовыми скриптами-парсерами")
        manifest = run_extraction_stage(cfg, doc_types, bundles)
        logger.info("Извлечено документов: %d из %d",
                    manifest["summary"]["extracted_ok"],
                    manifest["summary"]["total_documents"])

    known_errors = load_known_errors(known_errors_path)
    logger.info("Загружено %d заранее известных ошибок из %s",
                len(known_errors), known_errors_path)

    logger.info("Стадия правил: детерминированные чекеры по каждому документу")
    rule_findings = run_rules_stage(cfg, data_dir)
    logger.info("Чекеры документов нашли %d находок", len(rule_findings))

    logger.info("Стадия связок: сверка документов одного шкафа между собой")
    bundle_findings = run_bundle_stage(cfg, data_dir)
    logger.info("Сверка связок нашла %d находок", len(bundle_findings))

    rule_findings = rule_findings + bundle_findings
    logger.info("Всего находок скриптов: %d (до запуска нейросетей)", len(rule_findings))

    # Дальше нужен сервер ИИ - и зрению, и агентам. Он один на всех, поэтому
    # очередь берётся ОДИН раз на обе стадии: отпустив слот между ними, мы бы
    # заставили LM Studio выгрузить модель зрения и загрузить текстовую, потом
    # обратно - на 30-миллиардных моделях это дороже самой работы.
    needs_llm = visual or not skip_agents
    if needs_llm and llm_gate is not None:
        llm_gate()
    if needs_llm:
        preflight_llm(cfg, agents=not skip_agents, vision=visual)

    if visual:
        # Импорт здесь, а не наверху: visual_stage тянет fitz, а main
        # импортируется веб-сервером ради load_config - той же причины, по
        # которой внутри своих обработчиков импортируются report_pdf и fragment.
        import visual_stage

        logger.info("Стадия зрения: модель смотрит на растр листов")
        visual_findings = visual_stage.run_visual_stage(cfg, data_dir)
        logger.info("Зрение нашло %d находок", len(visual_findings))
        # Находки зрения идут в ту же корзину, что и находки чекеров, и не
        # случайно: судит по ответу модели детерминированная арифметика
        # (visual_stage.odd_ones_out), а не сама модель. Значит их, как и
        # находки чекеров, нельзя терять на LLM-слиянии.
        rule_findings = rule_findings + visual_findings

    if skip_agents:
        logger.info("Агенты и мерджер пропущены, отчёт только из находок чекеров"
                    + (" и зрения" if visual else ""))
        # known_errors применяются и здесь: мерджера в этом режиме нет, а он был
        # единственным местом, где файл вообще читался. Без этого «известная
        # ошибка» гасилась только при полном прогоне с ИИ - ровно наоборот тому,
        # чего ждёшь от режима «только скрипты».
        errors = known_filter.filter_findings(rule_findings, known_errors)
        merged = {
            "errors": sorted(errors,
                             key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9)),
            "summary": ((f"Анализ без текстовых агентов: найдено {len(errors)} "
                         f"замечаний детерминированными чекерами документов, сверкой "
                         f"связок и визуальной проверкой схем.")
                        if visual else
                        (f"Анализ без ИИ (только скрипты): найдено {len(errors)} "
                         f"замечаний детерминированными чекерами документов и сверкой "
                         f"связок.")),
        }
        merged_path = out_dir / "merged_report.json"
        merged_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        logger.info("Итоговый отчёт сохранён: %s", merged_path)
        return merged

    agent_cfg = cfg["agent"]
    max_repair = agent_cfg["max_json_repair_attempts"]
    limits = {
        "max_code_turns": agent_cfg.get("max_code_turns", 25),
        "timeout_seconds": agent_cfg.get("timeout_seconds", 1200),
    }
    helper_dir = resolve_path(cfg["paths"]["helper_scripts_dir"])
    helper_dir.mkdir(parents=True, exist_ok=True)

    agents_cfg = cfg.get("agents", {})
    agent_count = agents_cfg.get("count", 2)

    if agent_count == 1:
        # Один агент - мерджить не с кем, LLM-сшиватель (merge_reports.py) не
        # вызывается. Отчёт агента напрямую идёт в combine_rule_and_agent_findings
        # ниже - "слияние" в этом режиме нужно только с находками чекеров/связок.
        agent_key = agents_cfg.get("single_agent", "agent_1")
        logger.info("Режим одного агента: запуск (%s / %s)",
                    agent_key, cfg["llm_servers"][agent_key]["model"])
        report = run_analysis_agent(cfg["llm_servers"][agent_key], str(data_dir),
                                    helper_dir=str(helper_dir),
                                    max_json_repair_attempts=max_repair, **limits)

        if cfg.get("logging", {}).get("save_raw_agent_json", True):
            (out_dir / "report_1.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        merged = report
    else:
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
    merged = combine_rule_and_agent_findings(rule_findings, merged, known_errors)
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
    parser.add_argument("--visual", action="store_true",
                        help="включить стадию зрения: модель смотрит на растр листов схемы")
    parser.add_argument("--no-agents", action="store_true",
                        help="без текстовых агентов и мерджера. Вместе с --visual даёт "
                             "быстрый цикл отладки зрения на уже извлечённых данных")
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
        findings = run_rules_stage(cfg, data_dir) + run_bundle_stage(cfg, data_dir)
        # тот же фильтр, что и в полном прогоне: иначе быстрый цикл отладки
        # показывал бы находки, которых в отчёте пользователя уже нет
        findings = known_filter.filter_findings(findings, load_known_errors(
            resolve_path(args.known_errors or cfg["paths"]["known_errors_file"])))
        print(format_text_report({"errors": sorted(
            findings, key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 9)),
            "summary": f"Детерминированные чекеры документов и сверка связок: "
                       f"найдено {len(findings)} замечаний."}))
        return

    try:
        merged = run_pipeline(args.input, args.known_errors, args.output_dir,
                              args.config, skip_extract=args.skip_extract,
                              skip_agents=args.no_agents, visual=args.visual)
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
