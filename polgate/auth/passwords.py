"""Password hashing with argon2id, the current OWASP recommendation for new applications."""

from __future__ import annotations

import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

#: Length floor for a password an employee chooses. Deliberately a length rule and nothing else:
#: composition rules push people towards "Haslo1!" while a long passphrase survives guessing.
MIN_LENGTH = 10


def password_problem(password: str, *, username: str) -> str | None:
    """Return a short code naming the rule a new password breaks, or None when it is acceptable."""
    if len(password) < MIN_LENGTH:
        return "too_short"
    if password.strip().lower() == username.strip().lower():
        return "same_as_username"
    return None


def hash_password(password: str) -> str:
    """Return an argon2id hash of `password`."""
    return _hasher.hash(password)


@lru_cache(maxsize=1)
def decoy_hash() -> str:
    """Return the hash of a random password nobody knows, to check against when there is no account.

    Sign-in runs argon2 whatever the username, so how long an answer takes does not tell which
    accounts exist or which are disabled: without it an unknown name answered six times faster.
    """
    return hash_password(secrets.token_urlsafe(32))


def verify_password(password: str, password_hash: str) -> bool:
    """Return True when `password` matches `password_hash`."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
