#!/usr/bin/env python3
"""
Сборка фикстуры из живого корпуса: data/ -> tests/fixtures/<имя>/.

ЗАЧЕМ ЭТО ВООБЩЕ. Весь проект стоит на замерах по реальным файлам («было 489
находок, из них ~480 ложных; стало 4»), и до сих пор эти числа охранял только
человек, помнящий их наизусть. Тест сравнивает находки с эталоном - но
исходные PDF в репозиторий не положить: это документация заказчика. Зато
ИЗВЛЕЧЁННЫЕ данные - обычный JSON, и по ним чекеры работают ровно так же, как
по настоящему прогону.

ЧТО ВЫРЕЗАЕТСЯ. Целиком корпус ЩСКЗ - 11 МБ, из них 10 приходится на raw.json
и classified.json. raw.json чекерам не нужен вовсе. classified.json нужен, но
сверка связки (bundle_rules.load_scheme) читает у span'а ровно два поля - text
и entity_type, - поэтому в фикстуру уезжает урезанная копия: 4.9 МБ -> около
полумегабайта. Урезание живёт ЗДЕСЬ, а не делается руками, чтобы через год
было видно, что именно выброшено и почему это законно.

Запуск (из analyzer_to_errors/):
    python tests/build_fixtures.py               # корпус из data/ -> fixtures/щскз
    python tests/build_fixtures.py --name ЭОМ --data sessions/<id>/data
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

# Что нужно чекерам от каждого типа документа. Всё остальное в фикстуру не
# едет: чекеры этих файлов не открывают, а агентов в тесте нет.
NEEDED = {
    "scheme": ["nets.json", "classified.json"],
    "assembly": ["assembly.json"],
    "spec": ["specification.json"],
    "netlist": ["connections.json"],
}

# Поля span'а, которые реально читает bundle_rules.load_scheme.
SPAN_FIELDS = ("text", "entity_type")


def slim_classified(data):
    """classified.json без координат, шрифтов и геометрии - только то, по чему
    сверяется связка. Проверено по load_scheme: страница даёт page_number, а
    span - text и entity_type, больше оттуда не берётся ничего."""
    out = []
    for page in data:
        out.append({
            "page_number": page.get("page_number"),
            "text_spans": [
                {k: s.get(k) for k in SPAN_FIELDS}
                for s in page.get("text_spans", [])
            ],
        })
    return out


SLIMMERS = {"classified.json": slim_classified}


def build(data_dir: Path, out_dir: Path) -> dict:
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    kept_docs = []
    for doc in manifest.get("documents", []):
        src = PROJECT_ROOT / doc["data_dir"]
        dst = out_dir / doc["name"]
        dst.mkdir(parents=True, exist_ok=True)

        files = []
        for fname in NEEDED.get(doc["doc_type"], []):
            src_file = src / fname
            if not src_file.is_file():
                continue
            payload = json.loads(src_file.read_text(encoding="utf-8"))
            slim = SLIMMERS.get(fname)
            if slim:
                payload = slim(payload)
            dst_file = dst / fname
            dst_file.write_text(json.dumps(payload, ensure_ascii=False),
                                encoding="utf-8")
            files.append(fname)

        # Путь к данным в манифесте - относительно PROJECT_ROOT: ровно так его
        # собирает обратно main.run_rules_stage (PROJECT_ROOT / doc["data_dir"]).
        # Поэтому фикстуры и лежат ВНУТРИ analyzer_to_errors - по той же
        # причине, по которой там же обязаны лежать папки сессий.
        doc = dict(doc)
        doc["data_dir"] = str(dst.relative_to(PROJECT_ROOT)).replace("\\", "/")
        doc["files"] = files
        kept_docs.append(doc)

    manifest["documents"] = kept_docs
    manifest["_fixture"] = (
        "Собрано tests/build_fixtures.py из извлечённых данных реального "
        "корпуса. Исходных PDF здесь нет и быть не может - это документация "
        "заказчика. classified.json урезан до полей text/entity_type.")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    size = sum(p.stat().st_size for p in out_dir.rglob("*") if p.is_file())
    return {"documents": len(kept_docs), "bytes": size}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="data", help="папка data с manifest.json")
    ap.add_argument("--name", default="щскз", help="имя фикстуры")
    args = ap.parse_args()

    data_dir = (PROJECT_ROOT / args.data).resolve()
    if not (data_dir / "manifest.json").is_file():
        print(f"В {data_dir} нет manifest.json - сначала прогоните "
              f"python main.py --extract-only", file=sys.stderr)
        sys.exit(1)

    info = build(data_dir, TESTS_DIR / "fixtures" / args.name)
    print(f"Фикстура {args.name!r}: документов {info['documents']}, "
          f"{info['bytes'] / 1024:.0f} КБ")


if __name__ == "__main__":
    main()
