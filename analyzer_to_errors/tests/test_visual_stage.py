"""
Стадия зрения: геометрия цепей, суждение и фильтр против ложных срабатываний.

ЧТО ЗДЕСЬ ГЛАВНОЕ. Модель зрения не ищет ошибки - она отвечает, какие номера
подписаны у ОДНОЙ линии. Всё остальное считает геометрия и арифметика этого
модуля, и именно они решают, дойдёт находка до отчёта или нет. Ошибка здесь
стоит дороже всего: она выдаёт разные цепи за одну и печатает исправную
документацию как ошибочную.

Настоящая модель тут не запускается - подменяется функция ask (ровно так же,
как в тесте очереди подменяется скрипт-раннер): проверяется НАШЕ поведение на
её ответ, а не её поведение.

Замеры, которые эти тесты охраняют (лист 10.2 корпуса ЩСКЗ):
  * подозрений по геометрии - РОВНО ДВА, и оба настоящие («6» при четырёх «5»
    и «12» при четырёх «11»); на трёх остальных листах документа - ноль;
  * цепи на листе ПЕРЕКЛЕЕНЫ (в одну связную группу попадает полсотни
    подписей шести разных номиналов), поэтому суждение опирается не на
    «ровно два значения на цепь», а на признак самой описки - сдвиг на единицу.
"""

import json

import fitz
import pytest

import visual_stage


# --------------------------------------------------------------- суждение

@pytest.mark.parametrize("numbers, expected", [
    # обе подтверждённые ошибки листа 10.2
    (["5", "5", "5", "5", "6"], [("5", "6")]),
    (["11", "11", "11", "11", "12"], [("11", "12")]),
    # ...и они же внутри ПЕРЕКЛЕЕННОЙ цепи, где намешана половина листа
    (["4"] * 15 + ["5"] * 4 + ["6"] + ["10"] * 4 + ["11"] * 3, [("5", "6")]),
    # одиночка далеко от большинства - не описка (замер: «41» при трёх «51»)
    (["51", "51", "51", "41"], []),
    (["10", "10", "10", "10", "10", "10", "1010"], []),
    # большинства нет: двое против одного - слишком шаткое основание
    (["5", "5", "6"], []),
    # одиночки нет
    (["5", "5", "5", "6", "6"], []),
    # всё совпало - это норма, а не находка
    (["5", "5", "5", "5"], []),
    # группа слишком мала
    (["5", "6"], []),
])
def test_odd_ones_out(numbers, expected):
    assert visual_stage.odd_ones_out(numbers) == expected


def test_sequential_numbering_is_not_a_typo():
    """Клеммник, пронумерованный подряд (12-13-14-15), даёт у большинства «14»
    сразу ДВЕ соседние одиночки. Это ряд номеров, а не описка - замер на ША1:
    две ложные находки из семи. У настоящей описки зеркальный сосед либо
    отсутствует, либо не одиночка (на ЩСКЗ «4» при «5»/«6» встречается 15 раз).
    """
    assert visual_stage.odd_ones_out(["14"] * 8 + ["13", "15"]) == []
    assert visual_stage.odd_ones_out(["4"] * 15 + ["5"] * 4 + ["6"]) == [("5", "6")]


def test_repeated_pair_across_sheets_is_a_typical_node():
    """Одна и та же пара на многих листах - типовой узел, перерисованный на
    каждый агрегат (замер на ШУ-ТМ: «3» при «4» на восьми листах из 25).
    Описка так не тиражируется: обе настоящие ошибки ЩСКЗ встречаются по разу.
    """
    many = {sheet: [("4", "3")] for sheet in range(1, 9)}
    assert visual_stage._drop_repeated(many) == {("4", "3")}
    once = {1: [("5", "6")], 2: [("11", "12")]}
    assert visual_stage._drop_repeated(once) == set()


def test_parse_numbers_survives_junk():
    assert visual_stage.parse_numbers("модель ушла в рассуждения") == set()
    assert visual_stage.parse_numbers('```json\n{"numbers":["5","6"]}\n```') == {"5", "6"}
    # нечисловое в множество не идёт: маркировка провода - короткое число
    assert visual_stage.parse_numbers('{"numbers":["5","QF1","6"]}') == {"5", "6"}


# --------------------------------------------------------------- геометрия

def seg(x1, y1, x2, y2):
    axis = "h" if abs(y2 - y1) < abs(x2 - x1) else "v"
    return (fitz.Point(x1, y1), fitz.Point(x2, y2), axis)


def test_t_junction_joins_a_bus_with_its_drops():
    """Отвод отходит от СЕРЕДИНЫ шины, а не от её конца. Без T-образной
    стыковки шина с отводами рассыпается на куски, и подписи «5» на шине и «6»
    на отводе оказываются в разных группах - то есть сравнивать станет нечего.
    """
    lines = [seg(0, 100, 400, 100),        # шина
             seg(100, 100, 100, 200),      # отвод из середины
             seg(300, 100, 300, 200),      # ещё отвод
             seg(0, 500, 400, 500)]        # чужая линия, далеко
    groups = visual_stage.connect_segments(lines)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 3], "шина и её отводы обязаны быть одной цепью"


def test_markings_bind_to_the_line_they_stand_at():
    lines = [seg(0, 100, 400, 100)]
    marks = [{"text": "5", "rect": fitz.Rect(50, 92, 58, 99)},     # у линии
             {"text": "9", "rect": fitz.Rect(50, 300, 58, 307)}]   # далеко
    got = visual_stage.markings_of([0], lines, marks)
    assert [m["text"] for m in got] == ["5"]


def test_candidate_groups_finds_the_odd_label_on_a_bus():
    lines = [seg(0, 100, 400, 100),
             seg(100, 100, 100, 200),
             seg(200, 100, 200, 200),
             seg(300, 100, 300, 200)]
    marks = [{"text": "5", "rect": fitz.Rect(20, 92, 28, 99)},
             {"text": "5", "rect": fitz.Rect(104, 150, 112, 157)},
             {"text": "5", "rect": fitz.Rect(204, 150, 212, 157)},
             {"text": "6", "rect": fitz.Rect(304, 150, 312, 157)}]
    groups = visual_stage.candidate_groups(lines, marks)
    assert len(groups) == 1
    assert (groups[0]["major"], groups[0]["minor"]) == ("5", "6")
    assert groups[0]["odd"]["text"] == "6"


def test_crop_shows_the_odd_label_with_its_neighbours():
    """Кадр обязан быть МАЛЕНЬКИМ: шина тянется через весь лист, и в кадре
    шириной с лист подпись 6.3 pt снова нечитаема - ровно то, от чего уходили."""
    page_rect = fitz.Rect(0, 0, 1191, 842)
    odd = {"text": "6", "rect": fitz.Rect(304, 150, 312, 157)}
    marks = [{"text": "5", "rect": fitz.Rect(20, 92, 28, 99)},
             {"text": "5", "rect": fitz.Rect(204, 150, 212, 157)},
             {"text": "5", "rect": fitz.Rect(1100, 150, 1108, 157)},
             odd]
    box = visual_stage.crop_for(
        {"marks": marks, "major": "5", "minor": "6", "odd": odd}, page_rect)
    assert box.x0 <= odd["rect"].x0 and box.x1 >= odd["rect"].x1
    assert box.width < page_rect.width / 2, "дальняя подпись не должна раздувать кадр"


# ------------------------------------------------------- стадия и фильтры

def make_sheet_with_bus(tmp_path):
    """Лист: горизонтальная шина, три отвода, подписи 5/5/5 и ошибочная 6.

    Настоящих PDF заказчика в репозитории нет и быть не может, поэтому лист
    собирается здесь же - зато координаты и подписи известны точно.
    """
    doc = fitz.open()
    page = doc.new_page(width=600, height=400)
    page.draw_line(fitz.Point(50, 100), fitz.Point(500, 100))
    for x in (150, 250, 350):
        page.draw_line(fitz.Point(x, 100), fitz.Point(x, 200))
    page.insert_text((60, 96), "5", fontsize=6.3, fontname="helv")
    page.insert_text((154, 150), "5", fontsize=6.3, fontname="helv")
    page.insert_text((254, 150), "5", fontsize=6.3, fontname="helv")
    page.insert_text((354, 150), "6", fontsize=6.3, fontname="helv")

    data_dir = tmp_path / "doc"
    data_dir.mkdir(parents=True, exist_ok=True)
    # raw.json в том виде, в каком его пишет парсер: координаты НЕПОВЁРНУТЫЕ,
    # линии - словарями с толщиной (по ней отсеиваются контуры букв)
    lines = [{"x1": 50, "y1": 100, "x2": 500, "y2": 100, "width": 1.0}]
    lines += [{"x1": x, "y1": 100, "x2": x, "y2": 200, "width": 1.0}
              for x in (150, 250, 350)]
    (data_dir / "raw.json").write_text(
        json.dumps({"pages": [{"page_number": 1, "width": 600, "height": 400,
                               "lines": lines, "text_spans": [], "shapes": []}]}),
        encoding="utf-8")
    return doc, data_dir


def fake_ask(answer):
    calls = []

    def ask(prompt, images=()):
        calls.append((prompt, len(images)))
        return answer

    ask.calls = calls
    return ask


def run_page(tmp_path, scripts_dir, answer):
    doc, data_dir = make_sheet_with_bus(tmp_path)
    ask = fake_ask(answer)
    try:
        findings = visual_stage.analyze_page(
            ask, doc[0], "Схема", 1, data_dir, scripts_dir)
    finally:
        doc.close()
    return findings, ask


def test_confirmed_finding(tmp_path, scripts_dir):
    findings, ask = run_page(tmp_path, scripts_dir, '{"same_line":["5"]}')

    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "MISMATCH" and f["severity"] == "high"
    assert f["scope"] == "single_document"
    assert [r["marking"] for r in f["refs"]] == ["5", "6"]
    assert {r["document"] for r in f["refs"]} == {"Схема"}
    # ссылка ведёт на сохранённый кадр - то, что видела модель
    assert f["refs"][0]["source_file"].startswith(visual_stage.VISUAL_DIR + "/")
    assert (tmp_path / "doc" / visual_stage.VISUAL_DIR).is_dir()
    # модели показывают ОДИН кадр: обзорная картинка удваивала рассуждение
    assert ask.calls and ask.calls[0][1] == 1


def test_model_disagreement_downgrades_but_never_drops(tmp_path, scripts_dir):
    """САМЫЙ ВАЖНЫЙ ТЕСТ - и он изменился вместе с правилом проекта.

    Раньше несогласие модели ГАСИЛО находку. Теперь действует правило «лучше
    вдвое больше ложных, чем один пропущенный настоящий»: молча выброшенный
    кандидат - это пропуск, о котором никто никогда не узнает, а лишний REVIEW
    инженер закроет за минуту, глядя на приложенный кадр. Поэтому ответ модели
    определяет УВЕРЕННОСТЬ, а не право находки на существование.
    """
    findings, _ = run_page(tmp_path, scripts_dir, '{"same_line":["9"]}')
    assert len(findings) == 1
    assert findings[0]["kind"] == "REVIEW"
    assert findings[0]["severity"] == "low"


def test_unreadable_answer_becomes_a_question(tmp_path, scripts_dir):
    findings, _ = run_page(tmp_path, scripts_dir, "не могу разобрать")
    assert len(findings) == 1
    assert findings[0]["kind"] == "REVIEW"
    assert findings[0]["severity"] == "medium"


def test_model_failure_does_not_lose_the_finding(tmp_path, scripts_dir):
    """Упавший вызов - это отказ сервера, а не отсутствие ошибки в документе.
    Раньше находка при этом пропадала целиком: одна сетевая заминка на альбоме
    молча съедала настоящее замечание."""
    def ask(prompt, images=()):
        raise RuntimeError("сервер отвалился")

    doc, data_dir = make_sheet_with_bus(tmp_path)
    try:
        findings = visual_stage.analyze_page(
            ask, doc[0], "Схема", 1, data_dir, scripts_dir)
    finally:
        doc.close()
    assert len(findings) == 1
    assert findings[0]["kind"] == "REVIEW"
    assert findings[0]["severity"] == "medium"


def test_findings_pass_the_report_schema(tmp_path, scripts_dir):
    """Находка зрения обязана пройти ту же схему, что и ответы моделей, -
    иначе лишнее поле доедет до интерфейса пустой колонкой."""
    jsonschema = pytest.importorskip("jsonschema")
    from schema import REPORT_SCHEMA

    findings, _ = run_page(tmp_path, scripts_dir, chr(39))
    jsonschema.validate({"errors": findings}, REPORT_SCHEMA)


# ------------------------------------------------------------ режимы прогона

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
