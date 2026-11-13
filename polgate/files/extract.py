"""Text extraction from uploaded documents.

Everything is converted to plain text before anonymization, because the guards and the judge work on
text. Formatting is not preserved: the goal is to let a model reason about the content, not to
render the document.

Only the first `MAX_CHARS` characters are kept, so every extractor reads rows, paragraphs and pages
lazily and stops once it has that much. An ordinary export of a few hundred thousand rows otherwise
kept a worker busy for many seconds to produce text that was then thrown away.

A DOCX or XLSX file is a ZIP archive of XML, and XML that repeats itself compresses a few hundred
times: a 3 MB upload can hold a gigabyte. python-docx parses the whole document, and openpyxl the
whole table of shared strings, before the first paragraph or row can be read, at about eight and
three times the size of that XML in memory. So the sizes the archive declares are checked first,
and a file that would unpack past `MAX_PARSED_XML_BYTES` or `MAX_UNPACKED_BYTES` is refused unread.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import logging
import zipfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from polgate.files import ocr

logger = logging.getLogger(__name__)
# pypdf warns about damaged files in its own words, and some of those quote the file: given a text
# file named .pdf it logs the first bytes it found in place of a header. Its errors still show.
logging.getLogger("pypdf").setLevel(logging.ERROR)

#: A PDF starts with this marker, somewhere in its first kilobyte.
PDF_MARKER = b"%PDF-"

#: Hard limits, mirrored in the upload form.
MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 200_000
TRUNCATION_MARKER = "\n\n[... dokument skrócony / document truncated ...]"

#: Reasons an `UnsupportedFileError` carries, so the hub can say which one in the user's language.
OCR_UNAVAILABLE = "ocr_unavailable"
UNREADABLE = "unreadable"
UNPACKS_TOO_LARGE = "unpacks_too_large"
IMAGE_TOO_LARGE = "image_too_large"

#: The XML in an Office file that its parser holds in memory all at once: the whole document of a
#: DOCX, everything but the worksheets of an XLSX. Generous for any real document.
MAX_PARSED_XML_BYTES = 64 * 1024 * 1024
#: Everything an Office file unpacks to, worksheets included. openpyxl streams those, so this bounds
#: time rather than memory: a 10 MB export of 400,000 rows unpacks to about 105 MB.
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
#: Cells of a workbook walked through, empty ones included, before the rest is left out. A row
#: that names one cell in column A and one in XFD is still padded in between; this bounds the time
#: such a file takes, since its size does not. A real export of 200,000 characters has far fewer.
MAX_XLSX_CELLS = 2_000_000


class UnsupportedFileError(ValueError):
    """Raised for a file the hub cannot turn into text."""


def extract_text(path: Path, mime: str, filename: str) -> str:
    """Extract plain text from an uploaded file.

    Args:
        path: location of the stored upload.
        mime: content type reported by the browser, used only as a hint.
        filename: original file name, whose suffix decides the extractor.

    Returns:
        The document as text, truncated to `MAX_CHARS`.

    Raises:
        UnsupportedFileError: when no extractor matches the file, when its extractor cannot parse
            it (the message is then `UNREADABLE`), or when it needs OCR that is not installed
            (`OCR_UNAVAILABLE`).
    """
    suffix = Path(filename).suffix.lower()
    extractor = _EXTRACTORS.get(suffix)
    if extractor is None:
        raise UnsupportedFileError(f"unsupported file type: {suffix or mime}")
    try:
        text = extractor(path)
    except UnsupportedFileError:
        raise
    except ocr.ImageTooLargeError:
        raise UnsupportedFileError(IMAGE_TOO_LARGE) from None
    except Exception as exc:
        # A damaged or password-protected file fails deep inside a parser, with whatever exception
        # that parser uses. Its message can quote the document, so only the type is logged.
        logger.warning("could not read %s: %s", path.name, type(exc).__name__)
        raise UnsupportedFileError(UNREADABLE) from None
    return _truncate(text)


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + TRUNCATION_MARKER


def _join_capped(parts: Iterable[str], separator: str = "\n") -> str:
    """Join `parts`, stopping as soon as the result is longer than `MAX_CHARS`.

    The text past that point is cut by `_truncate` anyway; what matters is that the parts beyond it
    are never produced. Stopping only once the limit is exceeded, not reached, keeps the result long
    enough for `_truncate` to add its marker whenever something was left out.
    """
    kept: list[str] = []
    size = 0
    for part in parts:
        size += len(part) + (len(separator) if kept else 0)
        kept.append(part)
        if size > MAX_CHARS:
            break
    return separator.join(kept)


def _decode(raw: bytes) -> str:
    """Decode bytes as Unicode by their byte-order mark, then UTF-8, then cp1250.

    UTF-16 is what PowerShell 5.1, Notepad's "Unicode" and Excel's "Unicode Text" write. Read as
    cp1250 it "succeeds" with a NUL between every letter, and the guards, which read words and
    digit runs, then miss the PESEL and the e-mail address in it while the provider reads it fine.
    So a byte-order mark decides first, UTF-16 without one is told by its NUL bytes, and text that
    still holds a NUL after decoding is refused: it is binary, or an encoding the hub did not
    recognise, and masking cannot be trusted on it.

    Anything else that is neither UTF-8 nor cp1250 is decoded with replacement characters rather
    than refused: a document with a few unreadable bytes is still worth masking and sending.

    Raises:
        UnsupportedFileError: with ``UNREADABLE`` when the decoded text holds a NUL character.
    """
    text = _decode_unchecked(raw)
    if "\x00" in text:
        raise UnsupportedFileError(UNREADABLE)
    return text


def _decode_unchecked(raw: bytes) -> str:
    """Decode by byte-order mark, then as UTF-16 when NUL bytes say so, then UTF-8 or cp1250."""
    for mark, encoding in _BYTE_ORDER_MARKS:
        if raw.startswith(mark):
            return raw[len(mark) :].decode(encoding, errors="replace")
    utf16 = _utf16_without_mark(raw)
    if utf16:
        return raw.decode(utf16, errors="replace")
    for encoding in ("utf-8", "cp1250"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


#: Longest first: the UTF-32 little-endian mark begins with the UTF-16 one.
_BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _utf16_without_mark(raw: bytes) -> str | None:
    """Return the UTF-16 byte order of text saved without a mark, or None when it is not UTF-16.

    Text in a Latin script saved as UTF-16 has a NUL in every other byte: the odd ones for
    little-endian, the even ones for big-endian. Text in any 8-bit encoding has almost none.
    """
    sample = raw[:4096]
    pairs = len(sample) // 2
    if pairs < 2:
        return None
    if sample[1::2].count(0) > pairs * 0.3:
        return "utf-16-le"
    if sample[0::2].count(0) > pairs * 0.3:
        return "utf-16-be"
    return None


def _from_plain(path: Path) -> str:
    return _decode(path.read_bytes())


def _from_pdf(path: Path) -> str:
    from pypdf import PdfReader

    with path.open("rb") as handle:
        if PDF_MARKER not in handle.read(1024):
            raise UnsupportedFileError(UNREADABLE)
    reader = PdfReader(str(path))
    text = _join_capped(_numbered(page.extract_text() or "" for page in reader.pages), "\n\n")
    if text.strip():
        return text
    logger.info("PDF %s has no text layer, trying OCR", path.name)
    _require_ocr()
    return _join_capped(_numbered(ocr.ocr_pdf(path)), "\n\n")


def _numbered(pages: Iterable[str]) -> Iterator[str]:
    """Head every page that has text with its number, so the model can cite it."""
    for number, page in enumerate(pages, 1):
        if page.strip():
            yield f"--- strona {number} ---\n{page}"


def _refuse_if_it_unpacks_too_far(path: Path, *, streamed: Callable[[str], bool]) -> None:
    """Refuse an Office file whose archive declares more XML than the hub will expand.

    The check reads only the archive's directory. A member cannot unpack past the size declared
    there, since Python's ZIP reader stops at that size and fails its checksum, so the declared
    sizes are a bound and not a claim.

    Args:
        path: the stored upload.
        streamed: whether the parser reads a member piece by piece rather than whole.
    """
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
    total = sum(member.file_size for member in members)
    parsed_whole = sum(member.file_size for member in members if not streamed(member.filename))
    if total > MAX_UNPACKED_BYTES or parsed_whole > MAX_PARSED_XML_BYTES:
        raise UnsupportedFileError(UNPACKS_TOO_LARGE)


def _from_docx(path: Path) -> str:
    from docx import Document

    _refuse_if_it_unpacks_too_far(path, streamed=lambda name: name.startswith("word/media/"))
    document = Document(str(path))

    def parts() -> Iterator[str]:
        for paragraph in document.paragraphs:
            if paragraph.text.strip():
                yield paragraph.text
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                if any(cells):
                    yield "\t".join(cells)

    return _join_capped(parts())


def _from_xlsx(path: Path) -> str:
    from openpyxl import load_workbook

    _refuse_if_it_unpacks_too_far(
        path, streamed=lambda name: name.startswith(("xl/worksheets/", "xl/media/"))
    )
    workbook = load_workbook(str(path), read_only=True, data_only=True)

    def parts() -> Iterator[str]:
        visited = 0
        for sheet in workbook.worksheets:
            yield f"## Arkusz: {sheet.title}"
            # The declared dimension pads every row to the widest column the sheet claims: a 5 KB
            # file with cells in A1 and XFD1048576 made 17 billion empty cells to walk through.
            sheet.reset_dimensions()
            for row in sheet.iter_rows(values_only=True):
                visited += len(row)
                if visited > MAX_XLSX_CELLS:
                    yield TRUNCATION_MARKER.strip()
                    return
                values = ["" if cell is None else str(cell) for cell in row]
                while values and not values[-1].strip():
                    values.pop()
                if values:
                    yield "\t".join(values)

    try:
        return _join_capped(parts())
    finally:
        workbook.close()


#: Characters that end a line rather than separate two columns. A file with one column has no
#: delimiter to find, so on CRLF input the sniffer reports the carriage return that repeats on
#: every line, and `csv.reader` refuses to parse with it.
_LINE_ENDINGS = "\r\n"


def _dialect_for(raw: str) -> type[csv.Dialect]:
    """Return the dialect to read `raw` with, falling back to Excel's where sniffing fails.

    A single-column CSV saved by Excel carries CRLF and nothing else to go on. Such a file is a
    list of values, not a document the hub should refuse.
    """
    try:
        dialect = csv.Sniffer().sniff(raw[:4096])
    except csv.Error:
        return csv.excel
    if dialect.delimiter in _LINE_ENDINGS:
        return csv.excel
    return dialect


def _from_csv(path: Path) -> str:
    raw = _decode(path.read_bytes())
    rows = csv.reader(io.StringIO(raw, newline=""), _dialect_for(raw))
    return _join_capped("\t".join(row) for row in rows)


def _from_json(path: Path) -> str:
    raw = _decode(path.read_bytes())
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return raw


def _from_image(path: Path) -> str:
    _require_ocr()
    return ocr.ocr_image(path)


def _require_ocr() -> None:
    """Refuse the file when reading it needs OCR and OCR is not installed."""
    if not ocr.is_available():
        raise UnsupportedFileError(OCR_UNAVAILABLE)


_EXTRACTORS: dict[str, Callable[[Path], str]] = {
    ".pdf": _from_pdf,
    ".docx": _from_docx,
    ".xlsx": _from_xlsx,
    ".xlsm": _from_xlsx,
    ".json": _from_json,
    ".csv": _from_csv,
    ".tsv": _from_csv,
    **dict.fromkeys((".txt", ".md", ".log"), _from_plain),
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"), _from_image),
}

#: Every file name ending the hub can read, lowercased. Anything else is refused before storing.
SUPPORTED_SUFFIXES = frozenset(_EXTRACTORS)
