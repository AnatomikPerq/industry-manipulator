"""
ЗОЛОТОЙ БАЗЛАЙН: находки детерминированных чекеров на реальном корпусе.

Это главный тест репозитория. Все пороги и фильтры в чекерах выбраны замером
по настоящим файлам («230VAC спарен с 630 обозначениями - это не артикул, а
подпись на картинке: 320 ложных находок из 322»), и ценность правила измеряется
не тем, что оно находит, а тем, чего оно НЕ находит. Такое знание нельзя
проверить чтением кода - только повторным замером. Здесь он и автоматизирован.

ЧТО СРАВНИВАЕТСЯ. Не текст находки (он переписывается при любой правке
формулировок и ломал бы тест на ровном месте), а её СУТЬ: вид, важность, тип и
опознавательные поля каждого ref'а. Именно эти поля инженер читает в таблице
отчёта, и именно они меняются, когда правило начинает работать иначе.

ЕСЛИ ТЕСТ УПАЛ - это не повод обновлять эталон. Сначала ответьте, какое из
двух случилось:
  * находок стало БОЛЬШЕ - почти наверняка правило начало давать ложные;
  * находок стало МЕНЬШЕ - правило перестало видеть настоящую ошибку.
Эталон обновляют, только когда новое поведение разобрано по чертежу вручную и
признано верным - тем же способом, каким получены исходные шесть находок ЩСКЗ
(см. таблицу в CLAUDE.md).
"""

import json

import pytest

from conftest import FIXTURES, fixture_cfg, fixture_data_dir

import main as pipeline

# Поля ref'а, по которым опознаётся место находки. Координат и формулировок
# здесь нет сознательно - см. шапку модуля.
REF_KEYS = ("document", "doc_type", "sheet", "row", "terminal_block", "pin",
            "kks", "marking", "designator", "article")

CORPORA = [p.name for p in sorted(FIXTURES.iterdir()) if p.is_dir()] if FIXTURES.is_dir() else []


def signature(finding: dict) -> dict:
    """Суть находки, пригодная и для сравнения, и для чтения глазами в diff'е."""
    return {
        "kind": finding.get("kind"),
        "severity": finding.get("severity"),
        "type": finding.get("type"),
        "refs": [
            {k: ref.get(k) for k in REF_KEYS if ref.get(k) is not None}
            for ref in finding.get("refs", [])
        ],
    }


def run_checkers(name: str) -> list:
    """Стадия правил + стадия связок по фикстуре - ровно как в run_pipeline."""
    cfg = fixture_cfg(name)
    data_dir = fixture_data_dir(name)
    findings = (pipeline.run_rules_stage(cfg, data_dir)
                + pipeline.run_bundle_stage(cfg, data_dir))
    return [signature(f) for f in findings]


def expected_path(name: str):
    return FIXTURES / name / "expected_findings.json"


@pytest.mark.parametrize("corpus", CORPORA)
def test_findings_match_baseline(corpus):
    expected_file = expected_path(corpus)
    if not expected_file.is_file():
        pytest.skip(f"нет эталона для {corpus} - создайте его "
                    f"python tests/record_baseline.py --name {corpus}")

    expected = json.loads(expected_file.read_text(encoding="utf-8"))
    actual = run_checkers(corpus)

    # Порядок находок задан сортировкой по важности и внутри неё не определён,
    # поэтому сравниваем как множества - но сообщение об ошибке делаем
    # читаемым: показываем, что именно появилось и что пропало.
    def key(sig):
        return json.dumps(sig, ensure_ascii=False, sort_keys=True)

    exp, act = {key(s) for s in expected["findings"]}, {key(s) for s in actual}
    appeared = sorted(act - exp)
    vanished = sorted(exp - act)

    assert not appeared and not vanished, (
        f"\nкорпус {corpus}: было {len(exp)} находок, стало {len(act)}.\n"
        + ("\nПОЯВИЛИСЬ (проверьте, не ложные ли):\n"
           + "\n".join("  + " + a for a in appeared) if appeared else "")
        + ("\nПРОПАЛИ (проверьте, не настоящие ли ошибки):\n"
           + "\n".join("  - " + v for v in vanished) if vanished else "")
    )


@pytest.mark.parametrize("corpus", CORPORA)
def test_findings_valid_against_schema(corpus):
    """Находки чекеров обязаны проходить ту же схему, что и ответы моделей.

    Отдельным тестом, потому что ломается это иначе и молча: находка с лишним
    полем не падает нигде в пайплайне (валидируются только ответы LLM), а до
    интерфейса доезжает и рисуется пустой колонкой.
    """
    from jsonschema import Draft7Validator

    from schema import REPORT_SCHEMA

    cfg, data_dir = fixture_cfg(corpus), fixture_data_dir(corpus)
    findings = (pipeline.run_rules_stage(cfg, data_dir)
                + pipeline.run_bundle_stage(cfg, data_dir))
    errors = list(Draft7Validator(REPORT_SCHEMA).iter_errors({"errors": findings}))
    assert not errors, "\n".join(str(e) for e in errors[:5])
