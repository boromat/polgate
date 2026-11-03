"""Translation coverage.

A missing key renders as the key itself, which is a silent failure the eye catches only by luck, so
the templates are checked against both dictionaries here instead.
"""

import json
import re
from pathlib import Path

import pytest

from polgate.i18n import SUPPORTED, load, translator

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "polgate" / "templates"
TRANSLATE_CALL = re.compile(r"""\bt\(\s*['"]([a-z][a-z0-9_.]*)['"]""")


def _keys_used_in_templates() -> set[str]:
    used: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        used |= set(TRANSLATE_CALL.findall(path.read_text(encoding="utf-8")))
    return used


@pytest.mark.parametrize("language", SUPPORTED)
def test_every_template_key_exists_in_every_language(language):
    missing = sorted(_keys_used_in_templates() - set(load(language)))
    assert not missing, f"{language} is missing: {missing}"


def test_both_languages_define_the_same_keys():
    polish = set(json.loads((ROOT / "polgate/i18n/pl.json").read_text(encoding="utf-8")))
    english = set(json.loads((ROOT / "polgate/i18n/en.json").read_text(encoding="utf-8")))
    assert polish == english, f"only in one language: {sorted(polish ^ english)}"


def test_no_translation_is_left_empty():
    for language in SUPPORTED:
        blank = sorted(key for key, value in load(language).items() if not value.strip())
        assert not blank, f"{language} has empty values: {blank}"


def test_every_defined_key_is_referenced_somewhere():
    """An unused key is dead weight.

    Keys reach the page from templates and from Python, sometimes inside a redirect URL rather than
    a quoted literal, so the sources are searched for the key itself.
    """
    sources = [p.read_text(encoding="utf-8") for p in (ROOT / "polgate").rglob("*.py")]
    sources += [p.read_text(encoding="utf-8") for p in TEMPLATES.rglob("*.html")]
    unused = sorted(key for key in load("pl") if not any(key in source for source in sources))
    assert not unused, f"defined but never referenced: {unused}"


def test_unknown_key_falls_back_to_the_key_itself():
    assert translator("pl")("nie.ma.takiego") == "nie.ma.takiego"


def test_placeholders_are_substituted():
    assert "7" in translator("pl")("chat.review_hint", count=7)


def test_an_unsupported_language_falls_back_to_polish():
    assert load("de") == load("pl")
