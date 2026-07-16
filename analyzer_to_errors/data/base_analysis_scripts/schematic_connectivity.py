#!/usr/bin/env python3
"""
Связность схемы EPLAN: настоящие ЦЕПИ (неты) и что к ним подключено.

Зачем нужен отдельный скрипт, если есть schematic_diagram_to_data.py.
Тот скрипт склеивает отрезки в полилинии ТОЛЬКО по совпадению концов. Но в этой
схеме соединение обозначается не жирной точкой (заливок в PDF нет вообще), а
T-СТЫКОМ: конец одного провода упирается в СЕРЕДИНУ другого. Такие стыки старая
склейка не видит - замеры на реальном файле дали ~200 T-стыков на лист, и каждый
разрывал цепь надвое. В graph.json из-за этого лежат не цепи, а их осколки.

Вдобавок для каждой полилинии там сохранялись ровно ДВЕ точки (крайние по
координате), а у цепи бывает 6 и 18 свободных концов - всё, что сверх двух,
терялось. Именно эти концы и есть места подключения к клеммам.

Что делает этот скрипт:
1. Склеивает провода в цепи, учитывая И общие концы, И T-стыки.
2. Для КАЖДОГО свободного конца цепи собирает подписи вокруг него и выводит,
   к какой клемме/устройству/сигналу этот конец подключён.
3. Строит индекс "клемма -> цепи": по нему сразу видно, что с чем соединено на
   схеме, и это можно сравнивать с таблицей подключений (connections.json).
4. Проверяет межлистовые ссылки (/12.4:D): ведёт ли ссылка на существующий лист.

Вход:  PDF схемы
Выход: nets.json, terminals.json

Поиск ошибок сюда НЕ входит - только извлечение фактов. Решение за нейросетью.

Использование:
    python3 schematic_connectivity.py input.pdf output_dir/
"""

import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict

# Переиспользуем сырьё из соседнего базового скрипта: извлечение текста и линий,
# фикс кодировки шрифтов, классификацию надписей, отбор линий-проводов.
_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "_schematic_base", os.path.join(_HERE, "schematic_diagram_to_data.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)


# ============================================================
# Геометрия
# ============================================================

SNAP = 0.15          # точки ближе этого считаем одной (совпадение концов)
T_JUNCTION_TOL = 0.6  # конец провода на середине другого: допуск по расстоянию
LABEL_DIST = 14.0    # радиус поиска подписи вокруг конца цепи

# Подписи, которые имеет смысл привязывать к концу цепи.
LABEL_TYPES = ("device_tag", "instrument_tag", "terminal_no", "pin_ref",
               "power_pin", "wire_gauge", "io_channel", "signal_state",
               "reserve_label", "coil_terminal", "cable_type")


def _key(x, y):
    return (round(x / SNAP), round(y / SNAP))


def _dist(x1, y1, x2, y2):
    return math.hypot(x1 - x2, y1 - y2)


def _point_on_segment(px, py, line, tol=T_JUNCTION_TOL):
    """Точка лежит на ВНУТРЕННЕЙ части отрезка (не у самого конца)?"""
    x1, y1, x2, y2 = line["x1"], line["y1"], line["x2"], line["y2"]
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-6:
        return False
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    if not (0.02 < t < 0.98):   # у самых концов - это обычный стык, он уже склеен
        return False
    return _dist(px, py, x1 + t * dx, y1 + t * dy) <= tol


def build_nets(wires):
    """Склейка проводов в цепи: и по общим концам, и по T-стыкам.

    Возвращает список цепей: {"line_indices": [...], "free_ends": [(x, y), ...]}
    free_ends - концы, где провод ни во что не продолжается: именно там находятся
    клеммы, разъёмы и подписи.
    """
    # 1. Союзы по совпадающим концам
    parent = list(range(len(wires)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    by_point = defaultdict(list)
    for i, l in enumerate(wires):
        by_point[_key(l["x1"], l["y1"])].append(i)
        by_point[_key(l["x2"], l["y2"])].append(i)

    for idxs in by_point.values():
        for j in idxs[1:]:
            union(idxs[0], j)

    # 2. Союзы по T-стыкам: конец провода лежит на середине другого.
    #    Перебор ускорен корзинами по координате: без них это O(n^2) на 500 линий
    #    каждого из 87 листов.
    CELL = 12.0
    grid = defaultdict(list)
    for i, l in enumerate(wires):
        x0, x1 = sorted((l["x1"], l["x2"]))
        y0, y1 = sorted((l["y1"], l["y2"]))
        for cx in range(int(x0 // CELL), int(x1 // CELL) + 1):
            for cy in range(int(y0 // CELL), int(y1 // CELL) + 1):
                grid[(cx, cy)].append(i)

    t_junctions = 0
    for i, l in enumerate(wires):
        for (px, py) in ((l["x1"], l["y1"]), (l["x2"], l["y2"])):
            cx, cy = int(px // CELL), int(py // CELL)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((cx + dx, cy + dy), ()):
                        if i == j or find(i) == find(j):
                            continue
                        if _point_on_segment(px, py, wires[j]):
                            union(i, j)
                            t_junctions += 1

    # 3. Собираем цепи и их свободные концы
    groups = defaultdict(list)
    for i in range(len(wires)):
        groups[find(i)].append(i)

    nets = []
    for idxs in groups.values():
        point_count = Counter()
        for i in idxs:
            l = wires[i]
            point_count[(round(l["x1"], 1), round(l["y1"], 1))] += 1
            point_count[(round(l["x2"], 1), round(l["y2"], 1))] += 1
        free_ends = [pt for pt, n in point_count.items() if n == 1]
        nets.append({"line_indices": sorted(idxs), "free_ends": free_ends})

    return nets, t_junctions


# ============================================================
# Привязка подписей к концам цепи
# ============================================================

def _span_center(bbox):
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def collect_labels(x, y, spans):
    """Все подписи в радиусе LABEL_DIST вокруг точки - по одной ближайшей на тип."""
    best = {}
    for s in spans:
        t = s.get("entity_type")
        if t not in LABEL_TYPES:
            continue
        cx, cy = _span_center(s["bbox"])
        d = _dist(x, y, cx, cy)
        if d > LABEL_DIST:
            continue
        if t not in best or d < best[t]["dist"]:
            best[t] = {"type": t, "text": s["text"], "dist": round(d, 2)}
    return sorted(best.values(), key=lambda l: l["dist"])


PIN_REF_FULL_RE = re.compile(r'^([A-ZА-Я]{1,5}\d{1,4}):([A-Z0-9]{1,4})$')

# Как на листе подписан ВЛАДЕЛЕЦ вывода (замерено на реальном файле):
#   1) Клеммник подписан у КАЖДОГО вывода отдельно, слева от его номера, на той же
#      строке: "XA001" @x=299.5,y=498.3  ->  "9" @x=333.2,y=499.6  (то есть ~33pt).
#      Радиус в 14pt, которым ищутся подписи вокруг конца провода, до него не достаёт -
#      поэтому владельца ищем отдельным правилом, а не "ближайшей подписью".
#   2) У выводов полевых приборов клеммника нет вообще: там владелец - KKS-тег,
#      подписанный СВЕРХУ колонки ("00USE21AA021" @y=80 над выводами @y=187).
OWNER_SAME_ROW_DY = 4.0     # "та же строка"
OWNER_SAME_ROW_DX = 60.0    # клеммник не дальше этого слева от номера вывода
OWNER_ABOVE_DX = 60.0       # KKS над колонкой: допуск по горизонтали
OWNER_ABOVE_DY = 160.0      # ... и по вертикали


def build_pin_owners(spans):
    """Для каждого номера вывода на листе определяет, чей это вывод.

    Возвращает список записей о выводах: координаты, номер, клеммник-владелец
    (device) и/или KKS-владелец. По ним потом опознаются концы цепей.
    """
    pins = [s for s in spans
            if s.get("entity_type") in ("terminal_no", "coil_terminal", "pin_ref")]
    devices = [s for s in spans if s.get("entity_type") == "device_tag"]
    kks_tags = [s for s in spans if s.get("entity_type") == "instrument_tag"]

    records = []
    for p in pins:
        px, py = _span_center(p["bbox"])

        # "AA14:B1" - клеммник и вывод одной подписью, владелец известен сразу
        m = PIN_REF_FULL_RE.match(p["text"].strip())
        if m:
            records.append({"x": px, "y": py, "pin": m.group(2),
                            "device": m.group(1), "kks": None})
            continue

        device, best_dx = None, OWNER_SAME_ROW_DX
        for d in devices:
            dx_span, dy_span = _span_center(d["bbox"])
            if abs(dy_span - py) > OWNER_SAME_ROW_DY:
                continue
            dx = px - dx_span          # клеммник слева от номера вывода
            if 0 < dx < best_dx:
                best_dx, device = dx, d["text"].strip()
        owner_source = "row" if device else None

        # ПРОБОВАЛ И ОТКАЗАЛСЯ: правило "ближайшее обозначение модуля в радиусе 90pt"
        # для блоков RA01/RB03/XT01, которые подписаны один раз на весь модуль, а не
        # у каждого вывода. На реальном файле оно принимало МАРКИРОВКУ ПРОВОДА за
        # номер вывода и приписывало её ближайшему модулю: одна цепь получала концы
        # QF01:3, QF98:3, SFD1:3 - хотя "3" там был номером провода. Совпадений с
        # таблицей это почти не добавило (266 -> 300), зато отняло 222 маркировки
        # цепей, которые правило "съело". Вывод: пусть вывод останется без владельца.
        # Пропущенные данные честнее выдуманных - из выдуманных рождается ложное
        # "несоответствие документов", которое инженер будет искать вручную.

        kks, best_dy = None, OWNER_ABOVE_DY
        if device is None:
            for k in kks_tags:
                kx, ky = _span_center(k["bbox"])
                if abs(kx - px) > OWNER_ABOVE_DX:
                    continue
                dy = py - ky           # KKS выше своих выводов
                if 0 < dy < best_dy:
                    best_dy, kks = dy, k["text"].strip()
            if kks:
                owner_source = "kks_above"

        records.append({"x": px, "y": py, "pin": p["text"].strip(),
                        "device": device, "kks": kks,
                        "owner_source": owner_source})
    return records


def describe_endpoint(x, y, spans, pin_records):
    """Что находится на этом конце цепи: устройство, клемма, сигнал."""
    labels = collect_labels(x, y, spans)
    by_type = {l["type"]: l["text"] for l in labels}

    # ближайший вывод к концу провода - вместе с уже вычисленным владельцем
    pin_rec, best_d = None, LABEL_DIST
    for r in pin_records:
        d = _dist(x, y, r["x"], r["y"])
        if d < best_d:
            best_d, pin_rec = d, r

    device = pin_rec["device"] if pin_rec else None
    pin = pin_rec["pin"] if pin_rec else None
    kks = by_type.get("instrument_tag") or (pin_rec["kks"] if pin_rec else None)

    # владелец не опознан, но рядом стоит обозначение клеммника - берём его
    if device is None and by_type.get("device_tag"):
        device = by_type["device_tag"]

    owner = device or kks

    return {
        "x": round(x, 2),
        "y": round(y, 2),
        "device": device,
        "pin": pin,
        # чем определён владелец вывода: "row" - клеммник подписан на той же строке
        # (надёжно); "nearby" - единственное обозначение модуля в радиусе (надёжно
        # слабее); "kks_above" - вывод полевого прибора, владелец взят из KKS сверху.
        "owner_source": pin_rec.get("owner_source") if pin_rec else None,
        "kks": kks,
        "power": by_type.get("power_pin"),
        "io_channel": by_type.get("io_channel"),
        "signal": by_type.get("signal_state"),
        "wire_gauge": by_type.get("wire_gauge"),
        "cable": by_type.get("cable_type"),
        "reserve": "reserve_label" in by_type,
        "terminal": f"{owner}:{pin}" if owner and pin else None,
        "labels": labels,
    }


MARKING_MAX_DIST = 9.0   # маркировка подписана вплотную к своему проводу


def _point_to_segment_dist(px, py, l):
    x1, y1, x2, y2 = l["x1"], l["y1"], l["x2"], l["y2"]
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-6:
        return _dist(px, py, x1, y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length_sq))
    return _dist(px, py, x1 + t * dx, y1 + t * dy)


def collect_net_markings(net_lines, marking_spans):
    """Маркировка цепи (номер провода), подписанная ВДОЛЬ проводов этой цепи.

    Классификатор относит к terminal_no любое голое число, но по факту это два
    разных смысла: номер вывода (стоит рядом со своим клеммником, на одной строке
    с ним) и МАРКИРОВКА ЦЕПИ - номер провода, подписанный вдоль самого провода
    (в таблице подключений это поле circuit_marking: 47, 48, 752...).
    Владельца-клеммника у маркировки нет - по этому признаку мы её и отличаем.

    Маркировка - самый прямой ключ для сверки со схемой: одна и та же цепь в
    таблице подключений и на схеме несёт один и тот же номер провода.
    """
    markings = set()
    for s in marking_spans:
        for l in net_lines:
            if _point_to_segment_dist(s["x"], s["y"], l) <= MARKING_MAX_DIST:
                markings.add(s["text"])
                break
    return sorted(markings)


CROSS_REF_RE = re.compile(r'^/?(\d{1,3})\.(\d{1,2}):([A-F])$')


def page_cross_refs(page, nets_geom, spans, profile=None):
    """Межлистовые ссылки листа + к какой цепи каждая относится."""
    profile = profile or _base.profiles.DEFAULT_PROFILE
    refs = []
    for s in spans:
        if s.get("entity_type") != "cross_ref":
            continue
        parsed = profile.parse_cross_ref(s["text"])
        if not parsed:
            continue
        cx, cy = _span_center(s["bbox"])
        # ближайшая цепь: ссылка подписана у конца провода, уходящего на другой лист
        best_net, best_d = None, 30.0
        for net_idx, ends in nets_geom:
            for (ex, ey) in ends:
                d = _dist(cx, cy, ex, ey)
                if d < best_d:
                    best_d, best_net = d, net_idx
        refs.append({
            "raw_text": s["text"],
            "target_sheet": parsed["target_sheet"],
            "target_column": parsed["target_col"],
            "target_zone": parsed["target_zone"],
            "net_index": best_net,
            "dist_to_net": round(best_d, 2) if best_net is not None else None,
        })
    return refs


# ============================================================
# Главная сборка
# ============================================================

def build_connectivity(pdf_path):
    raw_pages, font_fix_map = _base.extract_raw(pdf_path)
    profile = _base.profiles.detect_profile(raw_pages, font_fix_map)
    raw_pages = _base.merge_split_tags(raw_pages, profile)
    pages = _base.classify_pages(raw_pages, profile)

    all_nets = []
    all_cross_refs = []
    total_t = 0

    for page in pages:
        page_num = page["page_number"]
        spans = page["text_spans"]
        wires, _bbox = _base._filter_wire_candidates(
            page["lines"], page["width"], page["height"])

        nets, t_junctions = build_nets(wires)
        total_t += t_junctions
        pin_records = build_pin_owners(spans)

        # Числовые подписи без клеммника-владельца - это маркировки цепей
        # (номера проводов), а не номера выводов. См. collect_net_markings.
        marking_spans = [{"x": r["x"], "y": r["y"], "text": r["pin"]}
                         for r in pin_records
                         if not r["device"] and not r["kks"] and r["pin"].isdigit()]
        # Если профиль умеет опознавать маркировку цепи ЯВНО (профиль D:
        # "13N1", "50C3", "A411" -- линия+фаза+сегмент), берём её напрямую:
        # эвристика "голое число без владельца" такие метки не находит вовсе,
        # а именно они -- ключ для сверки с таблицей подключений.
        marking_spans += [{"x": _span_center(s["bbox"])[0],
                           "y": _span_center(s["bbox"])[1],
                           "text": s["text"].strip()}
                          for s in spans if s.get("entity_type") == "wire_marking"]

        nets_geom = []
        page_nets = []
        for i, net in enumerate(nets):
            net_id = f"p{page_num}_n{i}"
            endpoints = [describe_endpoint(x, y, spans, pin_records)
                         for (x, y) in net["free_ends"]]
            net_lines = [wires[j] for j in net["line_indices"]]
            markings = collect_net_markings(net_lines, marking_spans)

            # цепь без единой подписи на концах и без маркировки - это, как правило,
            # элемент рамки или графика символа, а не провод
            named = [e for e in endpoints if e["labels"]]
            if not named and not markings:
                continue

            terminals = sorted({e["terminal"] for e in endpoints if e["terminal"]})
            kks_tags = sorted({e["kks"] for e in endpoints if e["kks"]})
            gauges = sorted({e["wire_gauge"] for e in endpoints if e["wire_gauge"]})

            page_nets.append({
                "id": net_id,
                "page": page_num,
                "n_segments": len(net["line_indices"]),
                "n_endpoints": len(endpoints),
                "wire_markings": markings,
                "terminals": terminals,
                "kks_tags": kks_tags,
                "wire_gauges": gauges,
                "endpoints": endpoints,
            })
            nets_geom.append((net_id, net["free_ends"]))

        refs = page_cross_refs(page, nets_geom, spans, profile)
        for r in refs:
            r["from_sheet"] = page_num
        all_cross_refs.extend(refs)
        all_nets.extend(page_nets)

        print(f"  [nets] лист {page_num}: проводов {len(wires)}, "
              f"цепей с подписями {len(page_nets)}, T-стыков {t_junctions}",
              file=sys.stderr)

    total_sheets = len(pages)
    for r in all_cross_refs:
        # target_sheet=None -- ссылка в пределах того же листа (формат Delta "(:3D)"):
        # целевой лист заведомо существует, это не битая ссылка.
        r["target_sheet_exists"] = (r["target_sheet"] is None
                                    or 1 <= r["target_sheet"] <= total_sheets)

    return all_nets, all_cross_refs, total_sheets, total_t


def build_terminal_index(nets):
    """Клемма (устройство:вывод) -> в каких цепях она встречается.

    Это прямой аналог таблицы подключений, но добытый ИЗ СХЕМЫ: по нему видно,
    что с чем реально соединено проводами, и это можно сверять с connections.json.
    """
    index = defaultdict(list)
    for net in nets:
        for term in net["terminals"]:
            index[term].append(net["id"])

    return {
        term: {
            "nets": net_ids,
            "n_nets": len(net_ids),
            "pages": sorted({int(n.split("_")[0][1:]) for n in net_ids}),
        }
        for term, net_ids in sorted(index.items())
    }


def extract_to_dir(pdf_path, out_dir):
    """Точка входа для пайплайна (ingest.py)."""
    os.makedirs(out_dir, exist_ok=True)

    nets, cross_refs, total_sheets, total_t = build_connectivity(pdf_path)
    terminal_index = build_terminal_index(nets)

    marking_index = defaultdict(list)
    for net in nets:
        for mk in net["wire_markings"]:
            marking_index[mk].append(net["id"])
    marking_index = {mk: sorted(ids) for mk, ids in sorted(marking_index.items())}

    broken_refs = [r for r in cross_refs if not r["target_sheet_exists"]]
    # Цепь ровно с одним концом-подписью, без второй стороны и без межлистовой
    # ссылки - кандидат в "провод в никуда". Это НЕ вердикт, а место для проверки.
    dangling = [n["id"] for n in nets
                if len(n["terminals"]) < 2 and n["n_endpoints"] < 2]

    nets_doc = {
        "domain_notes_for_analysis": [
            "net (цепь) - группа проводов, электрически соединённых между собой. "
            "Склейка учитывает и общие концы отрезков, и T-стыки (конец провода, "
            "упирающийся в середину другого) - в этой схеме соединение обозначается "
            "именно T-стыком, жирных точек соединения в PDF нет.",
            "endpoints - свободные концы цепи, то есть места её подключения. У каждого "
            "конца собраны подписи вокруг него: device (клеммник/модуль), pin (вывод), "
            "kks (тег устройства), io_channel (канал модуля), signal, wire_gauge.",
            "terminals - список клемм вида 'XB010:31', к которым подключена цепь. "
            "ЭТО КЛЮЧ ДЛЯ СВЕРКИ СО СХЕМОЙ: та же клемма в таблице подключений "
            "(connections.json) записана как terminal_block + pin.",
            "Если у цепи в terminals меньше двух клемм - подписи по концам распознались "
            "не полностью (или это действительно оборванный провод). Отсутствие клеммы "
            "в цепи само по себе НЕ ошибка, это повод проверить.",
        ],
        "summary": {
            "total_sheets": total_sheets,
            "total_nets": len(nets),
            "total_t_junctions": total_t,
            "nets_with_2plus_terminals": sum(1 for n in nets if len(n["terminals"]) >= 2),
            "nets_with_1_terminal": sum(1 for n in nets if len(n["terminals"]) == 1),
            "nets_without_terminals": sum(1 for n in nets if not n["terminals"]),
            "dangling_net_candidates": len(dangling),
            "cross_refs_total": len(cross_refs),
            "cross_refs_broken": len(broken_refs),
        },
        "cross_sheet_links": cross_refs,
        "broken_cross_sheet_links": broken_refs,
        "nets": nets,
    }

    with open(os.path.join(out_dir, "nets.json"), "w", encoding="utf-8") as f:
        json.dump(nets_doc, f, ensure_ascii=False, indent=1)

    terminals_doc = {
        "domain_notes_for_analysis": [
            "Индекс клемм, добытый ИЗ СХЕМЫ: клемма -> цепи, в которых она участвует.",
            "Клемма записана как 'ВЛАДЕЛЕЦ:ВЫВОД'. Владелец - либо клеммник/модуль "
            "('XA001:9'), либо KKS полевого прибора ('00USE21AA021:1'), если вывод "
            "принадлежит прибору, а не рейке. В таблице подключений (connections.json) "
            "та же клемма - это поля terminal_block и pin, а физический адрес - "
            "terminal_address = cabinet.terminal_block.pin.",
            "Клемма, найденная в таблице подключений, но отсутствующая в этом индексе, "
            "- кандидат на 'есть в таблице, нет на схеме' (и наоборот).",
            "wire_markings - индекс МАРКИРОВОК ЦЕПЕЙ (номеров проводов), подписанных "
            "вдоль проводов: маркировка -> цепи. В таблице подключений это поле "
            "circuit_marking. Самый прямой ключ для сверки двух документов: один и тот "
            "же провод несёт один и тот же номер и там, и там.",
        ],
        "summary": {
            "total_terminals": len(terminal_index),
            "terminals_in_multiple_nets": sum(
                1 for v in terminal_index.values() if v["n_nets"] > 1),
            "total_wire_markings": len(marking_index),
        },
        "terminals": terminal_index,
        "wire_markings": marking_index,
    }
    with open(os.path.join(out_dir, "terminals.json"), "w", encoding="utf-8") as f:
        json.dump(terminals_doc, f, ensure_ascii=False, indent=1)

    files = ["nets.json", "terminals.json"]
    stats = {
        "nets": len(nets),
        "t_junctions_recovered": total_t,
        "terminals_on_scheme": len(terminal_index),
        "wire_markings_on_scheme": len(marking_index),
        "nets_with_2plus_terminals": nets_doc["summary"]["nets_with_2plus_terminals"],
        "cross_refs_broken": len(broken_refs),
        "dangling_net_candidates": len(dangling),
    }
    return files, stats


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 schematic_connectivity.py input.pdf output_dir/")
        sys.exit(1)

    files, stats = extract_to_dir(sys.argv[1], sys.argv[2])
    print("\n=== ГОТОВО ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"Файлы: {', '.join(files)} -> {sys.argv[2]}/")


if __name__ == "__main__":
    main()
