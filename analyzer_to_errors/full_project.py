"""
ПОЛНЫЙ ПРОЕКТ: один PDF на 200+ листов -> отдельные документы в base_files.

Бюро отдаёт не комплект файлов на шкаф, а альбом целиком: 184-309 листов, внутри
которых лежат принципиальные схемы десятка разных шкафов, схемы внешних
подключений, общие виды, спецификация, кабельный журнал, планы расположения и
пояснительная записка. Пайплайну такой файл скормить нельзя: базовый парсер
рассчитан на документ ОДНОГО вида, а тип документа определяется по имени файла,
которого у листа внутри альбома нет.

Здесь альбом режется на части ДО стадии извлечения. Каждая часть выкладывается
в data/base_files/<шкаф>/ отдельным PDF с пометкой типа в имени
("(scheme)ЩС2. Схема электрическая принципиальная.pdf"), после чего ingest.py и
bundles.py работают ровно как раньше и править их не пришлось: пометка типа и
"подпапка = связка" - механизмы, которые уже есть.

ЧТО СЛУЖИТ ГРАНИЦЕЙ ДОКУМЕНТА

Графа "наименование" основной надписи (штампа) по ГОСТ 21.101. Часть - серия
подряд идущих листов с одним наименованием; лист без заполненной графы
(форма 4, продолжение) наследует наименование предыдущего листа.

Измерено на трёх реальных альбомах, два других сигнала отвергнуты:

* НОМЕР ЛИСТА. На "11-463-2026-АТХ" номера идут 4.1...4.70, затем 5.1...5.41 -
  ведущая цифра и есть номер документа, граница видна идеально. Но "24-051-ЭОМ"
  нумерует альбом СКВОЗНО (6, 22.1, 40.2), где ".N" - подлист одного листа, а не
  новый документ. Одна и та же запись значит у двух бюро разное.

* ФОРМА ШТАМПА (наличие граф "Стадия" и "Листов" = первый лист документа).
  По ГОСТ верно, и на "11-463-2026-АТХ" даёт ровно 10 документов. Но "24-051-ЭОМ"
  ставит полную форму на КАЖДОМ листе: 48 "документов", половина по одной
  странице. Правило проверяет привычку бюро, а не структуру альбома.

Наименование - единственное из трёх, что на всех трёх альбомах значит одно и то
же, потому что это единственное, что ГОСТ обязывает заполнять по смыслу.

КАК НАИМЕНОВАНИЕ ДОКУМЕНТА ОТДЕЛЯЕТСЯ ОТ НАИМЕНОВАНИЯ ОБЪЕКТА

В штампе они стоят в ОДНОЙ колонке друг под другом ("Блочно-модульная котельная
установленной мощностью 48,0 МВт" сверху, "Схема электрическая принципиальная
ЩС2" снизу), и разделить их по координате нельзя: число строк в верхней части
плавает от листа к листу. Разделяем по данным: наименование объекта дословно
повторяется на большинстве листов альбома, наименование документа - нет. Тот же
приём, что в assembly_rules.py для поиска парного листа, и по той же причине -
на надписи полагаться нельзя, на статистику можно.
"""

import logging
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

import fitz

import bundles
import normalize
import script_loader

logger = logging.getLogger(__name__)

# Порог, с которого альбом считается "полным проектом", а не одиночным
# документом. Самый большой одиночный документ в корпусе - спецификация на
# 64 листа; самый маленький альбом - 184. Порог стоит между ними с большим
# запасом в обе стороны.
FULL_PROJECT_MIN_PAGES = 80

# Метки-якоря основной надписи. Левый блок - шапка таблицы изменений, правый -
# блок "Стадия/Лист/Листов". Колонка наименования лежит МЕЖДУ ними.
LEFT_LABELS = {"изм.", "изм", "кол.уч.", "кол.уч", "кол. уч.", "кол.уч.",
               "№ док", "№ док.", "№док", "подп.", "дата"}
RIGHT_LABELS = {"стадия", "листов"}

# Графы, попадающие в ту же колонку по X, но наименованием не являющиеся.
# "Формат" стоит там на листах формы 4, блок "Масса/Масштаб" - на чертежах
# общего вида. Не выкинув их, каждый лист-продолжение получает собственное
# "наименование" и документ рассыпается полистно (проверено: 94 части вместо 48).
JUNK_LINE = re.compile(
    r"^(формат|масса|масштаб|лист|листов|стадия|дата|инв\.|взам\.|подп\.|"
    r"разработал|разраб\.|проверил|пров\.|н\.\s*контр|нач\.|гип|гап|"
    r"утвердил|согласовано|копировал)\b"
    r"|^[\d.,/\s×xX-]*$"
    # Голое обозначение формата без слова «Формат»: "А3", "A4x4", "А3х5".
    # Так пишет бюро на "24-051-АК", и без этой ветки "a3" оказывался самой
    # частой строкой колонки, объявлялся наименованием ОБЪЕКТА, а настоящее
    # наименование объекта оставалось в наименовании документа.
    r"|^[aаAА]\s*\d(\s*[xхXХ×]\s*\d+)?$",
    re.I,
)

# Наименование документа -> тип для пайплайна. Порядок ВАЖЕН: проверяется
# сверху вниз, первое совпадение выигрывает. "Схема подключения внешних
# проводок" должна попасть в netlist раньше, чем сработает общее "схема ...".
TITLE_TO_TYPE = [
    # Схема внешних соединений/подключений - это ЧЕРТЁЖ, а не таблица: на листе
    # нарисованы клеммники с графами «№ пров.»/«Конт.», номера проводов (A01,
    # 1А2) и обозначения клеммников (X01, XPE, 1Х1). netlist_to_json.py ждёт
    # построчную таблицу соединений по ГОСТ и на таком листе честно достаёт
    # НОЛЬ строк (проверено на всех восьми листах ЭОМ). Разбирать его должен
    # парсер схем: он строит цепи и индекс клемм, то есть ровно то, из чего
    # такой лист и состоит.
    (r"внешн\w*\s+(соединен|подключен|проводок)", "scheme"),
    (r"схема\s+(внешних|подключени|соединени)", "scheme"),
    # А вот это - настоящие таблицы, построчные.
    (r"кабельн\w*\s+журнал", "netlist"),
    (r"перечень\s+(входных|выходных)\s+сигналов", "netlist"),
    # «Перечень входных/выходных ПАРАМЕТРОВ контроля, регулирования,
    # управления» - опросная таблица ГОСТ 21.408. На «24-051-АК» это 27 листов
    # (два документа), и до V2.0 они пропадали целиком: их форму штампа не
    # читало чтение основной надписи. Вид таблицы дальше опознаётся сам
    # (netlist_to_json.detect_table_kind -> "param_list").
    (r"перечень\s+(входных|выходных|вход\w*/\w*выход\w*)[^.]*параметров", "netlist"),
    # спецификации и перечни заказываемого
    (r"спецификаци", "spec"),
    (r"перечень\s+элементов", "spec"),
    (r"ведомость\s+(оборудования|материалов)", "spec"),
    # чертежи общего вида = тот же сборочный чертёж шкафа.
    # "Вид спереди" - лист-продолжение общего вида: у него штамп чертёжной формы
    # (с графами "Масса"/"Масштаб"), из-за чего в графе наименования остаётся
    # обрывок вида "Вид спер" вместо полного наименования. Без этой строки
    # 18 листов сборочных чертежей ЭОМ уезжали в "не опознано".
    (r"(общий\s+вид|вид\s+общий|сборочн\w*\s+черт)", "assembly"),
    (r"^вид\b|вид\s+(спереди|сзади|сбоку|слева|справа|снизу|сверху)", "assembly"),
    # ФУНКЦИОНАЛЬНАЯ СХЕМА АВТОМАТИЗАЦИИ - НЕ электрическая схема, и стоять
    # обязана ВЫШЕ всех «схема ...»: на ней технологический процесс и приборы в
    # кружках по ГОСТ 21.208, а проводов и клемм нет вовсе. Пока она попадала к
    # парсеру Э3, судьба её листов решалась порогом густоты: лист 27 АК (70095
    # примитивов) пропускался как картинка, а лист 30 (8114) разбирался как
    # схема - в пределах одного документа.
    (r"функциональн\w*\s+схема\s+автоматизации", "functional"),
    (r"схема\s+автоматизации", "functional"),
    # схемы
    (r"схема\s+электрическая\s+принципиальная", "scheme"),
    (r"схема\s+электрическая\s+однолинейная", "scheme"),
    (r"однолинейная\s+схема", "scheme"),
    (r"принципиальная\s+схема", "scheme"),
    (r"функциональная\s+схема", "scheme"),
    (r"схема\s+структурная|структурная\s+схема", "scheme"),
]

# Наименования, которые СОЗНАТЕЛЬНО не анализируются. Это не ошибка и не
# недоработка: планы расположения, молниезащита, установочные чертежи датчиков и
# пояснительная записка не описывают комплектацию шкафа, а значит по ним нечего
# сверять с спецификацией и схемой. Тащить их в отчёт - только шуметь.
TITLE_SKIP = re.compile(
    r"план\s+расположени|планы\s+расположени|план\s+электрообогрева|"
    r"молниезащита|светоограждение|заземлени|уравнивания\s+потенциалов|"
    r"пояснительная\s+записка|общие\s+данные|содержание\s+тома|"
    r"установочный\s+чертеж|установочный\s+чертёж|установка\s+|"
    r"отборное\s+устройство|ведомость\s+ссылочных|ведомость\s+объемов",
    re.I,
)

# Обозначение шкафа (detect_cabinet, CABINET_RE, CABINET_STOP) переехало в
# bundles.py: теперь его зовёт и web_app/sessions.py для автонейминга сессии по
# именам файлов, а тот на голой стдлибе и full_project (с fitz) импортировать не
# может. bundles.py - стдлиб и общее место для обоих. Реэкспортируем, чтобы
# остальной модуль и тесты, зовущие full_project.detect_cabinet, не менялись.
from bundles import (CABINET_RE, CABINET_STOP,  # noqa: F401,E402
                     _unify_layout, detect_cabinet)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().replace("ё", "е")


def _load_font_fix(scripts_dir: Path, pdf_path: Path):
    """Карта "шрифт -> кодек" из базового парсера схем.

    CAD-экспорт пишет кириллицу битой ToUnicode-таблицей, и без этого фикса в
    штампе вместо наименования лежит "������ �3". Логика уже написана и
    выверена в schematic_diagram_to_data.py - переиспользуем её, а не пишем
    второй раз (второй раз неизбежно разъедется с первым).
    """
    module = script_loader.load(scripts_dir, "schematic_diagram_to_data.py",
                                require=("analyze_fonts", "apply_font_fix"))
    return module.analyze_fonts(str(pdf_path)), module.apply_font_fix


def _stamp_lines(page, font_map, apply_fix):
    """Строки текста в правом нижнем углу листа (там стоит основная надпись)."""
    r = page.rect
    clip = fitz.Rect(r.width * 0.30, r.height * 0.55, r.width, r.height)
    out = []
    for block in page.get_text("dict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(
                apply_fix(sp.get("text", ""), font_map.get(sp.get("font", "")))
                for sp in line.get("spans", [])
            ).strip()
            if text:
                out.append({"bbox": line["bbox"], "text": text})
    return out


def _right_labels(lines):
    """Строки-метки блока "Стадия/Лист/Листов" - правый якорь основной надписи."""
    return [l for l in lines if _norm(l["text"]) in RIGHT_LABELS]


def stamp_separators(page, y_from):
    """Вертикальная линовка основной надписи: X границ граф ниже y_from.

    Графы штампа - настоящая нарисованная таблица, и её границы прочерчены
    короткими отрезками (или тонкими прямоугольниками) по краям ячеек. Тот же
    приём, что в specification_pdf_to_json: длинных вертикалей на листе нет ни
    одной, поэтому копится суммарная длина по каждому X.
    """
    acc = defaultdict(float)
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                x, y0, y1 = p1.x, min(p1.y, p2.y), max(p1.y, p2.y)
            elif item[0] == "re":
                rect = item[1]
                x, y0, y1 = rect.x0, rect.y0, rect.y1
                if rect.width > 1.5:
                    continue
            else:
                continue
            if item[0] == "l" and (abs(p1.x - p2.x) > 1.0 or y1 - y0 < 3):
                continue
            if y1 < y_from:
                continue
            acc[round(x)] += y1 - max(y0, y_from)

    merged = []
    for x in sorted(acc):
        if merged and x - merged[-1] <= 3:
            continue
        merged.append(x)
    return merged


def _title_cell(lines, separators=()):
    """Строки, лежащие в графе наименования, сверху вниз.

    ГРАФУ ЗАДАЁТ ЛИНОВКА, А НЕ МЕТКИ. Прежде колонка ограничивалась правым
    краем левого блока меток ("Изм.", "Кол.уч.", "Дата"), и это отказывало
    молча на двух разных бюро сразу:

    * метки написаны ОДНОЙ строкой ("Изм. Кол.уч Лист N док.", "ѳ док. Дата"),
      и посимвольное сравнение с множеством их не узнаёт;
    * у форм для перечней (ГОСТ 21.101, штамп с расширенной левой частью)
      меток на листе ДВА блока - в рамке формата слева и в самом штампе. Ловился
      блок рамки, y_lo уезжал НИЖЕ наименования, и в графу попадали фамилии
      подписантов. Так на "24-051-АК" молча пропали 27 листов перечня
      входных/выходных параметров - весь документ целиком.

    Линовка от привычек бюро не зависит. Замерено на трёх альбомах и четырёх
    формах штампа: вертикали в полосе штампа идут частой гребёнкой узких граф
    таблицы изменений (шаг ~28 px на A4), а затем следует ОДИН широкий провал -
    это и есть графа наименования (по ГОСТ 21.101 она 70 мм против 10-15 мм у
    соседей) - и сразу за ней блок "Стадия/Лист/Листов". Поэтому графа
    наименования = последняя пара вертикалей левее метки "Стадия".

    Допуска на "левее метки" нет СОЗНАТЕЛЬНО: у "24-051-АК" правая граница самой
    графы "Стадия" отстоит от её надписи на 18 px, и допуска в 20 хватало, чтобы
    парой оказались границы графы "Стадия" (ширина 28 px вместо 198) - наименование
    целиком уезжало мимо.
    """
    right = _right_labels(lines)
    x_lo = x_hi = y_lo = None
    if right:
        x_right = min(l["bbox"][0] for l in right)
        inner = [x for x in separators if x <= x_right]
        if len(inner) >= 2:
            x_lo, x_hi = inner[-2], inner[-1]
            y_lo = min(l["bbox"][1] for l in right)

    if x_lo is None:
        # Штампа чертёжной формы без граф "Стадия"/"Листов" (у ЭОМ так подписаны
        # листы "Вид спереди ЩС1") линовка не выдаёт, и остаётся прежний признак
        # по меткам - он груб, обрезает наименование на границе графы, но шкаф в
        # нём уцелевает, а это ключ связки.
        left = [l for l in lines if _norm(l["text"]) in LEFT_LABELS]
        if not left:
            return []
        x_lo = max(l["bbox"][2] for l in left)
        x_hi = min((l["bbox"][0] for l in right), default=float("inf"))
        y_lo = min(l["bbox"][1] for l in left)

    def in_cell(line):
        x0, y0, x1, _ = line["bbox"]
        if y0 < y_lo - 2 or x1 > x_hi + 2:
            # Выход ВПРАВО за графу - это объединённая ячейка во всю ширину
            # штампа: наименование объекта или шифр проекта, а не наименование
            # документа.
            return False
        if x0 >= x_lo - 2:
            return True
        # Выход ВЛЕВО - другое дело: PyMuPDF склеивает в одну строку соседнюю
        # графу подписи с наименованием ("12.05.26 Отборное устройство..." на
        # шести листах АК), и требование лежать в графе целиком выбрасывало
        # наименование вместе с прилипшей датой. Мерой должно быть ПЕРЕСЕЧЕНИЕ с
        # графой: условие без max(x0, x_lo) пропускало любую строку левее
        # "Стадии" вообще, и в наименования уезжали фамилии подписантов и целые
        # строки меток - ЭОМ рассыпался с 48 частей на 85, АК с 67 на 102.
        return (min(x1, x_hi) - max(x0, x_lo)) >= 0.8 * (x1 - x0)

    cell = [l for l in lines
            if in_cell(l) and not JUNK_LINE.match(l["text"].strip())]
    cell.sort(key=lambda l: (round(l["bbox"][1]), l["bbox"][0]))
    return cell


def _load_progress(scripts_dir):
    """Модуль сообщений о ходе работы из папки скриптов (она копируется в
    каждую сессию, поэтому грузим по пути). Не нашёлся - работаем молча."""
    return script_loader.try_load(scripts_dir, "progress.py")


def read_sheet_titles(pdf_path, scripts_dir):
    """Наименование документа для каждого листа альбома.

    Возвращает список строк длиной в число листов; пустая строка = на листе
    графа не заполнена (форма 4) либо штамп не распознан.
    """
    pdf_path, scripts_dir = Path(pdf_path), Path(scripts_dir)
    font_map, apply_fix = _load_font_fix(scripts_dir, pdf_path)
    reporter = _load_progress(scripts_dir)

    doc = fitz.open(str(pdf_path))
    try:
        # Самое долгое место всей скриптовой стадии на альбоме: штамп читается у
        # каждого из трёхсот листов. Именно здесь пользователь и жмёт «отменить»,
        # решив, что программа повисла, - поэтому лист называем вслух.
        cells = []
        for i, p in enumerate(doc):
            if reporter:
                reporter.page(i + 1, len(doc), stage="разбор альбома: чтение штампов")
            lines = _stamp_lines(p, font_map, apply_fix)
            # Линовку читаем только там, где штамп вообще есть: get_drawings на
            # густом листе разбирает сотни тысяч примитивов, а листу-продолжению
            # (форма 4) она всё равно не понадобится.
            right = _right_labels(lines)
            seps = (stamp_separators(p, min(l["bbox"][1] for l in right))
                    if right else ())
            cells.append(_title_cell(lines, seps))
    finally:
        doc.close()

    # Наименование объекта = строки, повторяющиеся на большинстве ЗАПОЛНЕННЫХ
    # листов. Знаменатель именно заполненные: листов формы 4 в альбоме больше
    # половины, и от общего числа порог бы никогда не срабатывал.
    filled = [c for c in cells if c]
    freq = Counter()
    for cell in filled:
        freq.update({_norm(l["text"]) for l in cell})
    boiler = {t for t, c in freq.items() if c >= 0.5 * max(len(filled), 1)}

    titles = []
    for cell in cells:
        text = " ".join(l["text"] for l in cell if _norm(l["text"]) not in boiler)
        titles.append(re.sub(r"\s+", " ", text).strip())
    return titles


def split_into_parts(titles):
    """Серии подряд идущих листов с одним наименованием.

    Лист с пустым наименованием наследует предыдущее: это форма 4, продолжение
    того же документа. Листы до самого первого наименования (обложка, титул,
    содержание тома) в части не попадают - документа они не образуют.
    """
    parts, current = [], None
    for i, title in enumerate(titles):
        if title and (current is None or _norm(title) != _norm(current["title"])):
            current = {"title": title, "first_page": i, "last_page": i}
            parts.append(current)
        elif current is not None:
            current["last_page"] = i
    return parts


def classify(title):
    """(тип документа, причина). Тип None = часть не анализируется."""
    norm = _norm(title)
    if TITLE_SKIP.search(norm):
        return None, "не описывает комплектацию шкафа"
    for pattern, doc_type in TITLE_TO_TYPE:
        if re.search(pattern, norm):
            return doc_type, f"наименование соответствует {pattern!r}"
    return None, "наименование не опознано как документ известного вида"


# Функциональный код объекта в наименовании: две-четыре ЛАТИНСКИЕ буквы и три
# цифры (OS001, CC001, HQA001, UTG001). На "24-051-АК" коды приняты по
# СТО Газпром 2-1.15-749-2013 - об этом прямо сказано на листе условных
# обозначений ФСА.
FUNCTION_CODE_RE = re.compile(r"\b([A-Z]{2,4}\d{3})\b")


def cabinet_aliases(parts):
    """Обозначения, которые в ЭТОМ альбоме означают один и тот же шкаф.

    Бюро подписывает один щит по-разному на разных документах: «Щит
    общекотельной автоматики ША (ШУК) OS001» у принципиальной схемы и «Щит
    общекотельной автоматики (ЩА) OS001» у схемы подключения внешних проводок.
    Без сведения это ДВЕ связки, и два документа одного щита не сверятся друг с
    другом - молча, ровно как при разъезде раскладок, ради которого заведён
    _unify_layout.

    Ключ сведения - функциональный код объекта, единственное, что у бюро на
    этих листах совпадает. Именно код, а НЕ описательная часть наименования:
    «Щит автоматики котла» стоит в заголовке и ЩАК1, и ЩАК2, и ЩАК3, и слив их
    по описанию, получаем ровно тот вал ложных «разный артикул у одного
    обозначения», ради предотвращения которого связка и делится по шкафам.
    """
    by_code = {}
    for part in parts:
        cabinet = detect_cabinet(part["title"])
        if not cabinet:
            continue
        for code in set(FUNCTION_CODE_RE.findall(part["title"])):
            by_code.setdefault(code, Counter())[cabinet] += 1

    aliases = {}
    for code, names in sorted(by_code.items()):
        if len(names) < 2:
            continue
        canonical = names.most_common(1)[0][0]
        for name in names:
            if name != canonical:
                aliases[name] = canonical
                logger.info("Шкаф %s и %s - один и тот же щит (код %s)",
                            name, canonical, code)
    return aliases


def _safe_name(text, limit=90):
    text = re.sub(r'[<>:"/\\|?*]', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].strip() or "документ"


def is_full_project(pdf_path):
    """Похож ли файл на альбом целиком, а не на один документ."""
    if Path(pdf_path).suffix.lower() != ".pdf":
        return False
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001 - битый файл разберёт стадия извлечения
        return False
    try:
        return len(doc) >= FULL_PROJECT_MIN_PAGES
    finally:
        doc.close()


# Часть, у которой шкаф не определился (спецификация и кабельный журнал идут на
# весь альбом сразу), кладётся сюда. Отдельной связкой, а не в каждый шкаф:
# сверять спецификацию всего объекта с обозначениями одного шкафа - значит
# объявить "нет на чертеже" всё оборудование остальных двенадцати шкафов.
COMMON_BUNDLE_DIR = "общие документы"

# Метка "эту папку нарезал сплиттер, а не положил пользователь". Нужна, чтобы
# перед новым прогоном стереть части прошлой нарезки и не оставить документы от
# альбома, который пользователь уже удалил: связка со шкафом, которого больше
# нет во входных файлах, тихо сверялась бы сама с собой.
#
# Живёт в bundles.py: ту же метку читает web_app/sessions.py, а он по замыслу
# работает на голой стандартной библиотеке и импортировать этот модуль (с его
# fitz) не может.
GENERATED_MARKER = bundles.GENERATED_MARKER


def clear_generated_parts(base_files_dir):
    """Удалить папки, созданные прошлой нарезкой. Файлы пользователя не трогает."""
    base_files_dir = Path(base_files_dir)
    if not base_files_dir.is_dir():
        return 0
    removed = 0
    for item in sorted(base_files_dir.iterdir()):
        if item.is_dir() and (item / GENERATED_MARKER).exists():
            shutil.rmtree(item, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Удалено папок от прошлой нарезки альбомов: %d", removed)
    return removed


def split_full_project(pdf_path, base_files_dir, scripts_dir):
    """Режет один альбом на документы и раскладывает их по base_files/<шкаф>/.

    Возвращает отчёт: что вышло и что сознательно пропущено.
    """
    pdf_path = Path(pdf_path)
    base_files_dir = Path(base_files_dir)

    titles = read_sheet_titles(pdf_path, scripts_dir)
    parts = split_into_parts(titles)
    logger.info("Полный проект %s: %d листов -> %d частей",
                pdf_path.name, len(titles), len(parts))

    reporter = _load_progress(scripts_dir)
    source = fitz.open(str(pdf_path))
    written, skipped, used_names = [], [], set()
    prev_cabinet = None
    aliases = cabinet_aliases(parts)
    try:
        for n_part, part in enumerate(parts, 1):
            if reporter:
                reporter.page(n_part, len(parts),
                              stage="разбор альбома: нарезка документов")
            title = part["title"]
            pages = part["last_page"] - part["first_page"] + 1
            doc_type, reason = classify(title)

            if doc_type is None:
                skipped.append({"title": title, "pages": pages, "reason": reason})
                logger.info("  л.%d-%d пропуск (%s): %s",
                            part["first_page"] + 1, part["last_page"] + 1, reason, title)
                continue

            cabinet_named = detect_cabinet(title)
            if cabinet_named:
                cabinet_named = aliases.get(cabinet_named, cabinet_named)
            cabinet = cabinet_named
            # Лист-продолжение чертежа ("Вид спереди") своего шкафа в
            # наименовании не называет - он относится к тому же шкафу, что и
            # чертёж, за которым идёт. Наследование ограничено чертежами:
            # у схем и спецификаций наименование заполнено полностью, и молча
            # приписать их соседнему шкафу было бы догадкой.
            if cabinet is None and doc_type == "assembly" and prev_cabinet:
                cabinet = prev_cabinet
                logger.info("  л.%d-%d шкаф не назван, наследуется от предыдущего: %s",
                            part["first_page"] + 1, part["last_page"] + 1, cabinet)
            if cabinet is None:
                cabinet = COMMON_BUNDLE_DIR
            prev_cabinet = cabinet if cabinet != COMMON_BUNDLE_DIR else prev_cabinet
            out_dir = base_files_dir / _safe_name(cabinet, 60)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / GENERATED_MARKER).touch()

            stem = _safe_name(title)
            name = f"({doc_type}){stem}"
            # Наименования в альбоме повторяются (у каждого шкафа свой "Общий
            # вид"), а после обрезки до 90 символов совпадают и подавно.
            if (cabinet, name.lower()) in used_names:
                n = 2
                while (cabinet, f"{name} #{n}".lower()) in used_names:
                    n += 1
                name = f"{name} #{n}"
            used_names.add((cabinet, name.lower()))

            out_path = out_dir / f"{name}.pdf"
            part_doc = fitz.open()
            try:
                part_doc.insert_pdf(source, from_page=part["first_page"],
                                    to_page=part["last_page"])
                part_doc.save(str(out_path))
            finally:
                part_doc.close()

            written.append({
                "title": title,
                "doc_type": doc_type,
                "cabinet": cabinet,
                "pages": pages,
                "first_page": part["first_page"] + 1,
                "last_page": part["last_page"] + 1,
                "file": str(out_path.relative_to(base_files_dir)).replace("\\", "/"),
            })
            logger.info("  л.%d-%d -> [%s] %s/%s",
                        part["first_page"] + 1, part["last_page"] + 1,
                        doc_type, cabinet, name)
    finally:
        source.close()

    return {
        "source_file": pdf_path.name,
        "total_pages": len(titles),
        "parts_total": len(parts),
        "parts_written": written,
        "parts_skipped": skipped,
    }


def split_full_projects(full_projects_dir, base_files_dir, scripts_dir,
                        explicit_types=None):
    """Все альбомы из папки full_projects -> документы в base_files.

    Папка исходников лежит ВНЕ base_files сознательно: base_files сканируется
    рекурсивно, и подпапка в нём означает связку (см. bundles.py). Альбом,
    положенный внутрь, стал бы "связкой" из одного нечитаемого файла на 200
    листов, который к тому же не прошёл бы определение типа и осел в
    skipped_files.

    explicit_types: {имя файла в base_files: тип}, выставленные пользователем в
    интерфейсе. Файл с явным КОНКРЕТНЫМ типом (scheme/assembly/...) в альбом НЕ
    превращается, даже если в нём ≥80 листов: пометка пользователя главнее
    догадки по числу листов. Это симметрично прежнему правилу «помечен как
    альбом → альбом даже короче порога» (prepare_run).
    """
    full_projects_dir = Path(full_projects_dir)
    base_files_dir = Path(base_files_dir)

    albums = collect_albums(base_files_dir, full_projects_dir, explicit_types)
    if not albums:
        return []

    clear_generated_parts(base_files_dir)
    reports = [split_full_project(p, base_files_dir, scripts_dir) for p in albums]
    _write_parts_sidecar(base_files_dir, reports)
    return reports


def _write_parts_sidecar(base_files_dir, reports):
    """Сайдкар «часть -> страницы альбома» для интерфейса (bundles.ALBUM_PARTS_FILE).

    Пишется свежим на каждую нарезку (clear_generated_parts уже стёр прошлые
    части), ключ - путь части относительно base_files, тот же, каким web_app
    адресует файл. web_app открыть PDF не может, а номер страницы у части знаем
    здесь, где уже есть fitz."""
    import json

    index = {}
    for rep in reports:
        for part in rep.get("parts_written", []):
            index[part["file"]] = {
                "first_page": part["first_page"],
                "last_page": part["last_page"],
                "source_file": rep.get("source_file"),
            }
    try:
        (Path(base_files_dir) / bundles.ALBUM_PARTS_FILE).write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Не удалось записать сайдкар страниц частей альбома: %s", e)


def collect_albums(base_files_dir, full_projects_dir, explicit_types=None):
    """Альбомы, которые надо разрезать: из папки full_projects и из base_files.

    Разбирать base_files тоже приходится потому, что интерфейс кладёт туда ВСЁ,
    что загрузил пользователь: web_app работает на одной стандартной библиотеке
    и открыть PDF, чтобы сосчитать листы, не может (fitz там нет и по замыслу
    быть не должно). Поэтому альбом опознаётся здесь, где fitz уже есть, - по
    числу листов, а не по имени файла и не по тому, в какую папку он попал.

    Опознанный альбом ПЕРЕЕЗЖАЕТ в full_projects: иначе стадия извлечения
    следом попыталась бы разобрать его как обычный документ, не смогла бы
    определить тип и молча положила в skipped_files - рядом с уже нарезанными
    из него же частями.

    explicit_types: файлы с явным типом от пользователя (см. split_full_projects).
    Файл, помеченный КОНКРЕТНЫМ типом, из base_files не забираем даже при ≥80
    листах: большая принципиальная схема остаётся схемой, если пользователь так
    сказал. Пометка «полный проект» сюда не попадает - её файл перенёс в
    full_projects уже prepare_run.
    """
    base_files_dir = Path(base_files_dir)
    full_projects_dir = Path(full_projects_dir)
    explicit_types = explicit_types or {}

    albums = []
    if full_projects_dir.is_dir():
        albums += [
            p for p in sorted(full_projects_dir.iterdir())
            if p.is_file() and p.suffix.lower() == ".pdf"
            and not p.name.startswith((".", "~$"))
        ]

    if base_files_dir.is_dir():
        for path in sorted(base_files_dir.glob("*.pdf")):
            if path.name.startswith((".", "~$")) or not is_full_project(path):
                continue
            if path.name in explicit_types:
                logger.info("%s: ≥%d листов, но помечен пользователем как '%s' - "
                            "не превращаю в альбом", path.name,
                            FULL_PROJECT_MIN_PAGES, explicit_types[path.name])
                continue
            full_projects_dir.mkdir(parents=True, exist_ok=True)
            moved = full_projects_dir / path.name
            shutil.move(str(path), str(moved))
            logger.info("%s: %s листов - это полный проект, перенесён в %s",
                        path.name, "≥%d" % FULL_PROJECT_MIN_PAGES,
                        full_projects_dir.name)
            albums.append(moved)

    return albums
