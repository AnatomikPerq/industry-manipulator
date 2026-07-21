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
merged = run_pipeline(llm_gate=wait_fn)                    # block right before the agent stage
```

`llm_gate` is called **once**, immediately before `preflight_llm`, and may block for as
long as it likes. That is the whole mechanism behind the web UI's queue: scripts run
immediately and in parallel, only the agent stage is serialized. Nothing else in the
pipeline knows the queue exists.

There is no test suite, linter, or build step in this repo — verification is running
`--extract-only` / `--rules-only` against the real bundle in
`analyzer_to_errors/data/base_files/`, or `--check-llm` against the configured LM Studio
servers. `--rules-only` on already-extracted data is instant and is the fast loop when
working on checkers. A sudden jump in the finding count means a checker regressed into
false positives.

Extraction used to take minutes; it no longer does (ЩСКЗ bundle **7 s**, a 309-sheet
album **143 s**) — see the perf note under `schematic_diagram_to_data.py` below. Both
numbers are cheap enough to re-run on every change, so do.

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

**Альбомный корпус — папка `на проверку/`** (в корне репозитория, не в `data/`): два
полных проекта, на которых замерены V1.6-правки. Прогонять их — изолированной
папкой-сессией внутри `analyzer_to_errors/sessions/` (config с абсолютными путями, альбом
в `full_projects/`), НЕ в общий `data/` — иначе смешается с корпусом ЩСКЗ. Базлайны:

* `11-463-2026-АТХ Енисей.pdf` (184 л., один шкаф ЛСУ КОС) — **4 находки**: дубль катушки
  `5K9` (л.40/50, одинаковая позиция — копипаста листа), `C6` на чертеже и схеме, но не в
  спецификации, и два расхождения артикулов (`HLA` 828163≠828165, `XF` ASK 2S≠351109).
  До правок было 489, из них ~480 ложных: спецификация без строки нумерации колонок
  парсилась в 0 строк, диапазоны `1K1 - 1K24` не раскрывались, `230VAC` считался артикулом.
* `24-051-ЭОМ_2026.06.23.pdf` (237 л., 13 связок-шкафов) — **35 находок**: 23 MISSING
  «нарисовано+на схеме, но не заказано» (лампы `1HL03..13` панели 1 ВРУ и т.п. — в
  объектной спецификации их действительно нет), 4 дубля `XDO4:13` (подписан с ОБЕИХ
  сторон контакта «ПУСК» — тот же подтверждённый паттерн, что `5XT1:1` на ЩСКЗ),
  4 INCOMPLETE по спецификации, 3 MISMATCH, 1 REVIEW. До правок было 85.

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

   **Документов одного типа в связке бывает НЕСКОЛЬКО** — в альбоме у шкафа лежат
   принципиальная + однолинейная + схема внешних соединений, а чертёж разбит на «Общий
   вид» и «Вид спереди». Раньше молча брался первый по списку, и им оказывалась схема
   внешних соединений (сортировка!) — у неё почти нет обозначений приборов, и сверка
   «нарисовано, но не заказано» на альбоме тихо вырождалась. Теперь главный выбирается
   по полноте данных (`_doc_quality` в `main.py`: у принципиальной всегда больше
   привязанных клемм), остальные едут в `extra` и их обозначения объединяются
   (`load_scheme_bundle`/`load_assembly_bundle`), а ссылка находки ведёт на документ,
   где обозначение реально подписано (`tag["doc"]`). **Документ, извлёкшийся пустым, из
   сверки исключается** (`_is_empty` в `bundle_rules` + статус `partial` и `warnings` в
   манифесте): пустая спецификация читается правилами как «ничего не заказано» — 16
   ложных MISSING из 17 находок на КОС, ровно так и найденные.

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
   оказывались ОБЕ стороны пары (артикул `1/L1` при позиции `13NO`). Ещё три замеренных
   капкана (V1.6, альбомы `на проверку/`): «артикул», спаренный с МАССОЙ обозначений, —
   типовая подпись на картинке изделия, а не артикул (`230VAC` у КАЖДОГО реле КОС — 630
   пар, 320 ложных «разный артикул» из 322; порог `MASS_CAPTION_MIN_DESIGNATORS`, у
   настоящих артикулов максимум 19) плюс явный фильтр номинала (`VOLTAGE_RE`);
   обозначение в `rule_designator_not_in_spec` обязано ОКАНЧИВАТЬСЯ цифрой по ГОСТ 2.710
   (`3HL`, `17SA` — обрезанные подписи, 14 из 21 находки по ПЭСПЗ) и не быть
   МЭК-маркировкой (`1NO`); подпись-диапазон `FU1-FU3` в спецификации лежит по одному
   (`FU1`, `FU2`, `FU3`) и целиком не найдётся никогда — если все части есть, изделия
   заказаны. В `schematic_rules.rule_duplicate_terminal_address` два фильтра с той же
   родословной: клеммы шин `N`/`PE` подписываются у каждого присоединения (все 6 находок
   ЭОМ по ним ложные), а клеммник, у которого на листе задублирован ЦЕЛЫЙ РЯД адресов
   (`7X1:1..4` по два раза), — повторно изображённый клеммник чужого шкафа (типовая
   обвязка двух вентагрегатов на одном листе ЩОВ, 26 «дублей»), тогда как настоящие
   ошибки — одиночный дубль при уникальных соседях (обе ЩСКЗ, `XDO4:13` на ЭОМ). Where a
   guess is unavoidable ("not drawn — forgotten, or simply never drawn?"), the finding is
   a `REVIEW` (a question for the engineer), not an assertion of error.
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
  terminal addresses, relay coils). Wire candidates **drop zero-width lines** (`drop_glyph_hairlines`,
  which lives in `schematic_diagram_to_data.py` — connectivity imports it, the dependency
  runs that way):
  CAD export writes text as vector outlines, and those hairline fragments glue every net on
  the sheet into one blob (the biggest net on ЩСКЗ sheet 10.2 was 2837 segments; it is 39
  after the filter, with no terminal lost). Real wires are stroked with a real pen; the
  filter self-disables if a sheet has almost no stroked lines, so a bureau that draws wires
  hairline keeps the old behaviour instead of silently ending up with no wires at all.

  **The one perf fact worth not re-learning.** `build_issue_candidates` used to be
  **98.6% of the whole scheme parse** (34.7 s of 35.2 s on a *four-sheet* file; hours on an
  album). Not because Python is slow — because `find_wire_crossings` tested every pair of
  segments (27 M `_seg_intersect` calls per sheet) and the segments it was mostly testing
  were **letter outlines crossing themselves**. That is also where the "13901 crossings" on
  a file with a few hundred wires came from, which is why the agent prompt had to carry a
  warning that the list means nothing. Fixed by applying the hairline filter here too
  (n drops ~44×) plus a bbox grid for pair enumeration; the geometry is untouched, the same
  `_seg_intersect` still decides. **34.7 s → 0.02 s**, crossings 13901 → 241, ЩСКЗ baseline
  still exactly 6. If extraction ever "hangs while the CPU idles" again, look for another
  quadratic loop before reaching for C or multiprocessing.

  **Лист-картинка не схема** (`is_dense_graphic`, порог `DENSE_GRAPHIC_LINES = 20000`).
  Сплиттер альбома режет по наименованию штампа, поэтому план расположения, подшитый в
  конец «Схемы внешних подключений», остаётся листом этого документа. На двух таких
  листах Енисея 147333 и 99330 отрезков при 12 и 7 надписях (текст начерчен кривыми) —
  поиск пересечений занимал на них 58 с и 22 с, сборка цепей ещё четыре минуты, и всё
  ради «цепей» архитектурной штриховки. Замер по 124 листам корпуса: у самого густого
  НАСТОЯЩЕГО листа схемы 4885 отрезков, 95-й процентиль 3194 — порог стоит между
  группами с четырёхкратным запасом в обе стороны. Отличаем по густоте, а НЕ по
  наименованию листа: наименование — привычка бюро (ровно та причина, по которой связку
  нельзя угадывать по имени файла). Пропущенные листы перечислены в
  `pages_skipped_as_graphics` в `issues_candidates.json`/`nets.json` и в статистике
  манифеста — молча пропущенный лист читался бы как «здесь ничего не найдено».
  Итог на Енисее: **478 с → 86 с**, находки те же 18.
- `netlist` → `netlist_to_json.py`. **Под типом живут ТРИ разные таблицы**
  (`detect_table_kind` по заголовкам первого листа → `table_kind` в
  connections.json/манифесте): ГОСТ-таблица подключений (жёсткий шаблон COL_BOUNDS),
  «Перечень входных/выходных сигналов» ПЛК и «Кабельный журнал». Два последних режет из
  альбома `TITLE_TO_TYPE`, и ГОСТ-шаблон на них давал НОЛЬ строк при статусе ok — на АТХ
  так молчали все три нетлиста. Их колонки берутся из линовки + заголовков (как у
  спецификации в PDF); низ шапки считается ТОЛЬКО по верхней десятой листа — по верхней
  четверти в него попадали первые строки данных, и каждый перечень начинался с 1DO5
  вместо 1DO1. `netlist_rules.RULES_BY_KIND` включает для каждого вида только осмысленные
  правила (у перечня и журнала нет клеммников ПО ПОСТРОЕНИЮ — «не указана точка
  подключения» стреляла бы на каждой строке); для них добавлены точные дубль-правила
  (канал ПЛК / обозначение кабеля), замер: 177+280 каналов и 153 кабеля КОС — 0 ложных.
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

  **Строка нумерации колонок «1 2 3 …» необязательна.** Она была воротами листа — и все
  12 листов обеих спецификаций АТХ молча парсились в 0 строк при статусе ok, а сверка
  связки честно выдала 16 ложных «не заказано» из 17 находок по пустой спецификации.
  Без неё шапка ищется по самим заголовкам («Поз.»/«Кол.» — сокращения в
  `COLUMN_PATTERNS`), низ шапки дотягивается поглощением строк-переносов («Код обору-» /
  «дования» / «материала» — иначе обрывки уезжали в ячейки первой строки данных).
  Верх данных при живой строке нумерации — её низ, НЕ «20% высоты листа»: у АТХ данные
  начинаются на 13%, и старая эвристика съедала первые 3–4 строки каждого листа (ЭОМ
  «базлайн 837 строк» был занижен ровно этим — теперь 873). Диапазон позиций через
  дефис С ПРОБЕЛАМИ (`1K1 - 1K24`) — диапазон (`RANGE_SEP_RE`); склейка переносов в
  `_join_fragments` жуёт дефис только после БУКВЫ, иначе перенесённый диапазон
  («1KL1 -» / «1KL12») терял разделитель и десять промежуточных реле «не были заказаны».
  Контроль правильности раскрытия бесплатный: у реле строка «1K1 - 1K24, 2K1 - …»
  развернулась ровно в 246 обозначений при количестве 246.

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

### Two consumers of a finding beyond the table

**`fragment.py`** — the crop of the drawing behind the "фрагмент" button. It finds the
spot by **searching the source PDF for the finding's key**, not by coordinates: a ref
locates things in domain fields on purpose, since findings come from checkers *and* from
the model, and the model has no coordinates at all. Load-bearing details:

* keys are tried specific→general (article → designator → marking → kks → terminal
  block, the terminal block **without** the pin, because `1XT5:3` is drawn as split
  fragments and never matches whole);
* **the winner is the best key, not the first one that hits.** The "article" on an
  assembly drawing is regularly junk — a voltage (`230VAC`) or an IEC pin label — and
  such a key matches hundreds of times, pointing nowhere. A key hitting more than
  `MAX_HITS_USEFUL` times is skipped in favour of the next; if all are like that, the
  least frequent wins;
* **never draw into the page.** `page.draw_rect` writes into the content stream, and on
  a general-view drawing that stream holds 776k primitives — 0.33 s *per rectangle*.
  With 296 hits of `230VAC` that was 99 s for one request and the server looked hung.
  Highlights are painted on the finished pixmap (`_outline`), which costs the same
  regardless of sheet complexity. A pixmap made with `clip` does **not** start at (0,0)
  — it carries `pix.x/pix.y` (5609, 1882 on that sheet), and `set_rect` silently returns
  `False` for a rect outside it, which is exactly how the boxes once vanished while the
  crop itself stayed correct;
* the sheet named in the finding is only the *first* candidate, and when the key isn't
  there the response must say which sheet was actually rendered (`X-Fragment-Page` /
  `X-Fragment-Fallback`) — on "element missing from its peer sheet" the absence *is* the
  finding, and silently showing another sheet reads as a refutation of it;
* pages arrive with `/Rotate 270`, so `search_for` returns **un-rotated** coordinates —
  clip via `rotation_matrix`, draw with the raw rect. Getting that wrong crashed the
  render on some sheets and, worse, silently framed the wrong region on others.

**`report_pdf.py`** — the report as one PDF, which is what gets emailed and filed. It
opens with a **description of the analysis** — documents, bundles, what was checked and
explicitly what is *not* checked — before any finding. Without that framing a list of
findings reads as "here are the project's errors", which is false: whole checks are
deliberately absent, and `REVIEW` findings are questions, not assertions.

### `web_app/` — the local UI

Единица работы интерфейса — **СЕССИЯ** (`sessions.py`): комплект документов одного шкафа
плюс её прогон и её отчёт, со своей папкой `analyzer_to_errors/sessions/<id>/`
(`session.json`, `config.yaml`, `log.txt`, `data/`, `output/`). Полный проект грузится
той же формой: файл ложится в `base_files/`, а пайплайн сам опознаёт его по числу листов
и переносит в `data/full_projects/` сессии. Папка эта лежит **рядом** с `base_files`, а не
внутри: `base_files` сканируется рекурсивно и подпапка в нём означает связку, так что
альбом внутри стал бы «связкой» из одного нечитаемого файла на 200 листов. Сессии
создаются в интерфейсе и видны всем без авторизации (инструмент корпоративный,
локализации нет). Поставив сессию на исполнение, вкладку можно закрыть: статус, лог и
отчёт живут на диске, а не в памяти процесса или браузера.

**В ОЧЕРЕДЬ ВСТАЁТ НЕ ПРОГОН, А ТОЛЬКО СТАДИЯ ИИ** (`queue_worker.py`). Прежняя схема
«одна сессия за раз целиком» была неверной: скриптовая стадия грузит локальный процессор
и чужим сессиям не мешает ничем, а человек ждал чужой сорокаминутной работы с моделью
ради находок чекера, которые считаются за секунды. Тем более что режим «без ИИ» ждал в
той же очереди, хотя к серверу ИИ не обращается вовсе. Теперь:

* **скриптовый пул** (`SCRIPT_WORKERS = 4`) — сессия начинает считаться сразу; пул
  конечен, потому что десять одновременных разборов альбома упрут в диск;
* **слот к ИИ ровно один** (`LLM_SLOTS`) и поднимать его нельзя — LM Studio на всех один.

Механика гейта: дойдя до стадии агентов, подпроцесс печатает в stdout `@@LLM_WAIT` и
**замирает на чтении своего stdin**; воркер (который и так читает этот stdout построчно)
ставит сессию в llm-очередь, а освободив слот, пишет ей строку в stdin. Канал между
процессами уже был и уже читался построчно, а блокировка на `read()` снимается сама
собой, если процесс убьют (отмена) — никакого состояния на диске, которое пришлось бы
подчищать после падения сервера.

Статусы при этом не менялись; появилось поле `stage` (`"скрипты"` / `"очередь к ИИ"` /
`"ИИ"`), по нему интерфейс и различает «работает» и «ждёт».

**Ход разбора виден пользователю** (`data/base_analysis_scripts/progress.py`): парсеры
печатают в stdout строки `@@PROGRESS {...}`, воркер их разбирает, в лог НЕ кладёт (на
альбоме их триста) и складывает в `session.json` → интерфейс показывает «документ 15 из
30 · чтение схемы · лист 22 из 26», а разбираемый документ подсвечивается в списке
файлов. Для стадии ИИ такого показателя нет и быть не может: агент сам решает, какой
файл открыть и в каком порядке, и рисовать ему прогресс-бар значило бы врать.

**Прогресс надо звать из тех циклов, где реально идёт время.** Первая версия сообщала о
листах только при чтении PDF, а чтение — десятая часть разбора схемы (на 70-листовом
документе Енисея 1,5 с из 19). Остальное считалось молча, счётчик стоял на последнем
листе, и выглядело это как «прогресс всегда показывает последнюю страницу»; вдобавок
схему читают ДВА парсера подряд, поэтому счётчик успевал сбегать 1→N дважды — отсюда
«иногда почему-то первая». Сейчас о листах сообщают `extract_raw` (у второго прохода
своя подпись «повторное чтение схемы»), `build_graph` и сборка цепей в
`schematic_connectivity`. Придержанное троттлингом сообщение **запоминается**, а не
выбрасывается: иначе терялся последний лист документа и на экране навсегда оставался
предпоследний.

Подсветка строки в списке идёт по `path` (путь относительно `data/` сессии), а не по
имени: у альбома в каждой подпапке-шкафу своё «Общий вид». Части альбома создаёт сама
нарезка уже ВНУТРИ прогона, а список файлов загружен до его начала, поэтому при смене
разбираемого документа фронтенд перечитывает список, если такого пути в нём ещё нет.

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

Эндпоинты: `/api/config`, `/api/check-llm`, `/api/sessions` (список + состояние очередей,
POST — создать) и `/api/sessions/<id>/{rename,delete,upload,file-delete,set-type,enqueue,
cancel,log,report,report.pdf,file,fragment}`. Лог отдаётся порциями (`log?since=N`), а не
целиком на каждый опрос; вместе с ним едут `stage` и `progress`. Пометки типа документа
хранятся в `session.json` и уезжают в `data/.doc_types.json` сессии только перед запуском —
общий сайдкар был один на весь сервер и ключевался голым именем файла, из-за чего две
сессии спорили за одну запись.

Файлы адресуются **путём относительно `data/` сессии** (поле `path` в списке файлов), а
не именем: в альбоме у каждого шкафа своя подпапка и своё «Общий вид», имена повторяются,
и по имени сервер открыл бы или удалил не тот файл. `resolve_file` пускает наружу только
`base_files` и `full_projects` — рядом в `data/` лежат извлечённые данные и копия
скриптов.

`report.pdf` (`analyzer_to_errors/report_pdf.py`) и `fragment` (`fragment.py`) тянут
`fitz`, поэтому импортируются **внутри своих обработчиков**, а не наверху модуля.

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

**Уведомление о завершении — ЛЮБОЙ сессии, не только открытой** (`startFinishWatcher`
в `app.js`, V1.6). Открытая сессия и список поллятся своими таймерами, но оба живут
только на своём экране; наблюдатель же работает всегда: раз в 3 с сравнивает статусы
всех сессий с прошлым тиком и на переходе «queued/running → финал» показывает тост в
углу (клик открывает сессию) и системное Notification — последнее ТОЛЬКО когда вкладка
не в фокусе (смотрящему на страницу хватает тоста). Разрешение на Notification
спрашивается в момент запуска анализа (`enqueue`): браузер не даст спросить без клика,
и именно тогда уведомление становится нужным. Первый тик наблюдателя только запоминает
статусы: сессии, завершившиеся до открытия вкладки, — не новость. Серверной части у
механизма нет — только опрос `/api/sessions`.

**`has_files` считает и `full_projects`, не только `base_files`.** Иначе повторный запуск
сессии с альбомом был невозможен, а выглядело это как «файл не найден»: альбом
опознаётся уже ВНУТРИ прогона (по числу листов — `web_app` открыть PDF не может) и
переезжает из `base_files` в `full_projects`. Оборви прогон до того, как нарезка успела
разложить части обратно, — а обрывают его как раз там, потому что чтение штампов трёхсот
листов и есть самое долгое место, — и `base_files` оставался пуст. Сессия при этом
полностью исправна, но `enqueue` отказывал, и единственным выходом было загрузить
документ заново. По той же причине удаление последнего альбома стирает и нарезанные из
него части: иначе остались бы связки-шкафы от документа, которого в сессии больше нет, и
следующий прогон молча сверял бы призраков (нарезку чистит только сама нарезка).

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
