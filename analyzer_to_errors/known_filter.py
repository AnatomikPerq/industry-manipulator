"""
Заранее известные ошибки: находки, которые пользователь один раз разобрал и
признал НЕ ошибкой документации, - чтобы они больше не всплывали в отчётах.

ПОЧЕМУ ЭТОТ ФИЛЬТР ДЕТЕРМИНИРОВАННЫЙ, А НЕ ТОЛЬКО В ПРОМПТЕ МЕРДЖЕРА.
Раньше known_errors.json уходил единственным путём - текстом в промпт
LLM-сшивателя (merge_reports.py), и вырезать находку могла только модель.
Это работало ровно для находок агентов и не работало ни для чего больше:

  * находки чекеров добавляются в отчёт ПОСЛЕ слияния и детерминированно
    (combine_rule_and_agent_findings) - мимо мерджера;
  * в режиме «без ИИ» мерджера нет вовсе, а отчёт целиком состоит из находок
    чекеров;
  * при agents.count = 1 мерджер тоже не вызывается.

То есть в основном сегодняшнем сценарии - когда почти все находки дают
чекеры - файл не делал ничего. Инженеру, разобравшему ложное срабатывание по
чертежу, нечем было его погасить, кроме правки самого чекера.

КАК УСТРОЕНО СОПОСТАВЛЕНИЕ. Запись known_errors - это находка в формате
schema.REPORT_SCHEMA, но заполненная ЧАСТИЧНО: указывают ровно те поля,
которых достаточно, чтобы её опознать. Совпадением считается:

  * kind и type совпали (если они в записи указаны);
  * КАЖДЫЙ ref записи нашёл себе ref в находке, у которого совпали все
    заполненные в записи поля.

Поэтому погасить находку можно короткой записью:

    {"kind": "REVIEW", "refs": [{"document": "ША1 СО", "designator": "QS1"}]}

Подмножество, а не точное равенство, выбрано сознательно: требовать от
человека переписать в файл все пятнадцать полей ref'а - значит гарантировать,
что он ошибётся в одном из них и фильтр молча не сработает. Обратная опасность
(слишком общая запись погасит лишнее) видна сразу: чем меньше полей, тем
очевиднее, что запись широкая, а в лог пишется, сколько находок она убрала.
"""

import logging

logger = logging.getLogger(__name__)

# Поля ref'а, по которым вообще имеет смысл опознавать место находки. Пустые
# (None/"") в записи игнорируются - они означают «мне всё равно», а не
# «здесь обязан быть null».
REF_MATCH_FIELDS = (
    "document", "doc_type", "sheet", "row", "cabinet", "terminal_block", "pin",
    "kks", "marking", "conductor", "designator", "article",
)


def _ref_matches(pattern: dict, ref: dict) -> bool:
    """Совпадает ли конкретный ref находки с (частичным) ref'ом записи."""
    for field in REF_MATCH_FIELDS:
        want = pattern.get(field)
        if want in (None, ""):
            continue                      # поле в записи не указано - не сверяем
        if str(ref.get(field)) != str(want):
            return False
    return True


def matches(known: dict, finding: dict) -> bool:
    """Гасит ли запись known_errors эту находку."""
    if not isinstance(known, dict):
        return False

    for field in ("kind", "type", "scope"):
        want = known.get(field)
        if want and str(finding.get(field)) != str(want):
            return False

    patterns = known.get("refs") or []
    if not patterns:
        # Запись без refs, но с kind/type - слишком широкая, чтобы применять её
        # молча: так можно погасить целый класс находок, сам того не заметив.
        return False

    refs = finding.get("refs") or []
    return all(any(_ref_matches(p, r) for r in refs) for p in patterns)


def filter_findings(findings: list, known_errors: list) -> list:
    """Убирает из списка находок все, что погашены known_errors.

    Возвращает НОВЫЙ список; порядок сохраняется. Пишет в лог, сколько находок
    убрала каждая запись: запись, не убравшая ничего, - скорее всего опечатка в
    имени документа, и знать об этом надо (иначе «фильтр не сработал» выглядит
    как «чекер перестал находить»).
    """
    if not known_errors:
        return list(findings)

    kept, dropped = [], 0
    hits = [0] * len(known_errors)
    for f in findings:
        idx = next((i for i, k in enumerate(known_errors) if matches(k, f)), None)
        if idx is None:
            kept.append(f)
        else:
            hits[idx] += 1
            dropped += 1

    if dropped:
        logger.info("Заранее известные ошибки: из отчёта убрано %d находок", dropped)
    for i, (k, n) in enumerate(zip(known_errors, hits), 1):
        if n == 0:
            logger.warning(
                "Запись known_errors №%d не совпала ни с одной находкой (%s) - "
                "проверьте имя документа и поля: возможно, опечатка",
                i, _short(k))
    return kept


def _short(known: dict) -> str:
    """Короткое описание записи для лога."""
    bits = [str(known.get("kind") or "?")]
    for ref in (known.get("refs") or [])[:2]:
        where = ref.get("designator") or ref.get("terminal_block") or ref.get("kks")
        bits.append(f"{ref.get('document')}/{where}")
    return " ".join(bits)
