"""Optional OCR support.

OCR needs the ``tesseract`` binary, which the hub cannot install for the user. When it is absent the
feature simply reports itself as unavailable rather than failing an upload with a stack trace.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Tesseract language packs to use, Polish first.
LANGUAGES = "pol+eng"

#: The largest picture OCR reads, in pixels: an A4 page scanned at 600 dpi is about 35 million.
#: Pillow only refuses past 179 million, and a flat-colour PNG of that size fits in 10 MB; tesseract
#: then worked on it for minutes and gigabytes on a machine with eight.
MAX_OCR_PIXELS = 50_000_000


class ImageTooLargeError(ValueError):
    """Raised for a picture with more pixels than OCR reads."""


@lru_cache
def is_available() -> bool:
    """Return True when pytesseract, the tesseract binary and its Polish and English data are there.

    Without the language data every call fails, and an upload was reported as an unreadable file
    rather than as OCR the server is missing a piece of.
    """
    try:
        import pytesseract
    except ImportError:
        return False
    if shutil.which("tesseract") is None:
        return False
    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception as exc:  # any failure to ask means OCR cannot be used
        logger.warning("tesseract did not list its languages (%s)", type(exc).__name__)
        return False
    missing = set(LANGUAGES.split("+")) - installed
    if missing:
        logger.warning("tesseract lacks language data: %s", ", ".join(sorted(missing)))
        return False
    return True


def ocr_image(path: Path) -> str:
    """Return the text recognised in an image file.

    Raises:
        RuntimeError: when OCR support is not installed.
    """
    _require_available()

    import pytesseract
    from PIL import Image

    with Image.open(path) as image:
        _refuse_if_too_large(image)
        return str(pytesseract.image_to_string(image, lang=LANGUAGES))


def ocr_pdf(path: Path) -> Iterator[str]:
    """Yield the text recognised on each page of a PDF that has no text layer.

    A scanner stores each page as a picture inside the PDF, and pypdf hands those pictures out
    as images tesseract can read; Pillow cannot open the PDF itself. Pages come one at a time, so
    a caller that already has enough text stops before the rest are recognised.

    Raises:
        RuntimeError: when OCR support is not installed.
    """
    _require_available()

    import pytesseract
    from pypdf import PdfReader

    for page in PdfReader(str(path)).pages:
        texts = []
        for picture in page.images:
            _refuse_if_too_large(picture.image)
            texts.append(str(pytesseract.image_to_string(picture.image, lang=LANGUAGES)))
        yield "\n".join(texts)


def _refuse_if_too_large(image: Any) -> None:
    """Refuse a picture with more pixels than :data:`MAX_OCR_PIXELS`, before tesseract sees it."""
    width, height = image.size
    if width * height > MAX_OCR_PIXELS:
        raise ImageTooLargeError(f"{width}x{height} pixels")


def _require_available() -> None:
    if not is_available():
        raise RuntimeError("OCR is not available: install tesseract-ocr and the [ocr] extra")
