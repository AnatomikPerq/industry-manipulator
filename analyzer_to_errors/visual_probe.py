#!/usr/bin/env python3
"""
КАЛИБРОВКА ЗРЕНИЯ. Читает ли модель подписи на тайле листа - и при каком зуме.

ЗАЧЕМ ОТДЕЛЬНАЯ ПРОБА, А НЕ СРАЗУ СТАДИЯ. Весь визуальный анализ держится на
одном допущении: модель со зрением РАЗБИРАЕТ мелкие подписи на схеме. Если не
разбирает - никакая интеграция не поможет, а выяснится это не сразу, а в виде
вала правдоподобных ложных находок, которые не отличить от настоящих. Поэтому
допущение проверяется первым и отдельно, до единой правки пайплайна.

ПОЧЕМУ ЭТАЛОН БЕСПЛАТНЫЙ. Проверять чтение модели не нужно глазами: в PDF есть
текстовый слой, и он в точности говорит, что НАПЕЧАТАНО на этом куске листа.
Сравнение автоматическое, оценка воспроизводимая, лист можно взять любой.

ПОЧЕМУ ЛИСТ 10.2 ЩСКЗ. На нём лежат две подтверждённые ошибки нумерации
проводов, которые детерминированный чекер не берёт ПРИНЦИПИАЛЬНО (см. «не
вошло» в schematic_rules.py: маркировки привязываются к цепи по радиусу, и на
густом листе цепь получает чужие номера). В тексте листа они видны как
одиночки среди одинаковых:

    «4» - 15 раз, «5» - 4 раза, «6» - ОДИН раз,
    «10» - 6 раз, «11» - 4 раза, «12» - ОДИН раз.

«6» должно быть «5», «12» должно быть «11». Модель, прочитавшая тайл верно,
назовёт эти числа сама - отдельного вопроса «нет ли тут ошибки» не требуется, и
задавать его не надо: судит пусть чекер, у него это детерминированно.

ЧТО ИМЕННО МЕРЯЕТСЯ, по каждому варианту зума:
  * доля напечатанных подписей, которые модель прочитала (полнота);
  * доля прочитанного, чего на листе нет вовсе (выдумки - самое дорогое);
  * названы ли «6» и «12»;
  * секунды на тайл.

Запуск:
    python visual_probe.py --pdf ../tiler/Итог1.pdf --page 1
    python visual_probe.py --pdf ... --page 1 --cap-px 18 --max-tile-pixels 4000000
    python visual_probe.py --check          # только «видит ли модель картинку вообще»
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

import fitz

import tiling
from llm_client import make_vision_ask_fn
from settings import PROJECT_ROOT, load_config, resolve_vision_cfg

# Подпись провода - это короткое число. Длинные числа на листе есть (номер
# документа, год), но маркировкой они не бывают, и мешать их в эталон значило бы
# штрафовать модель за то, чего у неё не спрашивали.
MARKING_RE = re.compile(r"^\d{1,4}$")

# Короткий - по той же замеренной причине, что и промпт стадии (см.
# visual_stage.BAND_PROMPT): длинный разгонял рассуждение модели в полсотни раз.
# Проба обязана спрашивать РОВНО то же, что стадия, иначе она калибрует не то.
READ_PROMPT = """Второе изображение - фрагмент принципиальной электрической схемы
({position}); первое - лист целиком, для ориентации.

Выпиши номера проводов - короткие числа, подписанные у линий связи. КАЖДУЮ
подпись отдельно, повторы не объединяй. Пиши ровно то, что напечатано, ничего
не исправляй.

Только JSON:
{{"markings": [{{"number": "5", "attached_to": "описание линии"}}]}}
"""

SANITY_PROMPT = ("Что написано на картинке? Ответь одним словом, без пояснений.")

# ТОЛЬКО ЛАТИНИЦА И ЦИФРЫ. Картинка рисуется базовым шрифтом PyMuPDF (helv =
# Helvetica), а в нём кириллицы нет: слово «ЩСКЗ» вышло четырьмя точками, и
# модель, честно ответившая «многоточие», была объявлена незрячей. Проверка
# зрения не должна зависеть от того, какие шрифты вшиты в PDF-библиотеку.
SANITY_WORD = "QF17"


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


def _sanity_image() -> bytes:
    """Картинка с известным словом - проверка, что модель ВООБЩЕ смотрит.

    Текстовая модель, случайно прописанная в конфиг зрения, картинку молча
    проглотит и ответит правдоподобной чушью. Это надо ловить здесь, а не после
    сорока минут прогона по альбому.
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=120)
    page.insert_text((30, 75), SANITY_WORD, fontsize=44, fontname="helv")
    png = page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    doc.close()
    return png


def explain_failure(server: dict, error: Exception) -> None:
    """Понятная причина вместо traceback - и список того, что на сервере есть.

    Тот же принцип, что у preflight_llm в main.py: криптическая ошибка openai
    посреди прогона ничего не говорит о том, что чинить. Чаще всего чинить надо
    одно - в LM Studio не загружена названная модель, - и виден это только из
    списка моделей, которые сервер отдаёт на самом деле.

    check_server_alive импортируется ЗДЕСЬ, а не наверху файла: llm_check тянет
    за собой oi_agent, а тот - Open Interpreter, который этой пробе не нужен ни
    на секунду. В аварийной ветке лишний импорт ничего не стоит.
    """
    from llm_check import check_server_alive

    print(f"\n!!! Сервер не выполнил запрос: {type(error).__name__}: "
          f"{str(error)[:200]}")
    alive = check_server_alive(server)
    if not alive["ok"]:
        print(f"    Сервер {server.get('base_url')} не отвечает вовсе: "
              f"{alive.get('error')}")
        return
    models = alive.get("models") or []
    print(f"    Сервер отвечает. Моделей на нём: {len(models)}")
    for name in models[:20]:
        mark = " <- указана в config" if name == server.get("model") else ""
        print(f"      {name}{mark}")
    if server.get("model") not in models:
        print(f"\n    Модели {server.get('model')!r} среди них НЕТ - поправьте "
              f"llm_servers.vision.model в config.yaml либо загрузите её в LM Studio.")
    else:
        print("\n    Модель в списке есть, но запрос не обслужен. Обычно это значит, "
              "что она не загружена в память (LM Studio отдаёт 503, пока грузит "
              "или если JIT-загрузка выключена) - загрузите её в интерфейсе LM Studio "
              "и повторите.")


def check_vision(ask, server: dict) -> bool:
    try:
        answer = ask(SANITY_PROMPT, [_sanity_image()])
    except Exception as e:  # noqa: BLE001 - здесь важна причина, а не стек
        explain_failure(server, e)
        return False
    ok = SANITY_WORD.lower() in (answer or "").lower()
    print(f"  проверка зрения: модель ответила {answer.strip()[:60]!r} - "
          f"{'ВИДИТ' if ok else 'НЕ ВИДИТ (или это не зрячая модель)'}")
    return ok


def ground_truth(page, tile) -> Counter:
    """Что НАПЕЧАТАНО на этом тайле - маркировки проводов, по тексту PDF."""
    return Counter(s["text"] for s in tiling.text_in_tile(page, tile)
                   if MARKING_RE.match(s["text"]))


def _position(tile, plan) -> str:
    vert = ("верхняя", "средняя", "нижняя")[min(tile.row * 3 // max(plan.rows, 1), 2)]
    horiz = ("левая", "центральная", "правая")[min(tile.col * 3 // max(plan.cols, 1), 2)]
    return (f"{vert} {horiz} часть листа "
            f"(тайл {tile.index + 1} из {len(plan.tiles)}, сетка {plan.rows}x{plan.cols})")


def run_variant(page, ask, cap_px, max_tile_pixels, overlap, out_dir, verbose,
                only_tiles=None):
    plan = tiling.plan_page(page, cap_px=cap_px, max_tile_pixels=max_tile_pixels,
                            overlap=overlap)
    overview_png, overview_pix = tiling.render_overview(page)
    tiling.measure_ink(plan, page.rect, overview_pix)

    useful = plan.useful
    tile_w, tile_h = tiling.tile_pixels(plan.tiles[0], plan.zoom)
    print(f"\n=== зум ×{plan.zoom:.2f} (прописная {plan.cap_pt:.1f} pt -> "
          f"{plan.cap_pt * plan.zoom:.0f} px), сетка {plan.rows}x{plan.cols}, "
          f"тайл {tile_w}x{tile_h} px = {tile_w * tile_h / 1e6:.1f} Мпикс, "
          f"тайлов с содержимым {len(useful)} из {len(plan.tiles)} ===")

    total_gt, hit, extra, seconds, refused = Counter(), Counter(), Counter(), 0.0, 0
    for tile in plan.tiles:
        if only_tiles and (tile.index + 1) not in only_tiles:
            continue
        if tile.ink < tiling.DEFAULT_MIN_INK:
            print(f"  {tile.label}: пропущен, чернил {tile.ink * 100:.2f}%")
            continue

        png = tiling.render_tile(page, tile, plan.zoom)
        if out_dir:
            (out_dir / f"cap{int(cap_px)}_t{tile.index + 1:02d}.png").write_bytes(png)

        started = time.monotonic()
        try:
            answer = ask(READ_PROMPT.format(position=_position(tile, plan)),
                         [overview_png, png])
        except Exception as e:  # noqa: BLE001 - отказ на тайле не повод бросать замер
            # Отказы считаем отдельной строкой, а не выдаём за «ничего не
            # прочитано»: это разные вещи, и путать их - значит получить
            # правдоподобную, но лживую таблицу.
            refused += 1
            print(f"  {tile.label}: ОТКАЗ - {type(e).__name__}: {str(e)[:120]}")
            continue
        elapsed = time.monotonic() - started
        seconds += elapsed

        said = Counter(str(m.get("number", "")).strip()
                       for m in _extract_json(answer).get("markings", [])
                       if MARKING_RE.match(str(m.get("number", "")).strip()))
        truth = ground_truth(page, tile)
        total_gt += truth
        for value, count in said.items():
            hit[value] += min(count, truth.get(value, 0))
            if count > truth.get(value, 0):
                extra[value] += count - truth.get(value, 0)

        print(f"  {tile.label}: чернил {tile.ink * 100:.1f}%, {elapsed:5.1f} c, "
              f"напечатано {sorted(truth.elements())}, "
              f"прочитано {sorted(said.elements())}")
        if verbose:
            print("      ответ модели:", (answer or "").strip()[:400].replace("\n", " "))

    n_truth, n_hit, n_extra = sum(total_gt.values()), sum(hit.values()), sum(extra.values())
    print(f"  ИТОГО: прочитано {n_hit} из {n_truth} "
          f"({(n_hit / n_truth * 100 if n_truth else 0):.0f}%), "
          f"выдумано {n_extra}, отказов {refused}, время {seconds:.0f} c "
          f"({seconds / max(len(useful) - refused, 1):.1f} c/тайл)")

    # Ради этих двух чисел всё и затевалось: они и есть две подтверждённые
    # ошибки листа 10.2, и чекер их не берёт.
    for value in ("6", "12"):
        if total_gt.get(value):
            state = "НАЗВАНО" if hit.get(value) else "ПРОПУЩЕНО"
            print(f"  одиночка «{value}» (подтверждённая ошибка листа): {state}")

    return {"cap_px": cap_px, "zoom": plan.zoom, "grid": f"{plan.rows}x{plan.cols}",
            "tile_mpx": tile_w * tile_h / 1e6, "tiles": len(useful),
            "read": n_hit, "printed": n_truth, "invented": n_extra,
            "refused": refused, "seconds": seconds}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", help="исходный PDF со схемой")
    parser.add_argument("--page", type=int, default=1, help="номер листа, с единицы")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    parser.add_argument("--cap-px", type=float, action="append",
                        help="высота прописной в пикселях; можно повторять "
                             "(по умолчанию 12, 18, 26 - три варианта зума)")
    parser.add_argument("--max-tile-pixels", type=int,
                        default=tiling.DEFAULT_MAX_TILE_PIXELS,
                        help="бюджет пикселей модели на одну картинку")
    parser.add_argument("--overlap", type=float, default=tiling.DEFAULT_OVERLAP)
    parser.add_argument("--out", default=None, help="куда сложить отрендеренные тайлы")
    parser.add_argument("--check", action="store_true",
                        help="только проверка, что модель видит картинки")
    parser.add_argument("--verbose", action="store_true", help="печатать ответы модели")
    parser.add_argument("--tiles", default=None,
                        help="разбирать только эти тайлы, через запятую (нумерация "
                             "с единицы, как в выводе). Ради быстрого цикла: полный "
                             "лист - это 12 вызовов по полминуты")
    args = parser.parse_args()
    only_tiles = ({int(t) for t in args.tiles.replace(" ", "").split(",") if t}
                  if args.tiles else None)

    cfg = load_config(args.config)
    server = resolve_vision_cfg(cfg)
    print(f"Модель зрения: {server.get('model')} на {server.get('base_url')}")
    ask = make_vision_ask_fn(server)

    if not check_vision(ask, server):
        print("\nДальше идти незачем: проверьте, что в llm_servers.vision указана "
              "модель СО ЗРЕНИЕМ и что она загружена в LM Studio.")
        sys.exit(1)
    if args.check:
        return

    if not args.pdf:
        parser.error("нужен --pdf (или запускайте с --check)")

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(args.pdf)
    try:
        page = doc[args.page - 1]
        print(f"Лист {args.page} из {doc.page_count}: "
              f"{page.rect.width:.0f}x{page.rect.height:.0f} pt, поворот {page.rotation}°")

        rows = []
        for cap_px in (args.cap_px or [12.0, 18.0, 26.0]):
            rows.append(run_variant(page, ask, float(cap_px), args.max_tile_pixels,
                                    args.overlap, out_dir, args.verbose,
                                    only_tiles=only_tiles))
    finally:
        doc.close()

    print("\n" + "=" * 78)
    print(f"{'прописная':>10} {'зум':>6} {'сетка':>7} {'Мпикс':>7} {'тайлов':>7} "
          f"{'прочитано':>12} {'выдумано':>9} {'отказов':>8} {'c/тайл':>8}")
    for r in rows:
        share = f"{r['read']}/{r['printed']}"
        answered = max(r["tiles"] - r["refused"], 1)
        print(f"{r['cap_px']:>10.0f} {r['zoom']:>6.2f} {r['grid']:>7} "
              f"{r['tile_mpx']:>7.1f} {r['tiles']:>7} {share:>12} "
              f"{r['invented']:>9} {r['refused']:>8} {r['seconds'] / answered:>8.1f}")
    print("\nБерём наименьший зум, на котором прочитано ~всё и выдумано 0. "
          "Если выдумок много на ЛЮБОМ зуме - модель не годится для этой работы.")


if __name__ == "__main__":
    main()
