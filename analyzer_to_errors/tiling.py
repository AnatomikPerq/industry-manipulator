"""
Растр листа для модели ЗРЕНИЯ: обзорная картинка плюс читаемые тайлы.

ЗАЧЕМ ЭТО ОТДЕЛЬНЫЙ СЛОЙ. Модель со зрением не видит лист целиком - точнее,
видит, но не читает. Препроцессор любой VL-модели ужимает картинку под свой
бюджет пикселей (у семейства Qwen это max_pixels, по умолчанию ~1 Мпикс), и
лист A3, отданный одним изображением, приезжает к ней уменьшенным в четыре-пять
раз. Подпись провода высотой 6.3 pt превращается в четыре пикселя, и модель
начинает путать 5 с 6, а 11 с 12 - ровно те ошибки, ради которых всё и
затевается. Поэтому лист режется на тайлы, каждый из которых влезает в бюджет
модели БЕЗ уменьшения.

ЗУМ СЧИТАЕТСЯ ИЗ РАЗМЕРА ШРИФТА НА ЛИСТЕ, А НЕ ЗАДАЁТСЯ В DPI. На корпусе
ЩСКЗ подписи набраны 6.3 pt (высота прописной ~0.7 от кегля, то есть ~4.4 pt);
чтобы получить читаемые 18 px на прописную, нужен зум ~4.1 - это те самые 300
dpi, на которых стоял отдельный тайлер. Но 300 dpi - свойство ЭТОГО бюро и
ЭТОГО формата: лист A1 при том же кегле и том же зуме даст вчетверо больше
пикселей и вчетверо больше тайлов, а бюро с более крупным шрифтом можно читать
дешевле. Кегль лежит в самом PDF, гадать не нужно.

ПРО ПОВОРОТ ЛИСТА. Схемы приходят страницами с /Rotate 270. get_pixmap(clip=...)
работает в ПОКАЗЫВАЕМОМ пространстве (page.rect), а get_text/search_for отдают
координаты в НЕПОВЁРНУТОМ. Тайлы здесь планируются в показываемом - то есть в
том же, в котором рисует get_pixmap; всё, что приходит из текстового слоя,
обязано пройти через page.rotation_matrix. Та же грабля, на которой один раз
уже молча показывался не тот кусок листа (см. fragment.py).

ПУСТЫЕ ТАЙЛЫ ПРОПУСКАЮТСЯ. На листе 10.2 нижний правый тайл - это белое поле,
рамка и штамп: полноценный вызов модели, потраченный ни на что. Считаем чернила
по ОБЗОРНОЙ картинке, которая всё равно рендерится (её же вторым изображением
получает модель - тайл без общего вида листа читается вслепую), так что лишнего
рендера это не стоит.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional

import fitz

# Высота прописной буквы в пикселях, при которой VL-модель уверенно читает
# подпись. Значение ПОДБИРАЕТСЯ ЗАМЕРОМ (visual_probe.py), а не берётся из
# головы: оно зависит от модели и от её препроцессора.
DEFAULT_CAP_PX = 18.0

# Доля кегля, приходящаяся на прописную букву. Для рубленых шрифтов CAD ~0.7.
CAP_HEIGHT_RATIO = 0.7

# Бюджет пикселей на ОДНО изображение. Тайл крупнее модель уменьшит сама, и
# весь смысл тайлинга пропадёт. Значение по умолчанию - дефолт семейства Qwen-VL
# (1280 визуальных токенов); поднимается в настройках сервера LM Studio.
DEFAULT_MAX_TILE_PIXELS = 1_000_000

# Перекрытие соседних тайлов. Подпись, разрезанная границей тайла, не должна
# пропасть: на схемах номер провода стоит вплотную к линии и занимает 10-15 pt.
DEFAULT_OVERLAP = 0.15

# Предохранитель: лист, у которого текст набран микроскопическим кеглем (или
# кегль не определился вовсе), не должен превратиться в сотню тайлов.
MAX_ZOOM = 8.0
MIN_ZOOM = 1.0

# Обзорная картинка листа - для ориентации модели, не для чтения. Мельче тайла
# на порядок: её задача - показать, где на листе находится тайл.
OVERVIEW_MAX_PIXELS = 1_000_000

# Ниже этой доли непустых пикселей тайл считается пустым и модели не
# показывается.
#
# ЗАМЕР НА ЛИСТЕ 10.2 ЩСКЗ: порог отсекает ровно ничего, и это не недосмотр, а
# свойство чертежа. Самый пустой тайл (нижний правый - белое поле, рамка и
# штамп) даёт 19% непустых пикселей: рамка и линовка штампа пересекают почти
# каждый тайл листа, и по чернилам «пусто» от «только рамка» не отличить.
# Порог оставлен низким и работает лишь там, где он и осмыслен: на листах
# альбома с настоящими белыми полями. Чтобы отсеивать тайлы «одна рамка», нужен
# не растр, а СОДЕРЖИМОЕ - число линий и подписей из уже извлечённых данных
# парсера; это дело стадии, а не тайлера, который про парсер знать не должен.
DEFAULT_MIN_INK = 0.005


@dataclass
class Tile:
    """Один тайл листа. rect - в ПОКАЗЫВАЕМОМ пространстве страницы (pt)."""
    index: int
    row: int
    col: int
    rect: "fitz.Rect"
    ink: float = 0.0

    @property
    def label(self) -> str:
        return f"т{self.index + 1} (строка {self.row + 1}, столбец {self.col + 1})"


@dataclass
class PagePlan:
    page_number: int          # с единицы, как в находках
    zoom: float
    cols: int
    rows: int
    cap_pt: float             # высота прописной на листе, из которой считан зум
    tiles: List[Tile] = field(default_factory=list)

    @property
    def useful(self) -> List[Tile]:
        return [t for t in self.tiles if t.ink >= DEFAULT_MIN_INK]


# Сколько раз кегль должен встретиться на листе, чтобы считаться настоящим.
# Одиночный спан в 2 pt (служебная пометка CAD, обрывок) не должен утаскивать
# зум в потолок, а вот кегль подписей стоит на листе десятками.
MIN_SIZE_OCCURRENCES = 3


def cap_height_pt(page: "fitz.Page") -> Optional[float]:
    """Высота прописной буквы самого мелкого НАСТОЯЩЕГО текста листа, pt.

    Именно самого мелкого, а не «нижнего процентиля»: замер на листе 10.2 ЩСКЗ
    показал, чем это отличается. Процентиль по всем спанам дал 8.3 pt, потому
    что крупных надписей (штамп, наименования) на листе много, - и зум вышел
    таким, что подписи проводов, набранные 6.3 pt, получали 13 px вместо
    заказанных 18. То есть настраивались мы под тот текст, который и так
    читается, а мельчайший - тот единственный, ради которого всё затевается, -
    оставался нечитаемым.

    От случайного мусора защищаемся не процентилем, а повторяемостью: кегль
    засчитывается, если встретился MIN_SIZE_OCCURRENCES раз.
    """
    counts = {}
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip() and span.get("size"):
                    size = round(float(span["size"]), 1)
                    counts[size] = counts.get(size, 0) + 1
    real = [s for s, n in counts.items() if n >= MIN_SIZE_OCCURRENCES]
    if not real:
        real = list(counts)
    if not real:
        return None
    return min(real) * CAP_HEIGHT_RATIO


def zoom_for(page: "fitz.Page", cap_px: float = DEFAULT_CAP_PX) -> tuple:
    """(зум, высота прописной в pt). Зум - во сколько раз растянуть лист, чтобы
    самая мелкая подпись имела cap_px пикселей в высоту."""
    cap_pt = cap_height_pt(page)
    if not cap_pt or cap_pt <= 0:
        # Лист без текстового слоя (скан или текст, начерченный линиями).
        # Считать не из чего - берём середину диапазона, а не отказываемся:
        # именно такие листы зрению и нужнее всего.
        return 4.0, 0.0
    zoom = cap_px / cap_pt
    return max(MIN_ZOOM, min(MAX_ZOOM, zoom)), cap_pt


def _grid_for(width_px: float, height_px: float, max_tile_pixels: int,
              overlap: float) -> tuple:
    """Минимальная сетка, при которой тайл влезает в бюджет пикселей модели.

    Дробим по той стороне, которая сейчас длиннее, - иначе на альбомном листе
    получались бы узкие вертикальные полоски, на которых не видно ни одной
    цепи целиком.
    """
    cols = rows = 1
    for _ in range(64):
        tile_w = width_px / cols * (1 + overlap)
        tile_h = height_px / rows * (1 + overlap)
        if tile_w * tile_h <= max_tile_pixels:
            break
        if tile_w >= tile_h:
            cols += 1
        else:
            rows += 1
    return cols, rows


def plan_page(page: "fitz.Page", cap_px: float = DEFAULT_CAP_PX,
              max_tile_pixels: int = DEFAULT_MAX_TILE_PIXELS,
              overlap: float = DEFAULT_OVERLAP,
              zoom: float = None) -> PagePlan:
    """Разбиение листа на тайлы. Растр ещё не считается - только геометрия."""
    rect = page.rect                      # ПОКАЗЫВАЕМОЕ пространство
    cap_pt = 0.0
    if zoom is None:
        zoom, cap_pt = zoom_for(page, cap_px)

    cols, rows = _grid_for(rect.width * zoom, rect.height * zoom,
                           max_tile_pixels, overlap)

    step_w, step_h = rect.width / cols, rect.height / rows
    tile_w, tile_h = step_w * (1 + overlap), step_h * (1 + overlap)

    tiles = []
    for r in range(rows):
        for c in range(cols):
            x0 = rect.x0 + c * step_w
            y0 = rect.y0 + r * step_h
            box = fitz.Rect(x0, y0,
                            min(x0 + tile_w, rect.x1),
                            min(y0 + tile_h, rect.y1))
            tiles.append(Tile(index=len(tiles), row=r, col=c, rect=box))

    return PagePlan(page_number=page.number + 1, zoom=zoom, cols=cols, rows=rows,
                    cap_pt=cap_pt, tiles=tiles)


# Во сколько раз (2**INK_SHRINK) уменьшить обзор перед подсчётом чернил.
# Считать долю непустых пикселей приходится в питоновском цикле, и на обзоре в
# мегапиксель это секунда НА ЛИСТ - пять минут на альбоме, потраченные на
# арифметику. Уменьшение делает PyMuPDF в C, а для ответа «есть ли тут вообще
# что-нибудь» разрешения 1/8 хватает с запасом.
INK_SHRINK = 3


def render_overview(page: "fitz.Page", max_pixels: int = OVERVIEW_MAX_PIXELS):
    """(PNG обзора листа, уменьшенный pixmap для подсчёта чернил).

    Лист рендерится ОДИН раз: картинка для модели и карта чернил - один и тот же
    растр, второй просто уменьшен. Уменьшение идёт in-place, поэтому PNG
    забирается до него.
    """
    rect = page.rect
    zoom = math.sqrt(max_pixels / max(rect.width * rect.height, 1.0))
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    png = pix.tobytes("png")
    pix.shrink(INK_SHRINK)
    return png, pix


def measure_ink(plan: PagePlan, page_rect: "fitz.Rect", pix) -> None:
    """Доля непустых пикселей в каждом тайле - по обзорной картинке.

    Проставляет tile.ink на месте. Считается по строкам растра: тайл, в котором
    только белое поле, модели не показывается вовсе (на листе 10.2 таких
    один из шести, а на альбоме это часы вызовов в никуда).
    """
    if pix.n < 3:
        return
    sx = pix.width / max(page_rect.width, 1.0)
    sy = pix.height / max(page_rect.height, 1.0)
    samples = pix.samples
    stride, n = pix.stride, pix.n

    for tile in plan.tiles:
        x0 = max(0, int((tile.rect.x0 - page_rect.x0) * sx))
        x1 = min(pix.width, int((tile.rect.x1 - page_rect.x0) * sx))
        y0 = max(0, int((tile.rect.y0 - page_rect.y0) * sy))
        y1 = min(pix.height, int((tile.rect.y1 - page_rect.y0) * sy))
        if x1 <= x0 or y1 <= y0:
            tile.ink = 0.0
            continue
        dark = total = 0
        for y in range(y0, y1):
            base = y * stride
            for x in range(x0, x1):
                off = base + x * n
                if samples[off] < 245 or samples[off + 1] < 245 or samples[off + 2] < 245:
                    dark += 1
                total += 1
        tile.ink = dark / total if total else 0.0


def render_tile(page: "fitz.Page", tile: Tile, zoom: float) -> bytes:
    """PNG одного тайла. Рендерится ровно тот прямоугольник, что спланирован,
    и в том же (показываемом) пространстве - никаких пересчётов поворота."""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=tile.rect)
    return pix.tobytes("png")


def tile_pixels(tile: Tile, zoom: float) -> tuple:
    return int(tile.rect.width * zoom), int(tile.rect.height * zoom)


def text_in_tile(page: "fitz.Page", tile: Tile) -> List[dict]:
    """Текстовые спаны, попавшие в тайл: эталон для проверки чтения модели.

    ПОВОРОТ. get_text отдаёт координаты в НЕПОВЁРНУТОМ пространстве, а тайл
    спланирован в показываемом - поэтому каждый bbox прогоняется через
    page.rotation_matrix. Без этого на листах с /Rotate 270 эталон брался бы из
    совершенно другого угла листа, причём молча.
    """
    matrix = page.rotation_matrix
    out = []
    for block in page.get_text("dict").get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                if not text:
                    continue
                box = fitz.Rect(span["bbox"]) * matrix
                centre = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
                if centre in tile.rect:
                    out.append({"text": text, "bbox": [box.x0, box.y0, box.x1, box.y1],
                                "size": span.get("size")})
    return out
