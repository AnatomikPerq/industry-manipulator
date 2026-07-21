"""
Потоковый разбор multipart/form-data.

Написан взамен cgi.FieldStorage: модуль cgi УДАЛЁН в Python 3.13, и сервер
перестал бы запускаться на первом же обновлении Python. Вторая причина не
менее весомая: FieldStorage держал загрузку в памяти целиком, а сюда грузят
альбомы на сотни мегабайт.

Разбор своего формата - место, где ошибка стоит дорого и не видна: файл
приезжает молча битым (лишний CRLF в конце, обрезанный хвост), а замечают это
уже парсеры PDF, невнятной ошибкой fitz.
"""

import io

import pytest

import multipart

BOUNDARY = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
CTYPE = f"multipart/form-data; boundary={BOUNDARY}"


def body(*parts) -> bytes:
    """Тело запроса из (имя поля, имя файла, содержимое)."""
    out = b""
    for name, filename, content in parts:
        out += f"--{BOUNDARY}\r\n".encode()
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += f"Content-Disposition: {disposition}\r\n".encode("utf-8")
        if filename is not None:
            out += b"Content-Type: application/pdf\r\n"
        out += b"\r\n" + content + b"\r\n"
    return out + f"--{BOUNDARY}--\r\n".encode()


class Sink(io.BytesIO):
    """Приёмник, который не закрывается по close() - чтобы тест мог прочитать
    записанное после разбора."""

    def close(self):
        pass


def parse(raw, collector=None, **kw):
    files = collector if collector is not None else {}

    def open_part(field, filename):
        files[filename] = Sink()
        return files[filename]

    result = multipart.parse(io.BytesIO(raw), CTYPE, len(raw), open_part, **kw)
    return result, {k: v.getvalue() for k, v in files.items()}


def test_single_file_bytes_are_exact():
    """Содержимое обязано доехать байт в байт: PDF - двоичный файл, лишний
    перевод строки делает его нечитаемым."""
    content = b"%PDF-1.7\r\n\x00\x01binary\xff\xfe\r\n"
    result, files = parse(body(("files", "схема.pdf", content)))
    assert files["схема.pdf"] == content
    assert result[0]["size"] == len(content)


def test_several_files():
    raw = body(("files", "а.pdf", b"AAA"),
               ("files", "б.pdf", b"BBB"),
               ("files", "в.xlsx", b"CCC"))
    _, files = parse(raw)
    assert files == {"а.pdf": b"AAA", "б.pdf": b"BBB", "в.xlsx": b"CCC"}


def test_cyrillic_filename_decoded():
    """Имена документов почти всегда кириллические. http.server отдаёт
    заголовки декодированными как latin-1, и без обратного перекодирования
    «Схема.pdf» превращается в кракозябры."""
    _, files = parse(body(("files", "026.822.13-ИПК ЩСКЗ СБ.pdf", b"x")))
    assert "026.822.13-ИПК ЩСКЗ СБ.pdf" in files


def test_plain_field_is_skipped():
    """Часть без filename - обычное поле формы. Интерфейс их не шлёт, и
    держать их в памяти незачем."""
    result, files = parse(body(("mode", None, b"full"),
                               ("files", "а.pdf", b"AAA")))
    assert list(files) == ["а.pdf"]
    assert [r["filename"] for r in result] == ["а.pdf"]


def test_empty_file_part():
    _, files = parse(body(("files", "пусто.pdf", b"")))
    assert files["пусто.pdf"] == b""


@pytest.mark.parametrize("size", [
    multipart.CHUNK - 1, multipart.CHUNK, multipart.CHUNK + 1,
    multipart.CHUNK * 2 + 17,
])
def test_boundary_split_across_chunks(size):
    """САМОЕ ХРУПКОЕ МЕСТО потокового разбора: разделитель, пришедший на стыке
    двух кусков чтения. Не придержи мы хвост длиной с разделитель - часть либо
    оборвалась бы, либо утащила бы в файл всё остальное тело."""
    content = bytes(range(256)) * (size // 256 + 1)
    content = content[:size]
    _, files = parse(body(("files", "большой.pdf", content)))
    assert files["большой.pdf"] == content


def test_content_that_looks_like_boundary():
    """Внутри PDF может встретиться последовательность, похожая на разделитель.
    Настоящий разделитель всегда предваряется CRLF и стоит с начала строки."""
    tricky = b"--" + BOUNDARY.encode() + b" but not really\r\nmore data"
    _, files = parse(body(("files", "хитрый.pdf", tricky)))
    assert files["хитрый.pdf"] == tricky


def test_truncated_upload_raises():
    """Клиент оборвал загрузку - часть недокачана, и выдавать её за файл
    нельзя: PDF просто не откроется, а сессия будет выглядеть исправной."""
    raw = body(("files", "а.pdf", b"AAA"))[:-30]
    with pytest.raises(multipart.MultipartError):
        parse(raw)


def test_too_large_rejected_before_reading():
    """Предел проверяется по Content-Length, ДО чтения тела: смысл лимита в
    том, чтобы не принять гигабайты, а не в том, чтобы их принять и измерить."""
    raw = body(("files", "а.pdf", b"AAA"))
    with pytest.raises(multipart.MultipartError) as e:
        parse(raw, max_bytes=10)
    assert "размер" in str(e.value)


def test_missing_boundary_rejected():
    with pytest.raises(multipart.MultipartError):
        multipart.boundary_of("multipart/form-data")
    with pytest.raises(multipart.MultipartError):
        multipart.boundary_of("application/json")


def test_quoted_boundary():
    assert multipart.boundary_of('multipart/form-data; boundary="abc123"') == b"abc123"


def test_skipped_part_body_is_still_consumed():
    """Пропущенную часть надо ДОЧИТАТЬ: недочитанное тело браузер видит как
    обрыв соединения, а не как ответ с объяснением, почему файл не принят."""
    raw = body(("files", "плохой.exe", b"X" * 1000),
               ("files", "хороший.pdf", b"YYY"))
    saved = {}

    def open_part(field, filename):
        if filename.endswith(".exe"):
            return None            # такой файл не принимаем
        saved[filename] = Sink()
        return saved[filename]

    result = multipart.parse(io.BytesIO(raw), CTYPE, len(raw), open_part)
    assert [r["filename"] for r in result] == ["плохой.exe", "хороший.pdf"]
    assert [r["saved"] for r in result] == [False, True]
    assert saved["хороший.pdf"].getvalue() == b"YYY"
