"""Minimal internationalisation.

Dictionaries live in ``polgate/i18n/{pl,en}.json`` and reach templates as the ``t(key)`` helper.
"""

import json
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).parent / "i18n"
SUPPORTED = ("pl", "en")


@lru_cache
def load(lang: str) -> dict[str, str]:
    """Load and cache one language dictionary."""
    lang = lang if lang in SUPPORTED else "pl"
    data: dict[str, str] = json.loads((I18N_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    return data


def translator(lang: str) -> Callable[..., str]:
    """Return a ``t(key, **kwargs)`` function for one language, falling back to English."""
    table = load(lang)
    fallback = load("en")

    def t(key: str, **kwargs: object) -> str:
        text = table.get(key) or fallback.get(key) or key
        return text.format(**kwargs) if kwargs else text

    return t
