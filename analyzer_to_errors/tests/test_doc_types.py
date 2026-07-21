"""
Определение типа документа по имени файла (ingest).

Тихая ошибка: тип не определился - документ уехал в skipped_files, сверка
связки молча не выполнилась, отчёт пустой и «всё в порядке». Поэтому границы
слова у марки вида проверяются отдельно: без них "СО" находится внутри
"СОЕДИНЕНИЙ", а "СХ" - внутри "схема".
"""

import pytest

import ingest


@pytest.mark.parametrize("filename,expected", [
    # марка в конце - как называет файлы одно бюро
    ("026.809.01.01-ИПК  ША1 Э3_10.04.26.pdf", "scheme"),
    ("026.809.01.01-ИПК ША1 СБ_08.05.26.pdf", "assembly"),
    ("026_812_19_00CJF02_ИК_3192_АТХ2_015_СО_29_04_26.xlsx", "spec"),
    # ...и в начале - как называет другое
    ("СБ_ИК.3912-АТХ2.015_01.06.2026.pdf", "assembly"),
    ("СХ_ИК.3912-АТХ2.115_05.07.2026.pdf", "scheme"),
    ("NL_ИК.3912-АТХ3.115_05.07.2026.pdf", "netlist"),
    # явная пометка важнее марки
    ("(scheme)ИК.3912-АТХ2.115.pdf", "scheme"),
    ("(спецификация)что-угодно.xlsx", "spec"),
])
def test_detects_kind_mark(filename, expected):
    assert ingest.detect_doc_type(filename) == expected


@pytest.mark.parametrize("filename", [
    # "СО" внутри слова - не марка вида
    "ТАБЛИЦА СОЕДИНЕНИЙ.pdf",
    # "СХ" внутри "схема" - тоже
    "принципиальная схема.pdf",
    # "ТМ" - часть обозначения шкафа, а не марка
    "ШУ-ТМ-14082.pdf",
    "Итог1.pdf",
])
def test_no_false_kind_mark(filename):
    assert ingest.detect_doc_type(filename) is None


def test_rightmost_mark_wins():
    """Из нескольких марок берётся самая правая: слева могло попасться
    похожее из обозначения проекта."""
    assert ingest.detect_kind_mark("026_СХ_проект_АТХ2_015_СО_29_04_26") == "СО"


def test_type_marker_stripped_from_document_name():
    from pathlib import Path
    assert ingest.document_name(Path("(scheme)ЩС2. Схема.pdf")) == "ЩС2. Схема"


def test_overrides_by_path_beat_name(tmp_path):
    """Пометка по ПУТИ адресует один файл, а не все одноимённые.

    В альбоме у каждого шкафа своя подпапка и свой «Общий вид» - по голому
    имени пометка одного документа легла бы сразу на все.
    """
    for cabinet in ("ЩС1", "ЩС2"):
        (tmp_path / cabinet).mkdir()
        (tmp_path / cabinet / "Общий вид.pdf").write_bytes(b"x")

    docs = ingest.discover_documents(tmp_path, {"ЩС1/Общий вид.pdf": "assembly"})
    by_path = {str(d["source"].relative_to(tmp_path)).replace("\\", "/"): d["doc_type"]
               for d in docs}
    assert by_path["ЩС1/Общий вид.pdf"] == "assembly"
    assert by_path["ЩС2/Общий вид.pdf"] is None


def test_wrong_extension_for_type_is_skipped(tmp_path):
    """Спецификация, помеченная как схема, не должна дойти до PDF-парсера:
    он упал бы на .xlsx невнятной ошибкой fitz."""
    (tmp_path / "спека.xlsx").write_bytes(b"x")
    docs = ingest.discover_documents(tmp_path, {"спека.xlsx": "scheme"})
    assert docs[0]["doc_type"] is None
    assert "расширение" in docs[0]["skip_reason"]


def test_excel_lockfile_ignored(tmp_path):
    """Открытая книга Excel оставляет рядом ~$файл - это не документ."""
    (tmp_path / "~$спека.xlsx").write_bytes(b"x")
    (tmp_path / "спека СО.xlsx").write_bytes(b"x")
    docs = ingest.discover_documents(tmp_path)
    assert [d["doc_type"] for d in docs] == ["spec"]


# ---------------------------------------------------------------- конфиг

def test_local_config_overrides_only_named_branches(tmp_path):
    """Адрес сервера ИИ - свойство установки, а не проекта, и живёт в
    config.local.yaml. Слияние идёт ПО ВЕТКАМ: иначе, чтобы поменять один
    base_url, пришлось бы скопировать в локальный файл весь llm_servers - и он
    тихо разъехался бы с config.yaml при первой правке модели или лимитов.
    """
    import main

    (tmp_path / "config.yaml").write_text(
        "llm_servers:\n"
        "  agent_1:\n"
        "    base_url: 'http://localhost:1234/v1'\n"
        "    model: 'общая-модель'\n"
        "agent:\n"
        "  max_code_turns: 30\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text(
        "llm_servers:\n"
        "  agent_1:\n"
        "    base_url: 'http://10.0.0.5:1234/v1'\n", encoding="utf-8")

    cfg = main.load_config(str(tmp_path / "config.yaml"))
    assert cfg["llm_servers"]["agent_1"]["base_url"] == "http://10.0.0.5:1234/v1"
    assert cfg["llm_servers"]["agent_1"]["model"] == "общая-модель"
    assert cfg["agent"]["max_code_turns"] == 30


def test_config_works_without_local_file(tmp_path):
    """Локального файла может не быть - это нормальная установка «всё на этой
    машине», и падать из-за его отсутствия нельзя."""
    import main

    (tmp_path / "config.yaml").write_text("paths:\n  output_dir: './out'\n",
                                          encoding="utf-8")
    assert main.load_config(str(tmp_path / "config.yaml"))["paths"]["output_dir"] == "./out"
