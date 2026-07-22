#!/usr/bin/env python3
"""
Стадия ЗРЕНИЯ: модель смотрит на растр листа и решает то, чего не может решить
геометрия, - КАКОЙ ЛИНИИ ПРИНАДЛЕЖИТ ПОДПИСЬ.

ЗАЧЕМ ЭТО ВООБЩЕ. Проверка маркировки проводов была написана детерминированно и
ОТВЕРГНУТА: 250 ложных находок на трёх файлах (см. «не вошло» в
schematic_rules.py). Замер на листе 10.2 ЩСКЗ показывает, где именно она
ломалась: цепь p1_n8 собрала 39 отрезков и маркировки ['1','10','1010','4','5'] -
шины +24 В и 0 В слиплись в одну цепь, потому что подпись привязывается к цепи
ПО РАДИУСУ. Неверна была привязка, а не суждение: на верной привязке правило
«все подписи одной цепи совпадают, одиночка среди одинаковых - ошибка» даёт на
этом листе ровно две находки, обе подтверждённые (напечатано «6» там, где
должно быть «5», и «12» там, где «11»), и ни одной ложной.

ПОЧЕМУ КАДР - ОКРЕСТНОСТЬ ПОДОЗРИТЕЛЬНОГО МЕСТА, А НЕ КЛЕТКА СЕТКИ. Первая
версия резала лист сеткой 3x4 и просила модель самой разложить подписи по
линиям. Замерено на двух моделях: этого не может ни одна.

    Qwen3 (думающая):  простой вопрос «какие номера здесь» - 20 c, ответ верный;
                       «разложи по линиям» - рассуждение на весь лимит, пусто.
    Gemma 4 31B:       простой вопрос - 21 c на разреженном тайле, 27 c на
                       плотном, ответы верные; «разложи по линиям» - на плотном
                       тайле 91 c и пусто.

Группировка - самая дорогая часть задачи, и поручать её модели незачем:
СВЯЗНОСТЬ ОТРЕЗКОВ ДЕЛАЕТ ЕЁ ГЕОМЕТРИЕЙ (connect_segments). Модели остаётся
маленький кадр и вопрос, привязанный к одной подписи.

ЧТО ОТ ЧЕГО ЗАВИСИТ:
  * ЛИНИИ и ПОДПИСИ берём из документа (парсер + текстовый слой) - это точно;
  * ПОДОЗРЕНИЕ считает арифметика (odd_ones_out) - воспроизводимо и тестируемо;
    геометрия и арифметика ВПРАВЕ отвергнуть кандидата по замеренному признаку,
    иначе находкой станет любая цепь с двумя разными номерами;
  * МОДЕЛЬ вызывается ТОЛЬКО на подозрительные места и задаёт УВЕРЕННОСТЬ -
    подтверждено / не удалось спросить / не подтвердилось. Выбросить находку
    она НЕ МОЖЕТ: по правилу проекта пропуск дороже вдвое большего числа
    ложных, а молча выброшенный кандидат - это пропуск, о котором никто
    никогда не узнает.

Отсюда и цена: вызовов не «сетка × листы», а «сколько подозрительных мест».
На листе 10.2 их два, а не сотня.
"""

import json
import logging
import re
from collections import Counter
from pathlib import Path

import fitz

import script_loader
import tiling
from settings import PROJECT_ROOT, resolve_path, resolve_vision_cfg

logger = logging.getLogger("error_analyzer")

# Подпись провода - короткое число. Длинные числа на листе есть (номер
# документа, год), но маркировкой не бывают.
MARKING_RE = re.compile(r"^\d{1,4}$")

# Сколько подписей должно быть в полосе, чтобы «одиночка среди одинаковых»
# что-то значила, и каким должно быть большинство. На листе 10.2 обе настоящие
# ошибки лежат в группах из пяти (4 верных + 1 неверная); двое против одного -
# слишком шаткое основание, чтобы утверждать, что неправ именно одиночка.
MIN_GROUP = 3
MIN_MAJORITY = 3

# Полуширина полосы вокруг линии, pt. Номер провода стоит вплотную к нему;
# шире - и в полосу лезут подписи соседней линии (шины на схеме идут в 20-30 pt
# друг от друга), уже - теряются подписи, сдвинутые от линии. Замер на листе
# 10.2: все настоящие подписи стоят в 5.7-7.3 pt от своей линии.
BAND_HALF_PT = 11.0

# Совсем короткие штрихи - засечки, стрелки, обводка рамки. Отводы шины бывают
# от 70 pt, выводы аппаратов - от 14, поэтому порог низкий: он отсекает мусор,
# а не длину.
MIN_LINE_PT = 8.0

# Допуск стыковки отрезков, pt. CAD рисует шину и её отвод отдельными
# отрезками, и «одна цепь» - это связная группа: без стыковки подписи «5» на
# горизонтальной шине и «6» на отходящем от неё вертикальном отводе оказались
# бы в разных группах, а именно их и надо сравнивать (замер на листе 10.2:
# четыре «5» разнесены по шине и трём отводам).
JOIN_TOLERANCE_PT = 2.0

# Отклонение от строгой горизонтали/вертикали, при котором линия ещё считается
# прямой. Схемы чертят по осям; наклонная линия - это выноска или рамка.
AXIS_TOLERANCE_PT = 1.0

# Кадр вокруг подозрительного места: одиночка плюс ближайшие соседи по линии.
# Показывать полосу целиком незачем - шина тянется через весь лист, и в кадре
# 1191 pt подпись 6.3 pt снова становится нечитаемой.
NEIGHBOURS_EACH_SIDE = 2
CROP_MARGIN_PT = 26.0

# Папка с кропами внутри папки документа. Путь до кропа кладётся в
# ref.source_file - поле существующее и означает ровно это, так что схему
# находки менять не нужно, а кнопка «фрагмент» показывает РОВНО ТО, что видела
# модель, вместо повторного поиска по тексту.
VISUAL_DIR = "visual"

# ПРОМПТ КОРОТКИЙ: каждая лишняя оговорка стоит десятков секунд НА КАЖДОМ кадре -
# думающая модель тратит рассуждение на согласование инструкций между собой, а
# не на разглядывание схемы (замер: 20 строк против 4 - 186 секунд против 6).
#
# ВОПРОС ПРИВЯЗАН К КОНКРЕТНОЙ ПОДПИСИ, а не «перечисли всё, что видишь».
# Замер на корпусе ложных срабатываний: «какие номера видишь» подтверждает
# что угодно - оба числа ведь напечатаны в кадре, - а «проследи линию, у
# которой стоит вот эта подпись» отсеяло 4 ложных кандидата из 7 на ША1.
BAND_PROMPT = """На изображении - фрагмент принципиальной электрической схемы.

Найди подпись «{minor}» и проследи линию связи (провод), у которой она стоит.
Какие ещё номера подписаны у ЭТОЙ ЖЕ линии? Считай одной линией все её
ответвления. Номера у СОСЕДНИХ линий не включай.

Только JSON: {{"same_line": ["номер", "номер"]}}
"""


def _extract_json(text: str) -> dict:
    """JSON из ответа модели, которая почти наверняка обернёт его в ```."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}


def parse_numbers(answer: str) -> set:
    """Ответ модели -> множество прочитанных номеров. Мусор молча отбрасывается:
    модель может вернуть что угодно, и падать из-за этого стадия не должна.

    Ключ принимаем и "same_line" (как просит промпт), и "numbers": модель
    регулярно называет список по-своему, а ронять из-за этого находку - значит
    получить пропуск на ровном месте.
    """
    data = _extract_json(answer)
    values = data.get("same_line")
    if not isinstance(values, list):
        values = data.get("numbers")
    if not isinstance(values, list):
        return set()
    return {str(v).strip() for v in values if MARKING_RE.match(str(v).strip())}


def odd_ones_out(numbers) -> list:
    """Пары (большинство, одиночка), похожие на описку в номере провода.

    ПОЧЕМУ НЕ «РОВНО ДВА ЗНАЧЕНИЯ НА ЦЕПЬ». Так и было задумано, и так не
    работает: цепи на листе ПЕРЕКЛЕЕНЫ. Замер на листе 10.2 - все четыре «5» и
    ошибочная «6» действительно попадают в одну связную группу, но вместе с
    ними туда попадает и половина листа: значения ['4','5','6','10','11','1010'],
    28 отрезков. Требование «ровно два значения» на такой группе молчит всегда,
    а разделить группы честно значит переписать сборку цепей - ту самую, что
    уже дважды переделывалась.

    ПРИЗНАК САМОЙ ОШИБКИ УСТОЙЧИВЕЕ СТРУКТУРЫ. Описка в номере провода - это
    сдвиг на единицу: инженер скопировал соседний отвод и не поправил номер
    либо поправил лишний раз. Обе подтверждённые ошибки листа 10.2 ровно такие
    («6» там, где четыре «5», и «12» там, где четыре «11»), и на переклеенной
    цепи этот признак вылавливает их обе, не поднимая соседей: у «41» при трёх
    «51» разница 10, у «1010» при шести «10» - тоже не единица.

    Требуется: значение встречается РОВНО РАЗ, у соседнего по величине -
    не меньше MIN_MAJORITY раз. Всё остальное - недостаточное основание.
    """
    counts = Counter(numbers)
    if sum(counts.values()) < MIN_GROUP:
        return []
    majors = [v for v, n in counts.items() if n >= MIN_MAJORITY]
    singles = [v for v, n in counts.items() if n == 1]
    out = []
    for minor in singles:
        for major in majors:
            try:
                if abs(int(major) - int(minor)) != 1:
                    continue
                # ПОСЛЕДОВАТЕЛЬНАЯ НУМЕРАЦИЯ, А НЕ ОПИСКА. Если по ДРУГУЮ
                # сторону от большинства стоит такая же одиночка, перед нами
                # ряд подряд идущих номеров (клеммник 12-13-14-15), а не
                # ошибка. Замер на ША1: у большинства «14» одиночками идут и
                # «13», и «15» - две ложные находки из семи.
                mirror = str(2 * int(major) - int(minor))
                if counts.get(mirror) == 1:
                    continue
                out.append((major, minor))
            except ValueError:          # не число - не наш случай
                continue
    return out


# --------------------------------------------------------------------------
# Геометрия: линии листа и подписи на них
# --------------------------------------------------------------------------

def page_markings(page) -> list:
    """Подписи-номера листа: [{"text", "rect"}] в ПОКАЗЫВАЕМОМ пространстве.

    Поворот: get_text отдаёт координаты неповёрнутыми, а рисует get_pixmap в
    показываемых - переводим сразу, чтобы дальше всё жило в одной системе
    (см. tiling и предостережение в fragment.py).
    """
    matrix = page.rotation_matrix
    out = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if MARKING_RE.match(text):
                    out.append({"text": text,
                                "rect": fitz.Rect(span["bbox"]) * matrix})
    return out


def wire_lines(page, data_dir, scripts_dir) -> list:
    """Прямые длинные линии листа - кандидаты в шины, в показываемом пространстве.

    Берём УЖЕ ИЗВЛЕЧЁННЫЕ парсером линии (raw.json), а не зовём get_drawings
    заново: на густом листе это тысячи примитивов, и второй разбор того же
    самого - лишняя минута на каждом листе.

    КОНТУРЫ БУКВ ОТСЕИВАЕМ ОБЩИМ ФИЛЬТРОМ парсера (drop_glyph_hairlines): на
    листе 10.2 из 7423 «линий» настоящих проводов 169, остальное - обводка
    текста кривыми. Своя копия этого фильтра разъехалась бы с оригиналом.

    В raw.json координаты НЕПОВЁРНУТЫЕ (у подписи «6» листа 10.2 y=975 при
    высоте показываемого листа 842) - переводим их через rotation_matrix.
    """
    raw_path = Path(data_dir) / "raw.json"
    if not raw_path.is_file():
        logger.warning("  нет raw.json в %s - линии листа взять неоткуда", data_dir)
        return []

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    pages = raw.get("pages") or []
    index = page.number
    if index >= len(pages):
        return []
    lines = pages[index].get("lines") or []

    parser = script_loader.try_load(scripts_dir, "schematic_diagram_to_data.py")
    if parser is not None and hasattr(parser, "drop_glyph_hairlines"):
        # возвращает ПАРУ: оставшиеся линии и сколько выброшено
        lines, dropped = parser.drop_glyph_hairlines(lines)
        logger.debug("  лист %d: контуров букв отброшено %d, линий осталось %d",
                     index + 1, dropped, len(lines))

    matrix = page.rotation_matrix
    out = []
    for line in lines:
        p1 = fitz.Point(line["x1"], line["y1"]) * matrix
        p2 = fitz.Point(line["x2"], line["y2"]) * matrix
        dx, dy = abs(p2.x - p1.x), abs(p2.y - p1.y)
        if dx < AXIS_TOLERANCE_PT and dy >= MIN_LINE_PT:
            out.append((p1, p2, "v"))
        elif dy < AXIS_TOLERANCE_PT and dx >= MIN_LINE_PT:
            out.append((p1, p2, "h"))
    return out


def band_rect(p1, p2, axis) -> "fitz.Rect":
    """Полоса вдоль отрезка: сам отрезок плюс поля по перпендикуляру."""
    x0, x1 = sorted((p1.x, p2.x))
    y0, y1 = sorted((p1.y, p2.y))
    if axis == "h":
        return fitz.Rect(x0, y0 - BAND_HALF_PT, x1, y1 + BAND_HALF_PT)
    return fitz.Rect(x0 - BAND_HALF_PT, y0, x1 + BAND_HALF_PT, y1)


def _touching(seg, other) -> bool:
    """Стыкуются ли два отрезка - концами или T-образно.

    T-образная стыковка обязательна: отвод отходит от СЕРЕДИНЫ шины, а не от её
    конца, и без этого случая шина с отводами рассыпается на десяток кусков.
    """
    (a1, a2, a_axis), (b1, b2, b_axis) = seg, other
    t = JOIN_TOLERANCE_PT
    for p in (a1, a2):
        for q in (b1, b2):
            if abs(p.x - q.x) <= t and abs(p.y - q.y) <= t:
                return True
    for p in (a1, a2):                       # конец A лежит на теле B
        if b_axis == "h":
            if (abs(p.y - b1.y) <= t
                    and min(b1.x, b2.x) - t <= p.x <= max(b1.x, b2.x) + t):
                return True
        elif (abs(p.x - b1.x) <= t
              and min(b1.y, b2.y) - t <= p.y <= max(b1.y, b2.y) + t):
            return True
    for q in (b1, b2):                       # и наоборот
        if a_axis == "h":
            if (abs(q.y - a1.y) <= t
                    and min(a1.x, a2.x) - t <= q.x <= max(a1.x, a2.x) + t):
                return True
        elif (abs(q.x - a1.x) <= t
              and min(a1.y, a2.y) - t <= q.y <= max(a1.y, a2.y) + t):
            return True
    return False


def connect_segments(lines) -> list:
    """Связные группы отрезков - «цепи» листа. Возвращает список списков индексов.

    ЗАЧЕМ СВОЯ СВЯЗНОСТЬ, А НЕ nets.json. Та строится с большими допусками и на
    листе 10.2 склеивает шины +24 В и 0 В в одну цепь из 39 отрезков с
    маркировками ['1','10','1010','4','5'] - именно это и сделало правило
    непригодным. Здесь допуск жёсткий (JOIN_TOLERANCE_PT), а от остатков
    переклейки защищает само суждение: у переклеенной цепи РАЗНЫХ значений
    больше двух, и odd_ones_out на ней молчит.

    Перебор пар ограничен корзинами по координате оси: на листе схемы отрезков
    бывает до пяти тысяч, и честный квадрат стоил бы десятки секунд на лист.
    """
    from collections import defaultdict

    buckets = defaultdict(list)
    for i, (p1, p2, axis) in enumerate(lines):
        lo, hi = (sorted((p1.y, p2.y)) if axis == "h" else sorted((p1.x, p2.x)))
        for cell in range(int((lo - BAND_HALF_PT) // 20), int((hi + BAND_HALF_PT) // 20) + 1):
            buckets[(axis, cell)].append(i)
        # отрезок «поперёк» тоже должен встретиться со своими соседями
        other = "v" if axis == "h" else "h"
        span_lo, span_hi = (sorted((p1.x, p2.x)) if axis == "h" else sorted((p1.y, p2.y)))
        for cell in range(int(span_lo // 20), int(span_hi // 20) + 1):
            buckets[(other, cell)].append(i)

    parent = list(range(len(lines)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for members in buckets.values():
        for pos, i in enumerate(members):
            for j in members[pos + 1:]:
                if find(i) != find(j) and _touching(lines[i], lines[j]):
                    union(i, j)

    groups = defaultdict(list)
    for i in range(len(lines)):
        groups[find(i)].append(i)
    return list(groups.values())


def markings_of(component, lines, markings) -> list:
    """Подписи, стоящие вдоль отрезков этой цепи."""
    bands = [band_rect(*lines[i]) for i in component]
    out = []
    for m in markings:
        centre = fitz.Point((m["rect"].x0 + m["rect"].x1) / 2,
                            (m["rect"].y0 + m["rect"].y1) / 2)
        if any(centre in b for b in bands):
            out.append(m)
    return out


def candidate_groups(lines, markings) -> list:
    """Цепи, вдоль которых подписи расходятся: [{marks, major, minor, odd}].

    Считается арифметикой, без модели: сюда попадает то, что СТОИТ показать.
    """
    out = []
    for component in connect_segments(lines):
        marks = markings_of(component, lines, markings)
        for major, minor in odd_ones_out([m["text"] for m in marks]):
            odd = next(m for m in marks if m["text"] == minor)
            # соседями по кадру должны быть подписи БОЛЬШИНСТВА, а не вся цепь:
            # в переклеенной группе их полсотни, и кадр вышел бы в весь лист
            kin = [m for m in marks if m["text"] in (major, minor)]
            out.append({"marks": kin, "major": major, "minor": minor, "odd": odd})
    return out


def crop_for(group, page_rect) -> "fitz.Rect":
    """Кадр вокруг одиночки: она плюс ближайшие подписи той же цепи.

    Цепь целиком показывать нельзя: шина тянется через весь лист, и в кадре
    шириной 1191 pt подпись в 6.3 pt снова нечитаема - ровно то, от чего
    уходили тайлингом. Соседей берём по расстоянию: цепь идёт и вдоль, и
    поперёк листа, «слева-справа» на ней не определено.
    """
    odd = group["odd"]
    ox = (odd["rect"].x0 + odd["rect"].x1) / 2
    oy = (odd["rect"].y0 + odd["rect"].y1) / 2

    def distance(m):
        cx = (m["rect"].x0 + m["rect"].x1) / 2
        cy = (m["rect"].y0 + m["rect"].y1) / 2
        return (cx - ox) ** 2 + (cy - oy) ** 2

    nearest = sorted((m for m in group["marks"] if m is not odd), key=distance)
    box = fitz.Rect(odd["rect"])
    for m in nearest[:NEIGHBOURS_EACH_SIDE]:
        box |= m["rect"]

    box = fitz.Rect(box.x0 - CROP_MARGIN_PT, box.y0 - CROP_MARGIN_PT,
                    box.x1 + CROP_MARGIN_PT, box.y1 + CROP_MARGIN_PT)
    box &= page_rect
    return box


# --------------------------------------------------------------------------
# Находка
# --------------------------------------------------------------------------

# Три исхода проверки моделью. Определяют УВЕРЕННОСТЬ находки, но НЕ ПРАВО НА
# СУЩЕСТВОВАНИЕ: правило проекта - лучше вдвое больше ложных, чем один
# пропущенный настоящий. Молча выброшенный кандидат - это пропуск, о котором
# никто никогда не узнает, а лишняя REVIEW - вопрос инженеру, который он
# закроет за минуту, глядя на приложенный кадр.
CONFIRMED = "confirmed"    # модель видит одиночку рядом с большинством
UNANSWERED = "unanswered"  # модель не ответила (отказ, нечитаемый ответ)
DENIED = "denied"          # модель прочитала кадр и большинства там не увидела

_VERDICT = {
    CONFIRMED: ("MISMATCH", "high",
                "принадлежность подписей одной линии подтверждена нейросетью "
                "по растру листа"),
    UNANSWERED: ("REVIEW", "medium",
                 "нейросеть не смогла прочитать этот участок, подтвердить "
                 "принадлежность подписей одной линии нечем"),
    DENIED: ("REVIEW", "low",
             "нейросеть на этом участке разных номеров у одной линии не увидела - "
             "возможно, подписи относятся к соседним линиям"),
}


def _finding(document: str, sheet: int, major: str, minor: str,
             source_file: str, verdict: str = CONFIRMED) -> dict:
    """Находка о разной маркировке вдоль одной линии.

    scope=single_document: ошибка внутри одного документа. kind=MISMATCH - из
    перечисленных в схеме именно он означает «данные не совпадают»; в описании
    схемы MISMATCH приведён как междокументный, но на маршрутизацию в
    интерфейсе kind не влияет (там смотрят на типы документов в refs), а
    выдавать разную маркировку за DUPLICATE или FORMAT было бы прямой неправдой.
    Неподтверждённое идёт как REVIEW - вопрос инженеру, а не утверждение.

    Два ref'а на ОДИН документ - ровно так схема описывает находку о двух
    местах внутри одного документа.
    """
    kind, severity, explanation = _VERDICT[verdict]
    return {
        "kind": kind,
        "scope": "single_document",
        "severity": severity,
        "type": "Разная маркировка проводов на одной цепи",
        "refs": [
            {"document": document, "doc_type": "scheme", "source_file": source_file,
             "sheet": sheet, "marking": major,
             "found": f"вдоль линии подписано «{major}» (большинство подписей)"},
            {"document": document, "doc_type": "scheme", "source_file": source_file,
             "sheet": sheet, "marking": minor,
             "found": f"на той же линии одна подпись «{minor}»"},
        ],
        "finding": (
            f"На листе {sheet} вдоль одной линии связи подписаны разные номера "
            f"провода: «{major}» и одна подпись «{minor}». Подписи и их привязка "
            f"к линии взяты из чертежа; {explanation}."),
        "action": (f"Посмотреть приложенный кадр: если «{minor}» стоит у той же "
                   f"линии, что и «{major}», привести его к «{major}»; если у "
                   f"соседней - замечание снять."),
        "evidence": f"кадр {source_file}: подписи одной линии - {major} и {minor}",
    }


# --------------------------------------------------------------------------
# Стадия
# --------------------------------------------------------------------------

def analyze_page(ask, page, document: str, sheet: int, data_dir, scripts_dir,
                 cap_px: float = tiling.DEFAULT_CAP_PX,
                 save_crops: bool = True, reporter=None,
                 sheets_total: int = 1, skip_pairs=()) -> list:
    """Один лист: подозрительные цепи -> вопрос модели -> находки.

    skip_pairs - пары «большинство/одиночка», признанные типовым узлом по
    документу целиком (см. analyze_document).
    """
    lines = wire_lines(page, data_dir, scripts_dir)
    marks = page_markings(page)
    bands = [b for b in candidate_groups(lines, marks)
             if (b["major"], b["minor"]) not in set(skip_pairs)]
    logger.info("  лист %s: линий %d, подписей %d, подозрительных цепей %d",
                sheet, len(lines), len(marks), len(bands))
    if not bands:
        return []

    zoom, _ = tiling.zoom_for(page, cap_px)
    visual_dir = Path(data_dir) / VISUAL_DIR
    findings = []

    for number, band in enumerate(bands, start=1):
        if reporter:
            reporter.page(sheet, sheets_total,
                          stage=f"визуальная проверка, место {number} из {len(bands)}")

        clip = crop_for(band, page.rect)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        png = pix.tobytes("png")
        rel = f"{VISUAL_DIR}/л{sheet}_м{number}.png"
        if save_crops:
            visual_dir.mkdir(parents=True, exist_ok=True)
            (visual_dir / Path(rel).name).write_bytes(png)

        major, minor = band["major"], band["minor"]

        # ОТВЕТ МОДЕЛИ - ЭТО УВЕРЕННОСТЬ, А НЕ ПРАВО НА СУЩЕСТВОВАНИЕ.
        # Значения мы уже знаем из текстового слоя; модель спрашивают ради
        # одного: видит ли она одиночку РЯДОМ с большинством, то есть на ТОЙ ЖЕ
        # линии. Не видит - находка остаётся, но как вопрос (REVIEW), а не как
        # утверждение. Молча выбросить её нельзя: по правилу проекта пропущенная
        # настоящая ошибка дороже вдвое большего числа ложных, а выброшенный
        # кандидат - это пропуск, о котором никто никогда не узнает.
        try:
            answer = ask(BAND_PROMPT.format(minor=minor), [png])
        except Exception as e:  # noqa: BLE001 - отказ на одном месте не теряет лист
            logger.warning("  лист %s, место %d: модель не ответила (%s) - "
                           "находка остаётся вопросом инженеру", sheet, number, e)
            findings.append(_finding(document, sheet, major, minor, rel, UNANSWERED))
            continue

        seen = parse_numbers(answer)
        if not seen:
            logger.info("  лист %s, место %d: ответ модели не разобран - REVIEW",
                        sheet, number)
            verdict = UNANSWERED
        elif major in seen:
            verdict = CONFIRMED
            logger.info("  лист %s, место %d: ПОДТВЕРЖДЕНО («%s» на одной линии с «%s»)",
                        sheet, number, minor, major)
        else:
            verdict = DENIED
            logger.info("  лист %s, место %d: не подтверждено - у линии с «%s» модель "
                        "видит %s, а не «%s»", sheet, number, minor,
                        sorted(seen), major)
        findings.append(_finding(document, sheet, major, minor, rel, verdict))

    return findings


# На скольких листах документа пара «большинство/одиночка» должна повториться,
# чтобы считаться ТИПОВЫМ ФРАГМЕНТОМ, а не опиской.
#
# Замер на ШУ-ТМ: пара «4»/«3» встречается на ВОСЬМИ листах из 25, каждый раз с
# одинаковым кадром 70x106 pt - это один и тот же типовой узел, перерисованный
# на каждый агрегат. Описка так не тиражируется: обе настоящие ошибки ЩСКЗ
# встречаются ровно по разу. Тот же приём уже применён в
# schematic_rules.rule_duplicate_terminal_address (повторно изображённый
# клеммник чужого шкафа) и в assembly_rules (парный лист).
REPEATED_ON_SHEETS = 3


def _drop_repeated(per_sheet: dict) -> set:
    """Пары, повторяющиеся на многих листах документа, - типовой узел."""
    seen = Counter()
    for pairs in per_sheet.values():
        for pair in set(pairs):
            seen[pair] += 1
    return {pair for pair, n in seen.items() if n >= REPEATED_ON_SHEETS}


def analyze_document(ask, document: str, pdf_path, data_dir, scripts_dir,
                     pages=None, cap_px: float = tiling.DEFAULT_CAP_PX,
                     reporter=None) -> list:
    """Все листы одного документа. pages - номера листов с единицы (None = все).

    ТИПОВЫЕ УЗЛЫ ОТСЕИВАЮТСЯ ЗДЕСЬ, а не в analyze_page: повторяемость видна
    только по документу целиком. Поэтому сначала считается геометрия по всем
    листам (это доли секунды на лист), и только уцелевшие места показываются
    модели - иначе восемь одинаковых ложных мест ШУ-ТМ стоили бы восьми
    вызовов по минуте каждый.
    """
    pdf = fitz.open(str(pdf_path))
    try:
        sheets = list(pages) if pages else list(range(1, pdf.page_count + 1))

        per_sheet = {}
        for sheet in sheets:
            page = pdf[sheet - 1]
            groups = candidate_groups(
                wire_lines(page, data_dir, scripts_dir), page_markings(page))
            per_sheet[sheet] = [(g["major"], g["minor"]) for g in groups]

        repeated = _drop_repeated(per_sheet)
        if repeated:
            logger.info("  %s: типовые узлы (повторяются на %d+ листах), не "
                        "проверяются: %s", document, REPEATED_ON_SHEETS,
                        ", ".join(f"«{mi}» при «{ma}»" for ma, mi in sorted(repeated)))

        findings = []
        for sheet in sheets:
            if not per_sheet[sheet] or set(per_sheet[sheet]) <= repeated:
                continue
            findings += analyze_page(ask, pdf[sheet - 1], document, sheet,
                                     data_dir, scripts_dir, cap_px=cap_px,
                                     reporter=reporter, sheets_total=len(sheets),
                                     skip_pairs=repeated)
        return findings
    finally:
        pdf.close()


def run_visual_stage(cfg: dict, data_dir, ask=None) -> list:
    """Стадия зрения по всему прогону. Читает manifest.json, как и стадия правил.

    Смотрим ТОЛЬКО принципиальные схемы: маркировка проводов живёт на них.
    Сборочному чертежу и спецификации зрению тоже есть что показать, но у них
    другие вопросы и другие фильтры - это отдельная работа, и делать её надо
    после того, как эта замерена.
    """
    manifest_path = Path(data_dir) / "manifest.json"
    if not manifest_path.exists():
        logger.warning("manifest.json не найден в %s - стадия зрения пропущена", data_dir)
        return []

    if ask is None:
        from llm_client import make_vision_ask_fn
        ask = make_vision_ask_fn(resolve_vision_cfg(cfg))

    scripts_dir = resolve_path(cfg["paths"]["scripts_dir"])
    cap_px = float((cfg.get("vision") or {}).get("cap_px", tiling.DEFAULT_CAP_PX))
    reporter = script_loader.try_load(scripts_dir, "progress.py")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schemes = [d for d in manifest.get("documents", [])
               if d.get("doc_type") == "scheme" and d.get("status") != "failed"]

    findings = []
    for index, doc in enumerate(schemes, start=1):
        source = doc.get("source_file")
        if not source:
            continue
        pdf_path = PROJECT_ROOT / source
        if not pdf_path.is_file():
            logger.warning("  %s: исходный файл не найден, зрение пропущено", doc["name"])
            continue
        if reporter:
            reporter.document(index, len(schemes), doc["name"], doc_type="scheme",
                              path=doc.get("path"))

        doc_findings = analyze_document(
            ask, doc["name"], pdf_path, PROJECT_ROOT / doc["data_dir"], scripts_dir,
            cap_px=cap_px, reporter=reporter)
        logger.info("  зрение по %s: %d находок", doc["name"], len(doc_findings))
        findings += doc_findings

    if reporter:
        reporter.done()
    return findings
