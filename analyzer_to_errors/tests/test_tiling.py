"""
Нарезка листа на тайлы для модели зрения: геометрия, зум и поворот.

ЧТО ЗДЕСЬ ЛОВИТСЯ. Ошибка в этом файле НЕ ВИДНА в отчёте - она видна только в
том, что модель зрения «плохо читает». Три места, каждое из которых уже
успело выстрелить или было к этому близко:

  * ЗУМ ПО НЕ ТОМУ ШРИФТУ. Первая версия брала нижний процентиль кегля и на
    листе 10.2 ЩСКЗ выдавала 8.3 pt вместо 6.3: крупных надписей на листе
    много, и настройка шла под текст, который и так читается, а подписи
    проводов получали 13 px вместо заказанных 18;
  * ТАЙЛ БОЛЬШЕ БЮДЖЕТА МОДЕЛИ. Тайл, не влезающий в max_pixels, модель молча
    уменьшает сама - и весь смысл нарезки пропадает, причём незаметно;
  * ПОВОРОТ ЛИСТА. Схемы приходят с /Rotate 270: get_text отдаёт координаты в
    неповёрнутом пространстве, а тайлы живут в показываемом. Перепутать их -
    значит взять эталон из другого угла листа, и молча (ровно эта грабля
    описана в fragment.py).

Настоящих PDF заказчика в репозитории нет и быть не может, поэтому лист
собирается здесь же из fitz: кегли и координаты известны точно, а значит
известен и правильный ответ.
"""

import fitz
import pytest

import tiling

A3_W, A3_H = 1191.0, 842.0

# Кегль подписей на схемах корпуса ЩСКЗ. Прописная - 0.7 от кегля.
SMALL_PT = 6.3
BIG_PT = 12.0


def make_sheet(rotation: int = 0, with_text: bool = True) -> "fitz.Document":
    """Лист A3: несколько мелких подписей, несколько крупных и один
    единичный микроскопический спан - тот самый мусор, из-за которого нельзя
    брать просто минимум кегля."""
    doc = fitz.open()
    page = doc.new_page(width=A3_W, height=A3_H)
    # заливка, чтобы тайлы не считались пустыми по чернилам
    page.draw_rect(fitz.Rect(40, 40, A3_W - 40, A3_H - 40), color=(0, 0, 0), width=1)
    if with_text:
        for i in range(5):
            page.insert_text((100 + i * 60, 200), str(5 if i < 4 else 6),
                             fontsize=SMALL_PT, fontname="helv")
        for i in range(4):
            page.insert_text((100 + i * 120, 400), "Наименование",
                             fontsize=BIG_PT, fontname="helv")
        page.insert_text((60, 800), ".", fontsize=1.5, fontname="helv")
    if rotation:
        page.set_rotation(rotation)
    return doc


def test_cap_height_takes_the_smallest_real_font():
    """Кегль берётся самый мелкий из ПОВТОРЯЮЩИХСЯ, а не процентиль и не
    абсолютный минимум."""
    doc = make_sheet()
    try:
        cap = tiling.cap_height_pt(doc[0])
        assert cap == pytest.approx(SMALL_PT * tiling.CAP_HEIGHT_RATIO, abs=0.2), (
            "зум должен настраиваться по самым мелким подписям листа")
        assert cap > 1.5, "единичный спан в 1.5 pt не должен утаскивать зум в потолок"
    finally:
        doc.close()


def test_zoom_makes_the_smallest_text_readable():
    doc = make_sheet()
    try:
        zoom, cap_pt = tiling.zoom_for(doc[0], cap_px=18.0)
        assert cap_pt * zoom == pytest.approx(18.0, abs=0.5)
    finally:
        doc.close()


def test_page_without_text_still_gets_a_zoom():
    """Лист, у которого текст начерчен линиями, - именно тот случай, ради
    которого зрение и заводится. Отказываться от него нельзя."""
    doc = make_sheet(with_text=False)
    try:
        zoom, cap_pt = tiling.zoom_for(doc[0])
        assert zoom > 1.0 and cap_pt == 0.0
    finally:
        doc.close()


@pytest.mark.parametrize("budget", [1_000_000, 2_000_000, 4_000_000])
def test_every_tile_fits_the_model_budget(budget):
    doc = make_sheet()
    try:
        plan = tiling.plan_page(doc[0], cap_px=18.0, max_tile_pixels=budget)
        for tile in plan.tiles:
            w, h = tiling.tile_pixels(tile, plan.zoom)
            assert w * h <= budget, (
                f"тайл {w}x{h} не влезает в бюджет {budget} - модель уменьшит его сама")
    finally:
        doc.close()


def test_bigger_budget_means_fewer_tiles():
    """Бюджет пикселей модели - главный рычаг цены прогона: на листе A3 при
    читаемом зуме это 24 тайла против 6."""
    doc = make_sheet()
    try:
        small = tiling.plan_page(doc[0], cap_px=18.0, max_tile_pixels=1_000_000)
        big = tiling.plan_page(doc[0], cap_px=18.0, max_tile_pixels=4_000_000)
        assert len(small.tiles) > len(big.tiles)
    finally:
        doc.close()


def test_tiles_cover_the_whole_sheet_and_overlap():
    doc = make_sheet()
    try:
        page = doc[0]
        plan = tiling.plan_page(page, cap_px=18.0)
        assert plan.rows > 1 and plan.cols > 1

        # покрытие: правый нижний угол листа обязан попасть в последний тайл
        assert plan.tiles[-1].rect.x1 == pytest.approx(page.rect.x1)
        assert plan.tiles[-1].rect.y1 == pytest.approx(page.rect.y1)

        # перекрытие: соседи по строке обязаны заходить друг на друга, иначе
        # подпись на границе тайлов пропадёт целиком
        first, second = plan.tiles[0], plan.tiles[1]
        assert second.rect.x0 < first.rect.x1
    finally:
        doc.close()


@pytest.mark.parametrize("rotation", [0, 270])
def test_text_lands_in_the_tile_that_shows_it(rotation):
    """Эталон для проверки чтения берётся из текстового слоя - и обязан лежать
    в ТОМ ЖЕ тайле, который увидит модель, на повёрнутом листе тоже."""
    doc = make_sheet(rotation=rotation)
    try:
        page = doc[0]
        plan = tiling.plan_page(page, cap_px=18.0)
        found = [s["text"] for tile in plan.tiles for s in tiling.text_in_tile(page, tile)]
        assert "6" in found and found.count("5") >= 4, (
            "подписи листа потерялись при переводе координат "
            f"(поворот {rotation}°)")
        for tile in plan.tiles:
            for span in tiling.text_in_tile(page, tile):
                box = fitz.Rect(span["bbox"])
                centre = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
                assert centre in tile.rect
    finally:
        doc.close()


def test_ink_separates_a_blank_sheet_from_a_drawn_one():
    """Порог чернил осмыслен только на настоящем белом поле: на чертеже рамка
    и штамп пересекают почти каждый тайл (замер на листе 10.2 - самый пустой
    тайл даёт 19%)."""
    blank = fitz.open()
    blank.new_page(width=A3_W, height=A3_H)
    drawn = make_sheet()
    try:
        for doc, expect_ink in ((blank, False), (drawn, True)):
            page = doc[0]
            plan = tiling.plan_page(page, cap_px=18.0)
            _, pix = tiling.render_overview(page)
            tiling.measure_ink(plan, page.rect, pix)
            has_ink = any(t.ink >= tiling.DEFAULT_MIN_INK for t in plan.tiles)
            assert has_ink is expect_ink
    finally:
        blank.close()
        drawn.close()
