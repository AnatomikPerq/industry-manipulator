#!/usr/bin/env python3
"""
Записать фикстуру наименований листов альбомов из настоящих PDF.

Отдельной командой, а не флагом к pytest, по той же причине, что и
record_baseline.py: фикстура наименований - это ВХОД золотого теста нарезки, и
переписать её одним нажатием значит объявить верным всё, что чтение штампов
выдаёт сегодня. Читать штампы - единственная часть нарезки, которой нужны и
fitz, и сами PDF; всё остальное (границы документов, тип части, шкаф) выводится
из наименований, поэтому в репозитории лежат именно они.

Исходных PDF в репозитории нет и быть не может - это документация заказчика.
Они лежат в папке «на проверку/» в корне рабочей копии.

Запуск (из analyzer_to_errors/):
    python tests/record_album_titles.py
Печатает, что изменилось против прежней фикстуры, - это и надо прочитать
глазами, прежде чем коммитить: рост числа частей означает, что альбом начал
рассыпаться, а падение - что документы слиплись.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

import full_project as fp                          # noqa: E402
from conftest import FIXTURES                      # noqa: E402

# Ключ фикстуры -> имя файла альбома в «на проверку/».
ALBUMS = {
    "енисей": "11-463-2026-АТХ Енисей.pdf",
    "эом": "24-051-ЭОМ_2026.06.23.pdf",
    "ак": "24-051-АК 2026.07.15.pdf",
}

SCRIPTS = PROJECT_ROOT / "data" / "base_analysis_scripts"


def summarize(titles):
    parts = fp.split_into_parts(titles)
    types = Counter(fp.classify(p["title"])[0] for p in parts)
    return {"sheets": len(titles), "parts": len(parts), "types": dict(types)}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="../на проверку",
                    help="папка с альбомами (по умолчанию «на проверку» в корне)")
    args = ap.parse_args()

    source = (PROJECT_ROOT / args.source).resolve()
    out_path = FIXTURES / "album_titles.json"
    old = json.loads(out_path.read_text(encoding="utf-8")) if out_path.is_file() else {}

    titles = {}
    for key, fname in ALBUMS.items():
        pdf = source / fname
        if not pdf.is_file():
            print(f"НЕТ ФАЙЛА, пропуск: {pdf}", file=sys.stderr)
            if key in old:
                titles[key] = old[key]
            continue
        titles[key] = fp.read_sheet_titles(pdf, SCRIPTS)

    for key in sorted(titles):
        now = summarize(titles[key])
        was = summarize(old[key]) if key in old else None
        mark = "НОВЫЙ" if was is None else ("=" if was == now else "ИЗМЕНИЛОСЬ")
        print(f"[{mark}] {key}: {now}")
        if was and was != now:
            print(f"          было: {was}")
        if key in old:
            diff = sum(1 for a, b in zip(old[key], titles[key]) if a != b)
            print(f"          наименований листов изменилось: {diff}")

    out_path.write_text(json.dumps(titles, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"\n{out_path.relative_to(PROJECT_ROOT)}: "
          f"{out_path.stat().st_size / 1024:.0f} КБ, альбомов {len(titles)}")


if __name__ == "__main__":
    main()
