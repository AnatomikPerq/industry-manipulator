"""
Стадия зрения: суждение по ответу модели и фильтры против выдумок.

ЧТО ЗДЕСЬ ГЛАВНОЕ. Модель зрения не ищет ошибки - она отвечает, какие номера
подписаны у одной линии. Всё остальное считает арифметика этого модуля, и
именно она решает, попадёт находка в отчёт или нет. Ошибка здесь стоит дороже
всего: она выдаёт выдумку модели за подтверждённую ошибку документации.

Настоящая модель тут не нужна и не запускается - подменяется только функция
ask (ровно так же, как в тесте очереди подменяется скрипт-раннер): проверяется
НАШЕ поведение на её ответ, а не её поведение.

Три проверяемых обещания:
  * группа, в которой большинство подписей одинаково, а одна отличается, даёт
    находку (это две подтверждённые ошибки листа 10.2 ЩСКЗ: «6» вместо «5» и
    «12» вместо «11»);
  * число, которого на листе НЕ НАПЕЧАТАНО, находки не даёт никогда - сколько
    бы уверенно модель его ни называла;
  * лист без текстового слоя (надписи начерчены линиями) находку даёт, но как
    REVIEW - подтвердить её нечем, и выдавать её за факт нельзя.
"""

import json

import fitz
import pytest

import visual_stage

BIG_BUDGET = 8_000_000          # чтобы маленький лист укладывался в один тайл


def make_page(numbers=("5", "5", "5", "5", "6"), with_text=True):
    """Лист с подписями в одну строку. Рамка - чтобы тайл не сочли пустым."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=300)
    page.draw_rect(fitz.Rect(10, 10, 390, 290), color=(0, 0, 0), width=1)
    page.draw_line(fitz.Point(20, 150), fitz.Point(380, 150))
    if with_text:
        for i, n in enumerate(numbers):
            page.insert_text((30 + i * 60, 140), n, fontsize=6.3, fontname="helv")
        for i in range(3):
            page.insert_text((30 + i * 100, 250), "Наименование",
                             fontsize=6.3, fontname="helv")
    return doc


def answer_with(numbers, where="горизонтальная линия"):
    """Ответ модели в том виде, в каком он приходит: JSON внутри ```."""
    payload = {"circuits": [{"where": where, "numbers": list(numbers)}]}
    return "Вот что видно:\n```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def fake_ask(answer):
    calls = []

    def ask(prompt, images=()):
        calls.append((prompt, len(images)))
        return answer

    ask.calls = calls
    return ask


# ---------------------------------------------------------------- суждение

@pytest.mark.parametrize("numbers, expected", [
    (["5", "5", "5", "5", "6"], ("5", "6")),      # подтверждённая ошибка ЩСКЗ
    (["11", "11", "11", "11", "12"], ("11", "12")),
    (["5", "6"], None),                            # группа из двух ничего не доказывает
    (["5", "5", "6"], None),                       # большинство из двух - слишком шатко
    (["5", "5", "5", "6", "7"], None),             # разнобой: модель не разобрала лист
    (["5", "5", "5", "5"], None),                  # всё совпало - это норма
    (["5", "5", "6", "6"], None),                  # не одиночка
])
def test_odd_one_out(numbers, expected):
    assert visual_stage.odd_one_out(numbers) == expected


def test_parse_circuits_survives_junk():
    assert visual_stage.parse_circuits("модель ушла в рассуждения") == []
    assert visual_stage.parse_circuits(answer_with(["5", "5", "5", "6"])) == [
        ("горизонтальная линия", ["5", "5", "5", "6"])]
    # нечисловые подписи в группу не идут: маркировка провода - короткое число
    mixed = answer_with(["5", "5", "5", "QF1", "6"])
    assert visual_stage.parse_circuits(mixed) == [
        ("горизонтальная линия", ["5", "5", "5", "6"])]


# ---------------------------------------------------------------- фильтры

def test_confirmed_finding(tmp_path):
    doc = make_page()
    try:
        ask = fake_ask(answer_with(["5", "5", "5", "5", "6"]))
        findings = visual_stage.analyze_page(
            ask, doc[0], "Итог1", 1, tmp_path / "visual",
            max_tile_pixels=BIG_BUDGET)
    finally:
        doc.close()

    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "MISMATCH" and f["severity"] == "high"
    assert f["scope"] == "single_document"
    assert [r["marking"] for r in f["refs"]] == ["5", "6"]
    assert {r["document"] for r in f["refs"]} == {"Итог1"}
    assert all(r["sheet"] == 1 for r in f["refs"])
    # ссылка ведёт на сохранённый тайл - то, что видела модель
    assert f["refs"][0]["source_file"].startswith(visual_stage.VISUAL_DIR + "/")
    assert (tmp_path / "visual").is_dir()
    # модели показывают лист целиком И фрагмент: без обзора тайл читается вслепую
    assert ask.calls and ask.calls[0][1] == 2


def test_invented_number_is_dropped(tmp_path):
    """Самый дорогой отказ: модель уверенно называет подписи, которых на листе
    нет. Такая группа не должна доезжать до отчёта ни при каких условиях."""
    doc = make_page()
    try:
        ask = fake_ask(answer_with(["7", "7", "7", "7", "8"]))
        findings = visual_stage.analyze_page(
            ask, doc[0], "Итог1", 1, tmp_path / "visual",
            max_tile_pixels=BIG_BUDGET)
    finally:
        doc.close()
    assert findings == []


def test_half_invented_group_is_dropped(tmp_path):
    """Большинство прочитано верно, а одиночка выдумана - и это ровно тот
    случай, который выглядел бы как настоящая находка."""
    doc = make_page()
    try:
        ask = fake_ask(answer_with(["5", "5", "5", "5", "9"]))
        findings = visual_stage.analyze_page(
            ask, doc[0], "Итог1", 1, tmp_path / "visual",
            max_tile_pixels=BIG_BUDGET)
    finally:
        doc.close()
    assert findings == []


def test_sheet_without_text_layer_gives_review(tmp_path):
    """Надписи начерчены линиями - подтвердить прочитанное нечем. Находка
    остаётся, но как вопрос инженеру, а не как утверждение об ошибке."""
    doc = make_page(with_text=False)
    try:
        ask = fake_ask(answer_with(["5", "5", "5", "5", "6"]))
        findings = visual_stage.analyze_page(
            ask, doc[0], "Итог1", 1, tmp_path / "visual",
            max_tile_pixels=BIG_BUDGET)
    finally:
        doc.close()

    assert len(findings) == 1
    assert findings[0]["kind"] == "REVIEW"
    assert findings[0]["severity"] == "medium"


def test_model_failure_does_not_lose_the_sheet(tmp_path):
    """Упавший вызов одного тайла не должен ронять разбор листа целиком."""
    def ask(prompt, images=()):
        raise RuntimeError("сервер отвалился")

    doc = make_page()
    try:
        findings = visual_stage.analyze_page(
            ask, doc[0], "Итог1", 1, tmp_path / "visual",
            max_tile_pixels=BIG_BUDGET)
    finally:
        doc.close()
    assert findings == []


@pytest.mark.parametrize("mode, skip_agents, visual", [
    ("scripts", True, False),        # ни агентов, ни зрения - к серверу ИИ не идём
    ("full", False, False),          # как было до появления зрения
    ("visual", True, True),          # зрение без текстовых агентов
    ("full_visual", False, True),    # всё сразу
])
def test_run_flags(mode, skip_agents, visual):
    """Режим интерфейса раскладывается на два независимых переключателя.

    Ловится тихий отказ: перепутанный флаг не роняет прогон, а меняет ЧТО
    считалось - и заметить это можно только по отсутствию находок, которых
    никто не ждал.
    """
    from queue_worker import MODES, run_flags

    assert mode in MODES
    assert run_flags(mode) == {"skip_agents": skip_agents, "visual": visual}


def test_findings_pass_the_report_schema(tmp_path):
    """Находка зрения обязана пройти ту же схему, что и ответы моделей, -
    иначе лишнее поле доедет до интерфейса пустой колонкой."""
    jsonschema = pytest.importorskip("jsonschema")
    from schema import REPORT_SCHEMA

    doc = make_page()
    try:
        ask = fake_ask(answer_with(["5", "5", "5", "5", "6"]))
        findings = visual_stage.analyze_page(
            ask, doc[0], "Итог1", 1, tmp_path / "visual",
            max_tile_pixels=BIG_BUDGET)
    finally:
        doc.close()
    jsonschema.validate({"errors": findings}, REPORT_SCHEMA)
