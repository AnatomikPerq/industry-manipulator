#!/usr/bin/env python3
"""
Единый скрипт извлечения данных из EPLAN-PDF (векторная схема, не скан).

Вход:  PDF-файл
Выход: JSON-файлы в output_dir:
    raw.json         -- сырые текстовые span'ы + векторные линии по листам
    classified.json  -- то же самое + тип каждого текстового span'а
    graph.json        -- граф проводов (склеенные полилинии + подписи на концах)
    netlist.json      -- по каждому I/O-каналу модуля (AI/AO/DI/DO):
                          описание сигнала + связанные KKS/буквенно-цифровые теги

Поиск ошибок/несостыковок сюда НЕ входит -- это отдельный шаг, отдаётся
нейросети поверх этих файлов, а не детерминированному чекеру.

Всё в одном проходе, без промежуточных вызовов скриптов.

Использование:
    python3 extract_pipeline.py input.pdf output_dir/
"""
import sys
import os
import re
import json
import math
from collections import defaultdict

import fitz  # PyMuPDF

# Профили (диалекты) оформления схем: набор форматно-зависимых regex'ов на
# каждый шаблон бюро + авто-детект. См. profiles.py. Импорт устроен так,
# чтобы скрипт работал и при прямом запуске, и как импортируемый модуль.
try:
    from . import profiles  # type: ignore
except ImportError:
    import os as _os
    import importlib.util as _ilu
    _pspec = _ilu.spec_from_file_location(
        "profiles", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "profiles.py"))
    profiles = _ilu.module_from_spec(_pspec)
    _pspec.loader.exec_module(profiles)


# ============================================================
# ЧАСТЬ 1: сырое извлечение (текст + вектора) из PDF
# ============================================================

def fix_text(s: str) -> str:
    """Резервный фикс для случаев без font_fix_map (обратная совместимость)."""
    try:
        return s.encode('latin1').decode('cp1251')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


# ------------------------------------------------------------------
# Определение кодировки ПО ШРИФТУ (взято из eplan_pdf_parser.py).
# Почему это лучше жёсткого fix_text(): EPLAN может встраивать в один
# PDF несколько шрифтов, и сломан обычно только один из них (тот,
# которым рисуется рамка штампа) -- ToUnicode-таблица перепутана с
# Windows-1251, тогда как второй шрифт (обычно Tahoma для надписей
# цепей) закодирован нормально и его трогать не надо. Жёсткий
# .encode('latin1').decode('cp1251') для ВСЕГО текста -- случайно
# работает на этом конкретном файле, но ломается на файле, где не всё
# сломано одинаково. Здесь тестируем несколько кодеков отдельно на
# каждый шрифт и берём тот, что даёт наиболее "читаемый" результат.
# ------------------------------------------------------------------

ENCODING_CANDIDATES = ["cp1251", "koi8_r", "cp866", "mac_cyrillic", "iso8859_5"]
OK_PUNCT = set(",.:;/-()№²%+ '\"±≤≥×÷_")


def _readability_score(text: str) -> float:
    if not text:
        return 0.0
    good, total = 0.0, 0
    for ch in text:
        total += 1
        o = ord(ch)
        if ch == "\ufffd":
            good -= 3.0
        elif ch.isspace():
            good += 0.4
        elif 0x0400 <= o <= 0x04FF:
            good += 1.0
        elif ch.isalnum() and ch.isascii():
            good += 1.0
        elif ch in OK_PUNCT:
            good += 0.6
        elif o < 0x20:
            good -= 1.0
        else:
            good -= 0.3
    return good / max(total, 1)


def _demojibake(text, codec):
    """Развернуть mojibake (cp1251-байты, прочитанные как latin1) обратно.

    Устойчиво к СМЕШАННОМУ содержимому: если в строке есть символы вне latin1
    (напр. '…' U+2026 из "-10…+80°C" или уже корректная кириллица), обычный
    text.encode('latin1') упал бы на всей строке и НИЧЕГО бы не починил. Здесь
    посимвольно: латин1-совместимые символы разворачиваем через codec (все
    кириллические однобайтовые кодировки посимвольны), остальные оставляем как
    есть. Один посторонний символ больше не рушит расшифровку всего листа.
    """
    try:
        return text.encode("latin1").decode(codec)
    except (UnicodeEncodeError, UnicodeDecodeError):
        out = []
        for ch in text:
            if ord(ch) <= 0xFF:
                try:
                    out.append(ch.encode("latin1").decode(codec))
                except (UnicodeEncodeError, UnicodeDecodeError):
                    out.append(ch)
            else:
                out.append(ch)
        return "".join(out)


def detect_font_fix(sample_text, min_margin=0.08):
    base = _readability_score(sample_text)
    best_enc, best_score = None, base
    for enc in ENCODING_CANDIDATES:
        try:
            fixed = _demojibake(sample_text, enc)
        except LookupError:
            continue
        s = _readability_score(fixed)
        if s > best_score:
            best_enc, best_score = enc, s
    if best_enc is not None and (best_score - base) >= min_margin:
        return best_enc
    return None


def apply_font_fix(text, codec):
    if not codec:
        return text
    return _demojibake(text, codec)


def analyze_fonts(pdf_path):
    """Проход по всему документу: для каждого имени шрифта собираем
    образец текста и решаем, какой кодек (если вообще какой-то) нужен."""
    doc = fitz.open(pdf_path)
    samples = defaultdict(list)
    for page in doc:
        text_dict = page.get_text("dict")
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    raw = span.get("text", "")
                    if raw.strip():
                        samples[span.get("font", "")].append(raw)
    font_fix_map = {}
    for font, texts in samples.items():
        sample = "".join(texts)
        font_fix_map[font] = detect_font_fix(sample)
    print(f"  [fonts] карта фиксов кодировки: {font_fix_map}", file=sys.stderr)
    return font_fix_map


def _norm_color(c):
    """(r,g,b) 0..1 -> [R,G,B] 0..255, округлено. None -> None."""
    if c is None:
        return None
    return [round(v * 255) for v in c]


def _norm_color_int(c):
    """PyMuPDF span['color'] -- одно int-число (sRGB упаковано). Разложим в [R,G,B]."""
    if c is None:
        return None
    return [(c >> 16) & 255, (c >> 8) & 255, c & 255]


def extract_raw_page(page, page_num, font_fix_map=None):
    font_fix_map = font_fix_map or {}
    result = {
        "page_number": page_num,
        "width": page.rect.width,
        "height": page.rect.height,
        "text_spans": [],
        "lines": [],
        "shapes": [],   # кривые/прямоугольники/четырёхугольники -- точки соединения, дуги, залитые символы
    }

    text_dict = page.get_text("dict")
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                raw = span.get("text", "")
                if not raw.strip():
                    continue
                font = span.get("font")
                if font in font_fix_map:
                    fixed_text = apply_font_fix(raw, font_fix_map[font])
                else:
                    fixed_text = fix_text(raw)  # шрифт не встречался при analyze_fonts -- fallback
                result["text_spans"].append({
                    "text": fixed_text,
                    "bbox": [round(v, 2) for v in span["bbox"]],
                    "font": font,
                    "size": round(span.get("size", 0), 2),
                    "color": _norm_color_int(span.get("color")),
                })

    for d in page.get_drawings():
        stroke_color = _norm_color(d.get("color"))
        fill_color = _norm_color(d.get("fill"))
        width = d.get("width")
        dashes = d.get("dashes") or None
        draw_type = d.get("type")  # "f" заливка, "s" контур, "fs" оба

        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                result["lines"].append({
                    "x1": round(p1.x, 2), "y1": round(p1.y, 2),
                    "x2": round(p2.x, 2), "y2": round(p2.y, 2),
                    "width": width,
                    "color": stroke_color,
                    "dashes": dashes,
                })
            elif kind == "c":
                # кривая Безье: item[1..4] -- четыре точки (начало, 2 контр., конец)
                pts = item[1:5]
                xs = [p.x for p in pts]
                ys = [p.y for p in pts]
                result["shapes"].append({
                    "kind": "curve",
                    "bbox": [round(min(xs), 2), round(min(ys), 2),
                             round(max(xs), 2), round(max(ys), 2)],
                    "fill": fill_color,
                    "stroke": stroke_color,
                    "draw_type": draw_type,
                })
            elif kind == "re":
                rect = item[1]
                result["shapes"].append({
                    "kind": "rect",
                    "bbox": [round(rect.x0, 2), round(rect.y0, 2),
                             round(rect.x1, 2), round(rect.y1, 2)],
                    "fill": fill_color,
                    "stroke": stroke_color,
                    "draw_type": draw_type,
                })
            elif kind == "qu":
                quad = item[1]
                xs = [quad.ul.x, quad.ur.x, quad.ll.x, quad.lr.x]
                ys = [quad.ul.y, quad.ur.y, quad.ll.y, quad.lr.y]
                result["shapes"].append({
                    "kind": "quad",
                    "bbox": [round(min(xs), 2), round(min(ys), 2),
                             round(max(xs), 2), round(max(ys), 2)],
                    "fill": fill_color,
                    "stroke": stroke_color,
                    "draw_type": draw_type,
                })

    return result


def extract_raw(pdf_path):
    print("  [fonts] анализ шрифтов для определения фикса кодировки...", file=sys.stderr)
    font_fix_map = analyze_fonts(pdf_path)
    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        pages.append(extract_raw_page(page, i + 1, font_fix_map))
        print(f"  [raw] page {i+1}/{len(doc)}: "
              f"{len(pages[-1]['text_spans'])} spans, "
              f"{len(pages[-1]['lines'])} lines", file=sys.stderr)
    return pages, font_fix_map


def merge_split_tags(raw_pages, profile=None):
    """
    Иногда EPLAN кладёт одно обозначение как ДВА отдельных текстовых
    объекта PDF ("XM" и "9" рядом, без пробела, вместо единого "XM9").
    Span-парсер (что на PyMuPDF, что на pdfplumber) видит это как два
    разных span'а, и regex-классификатор не видит "XM9" целиком --
    только обрывки "XM" и "9" по отдельности.

    Склейка НЕ агрессивная: два соседних span'а на одной визуальной
    строке (тот же шрифт, близкий baseline) объединяются, ТОЛЬКО если
    их конкатенация образует распознаваемый тег (device_tag/instrument
    tag), а первый из них сам по себе ещё НЕ был валидным тегом. Это
    исключает случайное слипание двух не связанных подписей, стоящих
    рядом (частый риск при "склеить всё подряд, что близко").
    """
    profile = profile or profiles.DEFAULT_PROFILE
    device_tag_re = profile.device_tag_re
    device_tag_complete_re = profile.device_tag_complete_re
    kks_tag_re = profile.kks_tag_re

    merged_count = 0
    Y_TOL = 2.0
    # Реальный зазор между "XM" и "9" на схеме ~8.8pt -- между ними
    # нарисован маленький кружок разъёма (векторная графика, не текст),
    # поэтому зазор больше, чем можно было бы ожидать для соседних букв.
    # Риск ложного слияния ограничен тем, что merge всё равно срабатывает
    # только когда `a` -- "голый" префикс тега БЕЗ цифры (already_valid
    # ложно только в этом случае), что редкая, специфичная ситуация.
    X_GAP_MAX = 10.0
    # device_tag_re допускает 0 цифр (нужно для реестра тегов без номера),
    # но это значит, что голое "XM" само по себе уже "проходит" этот
    # regex -- и проверка "a само по себе ещё не валиден" никогда не
    # сработает. Для этой конкретной проверки нужен более строгий вариант
    # (device_tag_complete_re), требующий хотя бы одну цифру.

    for page in raw_pages:
        spans = page["text_spans"]
        order = sorted(range(len(spans)), key=lambda i: spans[i]["bbox"][0])

        to_remove = set()
        new_spans = []
        for pos in range(len(order) - 1):
            ia = order[pos]
            if ia in to_remove:
                continue
            a = spans[ia]
            # ищем ближайшего кандидата справа в пределах разумного окна по x
            for pos2 in range(pos + 1, min(pos + 60, len(order))):
                ib = order[pos2]
                if ib in to_remove:
                    continue
                b = spans[ib]
                if a.get("font") != b.get("font"):
                    continue
                gap = b["bbox"][0] - a["bbox"][2]
                if gap > X_GAP_MAX:
                    break  # дальше по x только хуже -- список отсортирован
                if gap < -0.5:
                    continue
                if abs(a["bbox"][1] - b["bbox"][1]) > Y_TOL:
                    continue
                combo = a["text"] + b["text"]
                already_valid = device_tag_complete_re.match(a["text"]) or kks_tag_re.match(a["text"])
                combo_valid = device_tag_re.match(combo) or kks_tag_re.match(combo)
                # Не склеивать через границу самостоятельной сущности: если левый
                # или правый span сам по себе -- уже осмысленный токен (межлистовая
                # ссылка "4.7.0", готовый номер и т.п.), их нельзя слипать в
                # псевдо-тег. Без этой проверки широкий device-regex профиля B
                # склеивал power-pin "N" + ссылку "4.7.0" в фейковый "N4.7.0",
                # уничтожая и ссылку, и power-pin.
                b_is_standalone = (profile.parse_cross_ref(b["text"]) is not None
                                   or profile.parse_cross_ref(a["text"]) is not None)
                if combo_valid and not already_valid and not b_is_standalone:
                    merged_bbox = [
                        min(a["bbox"][0], b["bbox"][0]), min(a["bbox"][1], b["bbox"][1]),
                        max(a["bbox"][2], b["bbox"][2]), max(a["bbox"][3], b["bbox"][3]),
                    ]
                    new_spans.append({
                        "text": combo, "bbox": merged_bbox,
                        "font": a.get("font"), "size": a.get("size"),
                        "color": a.get("color"), "merged_from": [a["text"], b["text"]],
                    })
                    to_remove.add(ia)
                    to_remove.add(ib)
                    merged_count += 1
                    break

        if to_remove:
            page["text_spans"] = [s for i, s in enumerate(spans) if i not in to_remove] + new_spans

    print(f"  [merge_tags] склеено разбитых тегов: {merged_count}", file=sys.stderr)
    return raw_pages


# ============================================================
# ЧАСТЬ 2: классификация текстовых span'ов по типу
# ============================================================
# ВНИМАНИЕ: сами regex'ы классификации теперь живут в profiles.py (по одному
# набору на шаблон оформления). Здесь их больше НЕТ -- правьте profiles.py
# (PROFILE_A для старого шаблона Regul/KKS, PROFILE_B для ОВЕН/IEC).


def classify_span(text, size, bbox=None, page_width=None, page_height=None, profile=None):
    """Тип одного текстового span'а. Сами правила живут в профиле (profiles.py):
    для разных бюро форматы обозначений разные, а логика пайплайна -- одна."""
    profile = profile or profiles.DEFAULT_PROFILE
    t = text.strip()
    return profile.classify_span(t, size, bbox, page_width, page_height)


def classify_pages(raw_pages, profile=None):
    profile = profile or profiles.DEFAULT_PROFILE
    counts = defaultdict(int)
    for page in raw_pages:
        for span in page["text_spans"]:
            cls = classify_span(span["text"], span["size"],
                                 bbox=span["bbox"],
                                 page_width=page["width"],
                                 page_height=page["height"],
                                 profile=profile)
            span["entity_type"] = cls
            counts[cls] += 1
    print(f"  [classify] профиль «{profile.name}», entity type counts:",
          dict(counts), file=sys.stderr)
    return raw_pages


# ============================================================
# ЧАСТЬ 3: граф связей (склейка проводов + подписи на концах)
# ============================================================

BORDER_WIDTH_MIN = 0.9
BORDER_WIDTH_MAX = 1.05
GLYPH_MAX_LEN = 3.0
GLYPH_MAX_WIDTH = 0.3
SNAP_TOL = 0.15
LABEL_TYPES = ("terminal_no", "pin_ref", "wire_gauge", "device_tag",
               "instrument_tag", "power_pin")


def _dist(x1, y1, x2, y2):
    return math.hypot(x1 - x2, y1 - y2)


def _seg_len(l):
    return _dist(l["x1"], l["y1"], l["x2"], l["y2"])


def _detect_border_bbox(lines, page_width, page_height, edge_tol=2.0):
    def touches_page_edge(l):
        for x, y in [(l["x1"], l["y1"]), (l["x2"], l["y2"])]:
            if (x <= edge_tol or x >= page_width - edge_tol or
                    y <= edge_tol or y >= page_height - edge_tol):
                return True
        return False

    border_lines = [l for l in lines
                     if l.get("width") and BORDER_WIDTH_MIN <= l["width"] <= BORDER_WIDTH_MAX
                     and _seg_len(l) > 100 and not touches_page_edge(l)]
    if not border_lines:
        return None
    xs = [l["x1"] for l in border_lines] + [l["x2"] for l in border_lines]
    ys = [l["y1"] for l in border_lines] + [l["y2"] for l in border_lines]
    return {"x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)}


def _inside_bbox(l, bbox, tol=1.0):
    if bbox is None:
        return True
    return (bbox["x0"] - tol <= l["x1"] <= bbox["x1"] + tol and
            bbox["y0"] - tol <= l["y1"] <= bbox["y1"] + tol and
            bbox["x0"] - tol <= l["x2"] <= bbox["x1"] + tol and
            bbox["y0"] - tol <= l["y2"] <= bbox["y1"] + tol)


def _filter_wire_candidates(lines, page_width, page_height):
    bbox = _detect_border_bbox(lines, page_width, page_height)
    candidates = []
    for l in lines:
        w = l.get("width") or 0
        length = _seg_len(l)
        if BORDER_WIDTH_MIN <= w <= BORDER_WIDTH_MAX and length > 100:
            continue
        if not _inside_bbox(l, bbox):
            continue
        candidates.append(l)
    return candidates, bbox


def _chain_segments(lines):
    def key(x, y):
        return (round(x / SNAP_TOL), round(y / SNAP_TOL))

    adj = defaultdict(list)
    for i, l in enumerate(lines):
        p1, p2 = (l["x1"], l["y1"]), (l["x2"], l["y2"])
        adj[key(*p1)].append(i)
        adj[key(*p2)].append(i)

    visited = set()
    polylines = []
    for i, l in enumerate(lines):
        if i in visited:
            continue
        comp = set()
        stack = [i]
        while stack:
            cur = stack.pop()
            if cur in comp:
                continue
            comp.add(cur)
            cl = lines[cur]
            for pt in [(cl["x1"], cl["y1"]), (cl["x2"], cl["y2"])]:
                for j in adj[key(*pt)]:
                    if j not in comp:
                        stack.append(j)
        visited |= comp
        pts = []
        for idx in comp:
            cl = lines[idx]
            pts.append((cl["x1"], cl["y1"]))
            pts.append((cl["x2"], cl["y2"]))
        polylines.append({
            "line_indices": sorted(comp),
            "endpoint_a": min(pts, key=lambda p: (p[0], p[1])),
            "endpoint_b": max(pts, key=lambda p: (p[0], p[1])),
            "n_segments": len(comp),
        })
    return polylines


def _span_center(bbox):
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def _nearest_span(x, y, spans, max_dist, allowed_types):
    best, best_d = None, max_dist
    for s in spans:
        if s["entity_type"] not in allowed_types:
            continue
        cx, cy = _span_center(s["bbox"])
        d = _dist(x, y, cx, cy)
        if d < best_d:
            best_d = d
            best = s
    return best, best_d


def _attach_labels(x, y, spans, max_link_dist, label_types=LABEL_TYPES):
    labels = []
    for lt in label_types:
        span, d = _nearest_span(x, y, spans, max_link_dist, {lt})
        if span:
            labels.append({"type": lt, "text": span["text"], "dist": round(d, 2)})
    return labels


def build_page_graph(page, max_link_dist=12.0, profile=None):
    profile = profile or profiles.DEFAULT_PROFILE
    label_types = profile.label_types
    raw_lines = page["lines"]
    spans = page["text_spans"]

    wire_candidates, border_bbox = _filter_wire_candidates(
        raw_lines, page["width"], page["height"])
    polylines = _chain_segments(wire_candidates)

    nodes, edges = [], []
    for pl in polylines:
        ax, ay = pl["endpoint_a"]
        bx, by = pl["endpoint_b"]
        if pl["n_segments"] == 1:
            only_idx = pl["line_indices"][0]
            l = wire_candidates[only_idx]
            if _seg_len(l) < GLYPH_MAX_LEN and (l.get("width") or 0) < GLYPH_MAX_WIDTH:
                _, da = _nearest_span(ax, ay, spans, max_link_dist, set(label_types))
                _, db = _nearest_span(bx, by, spans, max_link_dist, set(label_types))
                if da is None and db is None:
                    continue

        na = {"id": len(nodes), "page": page["page_number"], "x": ax, "y": ay,
              "labels": _attach_labels(ax, ay, spans, max_link_dist, label_types)}
        nodes.append(na)
        nb = {"id": len(nodes), "page": page["page_number"], "x": bx, "y": by,
              "labels": _attach_labels(bx, by, spans, max_link_dist, label_types)}
        nodes.append(nb)
        edges.append({
            "page": page["page_number"],
            "x1": ax, "y1": ay, "x2": bx, "y2": by,
            "endpoints": [na["id"], nb["id"]],
            "n_segments": pl["n_segments"],
        })

    cross_links = []
    # сортируем span'ы по строкам (округлённый y0) и x -- нужно для поиска
    # "слова прямо перед cross-ref'ом на той же строке" (см. ниже)
    sorted_spans = sorted(spans, key=lambda s: (round(s["bbox"][1]), s["bbox"][0]))
    PIN_REF_FULL_RE = re.compile(r'^([A-ZА-Я]{1,4}\d{1,4}):([A-Z0-9]{1,3})$')

    for i, s in enumerate(sorted_spans):
        if s["entity_type"] != "cross_ref":
            continue
        parsed = profile.parse_cross_ref(s["text"])
        if not parsed:
            continue
        cx, cy = _span_center(s["bbox"])
        near_node, best_d = None, 25.0
        for node in nodes:
            d = _dist(cx, cy, node["x"], node["y"])
            if d < best_d:
                best_d = d
                near_node = node["id"]

        # device_pin: обозначение вывода/устройства прямо перед этой
        # ссылкой на той же строке (напр. "AA14:B1 / 4.9:D" -- ищем
        # "AA14:B1" среди 1-3 предыдущих span'ов той же строки).
        # Это прямая текстовая привязка -- надёжнее, чем "ближайший узел
        # графа по координатам", когда обе они доступны рядом.
        device_pin = None
        for prev in sorted_spans[max(0, i - 3):i]:
            if abs(prev["bbox"][1] - s["bbox"][1]) <= 4:
                if PIN_REF_FULL_RE.match(prev["text"]):
                    device_pin = prev["text"]
                    break
                if prev["entity_type"] == "device_tag":
                    device_pin = prev["text"]
                    break

        cross_links.append({
            "from_page": page["page_number"],
            "raw_text": s["text"],
            "target_sheet": parsed["target_sheet"],
            "target_col": parsed["target_col"],
            "target_zone": parsed["target_zone"],
            "near_node_id": near_node,
            "device_pin": device_pin,
        })

    return nodes, edges, cross_links


def build_graph(classified_pages, max_link_dist=12.0, profile=None):
    profile = profile or profiles.DEFAULT_PROFILE
    all_nodes, all_edges, all_cross_links = [], [], []
    offset = 0
    for page in classified_pages:
        nodes, edges, cross_links = build_page_graph(page, max_link_dist, profile)
        for n in nodes:
            n["id"] += offset
        for e in edges:
            e["endpoints"] = [ep + offset for ep in e["endpoints"]]
        for cl in cross_links:
            if cl["near_node_id"] is not None:
                cl["near_node_id"] += offset
        offset += len(nodes)
        all_nodes.extend(nodes)
        all_edges.extend(edges)
        all_cross_links.extend(cross_links)
        print(f"  [graph] page {page['page_number']}: {len(nodes)} nodes, "
              f"{len(edges)} wires, {len(cross_links)} cross-refs", file=sys.stderr)

    return {
        "nodes": all_nodes,
        "edges": all_edges,
        "cross_page_links": all_cross_links,
        "summary": {
            "total_nodes": len(all_nodes),
            "total_edges": len(all_edges),
            "total_cross_links": len(all_cross_links),
        },
    }


# ============================================================
# ЧАСТЬ 4: netlist по каналам ввода/вывода (модуль -> канал -> сигнал)
# ============================================================
# Портировано из отдельного скрипта (qwen_1.py) с починкой бага: там
# использовался built up description с regex-очисткой ПОСЛЕ decode,
# но regex искал старые mojibake-байты, которых после smart_decode()
# уже не было -- поэтому текст штампа листа утекал в описание сигнала
# (~16% записей были "грязными"). Здесь фильтрация построчная, до
# накопления в description, и паттерны штампа написаны на уже
# декодированной кириллице.

# Паттерны модуля/канала/тегов/мусора для netlist -- в profiles.py (nl_* поля
# профиля). PROFILE_A -- старый шаблон (модули [A-C]A\d{2}, KKS "00..."),
# PROFILE_B -- ОВЕН (МВ210/МУ210). Правьте там, не здесь.


def extract_netlist(pdf_path, profile=None):
    profile = profile or profiles.DEFAULT_PROFILE
    nl_module_re = profile.nl_module_re
    nl_channel_re = profile.nl_channel_re
    nl_kks_re = profile.nl_kks_re
    nl_eq_re = profile.nl_eq_re
    nl_junk_patterns = profile.nl_junk_patterns

    doc = fitz.open(pdf_path)
    netlist_dict = {}
    current_module = None
    current_key = None

    def finalize(key):
        if key and key in netlist_dict:
            data = netlist_dict[key]
            data["description"] = ' '.join(data["description"].split()).strip()
            tags = list(set(data["tags"]))
            data["tags"] = [t for t in tags
                             if not re.match(r'^\d+W\d+$', t)
                             and 'мм' not in t and len(t) > 2
                             and not re.match(r'^RB\d{2}', t)
                             and not re.match(r'^RA\d{2}', t)]

    for page_num in range(len(doc)):
        page = doc[page_num]
        raw_text = page.get_text("text")
        lines = [ln.strip() for ln in raw_text.split('\n') if ln.strip()]

        for line in lines:
            decoded = fix_text(line) if not re.search(r'[а-яА-ЯёЁ]', line) else line

            mod_match = nl_module_re.match(decoded)
            if mod_match:
                finalize(current_key)
                current_module = mod_match.group(1)
                current_key = None
                continue

            chan_match = nl_channel_re.match(decoded)
            if chan_match and current_module:
                finalize(current_key)
                channel = chan_match.group(0).replace(' ', '')
                current_key = f"{page_num + 1}_{current_module}_{channel}"
                if current_key not in netlist_dict:
                    netlist_dict[current_key] = {
                        "page": page_num + 1, "module": current_module,
                        "channel": channel, "description": "", "tags": [],
                    }
                continue

            if not current_key:
                continue

            if any(jp.search(decoded) for jp in nl_junk_patterns):
                continue
            if len(decoded) < 3 and not re.search(r'[а-яА-ЯёЁ]', decoded):
                continue

            kks_tags = nl_kks_re.findall(decoded)
            eq_tags = [t for t in nl_eq_re.findall(decoded) if not re.match(r'^\d+W\d+$', t)]
            netlist_dict[current_key]["tags"].extend(kks_tags)
            netlist_dict[current_key]["tags"].extend(eq_tags)

            if re.search(r'[а-яА-ЯёЁ]', decoded):
                clean_line = decoded
                for tag in kks_tags + eq_tags:
                    clean_line = clean_line.replace(tag, '')
                clean_line = clean_line.strip()
                if len(clean_line) > 2:
                    netlist_dict[current_key]["description"] += " " + clean_line

    finalize(current_key)
    netlist = list(netlist_dict.values())
    print(f"  [netlist] {len(netlist)} каналов извлечено", file=sys.stderr)
    return netlist


# ============================================================
# ЧАСТЬ 5: геометрические "кандидаты на ошибку" -- то, что раньше
# можно было увидеть только глазами на картинке. Здесь -- НЕ вердикт
# "это ошибка", а список мест, куда стоит посмотреть; финальное
# решение по-прежнему за нейросетью/инженером.
# ============================================================

DOT_MAX_SIZE = 3.0          # pt, максимальный размер точки соединения
DOT_ASPECT_MAX = 2.5        # точка почти круглая/квадратная, не полоска
CROSSING_DOT_TOL = 2.5      # pt, точка считается "на пересечении", если ближе этого
ENDPOINT_TOL = 0.3          # pt, пересечение у самого конца отрезка не считаем "крестом"


def _is_dot_shape(shape):
    if shape.get("fill") is None:
        return False
    x0, y0, x1, y1 = shape["bbox"]
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0 or w > DOT_MAX_SIZE or h > DOT_MAX_SIZE:
        return False
    ratio = max(w, h) / max(min(w, h), 0.01)
    return ratio < DOT_ASPECT_MAX


def detect_junction_dots(page):
    dots = []
    for sh in page.get("shapes", []):
        if _is_dot_shape(sh):
            x0, y0, x1, y1 = sh["bbox"]
            dots.append({"x": round((x0 + x1) / 2, 2), "y": round((y0 + y1) / 2, 2)})
    return dots


def _seg_intersect(l1, l2, eps=1e-9):
    """Точка пересечения двух отрезков (внутри обоих), либо None."""
    ax1, ay1, ax2, ay2 = l1["x1"], l1["y1"], l1["x2"], l1["y2"]
    bx1, by1, bx2, by2 = l2["x1"], l2["y1"], l2["x2"], l2["y2"]
    d1x, d1y = ax2 - ax1, ay2 - ay1
    d2x, d2y = bx2 - bx1, by2 - by1
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < eps:
        return None
    t = ((bx1 - ax1) * d2y - (by1 - ay1) * d2x) / denom
    u = ((bx1 - ax1) * d1y - (by1 - ay1) * d1x) / denom
    if not (0.0 <= t <= 1.0 and 0.0 <= u <= 1.0):
        return None
    # пересечение почти у конца отрезка -- это не "крест", а стык/угол,
    # такие случаи уже обработаны склейкой полилиний
    if min(t, 1 - t) * _seg_len(l1) < ENDPOINT_TOL:
        return None
    if min(u, 1 - u) * _seg_len(l2) < ENDPOINT_TOL:
        return None
    return (ax1 + t * d1x, ay1 + t * d1y)


def find_wire_crossings(page, wire_candidates):
    """Все X-пересечения проводов на листе + есть ли рядом точка соединения."""
    dots = detect_junction_dots(page)
    crossings = []
    n = len(wire_candidates)
    for i in range(n):
        for j in range(i + 1, n):
            pt = _seg_intersect(wire_candidates[i], wire_candidates[j])
            if pt is None:
                continue
            ix, iy = pt
            has_dot = any(_dist(ix, iy, d["x"], d["y"]) <= CROSSING_DOT_TOL for d in dots)
            crossings.append({
                "page": page["page_number"],
                "x": round(ix, 2), "y": round(iy, 2),
                "has_junction_dot": has_dot,
            })
    return crossings


# Обозначение клеммной колодки для проверки порядка клемм -- в profiles.py
# (profile.connector_tag_re), свой вариант на каждый шаблон.


def check_terminal_order(page, max_pair_dx=60.0, y_tol=3.0, profile=None):
    """Клеммы одной физической колодки (XA/XM/XB/XT/XP...) должны идти по
    номерам последовательно слева направо. В отличие от первой версии
    (группировка по голой y-полосе листа), здесь номер клеммы сначала
    привязывается к БЛИЖАЙШЕМУ обозначению разъёма на той же строке --
    это разделяет соседние колодки и не путает их с повторяющимися
    паттернами вроде "9 18 27 36" у многоканальных DI/DO-модулей
    (там это не порядковые номера клемм одной рейки, а номера жил в
    разных группах кабеля, они закономерно повторяются)."""
    profile = profile or profiles.DEFAULT_PROFILE
    connector_tag_re = profile.connector_tag_re
    spans = page["text_spans"]
    anchors = [s for s in spans
               if s.get("entity_type") == "device_tag"
               and connector_tag_re.match(s["text"].strip())]
    terms = [s for s in spans if s.get("entity_type") in ("terminal_no", "pin_ref")]

    groups = defaultdict(list)  # (тег_разъёма, номер_строки_по_y) -> [(x, текст)]
    for t in terms:
        tx, ty = _span_center(t["bbox"])
        best_a, best_d = None, max_pair_dx
        for a in anchors:
            ax, ay = _span_center(a["bbox"])
            if abs(ay - ty) > y_tol:
                continue
            d = tx - ax  # клемма обычно правее своего обозначения разъёма
            if 0 <= d < best_d:
                best_d = d
                best_a = a
        if best_a is None:
            continue
        row_key = (best_a["text"], round(ty / (y_tol * 2)) * (y_tol * 2))
        groups[row_key].append((tx, t["text"]))

    anomalies = []
    for (tag, y), items in groups.items():
        items = sorted(items, key=lambda p: p[0])
        if len(items) < 3:
            continue
        nums = []
        for _, txt in items:
            digits = re.sub(r'\D', '', txt)
            nums.append(int(digits) if digits else -1)
        pairs = list(zip(nums, nums[1:]))
        inc = sum(1 for a, b in pairs if b > a)
        ratio = inc / len(pairs) if pairs else 1.0
        if ratio < 0.9:
            anomalies.append({
                "page": page["page_number"],
                "connector": tag,
                "row_y": round(y, 2),
                "sequence": [t for _, t in items],
            })
    return anomalies


def check_wire_gauge_vs_width(page, wire_candidates, max_dist=8.0):
    """Сверяем подписанное сечение провода ('4 мм²' и т.п.) с реальной
    толщиной ближайшей линии -- ищем случаи, где подпись и штрих не
    соответствуют друг другу (например, силовая жила подписана как 0,5 мм²,
    но нарисована той же толщиной, что и соседние силовые 4 мм²)."""
    gauge_spans = [s for s in page["text_spans"] if s.get("entity_type") == "wire_gauge"]
    results = []
    for s in gauge_spans:
        cx, cy = _span_center(s["bbox"])
        best_w, best_d = None, max_dist
        for l in wire_candidates:
            mx, my = (l["x1"] + l["x2"]) / 2, (l["y1"] + l["y2"]) / 2
            d = _dist(cx, cy, mx, my)
            if d < best_d and (l.get("width") or 0) > 0:
                best_d = d
                best_w = l.get("width")
        if best_w is not None:
            results.append({
                "page": page["page_number"],
                "label": s["text"],
                "nearest_line_width_pt": best_w,
                "dist": round(best_d, 2),
            })
    return results


def build_issue_candidates(classified_pages, profile=None):
    profile = profile or profiles.DEFAULT_PROFILE
    all_crossings, all_terminal_anomalies, all_gauge_checks = [], [], []
    for page in classified_pages:
        wire_candidates, _ = _filter_wire_candidates(
            page["lines"], page["width"], page["height"])

        crossings = find_wire_crossings(page, wire_candidates)
        all_crossings.extend(crossings)

        term_anomalies = check_terminal_order(page, profile=profile)
        all_terminal_anomalies.extend(term_anomalies)
        all_gauge_checks.extend(check_wire_gauge_vs_width(page, wire_candidates))

        print(f"  [issues] page {page['page_number']}: "
              f"{len(crossings)} crossings "
              f"({sum(1 for c in crossings if c['has_junction_dot'])} with dot), "
              f"{len(term_anomalies)} terminal-order anomalies",
              file=sys.stderr)

    crossings_no_dot = [c for c in all_crossings if not c["has_junction_dot"]]
    crossings_with_dot = [c for c in all_crossings if c["has_junction_dot"]]

    return {
        "wire_crossings_with_dot": crossings_with_dot,
        "wire_crossings_without_dot": crossings_no_dot,
        "terminal_order_anomalies": all_terminal_anomalies,
        "wire_gauge_vs_line_width": all_gauge_checks,
        "summary": {
            "total_crossings": len(all_crossings),
            "crossings_with_dot": len(crossings_with_dot),
            "crossings_without_dot": len(crossings_no_dot),
            "terminal_order_anomalies": len(all_terminal_anomalies),
            "note": ("Это НЕ список подтверждённых ошибок, а геометрические "
                     "кандидаты для дальнейшей проверки (нейросетью или "
                     "инженером). crossings_with_dot стоит смотреть в первую "
                     "очередь -- там, где на схеме предполагалось простое "
                     "пересечение без соединения, точка не должна стоять."),
        },
    }


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================

def extract_to_dir(pdf_path, out_dir):
    """Точка входа для пайплайна (ingest.py).

    Прогоняет все 5 стадий и кладёт JSON-файлы в out_dir.
    Возвращает (список созданных файлов, краткая статистика) - это уходит
    в manifest.json, чтобы агент сразу видел объём данных, не открывая файлы.
    """
    os.makedirs(out_dir, exist_ok=True)

    raw_pages, font_fix_map = extract_raw(pdf_path)
    profile = profiles.detect_profile(raw_pages, font_fix_map)
    with open(os.path.join(out_dir, "raw.json"), "w", encoding="utf-8") as f:
        json.dump({"profile": profile.name, "font_fix_map": font_fix_map,
                   "pages": raw_pages},
                  f, ensure_ascii=False, indent=1)

    raw_pages = merge_split_tags(raw_pages, profile)

    classified_pages = classify_pages(raw_pages, profile)
    with open(os.path.join(out_dir, "classified.json"), "w", encoding="utf-8") as f:
        json.dump(classified_pages, f, ensure_ascii=False, indent=1)

    graph = build_graph(classified_pages, profile=profile)
    with open(os.path.join(out_dir, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=1)

    netlist = extract_netlist(pdf_path, profile)
    with open(os.path.join(out_dir, "netlist.json"), "w", encoding="utf-8") as f:
        json.dump(netlist, f, ensure_ascii=False, indent=1)

    issues = build_issue_candidates(classified_pages, profile)
    with open(os.path.join(out_dir, "issues_candidates.json"), "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=1)

    files = ["raw.json", "classified.json", "graph.json",
             "netlist.json", "issues_candidates.json"]
    summary = {
        "profile": profile.name,
        "total_pages": len(raw_pages),
        "graph_nodes": graph["summary"]["total_nodes"],
        "graph_edges": graph["summary"]["total_edges"],
        "cross_page_links": graph["summary"]["total_cross_links"],
        "io_channels": len(netlist),
        "wire_crossings_with_dot": issues["summary"]["crossings_with_dot"],
        "wire_crossings_without_dot": issues["summary"]["crossings_without_dot"],
        "terminal_order_anomalies": issues["summary"]["terminal_order_anomalies"],
    }
    return files, summary


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 extract_pipeline.py input.pdf output_dir/")
        sys.exit(1)

    pdf_path, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    print("=== 1/4: сырое извлечение текста и линий из PDF ===", file=sys.stderr)
    raw_pages, font_fix_map = extract_raw(pdf_path)
    profile = profiles.detect_profile(raw_pages, font_fix_map)
    with open(os.path.join(out_dir, "raw.json"), "w", encoding="utf-8") as f:
        json.dump({"profile": profile.name, "font_fix_map": font_fix_map,
                   "pages": raw_pages},
                   f, ensure_ascii=False, indent=1)

    print("=== 1.5/4: склейка разбитых тегов (напр. 'XM'+'9' -> 'XM9') ===", file=sys.stderr)
    raw_pages = merge_split_tags(raw_pages, profile)

    print("=== 2/4: классификация текстовых span'ов ===", file=sys.stderr)
    classified_pages = classify_pages(raw_pages, profile)
    with open(os.path.join(out_dir, "classified.json"), "w", encoding="utf-8") as f:
        json.dump(classified_pages, f, ensure_ascii=False, indent=1)

    print("=== 3/4: построение графа связей ===", file=sys.stderr)
    graph = build_graph(classified_pages, profile=profile)
    with open(os.path.join(out_dir, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=1)

    print("=== 4/5: netlist по каналам ввода/вывода ===", file=sys.stderr)
    netlist = extract_netlist(pdf_path, profile)
    with open(os.path.join(out_dir, "netlist.json"), "w", encoding="utf-8") as f:
        json.dump(netlist, f, ensure_ascii=False, indent=1)

    print("=== 5/5: геометрические кандидаты (точки соединения, порядок клемм, "
          "сечение vs толщина линии) ===", file=sys.stderr)
    issues = build_issue_candidates(classified_pages, profile)
    with open(os.path.join(out_dir, "issues_candidates.json"), "w", encoding="utf-8") as f:
        json.dump(issues, f, ensure_ascii=False, indent=1)

    print("\n=== ГОТОВО ===")
    print(f"Листов обработано: {len(raw_pages)}")
    print(f"Узлов графа: {graph['summary']['total_nodes']}, "
          f"проводов: {graph['summary']['total_edges']}, "
          f"межлистовых ссылок: {graph['summary']['total_cross_links']}")
    print(f"Каналов в netlist: {len(netlist)}")
    print(f"Пересечений проводов: {issues['summary']['total_crossings']} "
          f"(с точкой: {issues['summary']['crossings_with_dot']}, "
          f"без точки: {issues['summary']['crossings_without_dot']})")
    print(f"Аномалий порядка клемм: {issues['summary']['terminal_order_anomalies']}")
    print(f"\nФайлы сохранены в: {out_dir}/")
    print("  raw.json, classified.json, graph.json, netlist.json, issues_candidates.json")
    print("\nПоиск ошибок/несостыковок в этот скрипт по-прежнему не входит --")
    print("issues_candidates.json -- это геометрические кандидаты для проверки,")
    print("а не готовый вердикт; финальный анализ отдан отдельно нейросети/инженеру.")


if __name__ == "__main__":
    main()
