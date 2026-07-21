"""
Разбор multipart/form-data ПОТОКОМ, прямо в файл.

ЗАЧЕМ СВОЙ, А НЕ cgi.FieldStorage. Две причины, обе жёсткие:

1. Модуля cgi больше нет. Он объявлен устаревшим в Python 3.11 и УДАЛЁН в
   3.13 (PEP 594). Сервер перестал бы запускаться на первом же обновлении
   Python - без единого предупреждения заранее.

2. cgi.FieldStorage складывает загрузку в память. Здесь грузят альбомы на
   184-309 листов, то есть сотни мегабайт, и каждая такая загрузка целиком
   оседала в оперативной памяти сервера - вместе с копией, которую делал
   item.file.read() перед записью на диск.

Здесь тело запроса читается кусками по 64 КБ и пишется сразу в файл; в памяти
никогда не лежит больше буфера и хвоста длиной с разделитель.

ЧТО НАРОЧНО НЕ ПОДДЕРЖАНО: вложенный multipart/mixed (браузеры его не
отправляют с 2000-х), Content-Transfer-Encoding (для FormData всегда binary) и
разбор полей в память (интерфейсу они не нужны - он шлёт одни файлы).
"""

import re
from email.parser import Parser
from urllib.parse import unquote

# Кусок чтения. 64 КБ - компромисс: меньше делает системных вызовов много,
# больше не ускоряет (упираемся в диск).
CHUNK = 64 * 1024

# Больше этого одна загрузка быть не может. Альбом на 309 листов - около
# 250 МБ; гигабайт с запасом покрывает всё, что бюро реально присылает, и при
# этом не даёт забить диск одним запросом.
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024

_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]+)"|([^;]+))', re.I)


class MultipartError(Exception):
    """Тело запроса не разобрать. Это ошибка клиента (400), а не сбой сервера."""


def boundary_of(content_type: str) -> bytes:
    if "multipart/form-data" not in (content_type or "").lower():
        raise MultipartError("Ожидается multipart/form-data")
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise MultipartError("В заголовке Content-Type нет boundary")
    return (m.group(1) or m.group(2)).strip().encode("latin-1")


def _decode_filename(disposition: str):
    """Имя файла из Content-Disposition.

    Браузер шлёт кириллические имена в filename="..." уже в UTF-8 (RFC 7578
    прямо это разрешает), а старые клиенты - в filename*= по RFC 5987. Читаем
    оба: имена документов здесь почти всегда кириллические, и промах означал
    бы файл с именем в виде кракозябр.
    """
    m = re.search(r"filename\*\s*=\s*([^;]+)", disposition, re.I)
    if m:
        value = m.group(1).strip().strip('"')
        parts = value.split("'", 2)
        raw = parts[2] if len(parts) == 3 else value
        charset = parts[0] or "utf-8" if len(parts) == 3 else "utf-8"
        try:
            return unquote(raw, encoding=charset, errors="replace")
        except LookupError:
            return unquote(raw, errors="replace")

    m = re.search(r'filename\s*=\s*"([^"]*)"', disposition, re.I)
    if m is None:
        m = re.search(r"filename\s*=\s*([^;\s]+)", disposition, re.I)
    if m is None:
        return None
    name = m.group(1)
    # http.server отдаёт заголовки декодированными как latin-1: возвращаем
    # байты и читаем их как UTF-8, иначе «Схема.pdf» станет «Ð¡Ñ…ÐµÐ¼Ð°.pdf»
    try:
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def _field_name(disposition: str):
    m = re.search(r'\bname\s*=\s*"([^"]*)"', disposition, re.I)
    return m.group(1) if m else None


class _Reader:
    """Чтение тела запроса с буфером и жёстким лимитом по Content-Length."""

    def __init__(self, rfile, remaining):
        self._rfile = rfile
        self._remaining = remaining
        self.buf = b""

    def fill(self) -> bool:
        """Дочитать очередной кусок. False - тело кончилось."""
        if self._remaining <= 0:
            return False
        chunk = self._rfile.read(min(CHUNK, self._remaining))
        if not chunk:
            self._remaining = 0
            return False
        self._remaining -= len(chunk)
        self.buf += chunk
        return True

    def read_until(self, needle: bytes):
        """Дочитывать, пока в буфере не появится needle. Возвращает индекс
        или None, если тело кончилось раньше."""
        while True:
            idx = self.buf.find(needle)
            if idx != -1:
                return idx
            if not self.fill():
                return None


def parse(rfile, content_type, content_length, open_part,
          max_bytes=MAX_UPLOAD_BYTES) -> list:
    """Разобрать тело запроса, отдавая файлы в open_part.

    open_part(field_name, filename) -> объект с write()/close(), либо None,
    чтобы эту часть пропустить (её байты будут прочитаны и выброшены - тело
    запроса обязано быть прочитано целиком, иначе браузер получит обрыв
    соединения вместо ответа с объяснением).

    Возвращает список {"name", "filename", "size", "saved"} по каждой части
    с файлом. Части без filename (обычные поля формы) пропускаются: интерфейс
    их не шлёт, а держать их в памяти незачем.
    """
    content_length = int(content_length or 0)
    if content_length <= 0:
        raise MultipartError("Пустое тело запроса")
    if content_length > max_bytes:
        raise MultipartError(
            f"Загрузка больше допустимого размера "
            f"({content_length / 1048576:.0f} МБ при пределе "
            f"{max_bytes / 1048576:.0f} МБ)")

    delimiter = b"--" + boundary_of(content_type)
    reader = _Reader(rfile, content_length)
    results = []

    # 1) до первого разделителя всё - преамбула, её выбрасываем
    idx = reader.read_until(delimiter)
    if idx is None:
        raise MultipartError("В теле запроса не найден разделитель частей")
    reader.buf = reader.buf[idx + len(delimiter):]

    while True:
        # после разделителя идёт либо CRLF (дальше часть), либо "--" (конец)
        while len(reader.buf) < 2 and reader.fill():
            pass
        if reader.buf[:2] == b"--":
            break
        if reader.buf[:2] != b"\r\n":
            raise MultipartError("Испорченный разделитель частей")
        reader.buf = reader.buf[2:]

        # 2) заголовки части
        idx = reader.read_until(b"\r\n\r\n")
        if idx is None:
            raise MultipartError("Часть запроса без заголовков")
        raw_headers = reader.buf[:idx].decode("latin-1", "replace")
        reader.buf = reader.buf[idx + 4:]
        headers = Parser().parsestr(raw_headers + "\n\n")
        disposition = headers.get("Content-Disposition", "")

        filename = _decode_filename(disposition)
        name = _field_name(disposition)
        sink = open_part(name, filename) if filename else None

        # 3) тело части - до CRLF + разделитель
        size = _pump_part(reader, delimiter, sink)

        if filename:
            results.append({"name": name, "filename": filename,
                            "size": size, "saved": sink is not None})

    return results


def _pump_part(reader: _Reader, delimiter: bytes, sink) -> int:
    """Переливает тело одной части в sink до следующего разделителя.

    В буфере всегда придерживается хвост длиной с разделитель: иначе
    разделитель, пришедший на стыке двух кусков чтения, не нашёлся бы - и
    часть либо оборвалась бы, либо утащила бы в файл всё остальное тело.
    """
    end = b"\r\n" + delimiter
    keep = len(end)
    size = 0
    while True:
        idx = reader.buf.find(end)
        if idx != -1:
            if sink is not None and idx:
                sink.write(reader.buf[:idx])
            size += idx
            reader.buf = reader.buf[idx + len(end):]
            return size

        if len(reader.buf) > keep:
            head, reader.buf = reader.buf[:-keep], reader.buf[-keep:]
            if sink is not None:
                sink.write(head)
            size += len(head)

        if not reader.fill():
            # тело кончилось, а закрывающего разделителя нет: клиент оборвал
            # загрузку. Часть недокачана, и выдавать её за файл нельзя.
            raise MultipartError("Загрузка оборвана: часть не завершена")
