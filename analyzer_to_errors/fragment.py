"""
Фрагмент чертежа вокруг места находки: картинка, а не координаты.

ЗАЧЕМ. Инженеру мало прочитать «обозначение QF1 есть на схеме и на чертеже, но
в спецификации его нет». Первое, что он делает, - лезет в PDF смотреть это
место глазами. Пока это значило: найти файл, открыть, вспомнить номер листа,
проискать по нему обозначение. Отчёт из-за этого читали с чертежами на втором
мониторе; тут показываем сам кусок листа прямо в таблице.

КАК НАХОДИТСЯ МЕСТО. Поиском текста по самому PDF, а не по координатам из
находки. Координат в находке нет и заводить их нельзя: ref по схеме (schema.py)
описывает место В ДОМЕННЫХ ПОЛЯХ - лист, обозначение, клемма, - и это правильно,
потому что находки приходят и от чекеров, и от нейросети, а у нейросети никаких
координат нет в принципе. Зато ключ находки - обозначение, артикул, адрес
клеммы - НАПЕЧАТАН на листе, и его умеет искать сам fitz.

Ключей пробуем несколько, от точного к общему (см. server: параметры q). Адрес
клеммы «1XT5:3» на листе разорван на фрагменты («1XT5:» и «3» - именно поэтому
schematic_rules и сшивает их обратно), поэтому искать надо «1XT5», а не адрес
целиком. Первый ключ, давший попадание, и выигрывает.

ЧТО ПОПАДАЕТ В КАДР. Первое попадание плюс те следующие, что помещаются рядом,
не раздув кадр больше MAX_CLIP. Это не украшательство: находки вида «один и тот
же адрес клеммы подписан на листе дважды» бессмысленно показывать по одной
точке - вся суть в том, что подписи ДВЕ, и их надо видеть рядом.

Исходный PDF на диске НЕ меняется: рамки рисуются в открытом в памяти документе,
который никто не сохраняет.
"""

import json
from pathlib import Path

import fitz

from settings import PROJECT_ROOT

# Поля вокруг попадания, pt. Обозначение само по себе ничего не объясняет -
# нужен контекст: что за аппарат, куда идут провода, что подписано рядом.
MARGIN = 90.0

# Кадр не меньше этого (иначе одно короткое обозначение даёт полоску 30x10 pt,
# на которой не видно ничего) и не больше (иначе это уже не фрагмент, а лист).
MIN_CLIP = (260.0, 190.0)
MAX_CLIP = (1500.0, 1100.0)

# Ширина картинки в пикселях, к которой подгоняется масштаб рендера.
TARGET_PX = 1700
MAX_ZOOM = 6.0

# Цвет обводки. Для set_rect нужен байтовый кортеж (картинка уже растровая),
# а не доли единицы, как у векторной отрисовки.
HIGHLIGHT_RGB = (217, 38, 26)


class FragmentError(Exception):
    """Показать фрагмент нельзя, и это нормально - причина для пользователя."""


def _find_document(manifest_path, document):
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise FragmentError("Данные этого прогона не найдены - запустите анализ заново")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for doc in manifest.get("documents", []):
        if doc.get("name") == document:
            return doc
    raise FragmentError(f"Документ {document!r} не найден в манифесте прогона")


def _source_pdf(doc):
    """Путь к исходному файлу документа.

    В манифесте он лежит относительно корня пайплайна (так его пишет ingest,
    и так же его собирает обратно main.py) - повторяем ту же сборку.
    """
    source = doc.get("source_file")
    if not source:
        raise FragmentError("У документа не записан исходный файл")
    path = PROJECT_ROOT / source
    if path.suffix.lower() != ".pdf":
        raise FragmentError(
            "Фрагмент можно показать только для чертежа или схемы: "
            "спецификация приходит книгой Excel, страниц у неё нет")
    if not path.is_file():
        raise FragmentError(f"Исходный файл не найден: {path.name}")
    return path


def _pages_to_search(pdf, sheet):
    """Номера листов в порядке поиска: сначала названный в находке, потом все.

    Лист в находке - это номер страницы с единицы (так его кладут и чекеры
    схемы, и чекеры чертежа). Он бывает пустым: часть находок к конкретному
    листу не привязана вовсе. Бывает и неверным - например, у находки по
    связке лист взят из СОСЕДНЕГО документа; поэтому названный лист не
    единственный, а лишь первый.
    """
    order = []
    try:
        n = int(str(sheet).strip())
        if 1 <= n <= pdf.page_count:
            order.append(n - 1)
    except (TypeError, ValueError):
        pass
    order += [i for i in range(pdf.page_count) if i not in order]
    return order


# Сколько раз ключ может встретиться на листе, чтобы фрагмент по нему что-то
# значил. Обозначение аппарата стоит на листе один-два раза; надпись, найденная
# сотни раз, - это не обозначение, а подпись напряжения или графа таблицы
# ("230VAC" на общем виде Енисея встречается 296 раз). Обводить их все
# бессмысленно: кадр всё равно охватит лишь угол листа, выбранный случайно.
MAX_HITS_USEFUL = 12

# Сколько рамок вообще рисовать. Даже пройдя фильтр выше, рисовать много рамок
# нельзя - см. комментарий про стоимость отрисовки в _render.
MAX_DRAWN = 8


def _hits(page, needles):
    """Лучший ключ на этом листе -> его прямоугольники.

    ЛУЧШИЙ, А НЕ ПЕРВЫЙ ПОПАВШИЙСЯ. Ключи идут от точного к общему, но «точный»
    артикул сплошь и рядом оказывается мусором: на сборочных чертежах в графу
    артикула попадает то напряжение ("230VAC"), то маркировка вывода по МЭК.
    Такой ключ находится сотнями и не указывает никуда. Поэтому среди ключей,
    давших разумное число попаданий, берём первый по порядку точности, а
    «разумное» - не больше MAX_HITS_USEFUL. Если разумных нет вовсе, берём тот,
    что дал меньше всего попаданий: показать хоть что-то лучше, чем отказать.

    ПОВОРОТ ЛИСТА. Схемы приходят страницами с /Rotate 270: mediabox стоит
    портретом 842x1191, а показывается лист альбомом 1191x842. search_for
    отдаёт координаты В НЕПОВЁРНУТОМ пространстве, и класть их в clip к
    get_pixmap, который работает в показываемом, нельзя: на листе ЩСКЗ №4
    подпись 5XT1 лежит на y=973 при высоте показываемой страницы 842, кадр
    вырождался в пустой и рендер падал невнятным "Invalid bandwriter header
    dimensions". Что хуже - на листах, где неповёрнутые координаты случайно
    попадали внутрь показываемого прямоугольника, ошибки не было вовсе:
    просто показывался НЕ ТОТ кусок листа, и понять это можно было только
    сверившись с чертежом.

    Поэтому наружу отдаём обе системы: rotation_matrix переводит в
    показываемую (по ней строится кадр), а неповёрнутая нужна для рисования -
    draw_rect пишет в поток содержимого страницы, то есть до поворота.
    """
    matrix = page.rotation_matrix
    fallback = None
    for needle in needles:
        needle = (needle or "").strip()
        if len(needle) < 2:
            continue                    # по одному символу «попадёт» пол-листа
        try:
            rects = page.search_for(needle)
        except Exception:  # noqa: BLE001 - битый лист не повод ронять запрос
            continue
        if not rects:
            continue
        pairs = [(fitz.Rect(r) * matrix, fitz.Rect(r)) for r in rects]
        if len(rects) <= MAX_HITS_USEFUL:
            return needle, pairs
        if fallback is None or len(pairs) < len(fallback[1]):
            fallback = (needle, pairs)
    return fallback if fallback else (None, [])


def _clip_for(shown_rects, page_rect):
    """Кадр вокруг попаданий: первое обязательно, остальные - если влезают.

    Координаты - в ПОКАЗЫВАЕМОМ пространстве листа (см. _hits).
    """
    box = fitz.Rect(shown_rects[0])
    for r in shown_rects[1:]:
        grown = fitz.Rect(box) | r
        if (grown.width + 2 * MARGIN <= MAX_CLIP[0]
                and grown.height + 2 * MARGIN <= MAX_CLIP[1]):
            box = grown

    box = fitz.Rect(box.x0 - MARGIN, box.y0 - MARGIN,
                    box.x1 + MARGIN, box.y1 + MARGIN)

    # добираем до минимального размера от центра
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    half_w = max(box.width, MIN_CLIP[0]) / 2
    half_h = max(box.height, MIN_CLIP[1]) / 2
    box = fitz.Rect(cx - half_w, cy - half_h, cx + half_w, cy + half_h)

    # Сдвигаем внутрь листа, а не обрезаем: обрезка у самого края оставляла бы
    # от кадра полоску (подпись 5XT1 стоит в 10 pt от края листа), а сдвиг
    # сохраняет и размер кадра, и саму подпись в нём.
    if box.width <= page_rect.width:
        box += (max(0.0, page_rect.x0 - box.x0) - max(0.0, box.x1 - page_rect.x1), 0,
                max(0.0, page_rect.x0 - box.x0) - max(0.0, box.x1 - page_rect.x1), 0)
    if box.height <= page_rect.height:
        shift = max(0.0, page_rect.y0 - box.y0) - max(0.0, box.y1 - page_rect.y1)
        box += (0, shift, 0, shift)

    box = box & page_rect
    if box.is_empty or box.width < 1 or box.height < 1:
        return fitz.Rect(page_rect)     # лучше показать лист целиком, чем ничего
    return box


def _outline(pix, boxes, clip, zoom, thickness=3):
    """Рисует прямоугольные рамки прямо по пикселям готового изображения.

    Координаты boxes - в показываемом пространстве листа; переводим их в
    пиксели картинки (вычесть начало кадра, умножить на масштаб) и заливаем
    четыре полоски через pix.set_rect. Никаких зависимостей и никакой работы с
    содержимым PDF - стоимость зависит только от длины рамки.

    ВАЖНО ПРО НАЧАЛО КООРДИНАТ. Картинка, отрисованная с clip, лежит НЕ в нуле:
    у неё своё начало (pix.x, pix.y) - для этого чертежа, например, (5609, 1882).
    set_rect работает в этой системе, а не от левого верхнего угла картинки, и
    на прямоугольник за её пределами просто возвращает False, ничего не сообщая.
    Именно так рамки и пропали в первой версии: фрагмент рисовался правильный,
    но без единой пометки, а найти это можно было только глазами.
    """
    ox, oy = pix.x, pix.y
    for box in boxes[:MAX_DRAWN]:
        x0 = ox + int((box.x0 - clip.x0) * zoom) - thickness
        y0 = oy + int((box.y0 - clip.y0) * zoom) - thickness
        x1 = ox + int((box.x1 - clip.x0) * zoom) + thickness
        y1 = oy + int((box.y1 - clip.y0) * zoom) + thickness
        # обрезаем по картинке: попадание могло стоять у самого края кадра
        x0, y0 = max(x0, ox), max(y0, oy)
        x1, y1 = min(x1, ox + pix.width), min(y1, oy + pix.height)
        if x1 - x0 < 2 or y1 - y0 < 2:
            continue
        for bar in (
            fitz.IRect(x0, y0, x1, y0 + thickness),           # верх
            fitz.IRect(x0, y1 - thickness, x1, y1),           # низ
            fitz.IRect(x0, y0, x0 + thickness, y1),           # лево
            fitz.IRect(x1 - thickness, y0, x1, y1),           # право
        ):
            pix.set_rect(bar, HIGHLIGHT_RGB)


def render(manifest_path, document, sheet=None, needles=()):
    """(PNG, сведения о том, что именно показано).

    Второе значение обязано доехать до подписи под картинкой. Лист в находке -
    лишь ПЕРВЫЙ кандидат поиска (см. _pages_to_search), и если ключа там нет,
    показывается тот лист, где он есть. Молча подставить другой лист под
    подписью «лист 1» нельзя, и особенно нельзя на находках вида «изделие
    пропало с парного листа»: там весь смысл в том, что на названном листе
    изделия НЕТ, а картинка с чужого листа выглядела бы прямым опровержением
    находки.

    Бросает FragmentError с объяснимой причиной, если показывать нечего.
    """
    needles = [n for n in (needles or []) if (n or "").strip()]
    if not needles:
        raise FragmentError("В находке нет ни обозначения, ни артикула - "
                            "искать на листе нечего")

    doc = _find_document(manifest_path, document)
    pdf = fitz.open(str(_source_pdf(doc)))
    try:
        found_page, found_needle, rects = None, None, []
        for index in _pages_to_search(pdf, sheet):
            needle, hits = _hits(pdf[index], needles)
            if hits:
                found_page, found_needle, rects = index, needle, hits
                break

        if found_page is None:
            raise FragmentError(
                "На листах этого документа не удалось найти "
                + ", ".join(repr(n) for n in needles[:3])
                + ". Обычно это значит, что надпись набрана не текстом, "
                  "а начерчена линиями.")

        page = pdf[found_page]
        clip = _clip_for([shown for shown, _ in rects], page.rect)

        zoom = min(MAX_ZOOM, TARGET_PX / max(clip.width, 1.0))
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, clip=clip)

        # Обводим попадания ПОВЕРХ ГОТОВОЙ КАРТИНКИ, а не в самой странице.
        #
        # Раньше здесь стоял page.draw_rect. Он пишет в поток содержимого
        # страницы, а на общем виде шкафа этот поток - 776 тысяч векторных
        # примитивов, и каждая рамка обходится в 0,33 с. На чертеже Енисея,
        # где ключ '230VAC' находился 296 раз, это давало 99 секунд на один
        # запрос: сервер выглядел зависшим. Отрисовка по пикселям стоит
        # столько же, сколько сама рамка, и от сложности листа не зависит
        # вовсе. Побочная выгода - страница остаётся нетронутой, и незачем
        # надеяться, что документ в памяти никто не сохранит.
        _outline(pix, [shown for shown, _ in rects if shown.intersects(clip)],
                 clip, zoom)

        try:
            requested = int(str(sheet).strip())
        except (TypeError, ValueError):
            requested = None
        info = {
            "page": found_page + 1,
            "requested_sheet": requested,
            "needle": found_needle,
            "hits": len(rects),
            # ключа на названном в находке листе нет - показан другой
            "fallback": requested is not None and requested != found_page + 1,
        }
        return pix.tobytes("png"), info
    finally:
        pdf.close()


def needles_from_ref(ref: dict) -> list:
    """Ключи для поиска, от точного к общему.

    Порядок не косметический. Артикул («NXB-63») на листе один, а обозначение
    («QF1») встречается и в подписи аппарата, и в перечне, и в штампе - поэтому
    сперва пробуем то, что вернёт меньше лишнего. Клеммник берём БЕЗ номера
    вывода: адрес «1XT5:3» напечатан разорванным на фрагменты, и целиком он на
    листе не ищется никогда.
    """
    out = []
    for key in ("article", "designator", "marking", "kks", "terminal_block"):
        value = (ref or {}).get(key)
        if value and str(value).strip():
            out.append(str(value).strip())
    return out
