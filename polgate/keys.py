"""Secrets the hub generates for itself when the deployment does not provide them.

A hub started with nothing configured has to be safe anyway, because ``docker run`` with no
environment is how most people first try it. A placeholder default is worse than none: a session
key anybody can read in this repository lets anybody sign an administrator's cookie, and with it
read the decrypted history. So each secret is generated once, written next to the database where
only the service account can read it, and read back on every later start: a restart neither signs
everybody out nor locks the history away.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path

from polgate.config import Settings, get_settings

logger = logging.getLogger(__name__)

SESSION_KEY_FILE = "session.key"
ADMIN_PASSWORD_FILE = "initial-admin-password"  # noqa: S105 - a file name, not a password

#: Values shipped in this repository as examples. A deployment that copied one unchanged has a key
#: everybody knows, so it is treated exactly like no key at all.
PLACEHOLDER_SECRET_KEYS = frozenset({"change-me-to-a-long-random-string", "dev-only-change-me"})


def state_dir(settings: Settings) -> Path:
    """Return the directory that holds the database, and with it every generated secret."""
    return Path(settings.database_url.removeprefix("sqlite:///")).parent


def read_or_create(path: Path, generate: Callable[[], str]) -> tuple[str, bool]:
    """Return the secret stored at `path`, generating and storing it the first time.

    The file is created exclusively and readable by its owner alone from the moment it exists, so a
    second process starting at the same time cannot overwrite it, and no other account ever sees it.

    Args:
        path: where the secret lives.
        generate: produces a new secret when there is none yet.

    Returns:
        The secret, and whether this call created it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip(), False
    value = generate()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)
    return value, True


def session_secret() -> str:
    """Return the key that signs session cookies: ``SECRET_KEY``, or one generated for this hub."""
    settings = get_settings()
    if settings.secret_key and settings.secret_key not in PLACEHOLDER_SECRET_KEYS:
        return settings.secret_key
    return _generated_session_secret(state_dir(settings) / SESSION_KEY_FILE)


@lru_cache
def _generated_session_secret(path: Path) -> str:
    secret, created = read_or_create(path, lambda: secrets.token_urlsafe(48))
    if created:
        logger.warning("Generated a session signing key at %s.", path)
    return secret


def bootstrap_password() -> str | None:
    """Return the first administrator's initial password, or None when there never was one.

    That is ``ADMIN_PASSWORD`` when it is set, otherwise the password generated on first start.
    """
    settings = get_settings()
    if settings.admin_password:
        return settings.admin_password
    path = state_dir(settings) / ADMIN_PASSWORD_FILE
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def create_bootstrap_password() -> str:
    """Return the first administrator's initial password, generating one when none is configured."""
    settings = get_settings()
    if settings.admin_password:
        return settings.admin_password
    password, _ = read_or_create(
        state_dir(settings) / ADMIN_PASSWORD_FILE, lambda: secrets.token_urlsafe(12)
    )
    return password
