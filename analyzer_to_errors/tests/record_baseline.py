#!/usr/bin/env python3
"""
Записать эталон находок для фикстуры.

Отдельной командой, а не флагом теста, СОЗНАТЕЛЬНО. Эталон - это результат
ручного разбора по чертежу, а не «то, что программа выдаёт сегодня». Кнопка
«обновить эталон», доступная одним ключом к pytest, превращает золотой тест в
самоисполняющееся пророчество: упало - обновил - зелено, и регрессия уехала в
репозиторий вместе с новым эталоном.

Запуск (из analyzer_to_errors/):
    python tests/record_baseline.py --name щскз
Печатает сводку по видам и важности - её и надо сверить с таблицей в CLAUDE.md,
прежде чем коммитить.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent))

from conftest import FIXTURES                      # noqa: E402
from test_baseline import run_checkers             # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="щскз")
    args = ap.parse_args()

    findings = run_checkers(args.name)
    out = FIXTURES / args.name / "expected_findings.json"
    out.write_text(json.dumps({
        "_comment": ("Эталон находок детерминированных чекеров. Обновлять "
                     "ТОЛЬКО после ручного разбора расхождения по чертежу - "
                     "см. шапку tests/test_baseline.py."),
        "findings": findings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    kinds = Counter(f["kind"] for f in findings)
    sev = Counter(f["severity"] for f in findings)
    print(f"{args.name}: {len(findings)} находок -> {out.relative_to(TESTS_DIR.parent)}")
    print("  по видам:     " + ", ".join(f"{k}={v}" for k, v in kinds.most_common()))
    print("  по важности:  " + ", ".join(f"{k}={v}" for k, v in sev.most_common()))
    for f in findings:
        where = next((r.get("designator") or r.get("terminal_block") or r.get("kks")
                      for r in f["refs"] if r.get("designator") or r.get("terminal_block")
                      or r.get("kks")), "")
        print(f"    [{f['severity']:6}] {f['type']} — {where}")


if __name__ == "__main__":
    main()
