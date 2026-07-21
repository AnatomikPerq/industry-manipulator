#!/usr/bin/env python3
"""
Стадия ЗРЕНИЯ: модель смотрит на растр листа и восстанавливает то, чего в
данных парсера нет.

ЧТО ИМЕННО ДЕЛАЕТ МОДЕЛЬ - И ЧЕГО НЕ ДЕЛАЕТ. Она НЕ ищет ошибки. Она отвечает
на один вопрос, на который умеют отвечать только глаза: КАКИЕ НОМЕРА ПОДПИСАНЫ
У ОДНОЙ И ТОЙ ЖЕ ЛИНИИ. Судит по её ответу обычный детерминированный чекер -
«все подписи одной цепи обязаны совпадать, одиночка среди одинаковых есть
ошибка». Это то же разделение труда, что уже принято в проекте: агент понимает,
что за изделие, - чекер сравнивает строки.

ПОЧЕМУ ИМЕННО ТАК, А НЕ «НАЙДИ ОШИБКИ НА ЛИСТЕ». Проверка маркировки проводов
уже была написана детерминированно и ОТВЕРГНУТА: 250 ложных находок на трёх
файлах (см. «не вошло» в schematic_rules.py). Замер на листе 10.2 ЩСКЗ
показывает, что именно там ломается: цепь p1_n8 собрала в себя 39 сегментов и
маркировки ['1','10','1010','4','5'] - шины +24 В и 0 В слиплись в одну цепь,
потому что маркировки привязываются к цепи по радиусу. То есть неверна была
ПРИВЯЗКА, а не суждение. Суждение на верной привязке даёт на этом листе ровно
две находки - обе подтверждённые (напечатано «6» там, где должно быть «5», и
«12» там, где «11») - и ни одной ложной.

Поэтому модели поручается ровно привязка, а всё остальное считается по её
ответу арифметикой, которую можно перепроверить и покрыть тестами.

ТРИ ФИЛЬТРА ПРОТИВ ВЫДУМОК. Модель, читающая мелкий шрифт, ошибается, и цена
ошибки здесь та же, что везде в проекте: ложная находка дороже пропуска.
  1. КАЖДОЕ число обязано быть НАПЕЧАТАНО на этом тайле - сверяем с текстовым
     слоем PDF. Число, которого на листе нет, - выдумка, и вся группа
     выбрасывается. Фильтр отключается сам на тайле без текстового слоя
     (надписи начерчены линиями - ровно тот случай, ради которого зрение и
     нужно), и находка оттуда получает kind=REVIEW: подтвердить её нечем.
  2. Группа меньше MIN_GROUP подписей не рассматривается: «5, 6» - это не
     большинство и одиночка, а два числа, о которых ничего не известно.
  3. Разнобой (три и более разных значения) - не ошибка, а непонятая моделью
     картинка. Молчим.
"""

import json
import logging
import re
from collections import Counter
from pathlib import Path

import fitz

import tiling
from settings import PROJECT_ROOT, resolve_path, resolve_vision_cfg

logger = logging.getLogger("error_analyzer")

# Подпись провода - короткое число (см. visual_probe: длинные числа на листе
# есть, но маркировкой не бывают).
MARKING_RE = re.compile(r"^\d{1,4}$")

# Сколько подписей должно быть в группе, чтобы «одиночка среди одинаковых»
# что-то значила. На листе 10.2 обе настоящие ошибки лежат в группах из пяти
# (4 верных + 1 неверная); группа из двух не даёт большинства вовсе.
MIN_GROUP = 3

# Сколько подписей должно быть у БОЛЬШИНСТВА. Два против одного - слишком
# шаткое основание, чтобы утверждать, что неправ именно одиночка.
MIN_MAJORITY = 3

# Папка с тайлами внутри папки документа. Путь до тайла кладётся в ref.
# source_file - поле существующее и означает ровно это («файл, откуда взята
# находка»), так что схему находки менять не нужно, а кнопка «фрагмент» может
# показать РОВНО ТО, что видела модель, вместо повторного поиска по тексту.
VISUAL_DIR = "visual"

TILE_PROMPT = """Ты читаешь фрагмент принципиальной электрической схемы (ГОСТ).

Первое изображение - лист целиком, для ориентации. Второе - фрагмент этого
листа: {position}.

ЗАДАЧА. Найди на ФРАГМЕНТЕ линии связи (провода) и выпиши числовые подписи
маркировки, СГРУППИРОВАВ их по линии, к которой они относятся. Одна группа =
одна линия (одна электрическая цепь) со всеми подписями вдоль неё, включая
ответвления, если видно, что это та же линия.

ЧТО НЕ ВЫПИСЫВАТЬ: номера выводов внутри прямоугольников аппаратов, номера
листов, содержимое штампа, размеры, позиционные обозначения.

ВАЖНО:
- пиши ровно то, что напечатано, даже если число выглядит неуместным;
- ничего не исправляй и не додумывай: если вдоль линии стоят 5, 5 и 6 - так и
  пиши, три подписи;
- если не уверен, что две подписи на одной линии, - разнеси их по разным
  группам;
- линии без подписей не упоминай.

Ответ - ТОЛЬКО JSON, без пояснений:
{{"circuits": [{{"where": "верхняя горизонтальная шина, идёт вправо от клеммы +24V",
                "numbers": ["5", "5", "6"]}}]}}
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


def printed_markings(page, tile) -> Counter:
    """Что НАПЕЧАТАНО на тайле - по текстовому слою PDF. Эталон для фильтра 1."""
    return Counter(s["text"] for s in tiling.text_in_tile(page, tile)
                   if MARKING_RE.match(s["text"]))


def parse_circuits(answer: str) -> list:
    """Ответ модели -> [(описание места, [числа])]. Мусор молча отбрасывается:
    модель может вернуть что угодно, и падать из-за этого стадия не должна."""
    data = _extract_json(answer)
    out = []
    for item in data.get("circuits", []) or []:
        if not isinstance(item, dict):
            continue
        numbers = [str(n).strip() for n in (item.get("numbers") or [])
                   if MARKING_RE.match(str(n).strip())]
        if numbers:
            out.append((str(item.get("where") or "").strip(), numbers))
    return out


def odd_one_out(numbers: list):
    """(большинство, одиночка) - или None, если группа ничего не доказывает.

    Требуется: группа не меньше MIN_GROUP, ровно два разных значения,
    большинство не меньше MIN_MAJORITY и одиночка ровно одна. Всё остальное -
    либо слишком мало данных, либо разнобой, который означает, что модель не
    разобрала картинку.
    """
    if len(numbers) < MIN_GROUP:
        return None
    counts = Counter(numbers)
    if len(counts) != 2:
        return None
    (major, major_n), (minor, minor_n) = counts.most_common()
    if minor_n != 1 or major_n < MIN_MAJORITY:
        return None
    return major, minor


def _finding(document: str, sheet: int, major: str, minor: str, where: str,
             source_file: str, confirmed: bool) -> dict:
    """Находка о разной маркировке на одной цепи.

    scope=single_document: ошибка внутри одного документа. kind=MISMATCH -
    из перечисленных в схеме именно он означает «данные не совпадают»; в
    описании схемы MISMATCH приведён как междокументный, но на маршрутизацию в
    интерфейсе kind не влияет (там смотрят на типы документов в refs), а
    выдавать разную маркировку за DUPLICATE или FORMAT было бы прямой
    неправдой.

    Два ref'а на ОДИН документ - ровно так схема описывает находку о двух
    местах внутри одного документа.
    """
    return {
        "kind": "MISMATCH" if confirmed else "REVIEW",
        "scope": "single_document",
        "severity": "high" if confirmed else "medium",
        "type": "Разная маркировка проводов на одной цепи",
        "refs": [
            {"document": document, "doc_type": "scheme", "source_file": source_file,
             "sheet": sheet, "marking": major,
             "found": f"вдоль цепи подписано «{major}» (большинство подписей)"},
            {"document": document, "doc_type": "scheme", "source_file": source_file,
             "sheet": sheet, "marking": minor,
             "found": f"на той же цепи одна подпись «{minor}»"},
        ],
        "finding": (
            f"На листе {sheet} вдоль одной цепи ({where or 'см. фрагмент'}) "
            f"подписаны разные номера провода: «{major}» и одна подпись «{minor}». "
            + ("Обе подписи прочитаны на растре листа и подтверждены текстовым "
               "слоем PDF." if confirmed else
               "Подписи прочитаны только на растре: текстового слоя на этом "
               "участке нет, подтвердить их нечем.")),
        "action": (f"Проверить по чертежу, какой номер верен, и привести подпись "
                   f"«{minor}» к «{major}» либо исправить остальные."),
        "evidence": f"тайл {source_file}: подписи одной цепи {major}×N и {minor}×1",
    }


def analyze_page(ask, page, document: str, sheet: int, visual_dir: Path,
                 cap_px: float = tiling.DEFAULT_CAP_PX,
                 max_tile_pixels: int = tiling.DEFAULT_MAX_TILE_PIXELS,
                 save_tiles: bool = True, reporter=None, sheets_total: int = 1) -> list:
    """Один лист: тайлы -> вопросы модели -> находки."""
    plan = tiling.plan_page(page, cap_px=cap_px, max_tile_pixels=max_tile_pixels)
    overview_png, ink_pix = tiling.render_overview(page)
    tiling.measure_ink(plan, page.rect, ink_pix)

    # У ЭТОЙ стадии прогресс есть и должен быть - в отличие от текстовых
    # агентов, где рисовать его значило бы врать: там модель сама решает, что
    # открыть, а здесь порядок листов и тайлов выбирает пайплайн. Единица
    # времени - тайл, а не лист: один тайл это один вызов модели.
    useful = [t for t in plan.tiles if t.ink >= tiling.DEFAULT_MIN_INK]

    findings = []
    for done, tile in enumerate(useful, start=1):
        if reporter:
            reporter.page(sheet, sheets_total,
                          stage=f"визуальная проверка, тайл {done} из {len(useful)}")

        png = tiling.render_tile(page, tile, plan.zoom)
        rel = f"{VISUAL_DIR}/л{sheet}_т{tile.index + 1}.png"
        if save_tiles:
            visual_dir.mkdir(parents=True, exist_ok=True)
            (visual_dir / Path(rel).name).write_bytes(png)

        position = (f"тайл {tile.index + 1} из {len(plan.tiles)}, "
                    f"сетка {plan.rows}x{plan.cols}, "
                    f"строка {tile.row + 1}, столбец {tile.col + 1}")
        try:
            answer = ask(TILE_PROMPT.format(position=position),
                         [overview_png, png])
        except Exception as e:  # noqa: BLE001 - один упавший тайл не повод терять лист
            logger.warning("  лист %s, тайл %d: модель не ответила (%s)",
                           sheet, tile.index + 1, e)
            continue

        printed = printed_markings(page, tile)
        for where, numbers in parse_circuits(answer):
            pair = odd_one_out(numbers)
            if pair is None:
                continue
            major, minor = pair

            # ФИЛЬТР 1. Оба числа обязаны быть напечатаны на этом тайле.
            # Текстового слоя нет вовсе - фильтр не применяется, но находка
            # уходит как REVIEW: подтвердить её нечем.
            if printed:
                if not printed.get(major) or not printed.get(minor):
                    logger.info("  лист %s, тайл %d: группа %s отброшена - "
                                "в тексте листа таких подписей нет",
                                sheet, tile.index + 1, numbers)
                    continue
                confirmed = True
            else:
                confirmed = False

            findings.append(_finding(document, sheet, major, minor, where,
                                     rel, confirmed))
    return findings


def analyze_document(ask, document: str, pdf_path, data_dir, pages=None,
                     cap_px: float = tiling.DEFAULT_CAP_PX,
                     max_tile_pixels: int = tiling.DEFAULT_MAX_TILE_PIXELS,
                     reporter=None) -> list:
    """Все листы одного документа. pages - номера листов с единицы (None = все)."""
    pdf = fitz.open(str(pdf_path))
    try:
        sheets = list(pages) if pages else list(range(1, pdf.page_count + 1))
        findings = []
        for sheet in sheets:
            findings += analyze_page(ask, pdf[sheet - 1], document, sheet,
                                     Path(data_dir) / VISUAL_DIR,
                                     cap_px=cap_px, max_tile_pixels=max_tile_pixels,
                                     reporter=reporter, sheets_total=len(sheets))
        return findings
    finally:
        pdf.close()


def run_visual_stage(cfg: dict, data_dir, ask=None) -> list:
    """Стадия зрения по всему прогону. Читает manifest.json, как и стадия правил.

    Пока смотрим ТОЛЬКО принципиальные схемы: маркировка проводов живёт на них.
    Сборочный чертёж и спецификация зрению тоже есть что показать, но у них
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

    vision_cfg = cfg.get("vision") or {}
    cap_px = float(vision_cfg.get("cap_px", tiling.DEFAULT_CAP_PX))
    max_tile_pixels = int(vision_cfg.get("max_tile_pixels",
                                         tiling.DEFAULT_MAX_TILE_PIXELS))

    # Модуль сообщений о ходе работы лежит в папке скриптов (она копируется в
    # каждую сессию), поэтому грузится по пути - как в full_project.py.
    # Не нашёлся - работаем молча.
    import script_loader
    reporter = script_loader.try_load(resolve_path(cfg["paths"]["scripts_dir"]),
                                      "progress.py")

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
            ask, doc["name"], pdf_path, PROJECT_ROOT / doc["data_dir"],
            cap_px=cap_px, max_tile_pixels=max_tile_pixels, reporter=reporter)
        logger.info("  зрение по %s: %d находок", doc["name"], len(doc_findings))
        findings += doc_findings

    if reporter:
        reporter.done()
    return findings
