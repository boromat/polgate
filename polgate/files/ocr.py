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

logger = logging.getLogger(__name__)

#: Tesseract language packs to use, Polish first.
LANGUAGES = "pol+eng"


@lru_cache
def is_available() -> bool:
    """Return True when both pytesseract and the tesseract binary are installed."""
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def ocr_image(path: Path) -> str:
    """Return the text recognised in an image file.

    Raises:
        RuntimeError: when OCR support is not installed.
    """
    _require_available()

    import pytesseract
    from PIL import Image

    with Image.open(path) as image:
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
        yield "\n".join(
            str(pytesseract.image_to_string(picture.image, lang=LANGUAGES))
            for picture in page.images
        )


def _require_available() -> None:
    if not is_available():
        raise RuntimeError("OCR is not available: install tesseract-ocr and the [ocr] extra")
