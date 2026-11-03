"""FastAPI dependencies for authentication and authorisation.

This is an HTML application, so an anonymous visitor is redirected to the login page rather than
receiving a JSON 401.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from polgate.models import Role, User


class LoginRequiredError(HTTPException):
    """Raised for anonymous visitors; the exception handler turns it into a redirect."""

    def __init__(self, next_url: str = "/") -> None:
        """Remember where the visitor wanted to go so login can send them back."""
        super().__init__(status_code=status.HTTP_303_SEE_OTHER)
        self.next_url = next_url


def current_user(request: Request) -> User:
    """Return the signed-in user or redirect to the login page.

    Raises:
        LoginRequiredError: when nobody is signed in.
    """
    user = getattr(request.state, "user", None)
    if not isinstance(user, User):
        raise LoginRequiredError(next_url=request.url.path)
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    """Return the signed-in user, provided they are an administrator.

    Raises:
        HTTPException: 403 when the user is not an administrator.
    """
    if user.role is not Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user
