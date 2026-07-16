#!/usr/bin/env python3
"""
Профили (диалекты) оформления принципиальных схем.

Зачем это нужно. Базовые скрипты (schematic_diagram_to_data.py,
schematic_connectivity.py) распознают подписи на схеме набором regex'ов.
Изначально эти regex'ы были заточены под ОДНО проектное бюро/шаблон:
Regul R500 + KKS-теги "00..." + межлистовые ссылки EPLAN вида "12.4:D".
На схеме другого бюро (ОВЕН МВ210/МУ210, IEC-позиционные теги "-2XT.AP",
ссылки вида "/4.9.2", сечение "4x1") те же regex'ы почти ничего не ловят --
90% осмысленного текста уходит в "unclassified", а межлистовые ссылки,
теги приборов, сечения и модули дают ноль.

Решение -- вынести ВСЕ форматно-зависимые правила в объект Profile и
держать по одному профилю на шаблон. Документ при загрузке авто-детектится
(по сигнатурным паттернам: формат межлистовой ссылки + марка модулей), и
дальше пайплайн работает через выбранный профиль.

Профили (проверены на реальных файлах):
  A -- Regul R500 / KKS-теги "00..." / ссылки EPLAN "12.4:D".
       Это ИСХОДНЫЕ правила один-в-один: нулевой регресс на старых файлах.
  B -- ОВЕН МВ210/МУ210, IEC-теги "-2XT.AP", ссылки "/4.9.2".
  C -- Delta DVP (щиты ША/Э3), ссылки "(6:5E)", кириллица в mojibake.
  D -- ОВЕН 110 (ПЛК110/МВ110/МУ110, щиты ШУ), теги "2XT-G1", маркировка
       цепей "13N1"; межлистовых ссылок в шаблоне НЕТ вообще.

Добавление пятого бюро = ещё один Profile + пара сигнатур в detect_profile(),
без правки логики пайплайна. Если ни одна сигнатура не сработала,
detect_profile() ГРОМКО предупреждает: молчаливый откат в A даёт мусор на
выходе (почти всё в "unclassified") и это легко не заметить.
"""
import re


# ==================================================================
# Общие (профиле-независимые) куски классификации
# ==================================================================

GRID_DIGIT_RE = re.compile(r'^[1-9]$')
CONNECTOR_GLYPH = {'-', '/', '+'}


def _classify_grid_or_terminal(t, bbox, page_height):
    """Одиночная цифра 1..9: координатная сетка у края листа vs номер клеммы
    в теле схемы. Общая логика для всех профилей."""
    if bbox is not None and page_height is not None:
        margin = 20.0
        y0 = bbox[1]
        if y0 <= margin or y0 >= page_height - margin:
            return "page_frame"
        return "terminal_no"
    return "page_frame"


# ==================================================================
# Класс профиля
# ==================================================================

class Profile:
    """Набор форматных правил одного шаблона оформления.

    Поля, которые читает пайплайн:
      name                 -- человекочитаемое имя (пишется в raw.json/логи)
      page_frame_words     -- множество надписей рамки/штампа -> "page_frame"
      classify_span(...)   -- главный классификатор одного текстового span'а
      parse_cross_ref(t)   -- разбор межлистовой ссылки -> dict|None
      device_tag_re,
      device_tag_complete_re,
      kks_tag_re           -- для склейки разбитых тегов (merge_split_tags)
      connector_tag_re     -- обозначение клеммной колодки (порядок клемм)
      label_types          -- какие типы подписей привязывать к концам цепей
      nl_module_re, nl_channel_re, nl_kks_re, nl_eq_re,
      nl_junk_patterns     -- извлечение netlist по каналам ввода/вывода
    """

    def __init__(self, name):
        self.name = name


# ==================================================================
# ПРОФИЛЬ A: Regul R500 / KKS / EPLAN  (текущие правила, один-в-один)
# ==================================================================

A_CROSS_REF_RE = re.compile(r'^/?\d{1,3}\.\d{1,2}:[A-F]$')
A_CROSS_REF_PARSE_RE = re.compile(r'^/?(\d{1,3})\.(\d{1,2}):([A-F])$')
A_DEVICE_TAG_RE = re.compile(
    r'^(AA|BA|CA|CB|RA|RB|XM|XB|XA|XT|XP[AB]|XS|QF|SF|SQ|VD|EL|SK|SFD|FU|U)\d{0,3}$')
A_DEVICE_TAG_COMPLETE_RE = re.compile(
    r'^(AA|BA|CA|CB|RA|RB|XM|XB|XA|XT|XP[AB]|XS|QF|SF|SQ|VD|EL|SK|SFD|FU|U)\d{1,3}$')
A_IO_CHANNEL_RE = re.compile(r'^(AI|AO|DI|DO)\d{1,2}$')
A_WIRE_GAUGE_RE = re.compile(r'^\d+([.,]\d+)?\s*мм²$')
A_MODULE_PARTNO_RE = re.compile(r'^R500\s')
A_CABLE_TYPE_RE = re.compile(r'^\d+[хx]\d+[хx][\d,.]+$|^КДВВГ')
A_KKS_TAG_RE = re.compile(r'^00[A-Z]{2,4}\d{2}[A-Z]{2}\d{3}$|^00CJF02')
A_PIN_REF_RE = re.compile(r'^\d{1,2}-\d$')
A_PLAIN_NUM_RE = re.compile(r'^\d{1,4}$')
A_SIGNAL_STATE_RE = re.compile(r'^(FB_[A-Z]+|C_[A-Z]+)$')
A_POWER_PIN_RE = re.compile(r'^(PE|0V|\+24|24V|X1|X2|X3|A1|A2|L|N)$')
A_RESERVE_RE = re.compile(r'^Резерв$')
A_COIL_TERMINAL_RE = re.compile(r'^\d{1,2}С$')
A_CONNECTOR_TAG_RE = re.compile(r'^(XA\d{0,3}|XM\d{0,3}|XB\d{0,3}|XT\d{0,3}|XP[AB]\d{0,3})$')

A_PAGE_FRAME_WORDS = {
    'Формат  А3', 'Инв.N подл.', 'Взам. инв. N', 'Подп. и дата', 'Лист',
    'Подп.', '№док.', 'Дата', 'Изм.', 'Кол.уч', 'Зам.', 'A', 'B', 'C', 'D', 'E', 'F',
}


def _classify_a(t, size, bbox, page_width, page_height):
    if t in A_PAGE_FRAME_WORDS:
        return "page_frame"
    if GRID_DIGIT_RE.match(t):
        return _classify_grid_or_terminal(t, bbox, page_height)
    if t in CONNECTOR_GLYPH:
        return "glyph"
    if A_SIGNAL_STATE_RE.match(t):
        return "signal_state"
    if A_POWER_PIN_RE.match(t):
        return "power_pin"
    if A_RESERVE_RE.match(t):
        return "reserve_label"
    if A_COIL_TERMINAL_RE.match(t):
        return "coil_terminal"
    if A_CROSS_REF_RE.match(t):
        return "cross_ref"
    if A_IO_CHANNEL_RE.match(t):
        return "io_channel"
    if A_WIRE_GAUGE_RE.match(t):
        return "wire_gauge"
    if A_MODULE_PARTNO_RE.match(t):
        return "module_partno"
    if A_CABLE_TYPE_RE.match(t):
        return "cable_type"
    if A_KKS_TAG_RE.match(t):
        return "instrument_tag"
    if A_DEVICE_TAG_RE.match(t):
        return "device_tag"
    if A_PIN_REF_RE.match(t):
        return "pin_ref"
    if A_PLAIN_NUM_RE.match(t):
        return "terminal_no"
    if t.startswith('ИК.'):
        return "doc_number"
    if re.match(r'^\d{2}\.\d{2}$', t):
        return "date"
    if len(t) > 15 and any(c.isalpha() for c in t) and size > 3:
        return "long_text"
    return "unclassified"


def _parse_cross_ref_a(text):
    m = A_CROSS_REF_PARSE_RE.match(text)
    if not m:
        return None
    return {"target_sheet": int(m.group(1)),
            "target_col": int(m.group(2)),
            "target_zone": m.group(3)}


A_NL_MODULE_RE = re.compile(r'^([A-C]A\d{2})\s*$')
A_NL_CHANNEL_RE = re.compile(r'^(DI|AI|DO|AO)\s*(\d{1,2})\s*$')
A_NL_KKS_RE = re.compile(r'\b(00[A-Z0-9]{9,11})\b')
A_NL_EQ_RE = re.compile(r'\b(\d{1,2}[A-Z]{1,3}\d{1,2})\b')
A_NL_JUNK_PATTERNS = [
    re.compile(r'^\d+[.,]?\d*\s*м?м²', re.IGNORECASE),
    re.compile(r'^\d+[õxх]\d+[xх]\d+'),
    re.compile(r'^\d{1,4}$'),
    re.compile(r'^\d+-\d+'),
    re.compile(r'^/[0-9]+\.[0-9]:[A-F]'),
    re.compile(r'^[A-Z]{2}\d+:\d+'),
    re.compile(r'^X[A-Z]\d+'),
    re.compile(r'^PE$'),
    re.compile(r'^\d+W\d+$'),
    re.compile(r'^FB_[A-Z]+'),
    re.compile(r'^C_[A-Z_]+'),
    re.compile(r'КДВВГнг|КДВВГ|LS', re.IGNORECASE),
    re.compile(r'^[A-C]A\d{2}:[A-B]\d'),
    re.compile(r'^RB\d{2}:\d+[A-Z]?'),
    re.compile(r'^RA\d{2}:\d+[A-Z]?'),
    re.compile(r'^Формат\s+А\d+'),
    re.compile(r'^Инв\.?\s*N\s*подл'),
    re.compile(r'^Взам\.?\s*инв'),
    re.compile(r'^Подп\.?\s*и\s*дата'),
    re.compile(r'^Лист$'),
    re.compile(r'^Подп\.?$'),
    re.compile(r'^№\s*док'),
    re.compile(r'^Дата$'),
    re.compile(r'^Изм\.?$'),
    re.compile(r'^Кол\.?\s*уч'),
    re.compile(r'^Зам\.?$'),
    re.compile(r'^ИК\.\d{4}-'),
    re.compile(r'^Схема\s+соединения\s+модуля'),
    re.compile(r'^Подключение\s+ПЛК'),
]

PROFILE_A = Profile("A: Regul R500 / KKS / EPLAN")
PROFILE_A.page_frame_words = A_PAGE_FRAME_WORDS
PROFILE_A.classify_span = staticmethod(_classify_a)
PROFILE_A.parse_cross_ref = staticmethod(_parse_cross_ref_a)
PROFILE_A.device_tag_re = A_DEVICE_TAG_RE
PROFILE_A.device_tag_complete_re = A_DEVICE_TAG_COMPLETE_RE
PROFILE_A.kks_tag_re = A_KKS_TAG_RE
PROFILE_A.connector_tag_re = A_CONNECTOR_TAG_RE
PROFILE_A.label_types = ("device_tag", "instrument_tag", "terminal_no", "pin_ref",
                         "power_pin", "wire_gauge", "io_channel", "signal_state",
                         "reserve_label", "coil_terminal", "cable_type")
PROFILE_A.nl_module_re = A_NL_MODULE_RE
PROFILE_A.nl_channel_re = A_NL_CHANNEL_RE
PROFILE_A.nl_kks_re = A_NL_KKS_RE
PROFILE_A.nl_eq_re = A_NL_EQ_RE
PROFILE_A.nl_junk_patterns = A_NL_JUNK_PATTERNS


# ==================================================================
# ПРОФИЛЬ B: ОВЕН МВ210/МУ210 / IEC-позиционные теги
# ==================================================================
# Отличия от A (замерено на реальном файле "..._Енисей_4..."):
#   межлистовая ссылка   /4.9.2   (лист.колонка.строка, три числа, без зоны-буквы)
#   тег устройства       -2XT.AP, 2XF1, XA.36, F1, S1, -A3   (IEC, префикс '-',
#                        ведущий номер локации, точка-суффикс)
#   модуль               МВ210-212, МУ210-403   (ОВЕН, не R500)
#   сечение              4x1, 10x1, 4х2х0,52    (жилы x мм², не "N мм²")
#   тип кабеля           КВВГнг-LS, ВВГнг(А)-LS, МКЭШнг(А)-LS, ParLan...
#   напряжение           48 B, 24B, 230 B, 0В, +24В   (лат./кир. B, пробелы)
#   ток/уставка          2А, 0,05А, 6A
#   длина кабеля         15 м
#   тегов KKS "00..."    нет вообще

B_CROSS_REF_RE = re.compile(r'^/?\d{1,3}\.\d{1,2}\.\d{1,2}$')
B_CROSS_REF_PARSE_RE = re.compile(r'^/?(\d{1,3})\.(\d{1,2})\.(\d{1,2})$')

# IEC-позиционный тег устройства. Структурный (а не по списку кодов) распознаватель:
#   [-]  необяз. дефис-признак обозначения
#   \d{0,2}(\.\d{1,2})?   номер монтажной локации: 13, 1.23
#   [A-Z]{1,3}            буквенный код устройства (K, KA, XT, QF, AIT...)
#   далее ОБЯЗАТЕЛЬНО хотя бы одно из: номер (13KA2), или точечный суффикс
#     (-XT.A2.A7) -- это отсекает голые двухбуквенные аббревиатуры (NO/NC/SS),
#     у которых нет ни номера, ни суффикса.
#   [а-яА-Я]?  необяз. буква-вариант вывода (-1X1а)
#   \*?        необяз. сноска-звёздочка (-AIT1.1*)
# Разделитель суффиксов -- ТОЛЬКО точка: это намеренно, чтобы не хватать
# номера моделей через дефис (RJ-45, HDR-30-24, DR-RDN20 -- это не теги).
_B_DEV_BODY = (
    r'-?\d{0,2}(\.\d{1,2})?'
    r'[A-Z]{1,3}'
    r'(\d{1,3}(\.[A-ZА-Я0-9]{1,5})*|(\.[A-ZА-Я0-9]{1,5})+)'
    r'[а-яА-Я]?\*?')
B_DEVICE_TAG_RE = re.compile(r'^' + _B_DEV_BODY + r'$')
# Для склейки разбитых тегов -- тот же распознаватель (он и так требует
# номер или суффикс, «голого» префикса без номера не пропустит).
B_DEVICE_TAG_COMPLETE_RE = B_DEVICE_TAG_RE
# У этого бюро KKS "00..." не встречается; regex оставлен ради единообразия
# (ничего не ловит на файле B, но и не мешает).
B_KKS_TAG_RE = re.compile(r'^00[A-Z]{2,4}\d{2}[A-Z]{2}\d{3}$')

# Кабельное обозначение (W = кабель/провод по ГОСТ/IEC): -W.H25.1, -WA.H1.01, W1.
# Отделяем от устройств: для поиска ошибок важно знать, что это ИМЕННО кабель.
B_CABLE_TAG_RE = re.compile(
    r'^-?\d{0,2}(\.\d{1,2})?WA?'
    r'(\d{1,3}(\.[A-ZА-Я0-9]{1,5})*|(\.[A-ZА-Я0-9]{1,5})+)'
    r'[а-яА-Я]?\*?$')

# Каналы ввода/вывода и их выводы:
#   DI13 (канал), DO1B/DO13A (контакты A/B реле), DO1,2A (групповой),
#   AI1-1 / AI1-2 (плюс/минус аналогового входа), AI1-R (опорный), AO1C (общий).
B_IO_CHANNEL_RE = re.compile(
    r'^(AI|AO|DI|DO)\d{1,2}([,.\-](\d{1,2}|[A-DR]))?[A-DR]?$')
B_IO_HEADER_RE = re.compile(r'^(AI|AO|DI|DO)$')
B_VOLTAGE_RE = re.compile(r'^[+\-]?\d{1,3}([.,]\d+)?\s*[BВvV]$')   # 48 B, 24B, 230 B, 0В, +24В
B_CURRENT_RE = re.compile(r'^\d{1,3}([.,]\d+)?\s*[AА]$')          # 2А, 0,05А, 6A
B_CABLE_LEN_RE = re.compile(r'^\d{1,4}\s*м$')                     # 15 м
B_WIRE_GAUGE_RE = re.compile(r'^\d+[xх]\d+([xх][\d.,]+)?$')       # 4x1, 10x1, 4х2х0,52
B_CABLE_TYPE_RE = re.compile(
    r'ВВГ|КВВГ|МКЭШ|КВК|КГ|ПвПг|ПуГВ|ParLan|LiYCY|РК\s?75|КИПЭВ', re.IGNORECASE)
B_MODULE_PARTNO_RE = re.compile(r'^М[ВУКР]\d{3}(-\d{2,3})?$')     # МВ210-212, МУ210-403
B_PIN_REF_RE = re.compile(r'^\d{1,2}-\d$')
B_PLAIN_NUM_RE = re.compile(r'^\d{1,4}$')
B_POWER_PIN_RE = re.compile(r'^(PE|0V|0В|\+24|24V|X1|X2|X3|A1|A2|L|N|L1|L2|L3)$')
B_RESERVE_RE = re.compile(r'^(Резерв|Резервный)$')
B_LOCATION_RE = re.compile(r'^\+[A-ZА-Я0-9]')                     # +G2, +L7, +ВРУ ...
B_DOC_NUMBER_RE = re.compile(r'^\d{2}-\d{3}-\d{4}-[А-Я]{2,4}$|^\d{5}-\d-\d-[А-Я]{2}$')
B_POWER_TERMINAL_RE = re.compile(r'^\d/(L\d?|N|PE)$')            # 2/L, 3/N, 1/PE

# Цвет жилы по IEC 60757 (важно для сверки монтажа): BK чёрный, BN коричн.,
# BU синий, GY серый, GN зелёный, RD красн., WH бел., YE жёлт., GNYE зел.-жёлт.
B_WIRE_COLOR = {
    'BK', 'BN', 'BU', 'GY', 'GN', 'RD', 'WH', 'YE', 'OG', 'VT', 'PK', 'GD', 'SR',
    'TQ', 'GNYE', 'BUWH', 'RDWH', 'GNWH', 'WHBU', 'WHBN', 'RDBU', 'YEGN',
}
# Тип контакта: NO норм. открытый, NC норм. закрытый, COM общий.
B_CONTACT_TYPE = {'NO', 'NC', 'COM'}
# Русская подпись-сигнал (что за сигнал/состояние на проводе): "Авария",
# "Команда открыть", "Открыт". Латиница/цифра/знак в начале -> НЕ сигнал
# (это отсекает строки таблицы оборудования вида "AA13.1.1 Затвор").
B_SIGNAL_LABEL_RE = re.compile(r'^[А-Яа-яЁё][А-Яа-яЁё /.,()«»\-+№]{2,39}$')
# Строка перечня оборудования: "поз. обозначение + наименование" одной подписью,
# напр. "AA13.1.1 Затвор". Тег-владелец + русское наименование. Полезно для
# сверки "что за устройство под этим тегом".
B_EQUIPMENT_ENTRY_RE = re.compile(r'^(' + _B_DEV_BODY + r')\s+[А-Яа-яЁё]')

B_PAGE_FRAME_WORDS = {
    'Формат', 'Формат  А3', 'Формат А3', 'Формат  А4', 'Формат А4',
    'Инв. № подл.', 'Взам. инв. №', 'Подп. и дата', 'Лист', 'Листов',
    'Подп.', '№ док', '№ док.', 'Дата', 'Изм.', 'Кол.уч', 'Кол. уч.', 'Зам.',
    'Согласовано', 'Разраб.', 'Пров.', 'Н. контр.', 'Утв.', 'Примечание:',
    'Наименование', 'Поз.', 'Поз', 'обозначение', 'Примечание', 'Кол.', 'Кол',
    'Наимен.', 'A', 'B', 'C', 'D', 'E', 'F',
}
B_FRAME_RE = re.compile(r'^Формат\b')


def _classify_b(t, size, bbox, page_width, page_height):
    if t in B_PAGE_FRAME_WORDS or B_FRAME_RE.match(t):
        return "page_frame"
    if GRID_DIGIT_RE.match(t):
        return _classify_grid_or_terminal(t, bbox, page_height)
    if t in CONNECTOR_GLYPH:
        return "glyph"
    if B_CROSS_REF_RE.match(t):
        return "cross_ref"
    if B_IO_CHANNEL_RE.match(t) or B_IO_HEADER_RE.match(t):
        return "io_channel"
    if B_POWER_PIN_RE.match(t):
        return "power_pin"
    if B_POWER_TERMINAL_RE.match(t):     # 2/L, 3/N, 1/PE
        return "power_pin"
    if B_VOLTAGE_RE.match(t):
        return "voltage"
    if B_CURRENT_RE.match(t):
        return "current_rating"
    if B_CABLE_LEN_RE.match(t):
        return "cable_length"
    if B_WIRE_GAUGE_RE.match(t):
        return "wire_gauge"
    if B_MODULE_PARTNO_RE.match(t):
        return "module_partno"
    if B_CABLE_TAG_RE.match(t):          # -W.H25.1, W1  (кабельное обозначение)
        return "cable_tag"
    if B_CABLE_TYPE_RE.search(t):        # КВВГнг-LS ... (тип/марка кабеля)
        return "cable_type"
    if t in B_WIRE_COLOR:
        return "wire_color"
    if t in B_CONTACT_TYPE:
        return "contact_type"
    if B_RESERVE_RE.match(t):
        return "reserve_label"
    if B_KKS_TAG_RE.match(t):
        return "instrument_tag"
    if B_DEVICE_TAG_RE.match(t):
        return "device_tag"
    if B_PIN_REF_RE.match(t):
        return "pin_ref"
    if B_PLAIN_NUM_RE.match(t):
        return "terminal_no"
    if B_LOCATION_RE.match(t):
        return "location_ref"
    if B_DOC_NUMBER_RE.match(t):
        return "doc_number"
    if re.match(r'^\d{2}\.\d{2}$', t):
        return "date"
    if B_EQUIPMENT_ENTRY_RE.match(t):     # "AA13.1.1 Затвор" (тег + наименование)
        return "equipment_entry"
    # Русская подпись-сигнал: короткий кириллический текст, не начинающийся
    # с латиницы/цифры (те -- строки таблиц оборудования, не сигналы).
    if B_SIGNAL_LABEL_RE.match(t) and sum(c.isalpha() for c in t) >= 3:
        return "signal_label"
    if len(t) > 15 and any(c.isalpha() for c in t) and size > 3:
        return "long_text"
    return "unclassified"


def _parse_cross_ref_b(text):
    m = B_CROSS_REF_PARSE_RE.match(text)
    if not m:
        return None
    # Формат "/4.9.2": ПЕРВОЕ число -- постоянный идентификатор раздела/комплекта
    # ("=4"), одинаковый для всех ссылок в документе; РЕАЛЬНЫЙ целевой лист -- ВТОРОЕ
    # число (замерено: диапазон 6..70 ровно под 70 листов), третье -- колонка внутри
    # листа. Зоны-буквы (как "D" в профиле A) в этом шаблоне нет.
    return {"target_section": int(m.group(1)),
            "target_sheet": int(m.group(2)),
            "target_col": int(m.group(3)),
            "target_zone": None}


B_NL_MODULE_RE = re.compile(r'^(AA\d{2}|М[ВУК]\d{3}-\d{2,3})\s*$')
B_NL_CHANNEL_RE = re.compile(r'^(DI|AI|DO|AO)\s*(\d{1,2})\s*$')
B_NL_KKS_RE = re.compile(r'\b(00[A-Z0-9]{9,11})\b')   # не встречается, оставлено
B_NL_EQ_RE = re.compile(r'\b(-?\d{0,2}[A-ZА-Я]{1,3}\d{1,2}(?:\.[A-ZА-Я0-9]{1,4})?)\b')
B_NL_JUNK_PATTERNS = [
    re.compile(r'^\d+[.,]?\d*\s*м?м²', re.IGNORECASE),
    re.compile(r'^\d+[xх]\d+([xх][\d.,]+)?$'),
    re.compile(r'^\d{1,4}$'),
    re.compile(r'^\d+-\d+'),
    re.compile(r'^/\d+\.\d+\.\d+'),                 # межлистовая ссылка B
    re.compile(r'^\d{1,3}\s*[BВ]$'),                # напряжение
    re.compile(r'^\d{1,3}([.,]\d+)?\s*[AА]$'),      # ток
    re.compile(r'^\d{1,4}\s*м$'),                   # длина
    re.compile(r'ВВГ|КВВГ|МКЭШ|LS|ParLan', re.IGNORECASE),
    re.compile(r'^PE$'),
    re.compile(r'^Формат'),
    re.compile(r'^Инв\.?\s*№\s*подл'),
    re.compile(r'^Взам\.?\s*инв'),
    re.compile(r'^Подп\.?\s*и\s*дата'),
    re.compile(r'^Лист(ов)?$'),
    re.compile(r'^Подп\.?$'),
    re.compile(r'^№\s*док'),
    re.compile(r'^Дата$'),
    re.compile(r'^Изм\.?$'),
    re.compile(r'^Согласовано$'),
    re.compile(r'^Примечание'),
]

PROFILE_B = Profile("B: ОВЕН МВ210/МУ210 / IEC")
PROFILE_B.page_frame_words = B_PAGE_FRAME_WORDS
PROFILE_B.classify_span = staticmethod(_classify_b)
PROFILE_B.parse_cross_ref = staticmethod(_parse_cross_ref_b)
PROFILE_B.device_tag_re = B_DEVICE_TAG_RE
PROFILE_B.device_tag_complete_re = B_DEVICE_TAG_COMPLETE_RE
PROFILE_B.kks_tag_re = B_KKS_TAG_RE
PROFILE_B.connector_tag_re = re.compile(
    r'^-?\d{0,2}(XA|XM|XB|XT|XP|XS)\d{0,3}(\.[A-ZА-Я0-9]{1,4})?$')
PROFILE_B.label_types = ("device_tag", "instrument_tag", "terminal_no", "pin_ref",
                         "power_pin", "wire_gauge", "io_channel", "voltage",
                         "current_rating", "reserve_label", "cable_type",
                         "cable_tag", "cable_length", "location_ref",
                         "wire_color", "contact_type", "signal_label")
PROFILE_B.nl_module_re = B_NL_MODULE_RE
PROFILE_B.nl_channel_re = B_NL_CHANNEL_RE
PROFILE_B.nl_kks_re = B_NL_KKS_RE
PROFILE_B.nl_eq_re = B_NL_EQ_RE
PROFILE_B.nl_junk_patterns = B_NL_JUNK_PATTERNS


# ==================================================================
# ПРОФИЛЬ C: Delta DVP (щиты ША/Э3)
# ==================================================================
# Отличия (замерено на "026.809.01.01-ИПК ША1 Э3"):
#   межлистовая ссылка   (6:5E), (:3D)   (лист:колонка+зона; лист пуст = тот же лист)
#   модуль ПЛК           DVP04AD-S2, DVP16SM11N   (Delta DVP, не ОВЕН/Regul)
#   клеммник             XT-G1, XT-AI1, XT01
#   пины аналог. модуля  V+, V1+..V4+, I1+, S/S
#   силовые клеммы        1/L1, 2/T1 (контактор); контакты реле RA/RB/RC, TA/TB/TC
#   автомат/УЗО          C6A, C6A/30мА
#   кабель               ШНК 4х11 3L
#   русский текст        ЗАГЛАВНЫМИ и в mojibake-кодировке (GOSTTypeB, чинится cp1251)

C_CROSS_REF_RE = re.compile(r'^\(\d{0,3}:\d{1,2}[A-F]\)$')
C_CROSS_REF_PARSE_RE = re.compile(r'^\((\d{0,3}):(\d{1,2})([A-F])\)$')

C_MODULE_PARTNO_RE = re.compile(r'^DVP[0-9A-Z][0-9A-Z\-]*$')
C_IO_CHANNEL_RE = re.compile(r'^(AI|AO|DI|DO)\d{0,2}$')
# Пины аналоговых/спец-модулей Delta: V+, V1+..V4+, I1+, V-, S/S.
C_MODULE_PIN_RE = re.compile(r'^([VI]\d{0,2}[+\-]|S/S)$')
C_POWER_PIN_RE = re.compile(
    r'^(PE|XPE|N|L[123]?|FG|GND|ACM|DCM|COM|0V|0В|24VDC|24G|SG[+\-]|ZP|PH|UP'
    r'|A1|A2|X\d{1,2})$')
# Силовая клемма: 1/L1, 2/T1 (контактор) и U/T1, R/L1 (клеммы ПЧ/двигателя).
C_POWER_TERMINAL_RE = re.compile(r'^[A-Z0-9]/[LT]\d$')
C_WIRE_GAUGE_RE = re.compile(r'^\d+[xх]\d+([xх][\d.,]+)?$')   # 5x16, 4х2х0,5
C_RELAY_CONTACT_RE = re.compile(r'^[RT][ABC]$')           # RA, RB, RC, TA, TB, TC
# Автомат/уставка: C6A, 9A, 0.5А, 100A, C6A/30мА (номинал/утечка УЗО).
C_CURRENT_RE = re.compile(r'^[A-DK]?\d{1,3}([.,]\d+)?\s?[AА](/\d{1,3}\s?м?[АA])?$')
C_CABLE_TYPE_RE = re.compile(r'ШНК|ВВГ|КВВГ|МКЭШ|ПВС|КГ|КИПЭВ', re.IGNORECASE)
C_VOLTAGE_RE = re.compile(r'^[=~+\-]?\d{1,3}([.,]\d+)?\s*[BВvV]$')  # 24В, =24В, 48 B
C_RESERVE_RE = re.compile(r'^резерв', re.IGNORECASE)                # РЕЗЕРВ / Резерв
C_DOC_NUMBER_RE = re.compile(r'^\d{3}\.\d{3}\.\d{2}\.\d{2}-[А-Я]')  # 026.809.01.01-ИПК
# Тег устройства IEC/Delta: XT-G1, XT-AI1, XT01, -K1, QF1. Разделители суффикса --
# и точка, и дефис (в этом шаблоне клеммники подписаны как XT-AI1).
C_DEVICE_TAG_RE = re.compile(
    r'^-?\d{0,2}[A-Z]{1,3}'
    r'(\d{1,3}([.\-][A-ZА-Я0-9]{1,5})*|([.\-][A-ZА-Я0-9]{1,5})+)'
    r'[а-яА-Я]?$')

C_PAGE_FRAME_WORDS = {
    'Формат  А3', 'Формат А3', 'Формат  А4', 'Инв.N подл.', 'Инв. N подл.',
    'Взам. инв. N', 'Подп. и дата', 'Копировал', 'Лист', 'Листов', 'Подп.',
    '№док.', '№ док.', 'Дата', 'Изм.', 'Кол.уч', 'Зам.', 'Разраб.', 'Пров.',
    'Н.контр.', 'Н. контр.', 'Утв.', 'Т.контр.', 'A', 'B', 'C', 'D', 'E', 'F',
}
C_FRAME_RE = re.compile(r'^Формат\b')
# Русская подпись-сигнал: в этом шаблоне ЗАГЛАВНЫМИ (АВАРИЯ НАСОСА, ГЕНЕРАТОР ОЗОНА).
C_SIGNAL_LABEL_RE = re.compile(r'^[А-ЯЁ][А-ЯЁа-яё /.,()«»\-+№\d]{2,39}$')


def _classify_c(t, size, bbox, page_width, page_height):
    if t in C_PAGE_FRAME_WORDS or C_FRAME_RE.match(t):
        return "page_frame"
    if GRID_DIGIT_RE.match(t):
        return _classify_grid_or_terminal(t, bbox, page_height)
    if t in CONNECTOR_GLYPH:
        return "glyph"
    if C_CROSS_REF_RE.match(t):
        return "cross_ref"
    if C_MODULE_PARTNO_RE.match(t):
        return "module_partno"
    if C_MODULE_PIN_RE.match(t):
        return "module_pin"
    if C_IO_CHANNEL_RE.match(t):
        return "io_channel"
    if C_POWER_TERMINAL_RE.match(t):     # 1/L1, 2/T1
        return "power_pin"
    if C_POWER_PIN_RE.match(t):
        return "power_pin"
    if C_RELAY_CONTACT_RE.match(t):
        return "relay_contact"
    if t in B_CONTACT_TYPE:              # NO, NC, COM
        return "contact_type"
    if C_VOLTAGE_RE.match(t):
        return "voltage"
    if C_CURRENT_RE.match(t):
        return "current_rating"
    if C_WIRE_GAUGE_RE.match(t):         # 5x16 (сечение жилы)
        return "wire_gauge"
    if C_CABLE_TYPE_RE.search(t):
        return "cable_type"
    if C_RESERVE_RE.match(t):
        return "reserve_label"
    if C_DOC_NUMBER_RE.match(t):
        return "doc_number"
    if C_DEVICE_TAG_RE.match(t):
        return "device_tag"
    if B_PIN_REF_RE.match(t):
        return "pin_ref"
    if B_PLAIN_NUM_RE.match(t):
        return "terminal_no"
    if re.match(r'^\d{2}\.\d{2}$', t):
        return "date"
    if C_SIGNAL_LABEL_RE.match(t) and sum(c.isalpha() for c in t) >= 3:
        return "signal_label"
    if len(t) > 15 and any(c.isalpha() for c in t) and size > 3:
        return "long_text"
    return "unclassified"


def _parse_cross_ref_c(text):
    m = C_CROSS_REF_PARSE_RE.match(text)
    if not m:
        return None
    # "(6:5E)": лист:колонка+зона-буква. Пустой лист "(:3D)" = ссылка в пределах
    # ТОГО ЖЕ листа -> target_sheet=None (потребители трактуют None как "текущий").
    sheet = int(m.group(1)) if m.group(1) else None
    return {"target_sheet": sheet,
            "target_col": int(m.group(2)),
            "target_zone": m.group(3)}


C_NL_MODULE_RE = re.compile(r'^(DVP[0-9A-Z\-]+)\s*$')
C_NL_CHANNEL_RE = re.compile(r'^(DI|AI|DO|AO)\s*(\d{1,2})\s*$')
C_NL_KKS_RE = re.compile(r'\b(00[A-Z0-9]{9,11})\b')   # не встречается
C_NL_EQ_RE = re.compile(r'\b(-?\d{0,2}[A-ZА-Я]{1,3}\d{1,2}(?:[.\-][A-ZА-Я0-9]{1,4})?)\b')
C_NL_JUNK_PATTERNS = [
    re.compile(r'^\d{1,4}$'),
    re.compile(r'^\(\d*:\d+[A-F]\)$'),             # межлистовая ссылка C
    re.compile(r'^\d{1,3}\s*[BВ]$'),
    re.compile(r'ШНК|ВВГ|КВВГ|МКЭШ', re.IGNORECASE),
    re.compile(r'^PE$|^N$|^FG$'),
    re.compile(r'^Формат'),
    re.compile(r'^Инв\.?\s*N\s*подл'),
    re.compile(r'^Взам\.?\s*инв'),
    re.compile(r'^Подп'),
    re.compile(r'^Лист(ов)?$'),
    re.compile(r'^№\s*док'),
    re.compile(r'^Дата$'),
    re.compile(r'^Изм\.?$'),
    re.compile(r'^Копировал$'),
]

PROFILE_C = Profile("C: Delta DVP (ША/Э3)")
PROFILE_C.page_frame_words = C_PAGE_FRAME_WORDS
PROFILE_C.classify_span = staticmethod(_classify_c)
PROFILE_C.parse_cross_ref = staticmethod(_parse_cross_ref_c)
PROFILE_C.device_tag_re = C_DEVICE_TAG_RE
PROFILE_C.device_tag_complete_re = C_DEVICE_TAG_RE
PROFILE_C.kks_tag_re = re.compile(r'^00[A-Z]{2,4}\d{2}[A-Z]{2}\d{3}$')  # не встречается
PROFILE_C.connector_tag_re = re.compile(r'^-?\d{0,2}XT\d{0,3}([.\-][A-ZА-Я0-9]{1,5})?$')
PROFILE_C.label_types = ("device_tag", "terminal_no", "pin_ref", "power_pin",
                         "module_pin", "io_channel", "voltage", "current_rating",
                         "relay_contact", "contact_type", "reserve_label",
                         "cable_type", "wire_gauge", "signal_label")
PROFILE_C.nl_module_re = C_NL_MODULE_RE
PROFILE_C.nl_channel_re = C_NL_CHANNEL_RE
PROFILE_C.nl_kks_re = C_NL_KKS_RE
PROFILE_C.nl_eq_re = C_NL_EQ_RE
PROFILE_C.nl_junk_patterns = C_NL_JUNK_PATTERNS


# ==================================================================
# ПРОФИЛЬ D: ОВЕН серии 110 (ПЛК110/МВ110/МУ110), щиты ШУ
# ==================================================================
# Отличия (замерено на "026.808.01-ИПК ШУ-ТМ-14082-0002 Э3"):
#   модули            ПЛК110-24.60.Р-М, МВ110-24.32ДН, МУ110-224.16Р
#                     (ОВЕН 110-й серии: точки + КИРИЛЛИЧЕСКИЙ суффикс -- этим и
#                      отличаются от МВ210-212 профиля B, где суффикса нет)
#   тег устройства    2K1, 2XT-G1, 2X-AC, 3X-RS, 1KL01, KM50, QFD35, TA1
#                     (префикс локации цифрой, суффикс через ДЕФИС)
#   маркировка цепи   1A1, 2B3, 13N1, 50C3, A411, N421  (линия+фаза+сегмент)
#   канал             2DI1, 3DO1, 2AI1, DI36, AI1-1, AI-R
#   автомат           C16А, C32А, 1P+N-C16, 30мА
#   напряжение        220 VAC, ~220В/+24В
#   МЕЖЛИСТОВЫХ ССЫЛОК НЕТ -- в этом шаблоне их не используют (проверено по всем
#   известным форматам). parse_cross_ref всегда None: это не потеря данных, а факт.

# Модуль ОВЕН 110-й серии. Отличать от МВ210-212 (профиль B) НАДО строго: это
# сигнатура выбора профиля, и оба вендора -- ОВЕН. Различие в том, что у 110-й
# серии за номером идёт ЛИБО внутренняя точка ("МВ110-24.32ДН", "ПЛК110-24.60.Р-М"),
# ЛИБО кириллический суффикс; у МВ210-212 нет ни того, ни другого.
# Важно: хвост здесь обязателен (+), а не '*' -- с '*' regex ловил и МВ210-212,
# то есть не различал серии вовсе.
D_MODULE_PARTNO_RE = re.compile(
    r'^(?:ПЛК|МВ|МУ|МК|МДВВ)\d{2,3}-[\d.]*\.\d+[А-ЯЁ\w.\-]*$'   # внутренняя точка
    r'|^(?:ПЛК|МВ|МУ|МК|МДВВ)\d{2,3}[-.][\d.]*[А-ЯЁ]+[\w.\-]*$'  # кириллический суффикс
    r'|^СЭТ-')
D_IO_CHANNEL_RE = re.compile(r'^\d{0,2}(AI|AO|DI|DO)\d{0,2}(-[\dR])?$')
# Маркировка цепи: 1A1 / 13N1 / 50C3 (линия+фаза+сегмент) и A411 / N421.
# Голые A1/A2/N1/L1 -- это НЕ маркировка, а клеммы катушки/фазы: они ловятся
# раньше в power_pin, поэтому здесь требуется либо префикс-цифра, либо 3 цифры.
D_WIRE_MARKING_RE = re.compile(r'^\d{1,2}[ABCN]\d{1,3}$|^[ABCN]\d{3}$')
D_POWER_PIN_RE = re.compile(
    r'^(PE|XPE|XN\d?|N\d?|L[123]?|FG|GND|GWG|0V|0В|\+?24V?|\+V|24V|A1|A2|B1|B2|C1|C2'
    r'|PWR[+\-]|COM\d{0,2}|S/S|X\d{1,2})$')
# 220 VAC, 24V, ~220В, а также пара «первичное/вторичное»: ~220В/+24В, ~220В/0B.
D_VOLTAGE_RE = re.compile(
    r'^[=~+\-]?\d{1,3}([.,]\d+)?\s?(V(AC|DC)?|[BВ])'
    r'(/[+\-]?\d{1,3}\s?[BВV])?$')
D_CURRENT_RE = re.compile(r'^[A-DСC]?\d{1,3}([.,]\d+)?\s?м?[AА]$')          # C16А, 38A, 30мА, 2A
D_BREAKER_SPEC_RE = re.compile(r'^\d[PР](\+[NН])?-[A-DСC]\d{1,3}$')          # 1P+N-C16
D_CT_RATIO_RE = re.compile(r'^\d{2,4}/\d{1,2}$')                             # 200/5 (транcформатор тока)
D_CABLE_TYPE_RE = re.compile(r'ВБШв|ВВГ|КВВГ|МКЭШ|ПВС|КГ|ШВВП|ПуГВ|КИПЭВ', re.IGNORECASE)
# Пин + имя сигнала: "4 (0V)", "5 (+24V)", "2(RxD)", "1(I+)".
D_PIN_SIGNAL_RE = re.compile(r'^\d{1,2}\s?\([^)]{1,6}\)$')
D_INTERFACE_RE = re.compile(r'^(RS-?\d{3}[-\w]*|RJ\d{2}|DB9[-\w]*|485[AB](/\w+)?|DEBUG|ETH\d?)$')
D_RESERVE_RE = re.compile(r'^резерв', re.IGNORECASE)
D_DOC_NUMBER_RE = re.compile(r'^\d{3}\.\d{3}\.\d{2}(\.\d{2})?-[А-Я]')
# Тег устройства: 2K1, KM50, QFD35, XT01, 2XT-G1, 2X-AC, SF-EL1, X1-2, PLC1.
D_DEVICE_TAG_RE = re.compile(
    r'^-?\d{0,2}[A-Z]{1,3}'
    r'(\d{1,3}([-.][A-ZА-Я0-9]{1,4})*|([-.][A-ZА-Я0-9]{1,4})+)$')

D_PAGE_FRAME_WORDS = {
    'Формат А3', 'Формат  А3', 'Формат А4', 'Инв. N подл.', 'Инв.N подл.',
    'Взам. инв. N', 'Подпись и дата', 'Подп. и дата', 'Согласовано', 'Изм.',
    'Кол.уч', 'Кол. уч.', 'Подп.', 'Дата', '№док.', '№ док.', 'Лист', 'Листов',
    'Зам.', 'Разраб.', 'Пров.', 'Н.контр.', 'Утв.', 'Копировал',
    'Конт.', '№пров.', 'Поз.', 'Наименование', 'Кол.', 'Примечание',
    'A', 'B', 'C', 'D', 'E', 'F',
}
D_FRAME_RE = re.compile(r'^Формат\b')
# Подпись-сигнал: кириллица в начале, дальше допускается латиница/цифры
# ("Линия QFD1", "Ввод №1", "Включить / выключить").
D_SIGNAL_LABEL_RE = re.compile(r'^[А-Яа-яЁё][А-Яа-яЁёA-Za-z0-9 /.,()«»№\-+]{2,39}$')


def _classify_d(t, size, bbox, page_width, page_height):
    if t in D_PAGE_FRAME_WORDS or D_FRAME_RE.match(t):
        return "page_frame"
    if GRID_DIGIT_RE.match(t):
        return _classify_grid_or_terminal(t, bbox, page_height)
    if t in CONNECTOR_GLYPH:
        return "glyph"
    if D_MODULE_PARTNO_RE.match(t):
        return "module_partno"
    if D_IO_CHANNEL_RE.match(t):
        return "io_channel"
    if D_POWER_PIN_RE.match(t):
        return "power_pin"
    if D_WIRE_MARKING_RE.match(t):
        return "wire_marking"
    if D_VOLTAGE_RE.match(t):
        return "voltage"
    if D_BREAKER_SPEC_RE.match(t):
        return "breaker_spec"
    if D_CURRENT_RE.match(t):
        return "current_rating"
    if D_CT_RATIO_RE.match(t):
        return "ct_ratio"
    if D_PIN_SIGNAL_RE.match(t):
        return "pin_signal"
    if D_INTERFACE_RE.match(t):
        return "interface"
    if D_CABLE_TYPE_RE.search(t):
        return "cable_type"
    if D_RESERVE_RE.match(t):
        return "reserve_label"
    if D_DOC_NUMBER_RE.match(t):
        return "doc_number"
    if D_DEVICE_TAG_RE.match(t):
        return "device_tag"
    if B_PIN_REF_RE.match(t):
        return "pin_ref"
    if B_PLAIN_NUM_RE.match(t):
        return "terminal_no"
    if re.match(r'^\d{2}\.\d{2}$', t):
        return "date"
    if D_SIGNAL_LABEL_RE.match(t) and sum(c.isalpha() for c in t) >= 3:
        return "signal_label"
    if len(t) > 15 and any(c.isalpha() for c in t) and size > 3:
        return "long_text"
    return "unclassified"


def _parse_cross_ref_d(text):
    # В этом шаблоне межлистовых ссылок нет (проверено на реальном файле по всем
    # известным форматам). Возвращаем None всегда -- честнее, чем притягивать
    # чужой формат и плодить выдуманные связи.
    return None


D_NL_MODULE_RE = re.compile(r'^((?:ПЛК|МВ|МУ|МК)\d{2,3}[-.][\d.]+[-.А-ЯЁA-Z\d]*)\s*$')
D_NL_CHANNEL_RE = re.compile(r'^\d{0,2}(DI|AI|DO|AO)\s*(\d{1,2})\s*$')
D_NL_KKS_RE = re.compile(r'\b(00[A-Z0-9]{9,11})\b')   # не встречается
D_NL_EQ_RE = re.compile(r'\b(-?\d{0,2}[A-ZА-Я]{1,3}\d{1,2}(?:[-.][A-ZА-Я0-9]{1,4})?)\b')
D_NL_JUNK_PATTERNS = [
    re.compile(r'^\d{1,4}$'),
    re.compile(r'^\d{1,3}\s?V(AC|DC)?$'),
    re.compile(r'^[A-DСC]?\d{1,3}\s?м?[АA]$'),
    re.compile(r'ВБШв|ВВГ|КВВГ|МКЭШ', re.IGNORECASE),
    re.compile(r'^(PE|N|L[123]?|FG|GND|XPE|XN\d?)$'),
    re.compile(r'^Формат'),
    re.compile(r'^Инв\.?\s*N\s*подл'),
    re.compile(r'^Взам\.?\s*инв'),
    re.compile(r'^Подп'),
    re.compile(r'^Лист(ов)?$'),
    re.compile(r'^№\s*(док|пров)'),
    re.compile(r'^Дата$'),
    re.compile(r'^Изм\.?$'),
    re.compile(r'^Кол\.?\s*уч'),
    re.compile(r'^Согласовано$'),
    re.compile(r'^Конт\.$'),
]

PROFILE_D = Profile("D: ОВЕН 110 (ПЛК110/МВ110/МУ110, ШУ)")
PROFILE_D.page_frame_words = D_PAGE_FRAME_WORDS
PROFILE_D.classify_span = staticmethod(_classify_d)
PROFILE_D.parse_cross_ref = staticmethod(_parse_cross_ref_d)
PROFILE_D.device_tag_re = D_DEVICE_TAG_RE
PROFILE_D.device_tag_complete_re = D_DEVICE_TAG_RE
PROFILE_D.kks_tag_re = re.compile(r'^00[A-Z]{2,4}\d{2}[A-Z]{2}\d{3}$')  # не встречается
PROFILE_D.connector_tag_re = re.compile(r'^-?\d{0,2}XT?\d{0,3}([-.][A-ZА-Я0-9]{1,4})?$')
PROFILE_D.label_types = ("device_tag", "terminal_no", "pin_ref", "power_pin",
                         "io_channel", "wire_marking", "voltage", "current_rating",
                         "breaker_spec", "ct_ratio", "pin_signal", "interface",
                         "reserve_label", "cable_type", "signal_label")
PROFILE_D.nl_module_re = D_NL_MODULE_RE
PROFILE_D.nl_channel_re = D_NL_CHANNEL_RE
PROFILE_D.nl_kks_re = D_NL_KKS_RE
PROFILE_D.nl_eq_re = D_NL_EQ_RE
PROFILE_D.nl_junk_patterns = D_NL_JUNK_PATTERNS


ALL_PROFILES = [PROFILE_A, PROFILE_B, PROFILE_C, PROFILE_D]
DEFAULT_PROFILE = PROFILE_A


# ==================================================================
# Авто-детект профиля по документу
# ==================================================================

def detect_profile(raw_pages, font_fix_map=None, verbose=True):
    """Определить шаблон оформления по сырым span'ам.

    Сигнатуры считаются по всему набору текста: у какого профиля больше
    "попаданий" его характерными паттернами (межлистовая ссылка, модуль,
    сечение, теги приборов), тот и выбирается. Порог мягкий -- при полном
    нуле сигнатур обоих берём A (обратная совместимость).
    """
    a_hits = 0
    b_hits = 0
    c_hits = 0
    d_hits = 0
    for page in raw_pages:
        for s in page["text_spans"]:
            t = s["text"].strip()
            # сигнатуры A
            if A_CROSS_REF_RE.match(t):
                a_hits += 3
            if A_KKS_TAG_RE.match(t):
                a_hits += 3
            if A_MODULE_PARTNO_RE.match(t):
                a_hits += 3
            if A_WIRE_GAUGE_RE.match(t):
                a_hits += 1
            # сигнатуры B
            if B_CROSS_REF_RE.match(t):
                b_hits += 3
            if B_MODULE_PARTNO_RE.match(t):
                b_hits += 3
            if B_WIRE_GAUGE_RE.match(t):
                b_hits += 1
            if B_CABLE_TYPE_RE.search(t):
                b_hits += 1
            # сигнатуры C (Delta DVP)
            if C_CROSS_REF_RE.match(t):
                c_hits += 3
            if C_MODULE_PARTNO_RE.match(t):
                c_hits += 3
            # сигнатуры D (ОВЕН 110)
            if D_MODULE_PARTNO_RE.match(t):
                d_hits += 3
            if D_BREAKER_SPEC_RE.match(t):
                d_hits += 2

    import sys
    scores = [(a_hits, PROFILE_A), (b_hits, PROFILE_B),
              (c_hits, PROFILE_C), (d_hits, PROFILE_D)]
    best_score, profile = max(scores, key=lambda x: x[0])
    if best_score == 0:
        # Ни один профиль не узнал документ. Молча свалиться в A -- ровно тот
        # случай, когда на выходе получается мусор (почти всё в "unclassified",
        # ноль межлистовых ссылок), причём НЕЗАМЕТНО. Кричим об этом громко.
        profile = PROFILE_A
        print("  [profile] ВНИМАНИЕ: ни один профиль не опознал этот документ "
              "(все сигнатуры = 0). Взят профиль A по умолчанию, но извлечение "
              "почти наверняка будет неполным: скорее всего это ЕЩЁ ОДИН шаблон "
              "оформления, под который нужен новый Profile в profiles.py.",
              file=sys.stderr)
    if verbose:
        print(f"  [profile] сигнатуры A={a_hits} B={b_hits} C={c_hits} D={d_hits} "
              f"-> выбран профиль «{profile.name}»", file=sys.stderr)
    return profile
