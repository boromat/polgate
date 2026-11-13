"""Chat interface: conversations, sending messages and streaming answers back."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from polanon import Allowlist, Anonymizer, DenylistGuard, JudgeError, default_guards
from polanon.policy import enabled_entities
from sqlmodel import Session, col, desc, select
from sse_starlette.sse import EventSourceResponse

from polgate import audit
from polgate.auth.deps import current_user
from polgate.config import get_settings
from polgate.db import get_session
from polgate.judge_factory import build_judge
from polgate.llm.router import run_turn
from polgate.models import (
    Conversation,
    FileAsset,
    FileStatus,
    HilMode,
    Message,
    PendingTurn,
    User,
)
from polgate.policy_loader import get_policy, masking_is_off
from polgate.rendering import render
from polgate.security import is_same_origin
from polgate.vault import store as vault

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")

#: What an EventSource asks for, and the only thing the answer stream serves.
EVENT_STREAM = "text/event-stream"


@dataclass(frozen=True)
class QueuedTurn:
    """A message waiting to be streamed, read back out of the pending-turn table."""

    text: str
    extra_terms: str


def build_anonymizer(session: Session) -> Anonymizer:
    """Assemble the anonymizer from the current admin policy and application settings."""
    settings = get_settings()
    policy = get_policy(session)
    guards = default_guards(
        denylist=policy.denylist.splitlines(),
        with_ner=True,
        spacy_model=settings.spacy_model,
    )
    return Anonymizer(
        guards,
        judge=build_judge(settings, policy),
        enabled=enabled_entities(policy.enabled_entities),
        judge_fallback=policy.judge_fallback,
        allowlist=Allowlist(policy.allowlist.splitlines()),
    )


def _conversations(session: Session, user: User) -> list[Conversation]:
    return list(
        session.exec(
            select(Conversation)
            .where(col(Conversation.user_id) == user.id)
            .order_by(desc(col(Conversation.updated_at)))
        ).all()
    )


def _load_conversation(session: Session, conversation_id: int, user: User) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return conversation


@router.get("")
def chat_home(
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Open the most recent conversation, or offer to start one when there is none.

    Nothing is created here: a GET can be a link followed from another site, and it carries the
    session cookie. Signing in creates the first conversation instead, see
    :func:`ensure_a_conversation`.
    """
    existing = _conversations(session, user)
    if existing:
        return RedirectResponse(f"/chat/{existing[0].id}", status_code=303)
    return render(request, "chat_empty.html")


def ensure_a_conversation(session: Session, user: User) -> None:
    """Give an account that has no conversation its first one; the caller is a POST."""
    if _conversations(session, user):
        return
    session.add(Conversation(user_id=_require_user_id(user)))
    session.commit()


@router.get("/new")
def new_conversation_link() -> Response:
    """Open the chat for an old bookmark of the creation link, without creating anything.

    Creating a conversation is a POST: Lax cookies travel with a link followed from another site,
    so a GET that changed state could be triggered by any page the employee opens.
    """
    return RedirectResponse("/chat", status_code=303)


@router.post("/new")
def new_conversation(
    user: User = Depends(current_user), session: Session = Depends(get_session)
) -> Response:
    """Create an empty conversation and open it."""
    conversation = Conversation(user_id=_require_user_id(user))
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    return RedirectResponse(f"/chat/{conversation.id}", status_code=303)


@router.get("/{conversation_id}")
def show_conversation(
    request: Request,
    conversation_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Render one conversation with its history and attachments."""
    conversation = _load_conversation(session, conversation_id, user)
    messages = session.exec(
        select(Message)
        .where(col(Message.conversation_id) == conversation_id)
        .order_by(col(Message.id))
    ).all()
    files = session.exec(
        select(FileAsset).where(col(FileAsset.conversation_id) == conversation_id)
    ).all()
    policy = get_policy(session)
    return render(
        request,
        "chat.html",
        conversation=conversation,
        conversations=_conversations(session, user),
        messages=[
            {
                "id": m.id,
                "role": m.role.value,
                "text": vault.decrypt(m.content_original_enc),
                "sent": m.content_sent,
                "entity_count": m.entity_count,
                "judge_hits": m.judge_hits,
                "created_at": m.created_at,
            }
            for m in messages
        ],
        files=files,
        approved_files=[f for f in files if f.status is FileStatus.APPROVED],
        review_mode=policy.hil_chat is HilMode.REVIEW,
        masking_off=masking_is_off(policy),
    )


@router.post("/{conversation_id}/send")
async def send_message(
    request: Request,
    conversation_id: int,
    text: str = Form(...),
    extra_terms: str = Form(""),
    approved: str = Form(""),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Accept a message and either preview its masking or start streaming the answer."""
    conversation = _load_conversation(session, conversation_id, user)
    # Surrounding whitespace means nothing to the model, and the trailing blank lines left by an
    # Enter pressed in hope of sending showed up in the stored preview.
    text = text.strip()
    if not text:
        # The textarea is `required`, but that only checks for an empty string: a browser happily
        # submits a line of spaces, and the provider would be paid for answering it.
        return render(request, "partials/notice.html", reason="chat.empty", status_code=400)

    policy = get_policy(session)
    anonymizer = build_anonymizer(session)
    if extra_terms.strip():
        anonymizer.guards = [*anonymizer.guards, _manual_guard(extra_terms)]

    if policy.hil_chat is HilMode.REVIEW and not approved:
        try:
            preview = await anonymizer.anonymize(text, vault.load_mapping(session, conversation_id))
        except JudgeError as exc:
            # The same refusal the answer stream gives when the policy blocks on a failed judge;
            # without it the review step ended in a server error and nothing in the audit trail.
            logger.warning("preview blocked: judge unavailable (%s)", type(exc).__name__)
            audit.log(
                session,
                event="turn_blocked_judge",
                user_id=user.id,
                conversation_id=conversation.id,
                detail="stage-2 judge unavailable and policy is 'block'",
            )
            return render(
                request, "partials/notice.html", reason="chat.error_judge", status_code=503
            )
        return render(
            request,
            "partials/send_review.html",
            conversation=conversation,
            text=text,
            extra_terms=extra_terms,
            preview=preview,
        )

    _queue_turn(session, conversation_id, _require_user_id(user), text, extra_terms)
    return render(
        request,
        "partials/message_pair.html",
        conversation=conversation,
        text=text,
    )


@router.get("/{conversation_id}/stream")
async def stream_answer(
    request: Request,
    conversation_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Stream the assistant's answer for the message queued by `send_message`."""
    # An EventSource can only GET, and this GET sends the queued message to the provider, so a link
    # on another site must not reach it. The origin check sees nothing over plain HTTP, where a
    # browser sends no Fetch Metadata and no Origin on a GET. The Accept header settles it: an
    # EventSource always asks for an event stream, and the only cross-site request that carries
    # the Lax cookie, a link followed at the top level, asks for a page.
    accepts = request.headers.get("accept", "")
    if EVENT_STREAM not in accepts or not is_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not an event stream")
    conversation = _load_conversation(session, conversation_id, user)
    pending = _take_queued_turn(session, conversation_id, _require_user_id(user))
    if pending is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="nothing to stream")

    anonymizer = build_anonymizer(session)
    if pending.extra_terms.strip():
        anonymizer.guards = [*anonymizer.guards, _manual_guard(pending.extra_terms)]

    async def events() -> AsyncIterator[dict[str, str]]:
        async for event in run_turn(
            session,
            conversation,
            user,
            pending.text,
            policy=get_policy(session),
            anonymizer=anonymizer,
            settings=get_settings(),
        ):
            yield {
                "event": event.type,
                "data": json.dumps({"text": event.text, "detail": event.detail}),
            }

    return EventSourceResponse(events())


@router.get("/{conversation_id}/messages/{message_id}/preview")
def message_preview(
    request: Request,
    conversation_id: int,
    message_id: int,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Show what the provider received for one message next to the original."""
    _load_conversation(session, conversation_id, user)
    message = session.get(Message, message_id)
    if message is None or message.conversation_id != conversation_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return render(
        request,
        "partials/preview.html",
        original=vault.decrypt(message.content_original_enc),
        sent=message.content_sent,
    )


def _queue_turn(
    session: Session, conversation_id: int, user_id: int, text: str, extra_terms: str
) -> None:
    """Store a message so the streaming endpoint can pick it up, replacing any earlier one."""
    for stale in session.exec(
        select(PendingTurn)
        .where(col(PendingTurn.conversation_id) == conversation_id)
        .where(col(PendingTurn.user_id) == user_id)
    ).all():
        session.delete(stale)
    session.add(
        PendingTurn(
            conversation_id=conversation_id,
            user_id=user_id,
            text_enc=vault.encrypt(text),
            extra_terms_enc=vault.encrypt(extra_terms) if extra_terms else "",
        )
    )
    session.commit()


def _take_queued_turn(session: Session, conversation_id: int, user_id: int) -> QueuedTurn | None:
    """Pop the queued message for this conversation, or None when there is nothing to stream."""
    row = session.exec(
        select(PendingTurn)
        .where(col(PendingTurn.conversation_id) == conversation_id)
        .where(col(PendingTurn.user_id) == user_id)
        .order_by(desc(col(PendingTurn.id)))
    ).first()
    if row is None:
        return None
    queued = QueuedTurn(
        text=vault.decrypt(row.text_enc),
        extra_terms=vault.decrypt(row.extra_terms_enc) if row.extra_terms_enc else "",
    )
    session.delete(row)
    session.commit()
    return queued


def _manual_guard(terms: str) -> DenylistGuard:
    """Build a denylist guard for terms the employee asked to mask for this message only."""
    return DenylistGuard(terms.replace(",", "\n").splitlines())


def _require_user_id(user: User) -> int:
    if user.id is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user.id
