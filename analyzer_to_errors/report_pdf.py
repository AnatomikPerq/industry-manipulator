"""
Отчёт сессии одним PDF: то, что уходит инженеру и подшивается к проекту.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФОРМАТ. merged_report.json читает программа, таблица в браузере
живёт ровно до закрытия вкладки. А замечания по проекту надо переслать, показать
на совещании и приложить к переписке с бюро - для этого нужен файл, который
откроется у кого угодно и выглядит одинаково везде.

ЧТО В НАЧАЛЕ. Сначала - ОПИСАНИЕ АНАЛИЗА, и только потом замечания. Отчёт
читает человек, который не запускал программу: ему надо понимать, какие
документы вообще смотрели, чем именно их проверяли и - главное - чего этот
анализ НЕ проверяет. Список замечаний без этой рамки читается как «вот все
ошибки проекта», что неправда и опасно: часть проверок сознательно не
делается (см. комментарии в чекерах), а находки вида REVIEW - это вопрос
инженеру, а не утверждение об ошибке.

ВЁРСТКА РУКАМИ, а не через fitz.Story: Story требует HTML со шрифтами в архиве,
и на кириллице это лишний слой, который нечем проверить офлайн. Здесь всё
сводится к «нарисуй строку, сдвинь курсор, кончилась страница - заведи новую»,
и такой код читается без документации.

ШРИФТ. Base-14 Helvetica кириллицу отдаёт ненадёжно, поэтому берём системный
TTF (на Windows Arial есть всегда). Если ни одного не нашлось - не падаем, а
печатаем helv: кривой отчёт лучше, чем никакого.
"""

import os
from collections import Counter
from datetime import datetime
from pathlib import Path

import fitz

# ------------------------------------------------------------------
# Страница и шрифты
# ------------------------------------------------------------------

PAGE = fitz.paper_rect("a4")
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 48, 52, 46
CONTENT_W = PAGE.width - 2 * MARGIN_X

# Системные шрифты в порядке предпочтения: (обычный, полужирный).
FONT_CANDIDATES = [
    ("arial.ttf", "arialbd.ttf"),
    ("segoeui.ttf", "segoeuib.ttf"),
    ("tahoma.ttf", "tahomabd.ttf"),
    ("verdana.ttf", "verdanab.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
]
FONT_DIRS = [
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/Library/Fonts"),
]

INK = (0.12, 0.13, 0.15)
MUTED = (0.42, 0.45, 0.50)
RULE = (0.82, 0.84, 0.87)
ACCENT = (0.10, 0.38, 0.72)

SEVERITY_RU = {"critical": "критическая", "high": "высокая", "medium": "средняя",
               "low": "низкая", "info": "информация"}
SEVERITY_COLOR = {
    "critical": (0.70, 0.10, 0.12), "high": (0.83, 0.29, 0.09),
    "medium": (0.72, 0.53, 0.05), "low": (0.25, 0.45, 0.65),
    "info": (0.45, 0.47, 0.50),
}
SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]

KIND_RU = {
    "MISMATCH": "расхождение между документами",
    "MISSING": "элемент отсутствует",
    "REVIEW": "требует проверки инженером",
    "DUPLICATE": "дубль",
    "BROKEN_LINK": "оборванная ссылка",
    "FORMAT": "нарушение формата",
    "INCOMPLETE": "не заполнено",
}
DOC_TYPE_RU = {
    "scheme": "принципиальная схема (Э3)",
    "assembly": "сборочный чертёж (СБ)",
    "spec": "спецификация (СО)",
    "netlist": "таблица подключений",
}


def _find_fonts():
    """(путь к обычному, путь к полужирному) либо (None, None) - нет ни одного.

    Пути, а не объекты: insert_text рисует шрифтом, ЗАРЕГИСТРИРОВАННЫМ на
    странице (insert_font), а объект fitz.Font нужен отдельно - им меряется
    ширина строки при переносе. Меряем и рисуем одним и тем же файлом, иначе
    перенос считался бы по чужим метрикам и длинные артикулы вылезали бы за поле.
    """
    for regular, bold in FONT_CANDIDATES:
        for d in FONT_DIRS:
            r, b = d / regular, d / bold
            if r.is_file():
                return str(r), str(b) if b.is_file() else str(r)
    return None, None


class _Writer:
    """Курсор по документу: пишет строки сверху вниз и сам заводит страницы."""

    def __init__(self):
        self.doc = fitz.open()
        self.regular_file, self.bold_file = _find_fonts()
        if self.regular_file:
            self.regular = fitz.Font(fontfile=self.regular_file)
            self.bold = fitz.Font(fontfile=self.bold_file)
        else:
            # Base-14: кириллицу отдаёт не везде, но это последний рубеж -
            # лучше кривой отчёт, чем отказ его собрать
            self.regular, self.bold = fitz.Font("helv"), fitz.Font("hebo")
        self.page = None
        self.y = 0.0
        self._new_page()

    # ---------- страницы ----------

    def _new_page(self):
        self.page = self.doc.new_page(width=PAGE.width, height=PAGE.height)
        self._register_fonts(self.page)
        self.y = MARGIN_TOP

    def _register_fonts(self, page):
        """Шрифт регистрируется НА КАЖДОЙ странице: ресурсы шрифтов в PDF
        принадлежат странице, а не документу."""
        if self.regular_file:
            page.insert_font(fontname="F1", fontfile=self.regular_file)
            page.insert_font(fontname="F2", fontfile=self.bold_file)
        else:
            page.insert_font(fontname="F1", fontname_="helv")
            page.insert_font(fontname="F2", fontname_="hebo")

    def _name(self, bold):
        return "F2" if bold else "F1"

    def space_left(self):
        return PAGE.height - MARGIN_BOTTOM - self.y

    def need(self, height):
        """Заранее зарезервировать место: заголовок не должен остаться один
        внизу страницы, оторванный от того, что он озаглавливает."""
        if self.space_left() < height:
            self._new_page()

    def gap(self, height):
        self.y += height

    # ---------- текст ----------

    def _font(self, bold):
        return self.bold if bold else self.regular

    def wrap(self, text, size, width, bold=False):
        """Разбивка на строки по ширине. Меряем тем же шрифтом, которым потом
        рисуем, иначе длинные артикулы вылезают за поле."""
        font = self._font(bold)
        lines = []
        for paragraph in str(text).replace("\r", "").split("\n"):
            words, current = paragraph.split(), ""
            if not words:
                lines.append("")
                continue
            for word in words:
                trial = (current + " " + word).strip()
                if current and font.text_length(trial, size) > width:
                    lines.append(current)
                    current = word
                else:
                    current = trial
            lines.append(current)
        return lines

    def text(self, content, size=9.5, bold=False, color=INK, indent=0.0,
             leading=1.35, width=None, space_after=0.0):
        width = width if width is not None else CONTENT_W - indent
        name = self._name(bold)
        step = size * leading
        for line in self.wrap(content, size, width, bold):
            self.need(step)
            if line:
                self.page.insert_text((MARGIN_X + indent, self.y + size), line,
                                      fontsize=size, fontname=name, color=color)
            self.y += step
        self.y += space_after

    def heading(self, content, size=14, space_before=14, space_after=7):
        self.gap(space_before)
        self.need(size * 2.2)
        self.text(content, size=size, bold=True, color=INK)
        self.y += space_after

    def rule(self, space_before=4, space_after=8, color=RULE):
        self.need(space_before + space_after + 2)
        self.gap(space_before)
        self.page.draw_line((MARGIN_X, self.y), (PAGE.width - MARGIN_X, self.y),
                            color=color, width=0.7)
        self.gap(space_after)

    def key_value(self, key, value, key_w=132, size=9.5):
        """Строка «поле: значение» с выровненной колонкой значений."""
        value_w = CONTENT_W - key_w
        lines = self.wrap(value, size, value_w)
        step = size * 1.35
        self.need(step * len(lines))
        top = self.y
        self.page.insert_text((MARGIN_X, top + size), str(key), fontsize=size,
                              fontname="F1", color=MUTED)
        for line in lines:
            self.page.insert_text((MARGIN_X + key_w, self.y + size), line,
                                  fontsize=size, fontname="F1", color=INK)
            self.y += step

    def bullet(self, content, size=9.5, color=INK):
        step = size * 1.35
        lines = self.wrap(content, size, CONTENT_W - 14)
        self.need(step * len(lines))
        self.page.insert_text((MARGIN_X + 2, self.y + size), "•", fontsize=size,
                              fontname="F1", color=MUTED)
        for line in lines:
            self.page.insert_text((MARGIN_X + 14, self.y + size), line, fontsize=size,
                                  fontname="F1", color=color)
            self.y += step

    # ---------- завершение ----------

    def paginate(self, title):
        """Колонтитул проставляем в конце: пока документ не собран, общее число
        страниц неизвестно."""
        total = self.doc.page_count
        for i, page in enumerate(self.doc, 1):
            page.insert_text(
                (MARGIN_X, PAGE.height - 26), title, fontsize=7.5,
                fontname="F1", color=MUTED)
            label = f"{i} / {total}"
            page.insert_text(
                (PAGE.width - MARGIN_X - self.regular.text_length(label, 7.5),
                 PAGE.height - 26),
                label, fontsize=7.5, fontname="F1", color=MUTED)

    def tobytes(self):
        # Arial встраивается целиком - это мегабайт на трёхстраничный отчёт,
        # который потом пересылают почтой. Оставляем в файле только те глифы,
        # что действительно использованы.
        try:
            self.doc.subset_fonts()
        except Exception:  # noqa: BLE001 - не вышло, значит просто крупнее файл
            pass
        return self.doc.tobytes(garbage=4, deflate=True)


# ------------------------------------------------------------------
# Содержание отчёта
# ------------------------------------------------------------------

def _fmt_time(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")


def _read_manifest(manifest_path):
    import json
    path = Path(manifest_path)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - без манифеста отчёт всё равно соберём
        return {}


def _cover(w, session, report, manifest, version):
    errors = report.get("errors", [])
    mode = session.get("mode")
    mode_ru = ("без ИИ — только детерминированные скрипты" if mode == "scripts"
               else "полный — скрипты и нейросети")

    w.text("ИНДУСТРИЯ МАНИПУЛЯТОР", size=8.5, bold=True, color=MUTED)
    w.text("Анализ проектной документации АСУ ТП", size=8.5, color=MUTED,
           space_after=10)
    w.text("Отчёт о найденных замечаниях", size=21, bold=True, space_after=2)
    w.text(session.get("name") or "Без названия", size=13, color=ACCENT,
           space_after=12)

    w.key_value("Сессия анализа", session.get("id") or "—")
    w.key_value("Дата прогона", _fmt_time(session.get("finished_at")
                                          or session.get("started_at")))
    w.key_value("Режим", mode_ru)
    w.key_value("Версия анализатора", version)
    w.key_value("Всего замечаний", str(len(errors)))

    # ---- по важности ----
    counts = Counter(e.get("severity") for e in errors)
    if errors:
        parts = [f"{SEVERITY_RU.get(s, s)} — {counts[s]}"
                 for s in SEVERITY_ORDER if counts.get(s)]
        w.key_value("По важности", "; ".join(parts))

    w.rule(space_before=14)

    # ---- что смотрели ----
    w.heading("Какие документы проанализированы", size=13, space_before=2)
    documents = manifest.get("documents") or []
    if not documents:
        w.text("Сведения о разобранных документах недоступны: данные прогона "
               "были очищены.", color=MUTED)
    for doc in documents:
        stats = doc.get("stats") or {}
        detail = []
        if stats.get("total_pages"):
            detail.append(f"листов: {stats['total_pages']}")
        if stats.get("spec_rows"):
            detail.append(f"строк спецификации: {stats['spec_rows']}")
        if stats.get("assembly_elements"):
            detail.append(f"подписей изделий: {stats['assembly_elements']}")
        if stats.get("total_connections"):
            detail.append(f"строк соединений: {stats['total_connections']}")
        if doc.get("bundle"):
            detail.append(f"связка: {doc['bundle']}")
        w.bullet("{} — {}{}".format(
            doc.get("name"), DOC_TYPE_RU.get(doc.get("doc_type"), doc.get("doc_type")),
            (". " + ", ".join(detail)) if detail else ""))

    skipped = manifest.get("skipped_files") or []
    if skipped:
        w.gap(6)
        w.text("Не анализировались (не удалось определить вид документа):",
               size=9, color=MUTED)
        for s in skipped:
            w.bullet(f"{s.get('source_file')} — {s.get('reason')}", size=8.5,
                     color=MUTED)

    bundles = manifest.get("bundles") or []
    if len(bundles) > 1:
        w.gap(6)
        w.text("Связки (комплекты документов одного шкафа), сверявшиеся между "
               "собой:", size=9, color=MUTED)
        for b in bundles:
            w.bullet("{}: {}".format(
                b.get("bundle"),
                ", ".join(DOC_TYPE_RU.get(t, str(t))
                          for t in sorted(b.get("doc_types") or []))
                or "вид документов не определён"), size=8.5)

    # ---- как проверяли ----
    w.heading("Как выполнялся анализ", size=13)
    w.bullet("Из каждого PDF и книги Excel извлекаются данные: надписи схемы и "
             "цепи проводов, подписи изделий на чертеже, строки спецификации, "
             "строки таблицы подключений.")
    w.bullet("Каждый документ проверяется отдельно детерминированными правилами: "
             "дубли адресов клемм, две катушки реле с одним обозначением, ссылки "
             "на несуществующий лист, #REF! в ячейках, элемент, пропавший с "
             "парного листа чертежа.")
    w.bullet("Документы одной связки сверяются между собой по позиционному "
             "обозначению: разные артикулы у одного элемента, изделие нарисовано "
             "но не заказано, расхождение обозначений в штампах.")
    if mode != "scripts":
        w.bullet("Дополнительно документы читают две независимые нейросети: они "
                 "оценивают то, что нельзя проверить сравнением строк — "
                 "соответствует ли заказанное изделие подписи на схеме по смыслу.")
    else:
        w.bullet("Нейросети в этом прогоне НЕ запускались: отчёт собран только "
                 "детерминированными правилами. Смысловые расхождения "
                 "(«соответствует ли заказанный автомат подписи C40A») в нём "
                 "не искались.")

    # ---- границы применимости ----
    w.heading("Как читать этот отчёт", size=13)
    w.bullet("Отчёт НЕ является полным перечнем ошибок проекта. Часть проверок "
             "сознательно не выполняется: они давали слишком много ложных "
             "срабатываний на реальных файлах, а ложное замечание обходится "
             "дороже пропущенного — инженер сверяется с чертежом и перестаёт "
             "доверять отчёту.")
    w.bullet("Замечание с пометкой «требует проверки инженером» (REVIEW) — это "
             "вопрос, а не утверждение об ошибке. Например, изделие не найдено "
             "на чертеже: оно может быть не нарисовано законно.")
    w.bullet("Отсутствие какого-либо вида документа в комплекте ошибкой не "
             "считается и в отчёт не попадает: состав комплекта определяет "
             "проектировщик.")
    w.bullet("Место каждого замечания указано в доменных полях — лист, строка, "
             "позиционное обозначение, клемма. По ним замечание находится "
             "в исходном документе.")

    summary = report.get("summary")
    if summary:
        w.heading("Резюме", size=13)
        w.text(summary, size=9.5)


def _refs_block(w, error):
    """Места находки: по одной строке на документ."""
    for ref in error.get("refs") or []:
        where = []
        if ref.get("sheet") is not None:
            where.append(f"лист {ref['sheet']}")
        if ref.get("row") is not None:
            where.append(f"строка {ref['row']}")

        what = []
        for label, key in (("обозначение", "designator"), ("артикул", "article"),
                           ("наименование", "name"), ("кол-во", "quantity"),
                           ("шкаф", "cabinet"), ("маркировка", "marking"),
                           ("KKS", "kks"), ("проводник", "conductor")):
            if ref.get(key) not in (None, ""):
                what.append(f"{label}: {ref[key]}")
        if ref.get("terminal_block") or ref.get("pin"):
            what.append("клемма: {}/{}".format(ref.get("terminal_block") or "?",
                                               ref.get("pin") or "?"))

        head = "{} — {}".format(
            DOC_TYPE_RU.get(ref.get("doc_type"), ref.get("doc_type") or "документ"),
            ref.get("document") or "—")
        if where:
            head += " (" + ", ".join(where) + ")"
        w.text(head, size=9, bold=True, indent=14)
        if what:
            w.text("; ".join(what), size=9, indent=14, color=(0.3, 0.32, 0.36))
        if ref.get("found"):
            w.text(f"найдено: {ref['found']}", size=8.5, indent=14, color=MUTED)


def _findings(w, report):
    errors = report.get("errors") or []
    # Заголовок раздела не должен остаться внизу страницы один: под ним нужно
    # место хотя бы на шапку первого замечания.
    w.need(170)
    w.heading("Замечания", size=15, space_before=18)
    if not errors:
        w.text("Замечаний не найдено.", size=10)
        return

    for i, error in enumerate(errors, 1):
        severity = error.get("severity") or "info"
        # Номер, тип, важность и первое место находки держим вместе: разорванная
        # между страницами шапка не читается.
        w.need(130)
        w.rule(space_before=6, space_after=7)

        w.text("{}. {}".format(i, error.get("type") or "Замечание"),
               size=11, bold=True, space_after=2)
        w.text("важность: {} · {}".format(
            SEVERITY_RU.get(severity, severity),
            KIND_RU.get(error.get("kind"), error.get("kind") or "")),
            size=8.5, color=SEVERITY_COLOR.get(severity, MUTED), space_after=5)

        _refs_block(w, error)

        w.gap(4)
        w.text("Что найдено", size=8.5, bold=True, color=MUTED)
        w.text(error.get("finding") or "—", size=9.5, space_after=4)
        w.text("Что требуется", size=8.5, bold=True, color=MUTED)
        w.text(error.get("action") or "—", size=9.5)
        if error.get("evidence"):
            w.gap(3)
            w.text("Подтверждение", size=8.5, bold=True, color=MUTED)
            w.text(error["evidence"], size=8.5, color=(0.3, 0.32, 0.36))


def build(report, session, manifest_path=None, doc_types=None, version=""):
    """Собирает PDF отчёта и возвращает его байтами."""
    w = _Writer()
    manifest = _read_manifest(manifest_path) if manifest_path else {}
    _cover(w, session, report, manifest, version)
    _findings(w, report)
    w.paginate("Анализатор проектной документации · {}".format(
        session.get("name") or ""))
    return w.tobytes()
