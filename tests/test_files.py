"""Text extraction from the document formats employees actually upload."""

import io
import json

import pytest

from polgate.files.extract import MAX_CHARS, UnsupportedFileError, extract_text


def test_plain_text_is_read_as_utf8(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("Zażółć gęślą jaźń", encoding="utf-8")
    assert extract_text(path, "text/plain", "note.txt") == "Zażółć gęślą jaźń"


def test_legacy_polish_encoding_is_decoded(tmp_path):
    path = tmp_path / "old.txt"
    path.write_bytes("Łódź".encode("cp1250"))
    assert extract_text(path, "text/plain", "old.txt") == "Łódź"


def test_csv_becomes_tab_separated_rows(tmp_path):
    path = tmp_path / "clients.csv"
    path.write_text("imie,pesel\nJan,50810112354\n", encoding="utf-8")
    assert extract_text(path, "text/csv", "clients.csv") == "imie\tpesel\nJan\t50810112354"


def test_json_is_pretty_printed(tmp_path):
    path = tmp_path / "data.json"
    path.write_text(json.dumps({"nip": "5260000005"}), encoding="utf-8")
    assert '"nip": "5260000005"' in extract_text(path, "application/json", "data.json")


def test_docx_paragraphs_and_tables_are_extracted(tmp_path):
    from docx import Document

    document = Document()
    document.add_paragraph("Umowa z klientem")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "PESEL"
    table.rows[0].cells[1].text = "50810112354"
    path = tmp_path / "umowa.docx"
    document.save(str(path))

    text = extract_text(
        path,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "umowa.docx",
    )
    assert "Umowa z klientem" in text
    assert "PESEL\t50810112354" in text


def test_xlsx_sheets_are_extracted(tmp_path):
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Faktury"
    sheet.append(["NIP", "kwota"])
    sheet.append(["5260000005", 1230])
    path = tmp_path / "faktury.xlsx"
    workbook.save(str(path))

    text = extract_text(path, "application/vnd.ms-excel", "faktury.xlsx")
    assert "## Arkusz: Faktury" in text
    assert "5260000005\t1230" in text


def test_unsupported_extension_is_rejected(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK\x03\x04")
    with pytest.raises(UnsupportedFileError):
        extract_text(path, "application/zip", "archive.zip")


def test_long_documents_are_truncated(tmp_path):
    path = tmp_path / "big.txt"
    path.write_text("a" * (MAX_CHARS + 500), encoding="utf-8")
    text = extract_text(path, "text/plain", "big.txt")
    assert len(text) < MAX_CHARS + 200
    assert text.endswith("truncated ...]")


def test_image_without_ocr_reports_it_is_unavailable(tmp_path, monkeypatch):
    from polgate.files import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: False)
    path = tmp_path / "scan.png"
    path.write_bytes(b"\x89PNG")
    with pytest.raises(UnsupportedFileError, match="ocr_unavailable"):
        extract_text(path, "image/png", "scan.png")


def _pdf_with_text(path, text: str) -> None:
    """Write a minimal one-page PDF whose content stream draws `text`.

    Built by hand rather than with a library, because pypdf reads PDFs and the writers that create
    them are not dependencies of this project.
    """
    body = io.BytesIO()
    offsets: dict[int, int] = {}
    content = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1")

    def obj(number: int, payload: bytes) -> None:
        offsets[number] = body.tell()
        body.write(f"{number} 0 obj\n".encode())
        body.write(payload)
        body.write(b"\nendobj\n")

    body.write(b"%PDF-1.4\n")
    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
    )
    obj(4, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    start_xref = body.tell()
    body.write(b"xref\n0 6\n0000000000 65535 f \n")
    for number in range(1, 6):
        body.write(f"{offsets[number]:010d} 00000 n \n".encode())
    body.write(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n")
    body.write(f"{start_xref}\n%%EOF\n".encode())
    path.write_bytes(body.getvalue())


def test_a_pdf_with_a_text_layer_is_read_page_by_page(tmp_path):
    path = tmp_path / "umowa.pdf"
    _pdf_with_text(path, "PESEL 50810112354")
    text = extract_text(path, "application/pdf", "umowa.pdf")
    assert "--- strona 1 ---" in text
    assert "50810112354" in text


def test_a_pdf_without_a_text_layer_falls_through_to_ocr(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    from polgate.files import ocr

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "skan.pdf"
    with path.open("wb") as handle:
        writer.write(handle)

    monkeypatch.setattr(ocr, "is_available", lambda: False)
    with pytest.raises(UnsupportedFileError, match="ocr_unavailable"):
        extract_text(path, "application/pdf", "skan.pdf")


def test_a_scan_is_read_when_ocr_is_installed(tmp_path, monkeypatch):
    from polgate.files import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "ocr_image", lambda path: "PESEL 50810112354")
    path = tmp_path / "skan.png"
    path.write_bytes(b"\x89PNG")
    assert extract_text(path, "image/png", "skan.png") == "PESEL 50810112354"


def test_a_csv_the_sniffer_cannot_read_still_comes_back_as_rows(tmp_path):
    """A single column has no delimiter to sniff; the excel dialect has to take over."""
    path = tmp_path / "jedna.csv"
    path.write_bytes(b"jeden\ndwa\ntrzy\n")
    assert extract_text(path, "text/csv", "jedna.csv") == "jeden\ndwa\ntrzy"


def test_a_one_column_csv_saved_by_excel_comes_back_as_rows(tmp_path):
    """CRLF is the only repeating character here, and a dialect cannot be built from it."""
    path = tmp_path / "jedna-crlf.csv"
    path.write_bytes(b"jeden\r\ndwa\r\ntrzy\r\n")
    assert extract_text(path, "text/csv", "jedna-crlf.csv") == "jeden\ndwa\ntrzy"


def test_a_csv_with_a_real_delimiter_keeps_its_columns_on_crlf(tmp_path):
    """The fallback must not swallow a delimiter the sniffer did find."""
    path = tmp_path / "dwie.csv"
    path.write_bytes(b"imie;nip\r\nJan;1234567890\r\n")
    assert extract_text(path, "text/csv", "dwie.csv") == "imie\tnip\nJan\t1234567890"


def test_broken_json_is_returned_as_it_was_written(tmp_path):
    path = tmp_path / "zepsute.json"
    path.write_text('{"nip": ', encoding="utf-8")
    assert extract_text(path, "application/json", "zepsute.json") == '{"nip": '


def test_bytes_in_no_known_encoding_are_decoded_with_replacements(tmp_path):
    path = tmp_path / "smieci.txt"
    path.write_bytes(b"PESEL \x81\x90 50810112354")
    text = extract_text(path, "text/plain", "smieci.txt")
    assert "50810112354" in text
    assert "�" in text


def test_ocr_reports_itself_unavailable_without_the_binary(monkeypatch):
    import shutil

    from polgate.files import ocr

    ocr.is_available.cache_clear()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert ocr.is_available() is False
    ocr.is_available.cache_clear()


def test_ocr_refuses_to_run_when_it_is_not_installed(tmp_path, monkeypatch):
    from polgate.files import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="OCR is not available"):
        ocr.ocr_image(tmp_path / "scan.png")


def test_ocr_runs_tesseract_when_both_pieces_are_installed(tmp_path, monkeypatch):
    """The tesseract binary cannot be installed here, so the boundary is mocked, not the logic."""
    import sys
    import types

    from polgate.files import ocr

    fake_pytesseract = types.ModuleType("pytesseract")
    fake_pytesseract.image_to_string = lambda image, lang: f"tekst [{lang}]"
    fake_pil = types.ModuleType("PIL")
    fake_image_module = types.ModuleType("PIL.Image")

    class _Image:
        size = (2480, 3508)  # A4 at 300 dpi

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    fake_image_module.open = lambda path: _Image()
    fake_pil.Image = fake_image_module

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image_module)
    monkeypatch.setattr(ocr, "is_available", lambda: True)

    assert ocr.ocr_image(tmp_path / "skan.png") == f"tekst [{ocr.LANGUAGES}]"


def test_ocr_is_unavailable_without_the_python_package(monkeypatch):
    import builtins

    from polgate.files import ocr

    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "pytesseract":
            raise ImportError("no pytesseract")
        return real_import(name, *args, **kwargs)

    ocr.is_available.cache_clear()
    monkeypatch.setattr(builtins, "__import__", missing)
    assert ocr.is_available() is False
    ocr.is_available.cache_clear()


def test_extraction_stops_reading_once_it_has_enough_text(monkeypatch):
    """A large export used to be read to the end only to have most of it cut off afterwards."""
    from polgate.files import extract

    def rows():
        yield "a" * 60
        yield "b" * 60
        raise AssertionError("read past the limit")

    monkeypatch.setattr(extract, "MAX_CHARS", 100)
    assert extract._join_capped(rows()) == "a" * 60 + "\n" + "b" * 60


def test_text_that_just_fits_is_kept_whole(monkeypatch):
    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_CHARS", 9)
    assert extract._join_capped(iter(["abcd", "efgh"])) == "abcd\nefgh"


@pytest.mark.parametrize("suffix", [".xlsx", ".docx", ".csv", ".pdf"])
def test_every_long_document_format_is_cut_with_the_marker(tmp_path, monkeypatch, suffix):
    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_CHARS", 300)
    path = tmp_path / f"duzy{suffix}"
    lines = [f"wiersz {n} PESEL 50810112354" for n in range(200)]
    if suffix == ".xlsx":
        from openpyxl import Workbook

        workbook = Workbook()
        for line in lines:
            workbook.active.append([line])
        workbook.save(str(path))
    elif suffix == ".docx":
        from docx import Document

        document = Document()
        for line in lines:
            document.add_paragraph(line)
        document.save(str(path))
    elif suffix == ".csv":
        path.write_text("\n".join(lines), encoding="utf-8")
    else:
        _pdf_with_text(path, " ".join(lines))

    text = extract_text(path, "", path.name)
    assert text.endswith(extract.TRUNCATION_MARKER)
    assert len(text) == 300 + len(extract.TRUNCATION_MARKER)


def _docx_with_broken_xml(path) -> None:
    """A document whose XML breaks on a tag named after a person: lxml quotes it in its error."""
    import zipfile

    from docx import Document

    template = path.with_name("szablon.docx")
    Document().save(str(template))
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(path, "w") as target:
        for member in source.infolist():
            body = source.read(member)
            if member.filename == "word/document.xml":
                body = b"<w:document><Kowalski_PESEL_50810112354></w:document>"
            target.writestr(member, body)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("umowa.docx", b"Kowalski_PESEL_50810112354, not a zip archive"),
        ("tabela.xlsx", b"Kowalski_PESEL_50810112354 pretending to be a workbook"),
        ("skan.pdf", b"Kowalski_PESEL_50810112354, not a pdf at all"),
        ("skan.pdf", b"%PDF-1.4 Kowalski_PESEL_50810112354 and nothing a reader can use"),
        ("zepsuta.docx", None),
    ],
)
def test_a_damaged_document_is_reported_as_unreadable(tmp_path, caplog, name, payload):
    """A parser's message can quote the document, so neither it nor the parser's own log may."""
    from polgate.files.extract import UNREADABLE

    path = tmp_path / name
    if payload is None:
        _docx_with_broken_xml(path)
    else:
        path.write_bytes(payload)
    with caplog.at_level("DEBUG"), pytest.raises(UnsupportedFileError, match=UNREADABLE):
        extract_text(path, "", name)
    assert "Kowalski" not in caplog.text
    assert "Kowal" not in caplog.text


def test_a_scanned_pdf_is_read_page_by_page_with_ocr(tmp_path, monkeypatch):
    """Pillow cannot open a PDF, so a scan has to be read from the pictures inside its pages."""
    import sys
    import types

    image_module = pytest.importorskip("PIL.Image")
    from polgate.files import ocr

    first = image_module.new("RGB", (120, 60), "white")
    second = image_module.new("L", (80, 40), 128)
    path = tmp_path / "skan.pdf"
    first.save(path, save_all=True, append_images=[second])

    fake_pytesseract = types.ModuleType("pytesseract")
    fake_pytesseract.image_to_string = lambda image, lang: f"obraz {image.size[0]}x{image.size[1]}"
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setattr(ocr, "is_available", lambda: True)

    text = extract_text(path, "application/pdf", "skan.pdf")
    assert text == "--- strona 1 ---\nobraz 120x60\n\n--- strona 2 ---\nobraz 80x40"


def test_ocr_of_a_pdf_refuses_to_run_when_it_is_not_installed(tmp_path, monkeypatch):
    from polgate.files import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="OCR is not available"):
        next(ocr.ocr_pdf(tmp_path / "skan.pdf"))


def _docx_with_paragraphs(path, count: int) -> None:
    from docx import Document

    document = Document()
    for number in range(count):
        document.add_paragraph(f"akapit {number} PESEL 50810112354")
    document.save(str(path))


def _with_member(path, name: str, size: int) -> None:
    """Add `size` zero bytes as a member of an archive; zeros compress to almost nothing."""
    import zipfile

    with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, b"\0" * size)


def test_a_document_that_unpacks_past_what_is_parsed_whole_is_refused_unread(tmp_path, monkeypatch):
    """python-docx holds the whole document in memory; a small upload could unpack to gigabytes."""
    from docx import Document

    from polgate.files import extract

    path = tmp_path / "bomba.docx"
    _docx_with_paragraphs(path, 3)
    # python-docx's own template carries about 830 KB of styles, all of it parsed whole too.
    _with_member(path, "word/footer9.xml", 2_000_000)
    monkeypatch.setattr(extract, "MAX_PARSED_XML_BYTES", 1_500_000)
    opened: list[str] = []
    monkeypatch.setattr("docx.Document", lambda *args: opened.append("parsed") or Document(*args))

    with pytest.raises(UnsupportedFileError, match=extract.UNPACKS_TOO_LARGE):
        extract_text(path, "", "bomba.docx")
    assert opened == []


def test_pictures_in_a_document_do_not_count_as_xml_parsed_whole(tmp_path, monkeypatch):
    from polgate.files import extract

    path = tmp_path / "zdjecia.docx"
    _docx_with_paragraphs(path, 3)
    _with_member(path, "word/media/image1.png", 2_000_000)
    monkeypatch.setattr(extract, "MAX_PARSED_XML_BYTES", 1_500_000)

    assert "akapit 2" in extract_text(path, "", "zdjecia.docx")


def test_a_workbook_with_an_oversized_table_of_shared_strings_is_refused(tmp_path, monkeypatch):
    """openpyxl loads every shared string before the first row, whatever read_only says."""
    from openpyxl import Workbook

    from polgate.files import extract

    path = tmp_path / "bomba.xlsx"
    workbook = Workbook()
    workbook.active.append(["NIP", "5260000005"])
    workbook.save(str(path))
    _with_member(path, "xl/sharedStrings2.xml", 50_000)
    monkeypatch.setattr(extract, "MAX_PARSED_XML_BYTES", 40_000)

    with pytest.raises(UnsupportedFileError, match=extract.UNPACKS_TOO_LARGE):
        extract_text(path, "", "bomba.xlsx")


def test_a_long_worksheet_is_streamed_rather_than_refused(tmp_path, monkeypatch):
    """A real 10 MB export unpacks to about 105 MB of worksheet, which openpyxl reads row by row."""
    from openpyxl import Workbook

    from polgate.files import extract

    path = tmp_path / "eksport.xlsx"
    workbook = Workbook()
    for number in range(2000):
        workbook.active.append([f"klient {number}", "5260000005"])
    workbook.save(str(path))
    monkeypatch.setattr(extract, "MAX_PARSED_XML_BYTES", 20_000)

    assert "klient 1999" in extract_text(path, "", "eksport.xlsx")


def test_an_archive_that_unpacks_past_the_overall_limit_is_refused(tmp_path, monkeypatch):
    """Streamed parts cost time instead of memory, so they have a ceiling as well."""
    from openpyxl import Workbook

    from polgate.files import extract

    path = tmp_path / "puste-wiersze.xlsx"
    Workbook().save(str(path))
    _with_member(path, "xl/worksheets/sheet2.xml", 80_000)
    monkeypatch.setattr(extract, "MAX_UNPACKED_BYTES", 60_000)

    with pytest.raises(UnsupportedFileError, match=extract.UNPACKS_TOO_LARGE):
        extract_text(path, "", "puste-wiersze.xlsx")


def test_a_part_that_ends_exactly_at_the_limit_still_reads_one_more(monkeypatch):
    """Stopping when the limit is reached would drop what follows without the marker."""
    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_CHARS", 9)
    assert extract._join_capped(iter(["abcd", "efgh", "ijkl"])) == "abcd\nefgh\nijkl"


def test_rows_that_end_exactly_at_the_limit_are_marked_as_cut(tmp_path, monkeypatch):
    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_CHARS", 9)
    path = tmp_path / "tabela.csv"
    path.write_text("abcd\nefgh\nPESEL 50810112354\n", encoding="utf-8")
    assert extract_text(path, "", path.name) == "abcd\nefgh\nPESEL 50810112354"[:9] + (
        extract.TRUNCATION_MARKER
    )


ROWS = 2000


@pytest.fixture
def small_limit(monkeypatch):
    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_CHARS", 300)


def test_a_csv_is_read_only_as_far_as_the_limit(tmp_path, monkeypatch, small_limit):
    from polgate.files import extract

    path = tmp_path / "duzy.csv"
    path.write_text("\n".join(f"wiersz {n};PESEL 50810112354" for n in range(ROWS)), "utf-8")
    seen = []
    real_reader = extract.csv.reader

    def counting_reader(*args, **kwargs):
        for row in real_reader(*args, **kwargs):
            seen.append(row)
            yield row

    monkeypatch.setattr(extract.csv, "reader", counting_reader)
    extract_text(path, "", path.name)
    assert len(seen) < 50, f"read {len(seen)} of {ROWS} rows"


def test_a_workbook_is_read_only_as_far_as_the_limit(tmp_path, monkeypatch, small_limit):
    from openpyxl import Workbook
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    path = tmp_path / "duzy.xlsx"
    workbook = Workbook()
    for n in range(ROWS):
        workbook.active.append([f"wiersz {n}", "PESEL 50810112354"])
    workbook.save(str(path))
    seen = []
    real_iter_rows = ReadOnlyWorksheet.iter_rows

    def counting_iter_rows(self, *args, **kwargs):
        for row in real_iter_rows(self, *args, **kwargs):
            seen.append(row)
            yield row

    monkeypatch.setattr(ReadOnlyWorksheet, "iter_rows", counting_iter_rows)
    extract_text(path, "", path.name)
    assert len(seen) < 50, f"read {len(seen)} of {ROWS} rows"


def test_a_document_is_read_only_as_far_as_the_limit(tmp_path, monkeypatch, small_limit):
    from docx.text.paragraph import Paragraph

    path = tmp_path / "duzy.docx"
    _docx_with_paragraphs(path, ROWS)
    seen = []
    real_text = Paragraph.text

    def counting_text(self):
        seen.append(1)
        return real_text.fget(self)

    monkeypatch.setattr(Paragraph, "text", property(counting_text))
    extract_text(path, "", path.name)
    assert len(seen) < 100, f"read {len(seen)} paragraph texts for {ROWS} paragraphs"


def test_a_pdf_is_read_only_as_far_as_the_limit(tmp_path, monkeypatch, small_limit):
    from pypdf import PageObject, PdfWriter

    path = tmp_path / "duzy.pdf"
    writer = PdfWriter()
    for _ in range(60):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    seen = []

    def counting_extract(self, *args, **kwargs):
        seen.append(1)
        return "strona PESEL 50810112354 " * 4

    monkeypatch.setattr(PageObject, "extract_text", counting_extract)
    extract_text(path, "", path.name)
    assert len(seen) < 10, f"extracted {len(seen)} of 60 pages"


def test_a_scan_is_recognised_only_as_far_as_the_limit(tmp_path, monkeypatch, small_limit):
    """Tesseract takes a second or two a page, so the pages past the limit must not be read."""
    import sys
    import types

    from pypdf import PageObject

    from polgate.files import ocr

    image_module = pytest.importorskip("PIL.Image")
    pages = [image_module.new("RGB", (40, 20), "white") for _ in range(30)]
    path = tmp_path / "skan.pdf"
    pages[0].save(path, save_all=True, append_images=pages[1:])
    recognised = []
    fake_pytesseract = types.ModuleType("pytesseract")

    def image_to_string(image, lang):
        recognised.append(1)
        return "rozpoznany tekst PESEL 50810112354 " * 4

    fake_pytesseract.image_to_string = image_to_string
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *args, **kwargs: "")
    extract_text(path, "application/pdf", path.name)
    assert len(recognised) < 10, f"recognised {len(recognised)} of 30 pages"


UNICODE_TEXT = "Pracownica Zażółć Gęślą, PESEL 55831212340, mail z.gesla@example.com"


@pytest.mark.parametrize(
    "raw",
    [
        UNICODE_TEXT.encode("utf-16"),  # with a mark, as PowerShell 5.1 and Notepad write it
        UNICODE_TEXT.encode("utf-16-le"),
        UNICODE_TEXT.encode("utf-16-be"),
        UNICODE_TEXT.encode("utf-8-sig"),
        UNICODE_TEXT.encode("utf-32"),
    ],
    ids=["utf-16", "utf-16-le-no-mark", "utf-16-be-no-mark", "utf-8-mark", "utf-32"],
)
@pytest.mark.parametrize("suffix", [".txt", ".csv", ".json"])
def test_unicode_files_are_read_as_the_text_they_hold(tmp_path, raw, suffix):
    """Read as cp1250, UTF-16 had a NUL between every letter, and the PESEL went out unmasked."""
    path = tmp_path / f"eksport{suffix}"
    path.write_bytes(raw)
    text = extract_text(path, "text/plain", path.name)
    assert "\x00" not in text
    assert "55831212340" in text
    assert "Zażółć Gęślą" in text


def test_text_that_still_holds_a_nul_is_refused(tmp_path):
    path = tmp_path / "binarny.txt"
    path.write_bytes(b"PESEL 55831212340\x00\x01\x02 to nie jest tekst")
    with pytest.raises(UnsupportedFileError, match="unreadable"):
        extract_text(path, "text/plain", path.name)


def test_a_workbook_that_claims_the_whole_sheet_is_read_in_moments(tmp_path):
    """Cells in A1 and XFD1048576 made openpyxl pad 17 billion empty cells: 14 minutes of CPU."""
    import time

    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active["A1"] = "PESEL 55831212340"
    workbook.active["XFD1048576"] = "koniec"
    path = tmp_path / "bomba.xlsx"
    workbook.save(path)

    started = time.monotonic()
    text = extract_text(path, "application/vnd.ms-excel", path.name)

    assert time.monotonic() - started < 30
    assert "PESEL 55831212340" in text
    assert "koniec" in text


def test_a_workbook_past_the_cell_budget_is_cut_with_the_marker(tmp_path, monkeypatch):
    from openpyxl import Workbook

    from polgate.files import extract

    monkeypatch.setattr(extract, "MAX_XLSX_CELLS", 50)
    workbook = Workbook()
    for row in range(1, 101):
        workbook.active.append([f"wiersz {row}", "x"])
    path = tmp_path / "duzy.xlsx"
    workbook.save(path)

    text = extract_text(path, "application/vnd.ms-excel", path.name)

    assert "wiersz 1\tx" in text
    assert "wiersz 100" not in text
    assert text.endswith(extract.TRUNCATION_MARKER.strip())


def test_ocr_without_its_polish_data_reports_itself_unavailable(monkeypatch):
    """Every call failed for want of the language pack, and the upload read as unreadable."""
    import shutil
    import sys
    import types

    from polgate.files import ocr

    fake = types.ModuleType("pytesseract")
    fake.get_languages = lambda config="": ["eng", "osd"]
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    ocr.is_available.cache_clear()
    try:
        assert ocr.is_available() is False
        fake.get_languages = lambda config="": ["eng", "pol", "osd"]
        ocr.is_available.cache_clear()
        assert ocr.is_available() is True

        def broken(config=""):
            raise RuntimeError("tesseract crashed")

        fake.get_languages = broken
        ocr.is_available.cache_clear()
        assert ocr.is_available() is False
    finally:
        ocr.is_available.cache_clear()


def test_a_picture_with_too_many_pixels_is_refused_before_ocr(tmp_path, monkeypatch):
    from PIL import Image

    from polgate.files import ocr

    monkeypatch.setattr(ocr, "is_available", lambda: True)
    monkeypatch.setattr(ocr, "MAX_OCR_PIXELS", 100 * 100)
    path = tmp_path / "plakat.png"
    Image.new("L", (200, 200), color=255).save(path)

    with pytest.raises(UnsupportedFileError, match="image_too_large"):
        extract_text(path, "image/png", path.name)
