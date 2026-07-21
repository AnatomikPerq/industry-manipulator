"""
Стадии пайплайна: извлечение, правила по документам, сверка связок.

Выделено из main.py, где они занимали больше половины файла и заслоняли собой
run_pipeline - то единственное, ради чего main.py вызывают снаружи. Логика не
менялась: это ровно те же функции, что были там, вместе с их замерами и
объяснениями.

Стадии агентов здесь нет и не будет: она принципиально другая (LLM, очередь,
таймауты) и живёт в oi_agent.py, а порядок вызова - в main.run_pipeline.
"""

import json
import logging
import traceback
from pathlib import Path

import full_project
import script_loader
from ingest import run_extraction
from settings import PROJECT_ROOT, resolve_path

logger = logging.getLogger("error_analyzer")


def load_type_overrides(cfg: dict) -> dict:
    """Пометки типа документа, выставленные пользователем в веб-интерфейсе.

    Лежат в data/.doc_types.json (пишет web_app/server.py при выборе типа в
    списке файлов). Читаем их и здесь, чтобы CLI и сайт видели одно и то же:
    иначе `python main.py` игнорировал бы типы, заданные в интерфейсе, и падал
    на файлах без пометки в имени.
    """
    path = resolve_path(cfg["paths"]["input_dir"]) / ".doc_types.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def run_extraction_stage(cfg: dict, doc_types: dict = None,
                         bundles: dict = None) -> dict:
    """Стадия 0: PDF/XLSX -> data/<имя документа>/*.json + data/manifest.json.

    Тип документа берётся (по убыванию приоритета): из doc_types (передан явно,
    например с формы загрузки) -> из data/.doc_types.json -> из имени файла
    (марка вида по ГОСТ "Э3"/"СБ"/"СО" либо пометка "(scheme)...").

    bundles: {"имя файла": "связка 1"} - явная привязка документа к связке.
    Если не передано, связка определяется по подпапке в base_files или по
    общему префиксу имени файла (см. bundles.py).
    """
    paths = cfg["paths"]

    # Альбомы целиком режутся на отдельные документы ДО извлечения: базовый
    # парсер рассчитан на документ одного вида, а у листа внутри альбома нет
    # имени файла, по которому пайплайн определяет тип. Части выкладываются в
    # base_files/<шкаф>/, после чего всё идёт обычным путём.
    if paths.get("full_projects_dir"):
        reports = full_project.split_full_projects(
            full_projects_dir=resolve_path(paths["full_projects_dir"]),
            base_files_dir=resolve_path(paths["base_files_dir"]),
            scripts_dir=resolve_path(paths["scripts_dir"]),
        )
        for rep in reports:
            logger.info("Полный проект %s: %d листов -> %d документов "
                        "(%d частей не анализируется)",
                        rep["source_file"], rep["total_pages"],
                        len(rep["parts_written"]), len(rep["parts_skipped"]))

    overrides = doc_types or load_type_overrides(cfg)
    return run_extraction(
        base_files_dir=resolve_path(paths["base_files_dir"]),
        scripts_dir=resolve_path(paths["scripts_dir"]),
        data_dir=resolve_path(paths["input_dir"]),
        overrides=overrides,
        reuse=cfg.get("extraction", {}).get("reuse_existing", False),
        bundle_overrides=bundles,
    )


def run_rules_stage(cfg: dict, data_dir: Path) -> list:
    """Стадия правил: детерминированные чекеры по КАЖДОМУ типу документа.

    Читает manifest.json и прогоняет по каждому документу чекер его типа:
      netlist  -> netlist_rules.check_connections_file (connections.json)
      scheme   -> schematic_rules.check_schematic_file (nets.json)
      spec     -> spec_rules.check_specification_file (specification.json)
      assembly -> assembly_rules.check_assembly_file (assembly.json)
    Возвращает находки в формате schema.REPORT_SCHEMA - том же, что у агентов.
    LLM здесь не участвует.

    Здесь долго стояло, что у СБОРОЧНОГО ЧЕРТЕЖА однодокументного чекера нет и
    быть не может: в одиночку чертёж проверять нечем, всё проверяется сверкой с
    другими документами связки. Это оказалось неверно. Чертёж многолистовой, и
    одно изделие показано на нескольких листах сразу (общий вид, вид двери,
    таблица надписей); изделие, выпавшее с одного из них, доказывается ВНУТРИ
    самого чертежа - см. assembly_rules.py. Сверка с другими документами
    по-прежнему живёт отдельно, на стадии связок (run_bundle_stage).

    Раньше здесь стоял фильтр `if doc_type != "netlist": continue`, из-за которого
    схемы не проверялись вообще: анализ комплекта из одних схем всегда давал ноль
    замечаний независимо от содержимого.
    """
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        logger.warning("manifest.json не найден в %s - стадия правил пропущена", data_dir)
        return []

    scripts_dir = resolve_path(cfg["paths"]["scripts_dir"])

    # чекер на тип документа: (скрипт, функция, файл с данными)
    checkers = {
        "netlist": ("netlist_rules.py", "check_connections_file", "connections.json"),
        "scheme": ("schematic_rules.py", "check_schematic_file", "nets.json"),
        "spec": ("spec_rules.py", "check_specification_file", "specification.json"),
        "assembly": ("assembly_rules.py", "check_assembly_file", "assembly.json"),
    }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings = []
    for doc in manifest.get("documents", []):
        checker = checkers.get(doc.get("doc_type"))
        if checker is None:
            # Тип без своего чекера - не сбой: документ всё равно участвует в
            # сверке связки (run_bundle_stage) и его видят агенты.
            logger.info("  %s (%s): отдельных правил для этого вида документа нет, "
                        "он проверяется сверкой с другими документами связки",
                        doc.get("name"), doc.get("doc_type"))
            continue
        script, func_name, data_file = checker

        data_path = PROJECT_ROOT / doc["data_dir"] / data_file
        if not data_path.exists():
            logger.warning("  %s: нет файла %s - правила не применены",
                           doc["name"], data_file)
            continue

        # Спецификация из связки "общие документы" описывает ВЕСЬ объект, а не
        # шкаф: часть правил на ней неприменима (см. SINGLE_CABINET_ONLY_RULES
        # в spec_rules.py). Флаг ставится только спецификации - остальные
        # чекеры такого параметра не принимают, а в общей связке лежит ещё и
        # кабельный журнал.
        kwargs = {}
        if (doc.get("doc_type") == "spec"
                and doc.get("bundle") == full_project.COMMON_BUNDLE_DIR):
            kwargs["project_wide"] = True

        try:
            module = _load_parser_module(scripts_dir, script)
            doc_findings = getattr(module, func_name)(doc["name"], str(data_path), **kwargs)
        except Exception as e:  # noqa: BLE001 - падение чекера не должно ронять прогон
            logger.error("  %s: чекер %s упал: %s", doc["name"], script, e)
            continue

        logger.info("  правила по %s (%s): %d находок",
                    doc["name"], doc["doc_type"], len(doc_findings))
        findings.extend(doc_findings)
    return findings


def _lend_project_wide_docs(groups: dict) -> None:
    """Одолжить спецификацию всего объекта каждой связке-шкафу.

    В полном проекте спецификация одна на весь объект и шкафа в наименовании не
    называет, поэтому попадает в связку "общие документы"
    (full_project.COMMON_BUNDLE_DIR). Без этого шага у связок-шкафов нет
    спецификации ВООБЩЕ, а значит вся сверка по связке молча не выполняется -
    и самые дорогие ошибки (изделие нарисовано, но не заказано) на альбоме не
    ищутся, хотя ровно ради них всё и затевалось.

    Одолженная спецификация помечается project_wide: она описывает ВЕСЬ объект,
    и направление сверки "строка спецификации -> её нет на чертеже" по ней
    бессмысленно (в спецификации штатно лежит оборудование остальных двенадцати
    шкафов). Обратное направление - "обозначение есть на чертеже и схеме, но в
    спецификации его нет" - остаётся верным и работает.
    """
    common = groups.get(full_project.COMMON_BUNDLE_DIR)
    if not common or "spec" not in common:
        return

    for bundle, docs in groups.items():
        if bundle == full_project.COMMON_BUNDLE_DIR or "spec" in docs:
            continue
        if not any(t in docs for t in ("assembly", "scheme")):
            continue
        docs["spec"] = dict(common["spec"], project_wide=True)
        logger.info("Связка %r: подключена общая спецификация объекта (%s)",
                    bundle, common["spec"]["name"])


def _doc_quality(doc_type: str, stats: dict) -> tuple:
    """Сколько полезного извлечено из документа - ключ выбора ГЛАВНОГО документа
    типа внутри связки (см. run_bundle_stage). Метрики берутся из статистики
    манифеста, самые говорящие - первыми: у принципиальной схемы всегда больше
    привязанных клемм, чем у однолинейной или схемы внешних соединений, у
    полного чертежа больше уникальных обозначений, чем у листа-продолжения."""
    if doc_type == "scheme":
        return (stats.get("terminals_on_scheme") or 0,
                stats.get("wire_markings_on_scheme") or 0,
                stats.get("nets") or 0)
    if doc_type == "assembly":
        return (stats.get("assembly_unique_designators") or 0,
                stats.get("assembly_elements") or 0)
    if doc_type == "spec":
        return (stats.get("spec_rows") or 0,)
    if doc_type == "netlist":
        return (stats.get("total_connections") or 0,)
    return (0,)


def run_bundle_stage(cfg: dict, data_dir: Path) -> list:
    """Стадия СВЯЗОК: детерминированная сверка документов ОДНОГО шкафа между собой.

    Стадия правил (run_rules_stage) проверяет каждый документ по отдельности и
    по построению не видит ошибок ВИДА "в спецификации один артикул, а на
    чертеже другой". Здесь документы группируются по связкам (поле bundle в
    манифесте, см. bundles.py) и каждая связка целиком отдаётся bundle_rules.py.

    LLM здесь не участвует. Возвращает находки в формате schema.REPORT_SCHEMA.
    """
    manifest_path = data_dir / "manifest.json"
    if not manifest_path.exists():
        logger.warning("manifest.json не найден в %s - стадия связок пропущена", data_dir)
        return []

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    documents = manifest.get("documents", [])
    if not documents:
        return []

    scripts_dir = resolve_path(cfg["paths"]["scripts_dir"])
    try:
        module = _load_parser_module(scripts_dir, "bundle_rules.py")
    except Exception as e:  # noqa: BLE001
        logger.error("Чекер связок не загрузился: %s", e)
        return []

    # {связка: {тип документа: сведения}}. Документов одного типа в связке
    # бывает НЕСКОЛЬКО - в альбоме у шкафа рядом лежат принципиальная,
    # однолинейная и схема внешних соединений, а чертёж разбит на "Общий вид" и
    # "Вид спереди". Раньше для сверки молча брался первый по списку - и им
    # оказывалась схема внешних соединений (сортировка!), у которой почти нет
    # обозначений приборов: сверка «нарисовано, но не заказано» на альбоме
    # тихо вырождалась. Теперь главным выбирается документ с самыми полными
    # данными (_doc_quality), остальные передаются чекеру связки в "extra" -
    # он объединяет их обозначения с главным (см. bundle_rules.check_bundle).
    groups = {}
    for doc in documents:
        if doc.get("status") == "failed":
            continue
        bundle = doc.get("bundle") or "без связки"
        slot = groups.setdefault(bundle, {})
        dtype = doc.get("doc_type")
        slot.setdefault(dtype, []).append({
            "name": doc["name"],
            "data_dir": str(PROJECT_ROOT / doc["data_dir"]),
            "source": doc.get("source_file"),
            "stats": doc.get("stats") or {},
        })

    for bundle, slot in groups.items():
        for dtype, candidates in slot.items():
            candidates.sort(key=lambda d: _doc_quality(dtype, d["stats"]),
                            reverse=True)
            primary = candidates[0]
            if len(candidates) > 1:
                primary["extra"] = candidates[1:]
                logger.info("Связка %r: документов типа %r несколько - главный для "
                            "сверки %s, обозначения остальных (%s) объединяются с ним",
                            bundle, dtype, primary["name"],
                            ", ".join(d["name"] for d in candidates[1:]))
            # Документ, извлечённый ПУСТЫМ, - это провал парсера, а не пустой
            # шкаф. Пускать его в сверку нельзя: пустая спецификация читается
            # правилами как «ничего не заказано» и на КОС дала 16 ложных
            # "изделие не заказано" из 17 находок. Чекер связки перепроверяет
            # то же сам (check_bundle), здесь - предупреждение в лог.
            if not any(_doc_quality(dtype, primary["stats"])):
                logger.warning("Связка %r: %s извлёкся пустым - в сверке связки "
                               "он участвовать не будет", bundle, primary["name"])
            slot[dtype] = primary

    _lend_project_wide_docs(groups)

    findings = []
    for bundle, docs in groups.items():
        try:
            bundle_findings = module.check_bundle(bundle, docs)
        except Exception as e:  # noqa: BLE001 - падение чекера не должно ронять прогон
            logger.error("  связка %r: чекер упал: %s", bundle, e)
            logger.debug(traceback.format_exc())
            continue
        logger.info("  сверка связки %r (%s): %d находок", bundle,
                    ", ".join(sorted(t for t in docs if t)), len(bundle_findings))
        findings.extend(bundle_findings)
    return findings


def _load_parser_module(scripts_dir: Path, script_name: str):
    """Чекер из data/base_analysis_scripts. Загрузка по пути - см. script_loader.

    Раньше здесь был собственный загрузчик со своим ключом кэша (_stage_...),
    отличным от ingest'овского (_base_parser_...), и один и тот же файл мог
    оказаться в процессе дважды - двумя независимыми модулями со своим
    состоянием.
    """
    return script_loader.load(scripts_dir, script_name)
