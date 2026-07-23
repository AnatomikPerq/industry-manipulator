# -*- mode: python ; coding: utf-8 -*-
"""
Сборка "Индустрия манипулятор" в exe, работающий без установленного Python.

Два входа в ОДНОМ onedir-бандле (общий COLLECT - зависимости не дублируются):
  * IndustryManipulator.exe - веб-интерфейс (web_app/server.py)
  * runner.exe              - подпроцесс одного прогона анализа
                               (web_app/_pipeline_runner.py), которого
                               queue_worker.py запускает вместо
                               "python _pipeline_runner.py" (см. web_app/paths.py,
                               web_app/queue_worker.py:_runner_command)

Код (web_app/*.py, analyzer_to_errors/*.py и все сторонние библиотеки)
вкомпилирован внутрь - лежит в _internal/. Пользовательские вещи остаются
СНАРУЖИ, рядом с exe (см. build_dist.py, который докладывает их после сборки):
  analyzer_to_errors/config.yaml, config.local.yaml, known_errors.json,
  data/base_analysis_scripts/*.py, prompts/*.md, sessions/, data/, output/.

Собирать: `python -m PyInstaller package.spec --noconfirm`
(лучше через build_dist.py - он же раскладывает пользовательские файлы).
"""

from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH)
WEB = ROOT / "web_app"
AN = ROOT / "analyzer_to_errors"

# Плоские модули без пакетов (analyzer_to_errors/*.py импортируются по голому
# имени через sys.path.insert - см. CLAUDE.md). PyInstaller не умеет
# статически проследить sys.path.insert(Path(...)), поэтому список - руками.
ANALYZER_MODULES = [
    "settings", "stages", "text_report", "known_filter", "script_loader",
    "normalize", "bundles", "ingest", "main", "merge_reports", "oi_agent",
    "llm_client", "llm_check", "schema", "validation", "fragment",
    "report_pdf", "full_project", "visual_stage", "tiling",
]

# chat_llm импортируется ЛЕНИВО внутри обработчика чата (тянет openai/fitz) -
# статический анализ его обычно ловит, но перечисляем явно, как и остальные
# web-модули: гарантия важнее, чем «скорее всего найдёт».
WEB_MODULES = ["paths", "sessions", "users", "queue_worker", "multipart",
               "_pipeline_runner", "chats", "chat_llm"]

# Open Interpreter крутит цикл "модель пишет код" и импортирует свои языковые
# runner'ы (core/computer/terminal/languages/*) обычными import-ами - обычный
# статический анализ modulegraph их видит, но submodules собираем на всякий
# случай явно + всё, что тянет litellm/tiktoken по капотом.
HIDDEN_PKGS = [
    "interpreter", "litellm", "tiktoken", "tiktoken_ext", "tiktoken_ext.openai_public",
]

hiddenimports = list(ANALYZER_MODULES) + list(WEB_MODULES)

from PyInstaller.utils.hooks import collect_submodules, collect_data_files, copy_metadata

for pkg in HIDDEN_PKGS:
    hiddenimports += collect_submodules(pkg)

datas = [
    (str(WEB / "static"), "web_app/static"),
]
for pkg in ("litellm", "tiktoken", "yaspin"):
    datas += collect_data_files(pkg)

# Пакеты, которые сами спрашивают у importlib.metadata свою версию (readchar
# внутри inquirer, тянущегося за open-interpreter's terminal_interface) -
# без .dist-info это падает PackageNotFoundError уже на импорте oi_agent.
for pkg in (
    "readchar", "inquirer", "open-interpreter", "litellm", "openai",
    "tiktoken", "jsonschema", "PyMuPDF", "pdfplumber", "openpyxl", "PyYAML",
):
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# НИЧЕГО НЕ ИСКЛЮЧАЕМ из тяжёлого. Open Interpreter лениво импортирует
# matplotlib/numpy/cv2 и т.п. в своём модуле computer.display: если модуль на
# машине УСТАНОВЛЕН, но исключён из сборки, frozen-импортёр PyInstaller не
# возвращает None (как обычный importlib), а роняет ModuleNotFoundError на
# самом импорте interpreter - и сервер не стартует вовсе. Дешевле включить,
# чем воевать с ленивыми импортами. pytest не нужен только потому, что его
# в поставке и так нет.
common_excludes = [
    "pytest",
]

server_a = Analysis(
    [str(WEB / "server.py")],
    pathex=[str(WEB), str(AN)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=common_excludes,
    noarchive=False,
    cipher=block_cipher,
)

runner_a = Analysis(
    [str(WEB / "_pipeline_runner.py")],
    pathex=[str(WEB), str(AN)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=common_excludes,
    noarchive=False,
    cipher=block_cipher,
)

MERGE(
    (server_a, "server", "IndustryManipulator"),
    (runner_a, "runner", "runner"),
)

server_pyz = PYZ(server_a.pure, server_a.zipped_data, cipher=block_cipher)
runner_pyz = PYZ(runner_a.pure, runner_a.zipped_data, cipher=block_cipher)

server_exe = EXE(
    server_pyz,
    server_a.scripts,
    [],
    exclude_binaries=True,
    name="IndustryManipulator",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

runner_exe = EXE(
    runner_pyz,
    runner_a.scripts,
    [],
    exclude_binaries=True,
    name="runner",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    server_exe, server_a.binaries, server_a.zipfiles, server_a.datas,
    runner_exe, runner_a.binaries, runner_a.zipfiles, runner_a.datas,
    strip=False,
    upx=False,
    name="IndustryManipulator",
)
