"""Create the first admin account on startup if the users table is empty."""

import logging

from sqlmodel import Session, select

from polgate.auth.passwords import hash_password, verify_password
from polgate.config import get_settings
from polgate.db import get_engine
from polgate.keys import (
    ADMIN_PASSWORD_FILE,
    bootstrap_password,
    create_bootstrap_password,
    state_dir,
)
from polgate.models import Role, User

logger = logging.getLogger(__name__)


def ensure_admin() -> None:
    """Create the configured administrator when the database has no accounts yet.

    Without ``ADMIN_PASSWORD`` the password is generated rather than defaulted: a hub reachable on
    the office network with ``admin``/``admin`` belongs to whoever signs in first. The log names the
    file that holds it, never the password itself.
    """
    settings = get_settings()
    with Session(get_engine()) as session:
        if session.exec(select(User).limit(1)).first():
            return
        session.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(create_bootstrap_password()),
                role=Role.ADMIN,
            )
        )
        session.commit()
    if not settings.admin_password:
        logger.warning(
            "Created the administrator %r. Its first password is in %s; sign in and change it.",
            settings.admin_username,
            state_dir(settings) / ADMIN_PASSWORD_FILE,
        )


def uses_bootstrap_password(user: User) -> bool:
    """Return whether an account still has the password it was seeded with.

    The first admin exists so somebody can sign in at all; leaving it on the value from ``.env``, or
    on the generated one sitting in a file beside the database, is the single most likely way this
    hub gets opened by the wrong person, so the panel says so.
    """
    settings = get_settings()
    if user.username != settings.admin_username:
        return False
    password = bootstrap_password()
    return password is not None and verify_password(password, user.password_hash)
