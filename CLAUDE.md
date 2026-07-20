# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository

`origin` is `https://github.com/AnatomikPerq/industry-manipulator.git` — this is the main
repository for this project. Commits made locally are expected to be pushed there (`git push
origin main`) unless the user says otherwise for a specific commit.

## What this is

"Индустрия манипулятор" — офлайн анализатор проектной документации: находит ошибки
в комплекте документов на шкаф управления — принципиальных схемах (Э3), сборочных
чертежах (СБ), спецификациях (СО, xlsx) и таблицах подключений (нетлистах). Извлекает
данные из PDF/XLSX, прогоняет детерминированные чекеры (по документу и по связке),
затем два независимых LLM-агента (через Open Interpreter, локальные модели в LM Studio),
сшивает результаты в единый отчёт. Полностью офлайн — без облачных API.

**Ключевое понятие — СВЯЗКА** (`bundles.py`): документы ОДНОГО проекта (шкафа).
Самые дорогие ошибки лежат между документами (изделие нарисовано, но не заказано;
разные артикулы; чужое обозначение в штампе). Ключ сверки элемента — позиционное
обозначение (`designator`: `1QF1`, `DO1`).

**За один прогон загружаются документы одного проекта — поэтому по умолчанию ВСЕ
документы прогона это ОДНА связка.** Разбиение по имени файла было и провалилось:
марка вида стоит то в конце (`... ША1 СБ_08.05.26`), то в начале
(`СБ_ИК.3912-АТХ2.015`), и комплект разъезжался на две «связки», после чего сверка
молча не выполнялась. Имя файла — привычка бюро, а не данные; угадывать по нему
нельзя. Несколько связок за прогон бывает, только если пользователь сам разложил
файлы по подпапкам `base_files/` (явное решение, а не догадка).

**Состав связки не фиксирован** (Э3+СБ+СО+нетлист, или только СБ+СО, или одна Э3) —
отсутствие документа НЕ ошибка и о нём не сообщается; правила сами пропускают
связку, если нужных им документов нет.

**ПОЛНЫЙ ПРОЕКТ** (`full_project.py`) — второй способ загрузки: не комплект файлов на
один шкаф, а альбом целиком, один PDF на 184–309 листов, внутри которого схемы десятка
разных шкафов, общие виды, спецификация, кабельный журнал и планы расположения. Альбом
режется на документы **до** стадии извлечения; части выкладываются в
`base_files/<шкаф>/` с пометкой типа в имени (`(scheme)ЩС2. Схема ...pdf`), после чего
`ingest.py` и `bundles.py` работают как раньше — «пометка типа» и «подпапка = связка»
уже существуют, править их не пришлось. Здесь **связка = ШКАФ**, а не весь прогон: в
альбоме их до пятнадцати, и обозначения в них пересекаются (`1QF1` есть и в ШУПЧ1, и в
ЩС1, но это разные аппараты) — свалив всё в одну связку, получаешь вал ложных «разный
артикул у одного обозначения».

Two top-level pieces:
- `web_app/` — локальный веб-интерфейс (stdlib `http.server`, без зависимостей): **сессии**
  анализа, очередь, загрузка PDF, запуск/отмена, просмотр отчёта.
- `analyzer_to_errors/` — сам пайплайн анализа (ядро). Has its own [README](analyzer_to_errors/README.md) with the detailed data-format spec.

## Commands

```bash
# Install pipeline deps (web_app has none — stdlib only)
pip install -r analyzer_to_errors/requirements.txt

# Web UI
python web_app/server.py            # → http://localhost:8000
python web_app/server.py --port 9000

# CLI, from analyzer_to_errors/
cd analyzer_to_errors
python main.py --extract-only    # only the parsers, no LLM — good for debugging extraction
python main.py --rules-only      # deterministic checkers + bundle cross-checks, no LLM
python main.py --skip-extract    # only agents, on data already extracted in data/
python main.py                   # full pipeline → output/merged_report.json
python main.py --check-llm       # ping configured LLM servers/models only (no PDFs)
python main.py --check-llm --quick   # same, skip the JSON-format contract check
```

As a library (what `web_app/_pipeline_runner.py` does):
```python
from main import run_pipeline
merged = run_pipeline()                                   # doc type from the filename
merged = run_pipeline(doc_types={"файл.pdf": "scheme"})    # doc type from upload form
merged = run_pipeline(skip_agents=True)                    # "no AI" mode: checker findings only
```

There is no test suite, linter, or build step in this repo — verification is running
`--extract-only` / `--rules-only` against the real bundle in
`analyzer_to_errors/data/base_files/`, or `--check-llm` against the configured LM Studio
servers. Extraction takes minutes; `--rules-only` on already-extracted data is instant and
is the fast loop when working on checkers. A sudden jump in the finding count means a
checker regressed into false positives.

**Current corpus — ЩСКЗ** (`Итог1.pdf` Э3 + `026.822.13-ИПК ЩСКЗ СБ` + `... СО.xlsx`).
The schematic has no kind mark in its name, so its type comes from `data/.doc_types.json`
(`{"Итог1.pdf": "scheme"}`) — without it the file is skipped. Expected baseline:
**6 findings**, all confirmed against the drawings by hand:

| finding | where |
|---|---|
| `QF1` on drawing + schematic, missing from the spec (row 10 is blank) | high, `rule_designator_not_in_spec` |
| terminal `1XT5:3` labelled twice on sheet 10.4 (should be `1XT5:4`) | high, `rule_duplicate_terminal_address` |
| terminal `5XT1:1` labelled twice on sheet 10.5 (`+` and `-` of one device) | high, same rule |
| two relay **coils** labelled `2KL2` on sheet 10.3 (the left one should be `2KL1`) | high, `rule_duplicate_relay_coil` |
| `GB1` on sheet 3 but not on the general view (sheet 1) | medium, `rule_element_missing_from_peer_sheet` |
| `HL5` in the door legend (sheet 5) but not drawn on the door (sheet 1) | medium, same rule |

Two further known errors in this bundle are **not** caught deterministically and are left to
the agents: the wire numbers on sheet 10.2 (`6` should be `5`, `12` should be `11`). That
check was measured and rejected at 250 false positives on three files — see the "не вошло"
header in `schematic_rules.py`. Do not re-attempt it without first fixing the extraction it
depends on (markings are bound to a net by radius, so a dense sheet hands a net its
neighbour's numbers).

The previous corpus (`связка 1|2`: ША1 + ШУ-ТМ, baseline 11 findings) is **no longer in the
repo** — only the two Э3 schematics survive, in git history under `на проверку/`. They are
still the false-positive corpus for schematic rules (profiles C and D): recover them with
`git show`, extract, and confirm any new scheme rule stays at **0 findings** on both.

## Architecture

### Pipeline stages (`analyzer_to_errors/main.py::run_pipeline`)

−1. **Нарезка альбомов** (`full_project.py`, до извлечения) — PDF из `data/full_projects/`
   плюс всё, что нашлось в `base_files/` и оказалось альбомом (`is_full_project`: ≥80
   листов; самый большой одиночный документ корпуса — спецификация на 64, самый маленький
   альбом — 184). Опознанный альбом **переезжает** в `full_projects/`, иначе извлечение
   следом попыталось бы разобрать его как обычный документ. Детект живёт здесь, а не в
   `web_app`, потому что там нет и не должно быть `fitz`.

   **Граница документа — графа «наименование» основной надписи** (ГОСТ 21.101). Часть =
   серия подряд идущих листов с одним наименованием; лист без графы (форма 4, продолжение)
   наследует наименование предыдущего. Два других сигнала **измерены и отвергнуты**:
   *номер листа* (на Енисее `4.1…4.70`, `5.1…5.41` — ведущая цифра и есть документ; но ЭОМ
   нумерует альбом сквозно `6`, `22.1`, `40.2`, где `.N` — подлист) и *форма штампа*
   (наличие граф «Стадия»+«Листов» = первый лист; по ГОСТ верно и на Енисее даёт ровно 10
   документов, но ЭОМ ставит полную форму на КАЖДОМ листе — 48 «документов», половина по
   одной странице). Оба проверяют привычку бюро, а не структуру альбома.

   Наименование **объекта** отделяется от наименования **документа** тем, что повторяется
   на >50% заполненных листов (в штампе они стоят в одной колонке друг под другом, и по
   координате их не разделить — число строк верхней части плавает). Тот же приём, что в
   `assembly_rules.py` для парного листа.

   Обозначение шкафа из наименования — ключ связки. Раскладки **приводятся к кириллице**
   (`_unify_layout`): бюро пишет однолинейную схему как `ЩC1` с ЛАТИНСКОЙ `C`, а
   принципиальную того же шкафа — как `ЩС1`; без приведения это две разные связки, и два
   документа одного шкафа не сверяются друг с другом молча. Части без шкафа в наименовании
   (спецификация и кабельный журнал идут на весь объект) уходят в связку `общие документы`.
   Планы расположения, молниезащита, установочные чертежи и ПЗ **сознательно не
   анализируются** (`TITLE_SKIP`) — комплектацию шкафа они не описывают.

0. **Extraction** (`ingest.py`) — PDF/XLSX in `data/base_files/` (scanned **recursively**:
   subfolders are explicit bundles) → per-document data folders. The document type is
   resolved as, in priority order: `doc_types` argument (from the upload form) →
   `data/.doc_types.json` (written by `web_app/server.py`) → filename: a marker prefix
   (`(scheme)...pdf`, Russian aliases too — see `TYPE_ALIASES`) or a **kind mark** found as
   a whole word anywhere in the name (`Э3`/`СХ`→scheme, `СБ`→assembly, `СО`→spec,
   `NL`/`НЛ`→netlist — see `KIND_MARK_TO_TYPE`/`detect_kind_mark`). The mark appears both
   as a suffix (`... ША1 СБ_08.05.26`) and as a prefix (`СБ_ИК.3912-АТХ2.015`), so it's
   matched anywhere — but only with strict word boundaries, or `СО` matches inside
   `СОЕДИНЕНИЙ` and `СХ` inside `схема`. A file whose type can't be resolved is not
   processed; it lands in `manifest.json`'s `skipped_files`. `TYPE_SUFFIXES` also enforces
   extension per type (spec must be .xlsx). This guarantees each base parser only ever sees
   documents of its own type. Bundles are then assigned (`bundles.py`) and written to the
   manifest's `bundles` section.
1. **Rules stage** (`run_rules_stage` in `main.py`) — deterministic checkers, one per doc
   type, no LLM, milliseconds: `netlist_rules.py` (duplicate physical addresses, tag in
   conductor field, extra KKS suffixes, empty terminals), `schematic_rules.py`
   (reference to a nonexistent sheet, one-sided cross-sheet link, **duplicate terminal
   address on a sheet**, **two relay coils with one designator**), `spec_rules.py` (`#REF!`
   cells, quantity < number of positions, one article in several rows) and
   `assembly_rules.py` (**element on one sheet of the drawing but missing from its peer
   sheet**).

   Note what the schematic rules are careful *not* to check: "designator appears twice" is
   **not** an error — a relay legitimately shows its coil on one sheet and its contacts on
   several others, all bearing the same designator. Only a duplicated **coil** is an error,
   and coils are identified by their IEC `A1`/`A2` terminals (`find_relay_coils`), never by
   recognising the symbol. Designators are also stitched back together from fragments
   (`_adjacent_fragment`): a hand-edited label arrives split (`2KL` + `2`, `1XT5:` + `3`),
   which is precisely where these errors live.

   `assembly_rules.py` is newer than the rest and contradicts what this file used to say
   ("the drawing cannot be checked alone"). It can: the drawing is *multi-sheet*, the same
   element appears on the general view **and** on a detail sheet or legend table, and one
   that fell off a sheet is provable from that document alone. The peer sheet is discovered
   from the data (two sheets whose designator sets overlap ≥90% show the same set of
   products), never from the sheet's title — view names are a bureau habit, exactly like
   filenames.
2. **Bundle stage** (`run_bundle_stage` in `main.py` → `bundle_rules.py`) — deterministic
   cross-checking of the three documents of one cabinet, no LLM. Matches by `designator`:
   designation mismatch between stamps, different article for the same element, article on
   the drawing absent from the spec, designator on drawing **and** schematic but absent from
   the spec, spec item not on the drawing.
   Findings from both stages are ground truth and are merged back deterministically.

   **Спецификация полного проекта одна на весь объект** и лежит в связке `общие
   документы`. Без неё у связок-шкафов спецификации нет вообще, и вся сверка молча не
   выполняется — поэтому `_lend_project_wide_docs` (`main.py`) одалживает её каждому шкафу
   с пометкой `project_wide`. Пометка выключает два направления, где спецификация всего
   объекта заведомо врёт: `rule_spec_element_not_on_assembly` (оборудование двенадцати
   чужих шкафов закономерно отсутствует на чертеже разбираемого) и `rule_designation_mismatch`
   (у неё обозначение альбома, а не шкафа — иначе одно расхождение штампов размножилось бы
   на все пятнадцать связок). Обратное направление — «обозначение есть на чертеже и схеме,
   а в спецификации нет» — на ней верно и работает. По той же причине `spec_rules`
   получает `project_wide=True` и пропускает `rule_duplicate_code`: один артикул в
   нескольких строках здесь норма (один автомат стоит в десятке щитов), замер — 112 ложных
   находок на 837 строк.

   Deliberately few rules — every checker carries a long comment listing checks that were
   tried and **rejected** for false-positive rates measured on real files (a false positive
   costs more than a miss: the engineer checks it against the drawing and stops trusting the
   report). Key measured facts worth not re-learning: on the assembly drawing, element
   designators are **indistinguishable from pin labels** (`A1` is a PLC on ША1 but a relay
   coil pin on ШУ-ТМ) — so a designator-based assembly→spec check is only allowed when the
   designator is corroborated by a **second document** (`rule_designator_not_in_spec`
   requires it on the schematic too); the plain article check is the other way round
   (articles have no pins), and article comparison only uses `pair_source="block"` pairs (on
   ШУ-ТМ 3266 of 3277 pairs are `nearest`, where the article is often pulled from a
   neighbour). An "article" with no digit in it is not an article but a caption on a picture
   of the product (`Status`, `Force Button`) — it is filtered out. Цифра в «артикуле» ещё не
   делает его артикулом: маркировка вывода по МЭК (`1/L1`, `2/T1`, `13NO`, `A1`) проходит
   фильтр цифры и отсекается отдельно (`IEC_TERMINAL_RE`) — на ЩС1 полного проекта две
   находки из трёх были ровно такими, причём подписями выводов одного контактора
   оказывались ОБЕ стороны пары (артикул `1/L1` при позиции `13NO`). Where a guess is
   unavoidable ("not drawn — forgotten, or simply never drawn?"), the finding is a `REVIEW`
   (a question for the engineer), not an assertion of error.
3. **Agents** (`oi_agent.py`, Open Interpreter) — two independently-configured LLM agents
   (`llm_servers.agent_1` / `agent_2` in `config.yaml`) each analyze the whole `data/`
   folder, writing and executing their own code rather than having files pushed into
   context. They're left only what rules can't catch **in principle**: the checker compares
   strings, the agent understands *what the product is* (should a fan be on the schematic?
   does "Автоматический выключатель NXB-63 3P 40А" match the `C40A` label?), plus
   netlist-vs-scheme cross-checking and wording. Their scratch files go in
   `data/your_helping_scripts_and_files/`.
4. **Merge** (`merge_reports.py`) — a "merger" model (reuses `agent_1` or `agent_2`,
   configured via `llm_servers.merger.use_agent`) combines the two agent reports, dedupes,
   and strips anything in `known_errors.json`. The rule- and bundle-stage findings are then
   added back in **deterministically** (no LLM) via `combine_rule_and_agent_findings` —
   they're ground truth and must not be lost in an LLM merge.
5. **Validation** (`validation.py`, `schema.py`) — every model JSON response is validated
   against the schema, with an auto-repair retry loop (`agent.max_json_repair_attempts`
   in `config.yaml`). Exhausting retries raises `validation.JSONValidationError`.

### Extraction detail (`ingest.py` + `data/base_analysis_scripts/`)

Each doc type maps to one or more base parser scripts (`DOC_TYPES` in `ingest.py`), each
exposing `extract_to_dir(path, out_dir) -> (files, stats)`:
- `scheme` → `schematic_diagram_to_data.py` (text/lines/graph/IO channels) +
  `schematic_connectivity.py` (real nets incl. T-junctions, terminal index, duplicate
  terminal addresses, relay coils). Wire candidates **drop zero-width lines** (`drop_glyph_hairlines`):
  CAD export writes text as vector outlines, and those hairline fragments glue every net on
  the sheet into one blob (the biggest net on ЩСКЗ sheet 10.2 was 2837 segments; it is 39
  after the filter, with no terminal lost). Real wires are stroked with a real pen; the
  filter self-disables if a sheet has almost no stroked lines, so a bureau that draws wires
  hairline keeps the old behaviour instead of silently ending up with no wires at all.
- `netlist` → `netlist_to_json.py`
- `assembly` → `assembly_drawing_to_data.py` (labels only — **never** `get_drawings()`:
  one sheet holds up to 440k vector primitives and none of it helps find documentation
  errors. The PDF groups each element's label into one text block with the designator in
  a larger font than the article — that, not geometry, is the parsing rule. Reuses
  `analyze_fonts`/`apply_font_fix` from the schematic parser for mojibake.)
- `spec` → `specification_to_json.py` (xlsx via openpyxl; columns located by header text,
  not letter — bureaus differ (16 vs 9 columns). Its hard part is expanding the «Позиция»
  column: `1KL1...1KL3` and `1KL1 ... 50KL1` (range over the *leading* number) are both
  handled by comparing the two ends' numeric fields, not by a prefix+number regex.)
- `spec` **в PDF** → `specification_pdf_to_json.py`. Парсер выбирается по расширению
  (`scripts_by_suffix` в `DOC_TYPES`): в полном проекте спецификация — такой же лист
  альбома, книги Excel к ней нет, а без неё вся сверка по связке не работает. Контракт
  выхода тот же `specification.json`, а разбор строки (раскрытие «Позиции», отсев
  разделов) **импортируется** из xlsx-парсера, чтобы две копии не разъехались.
  Колонки берутся из **линовки** таблицы: разделители нарисованы короткими отрезками по
  границам ячеек (длинных вертикалей на листе нет ни одной), поэтому копится суммарная
  длина вертикальных отрезков по каждому X. Границы по заголовкам (середина между
  центрами соседних) **пробовались и не годятся**: колонки резко разной ширины, середина
  между «Позицией» и «Наименованием» проходит посреди наименования, и его перенос на
  вторую строку становился ложным обозначением (`4шт`, `248 Шайба 6.65Г.016 ГОСТ 6402-70`)
  — мусором в главном ключе сверки. Строки якорятся на колонке «Количество»: линовка даёт
  23 горизонтальные линейки на ~25 строк, а просвет по вертикали (шаг 23–30 px против
  высоты строки 14) не отличает соседнюю строку от переноса внутри ячейки.

**Layout profiles** (`data/base_analysis_scripts/profiles.py`): formatting rules (regexes)
are factored per design-bureau template and auto-detected on load (profiles `A`–`D`:
Regul R500/KKS/EPLAN, ОВЕН МВ210/МУ210, Delta DVP, ОВЕН 110). Without this a scheme from
an unrecognized bureau parsed almost empty. The detected profile is recorded in
`raw.json` and in manifest stats. Supporting a new bureau template means adding a new
`Profile`, not touching the pipeline.

`data/manifest.json` is the table of contents both the agents and `main.py`'s rules/bundle
stages read: which document is which type, **which bundle it belongs to**, where its data
lives, what files/stats came out of extraction, plus a `bundles` section (composition of
each bundle and what's missing from it).

### `analyzer_to_errors/data/` — the agent's sandbox

```
data/
  manifest.json                    # read first by agents and the rules/bundle stages
  base_files/                      # user's source files; subfolders = bundles
    связка 1/ связка 2/            #   each holds one cabinet's Э3 + СБ + СО
  base_analysis_scripts/           # base parser scripts + rule checkers + profiles
  your_helping_scripts_and_files/  # agents' own scratch folder
  <document name>/                 # per-document extracted JSON (raw/classified/graph/
                                    #   netlist/issues_candidates/nets/terminals/connections/
                                    #   assembly/specification)
```
Agents can see all of `data/`, including how the parser derived its output, and can go
back to the source PDF themselves if needed.

### Finding/report format (`schema.py`)

`output/merged_report.json` = `{"errors": [...], "summary": "..."}`. Each finding has
`scope`, `kind`, `severity`, `type`, `refs[]`, `finding`, `action`, `evidence`:
- `scope: "cross_document"` — a mismatch between documents. Two shapes:
  - netlist-vs-scheme: `refs[]` has **exactly two** entries, first `doc_type: "netlist"`,
    second `doc_type: "scheme"`;
  - bundle (spec/assembly/scheme): **up to three** refs, ordered `spec` → `assembly` →
    `scheme`; the document where the element is *absent* also gets a ref.

  `kind` ∈ `MISMATCH`, `MISSING`, `REVIEW`.
- `scope: "single_document"` — error inside one document. One ref, or two refs on the
  same document for a duplicate. `kind` ∈ `DUPLICATE`, `BROKEN_LINK`, `FORMAT`, `INCOMPLETE`.

A `ref` locates one spot in domain terms. Same fields for every doc type, in two families:
"wiring" (`sheet`, `row`, `cabinet`, `terminal_block`, `pin`, `terminal_type`, `marking`,
`kks`, `conductor`) and "element" (`designator` — the bundle matching key, `article`,
`name`, `quantity`), plus `document`, `doc_type`, `source_file`, `found`; absent fields
are `null`. The UI renders one table per family (their columns don't combine) — routing is
by the doc types present in `refs`, see `renderReport` in `app.js`.

Findings are deduped/matched across stages by `_finding_signature` in `main.py`: `(kind,
{(document, terminal_block, pin, kks, designator, article) for each ref})`. The element
fields are **required** in that key: bundle findings have no terminal/pin/kks at all, so
without them every bundle finding on a document collapses to one signature and distinct
errors vanish from the report.

### `web_app/` — the local UI

Единица работы интерфейса — **СЕССИЯ** (`sessions.py`): комплект документов одного шкафа
плюс её прогон и её отчёт, со своей папкой `analyzer_to_errors/sessions/<id>/`
(`session.json`, `config.yaml`, `log.txt`, `data/`, `output/`). Полный проект грузится
той же формой: файл ложится в `base_files/`, а пайплайн сам опознаёт его по числу листов
и переносит в `data/full_projects/` сессии. Папка эта лежит **рядом** с `base_files`, а не
внутри: `base_files` сканируется рекурсивно и подпапка в нём означает связку, так что
альбом внутри стал бы «связкой» из одного нечитаемого файла на 200 листов. Сессии
создаются в
интерфейсе, видны всем без авторизации (инструмент корпоративный, локализации нет) и
выполняются **глобальной очередью строго по одной** (`queue_worker.py`) — LM Studio на
всех один. Поставив сессию в очередь, вкладку можно закрыть: статус, лог и отчёт живут на
диске, а не в памяти процесса или браузера.

**Изоляция сессий сделана путями, а не правками пайплайна.** `_pipeline_runner.py`
собирает `config.yaml` сессии — копию базового, где весь раздел `paths` заменён на
абсолютные пути её папки, — и передаёт его в нетронутый `run_pipeline`. Отсюда следует
главное ограничение: **папка сессии обязана лежать внутри `analyzer_to_errors/`**, потому
что `ingest.py` пишет пути документов в манифест через `relative_to(PROJECT_ROOT)`, а
`main.py` собирает их обратно как `PROJECT_ROOT / doc["data_dir"]`; сессия на другом диске
уронит извлечение невнятным `ValueError`. `base_analysis_scripts` копируется в каждую
сессию (`prepare_run`), а не шарится: промпт агента описывает её как подпапку своей
песочницы `data/`, и `clear_previous_results` сохраняет служебные папки по `.name` из
`config.paths` — с копией обе вещи работают без правок. `known_errors.json` остаётся общим
для всех сессий: это накопленное знание, а не результат прогона.

Эндпоинты: `/api/config`, `/api/check-llm`, `/api/sessions` (список + состояние очереди,
POST — создать) и `/api/sessions/<id>/{rename,delete,upload,file-delete,set-type,enqueue,
cancel,log,report}`. Лог отдаётся порциями (`log?since=N`), а не целиком на каждый опрос.
Пометки типа документа хранятся в `session.json` и уезжают в `data/.doc_types.json` сессии
только перед запуском — общий сайдкар был один на весь сервер и ключевался голым именем
файла, из-за чего две сессии спорили за одну запись.

Анализ по-прежнему исполняется **отдельным подпроцессом** (`_pipeline_runner.py` с JSON
args-файлом), а не потоком — только так отмена убивает всё дерево процессов мгновенно и
безусловно: сетевой вызов к LLM или сам Open Interpreter кооперативный сигнал вполне
может проигнорировать. Раннер пишет лог в stdout (воркер ретранслирует его в `log.txt`) и
сайдкар `<args>.result.json` с `{"ok", "error", "n_findings"}`.

Статусы сессии: `draft` → `queued` → `running` → `done` | `error` | `cancelled` |
`interrupted`. Последний ставится на старте сервера тем сессиям, что были `running`:
их процесс умер вместе с прежним сервером. Автоматически они НЕ перезапускаются —
пользователь мог перезапустить сервер именно ради остановки прогона; `queued` при этом
возвращаются в очередь в порядке `queued_at`.

Папка сессии сама и есть архив прогона, поэтому прежнее копирование результатов в
`story/<имя>/<дата>/` удалено. Старые папки `story/` остались лежать — их, как и раньше,
никто не читает.

CLI (`python main.py …`) сессий не знает и продолжает работать по общему
`analyzer_to_errors/data/` — быстрый цикл отладки чекеров не изменился.

### Config (`analyzer_to_errors/config.yaml`)

All paths in `config.yaml` are resolved **relative to the config file's own directory**,
not the process cwd (`resolve_path` in `main.py`) — required because the pipeline is
invoked from `web_app/`'s subprocess with an arbitrary working directory.

Key sections: `llm_servers.agent_1` / `agent_2` / `merger` (OpenAI-compatible endpoints,
model names, `max_tokens` is response-length only — must stay well under
`context_window` or Open Interpreter truncates the prompt history), `agent`
(`max_json_repair_attempts`, `max_code_turns`, `timeout_seconds` — safeguards against an
agent looping forever since Open Interpreter only stops when the model itself decides
it's done), `extraction.reuse_existing` (skip re-parsing a PDF if its data folder already
exists), `paths` (в т.ч. `full_projects_dir` — альбомы целиком; папка сохраняется от
очистки в `clear_previous_results`, это вход пользователя, а не результат прогона),
`logging.save_raw_agent_json`.
