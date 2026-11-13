"""SQLModel tables.

Design notes:
- Message.content_original is stored ENCRYPTED (Fernet, see vault/store.py helpers) so that the
hub keeps full history for the employee while the DB at rest never holds raw PII in plaintext.
- Message.content_sent is what the provider actually received (tokens only) -> safe to store plain.
- VaultEntry maps token -> encrypted original, scoped to a conversation, with expiry from Policy.
- AuditEvent never stores raw values: only entity types, counts and sha256 hashes.
- Policy is a single-row table (single-tenant). Admin edits it from /admin/policy.
"""

from datetime import UTC, datetime
from enum import StrEnum

from polanon.allowlist import WELL_KNOWN_NAMES
from polanon.policy import ENTITY_TYPES
from polanon.types import JudgeFallback
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class Role(StrEnum):
    """Access level of an account."""

    USER = "user"
    ADMIN = "admin"


class User(SQLModel, table=True):
    """An employee or administrator who signs in to the hub."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    role: Role = Role.USER
    language: str = "pl"
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    #: Raised to end every session the account has: a new password, an administrator's reset,
    #: disabling or enabling the account, signing out. A session cookie names the epoch it was
    #: issued in and stops working once the account has moved past it.
    session_epoch: int = 0


class Conversation(SQLModel, table=True):
    """A chat thread owned by one user."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    #: Empty until the first message; the interface renders a translated placeholder.
    title: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MessageRole(StrEnum):
    """Who produced a message."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class Message(SQLModel, table=True):
    """One message, stored encrypted alongside the tokenized text that was sent."""

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    role: MessageRole
    content_original_enc: str  # Fernet-encrypted plaintext as the employee sees it
    content_sent: str = (
        ""  # tokenized text that left the network (user msgs) / raw provider output (assistant)
    )
    entity_count: int = 0
    judge_hits: int = 0  # how many extra spans stage 2 added on top of stage 1
    created_at: datetime = Field(default_factory=utcnow)


class FileStatus(StrEnum):
    """Where an upload sits in the review workflow."""

    PENDING_REVIEW = (
        "pending_review"  # extracted + anonymized, waiting for employee approval (per policy)
    )
    APPROVED = "approved"
    REJECTED = "rejected"


class FileAsset(SQLModel, table=True):
    """An uploaded document, its extracted text and its masked counterpart."""

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    user_id: int = Field(foreign_key="user.id")
    filename: str  # masked, as the provider saw it; safe in plaintext
    filename_enc: str = ""  # the name it was uploaded under, encrypted
    mime_type: str
    size_bytes: int
    storage_path: str  # under settings.upload_dir; original bytes, access-controlled
    text_original_enc: str = ""  # extracted text, encrypted
    text_sent: str = ""  # tokenized text attached to the LLM context
    status: FileStatus = FileStatus.PENDING_REVIEW
    created_at: datetime = Field(default_factory=utcnow)


class PendingTurn(SQLModel, table=True):
    """A message accepted by the send endpoint and waiting for its stream to start.

    Kept in the database rather than in process memory so the hub works with more than one worker,
    and encrypted because it holds the employee's original text for those few seconds.

    The extra terms are encrypted for the same reason and not a weaker one: they are the words the
    employee singled out as the ones that must not leave, which makes them the last thing that
    should sit in a column in the clear.
    """

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    text_enc: str
    extra_terms_enc: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class VaultEntry(SQLModel, table=True):
    """token -> original mapping. One row per (conversation, token)."""

    id: int | None = Field(default=None, primary_key=True)
    conversation_id: int = Field(foreign_key="conversation.id", index=True)
    token: str = Field(index=True)  # e.g. <OSOBA_1>
    entity_type: str  # PERSON, PESEL, NIP, ORG, SECRET, ...
    value_enc: str  # Fernet-encrypted original
    value_hash: str  # HMAC-SHA256(value) keyed from the vault key, for audit correlation
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None  # from Policy.vault_ttl_days; None = never


class AuditEvent(SQLModel, table=True):
    """One recorded action; holds counts and hashes, never personal data."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int | None = Field(default=None, foreign_key="user.id")
    conversation_id: int | None = None
    # e.g. login, message_sent, file_uploaded, file_approved, policy_changed, output_sweep_hit
    event: str
    provider: str = ""
    model: str = ""
    entity_counts_json: str = "{}"  # {"PERSON": 2, "PESEL": 1}
    stage2_hits: int = 0
    detail: str = ""  # free text, MUST NOT contain raw PII
    created_at: datetime = Field(default_factory=utcnow)


class HilMode(StrEnum):
    """Whether a channel sends immediately or waits for the employee to approve the mask."""

    AUTO = "auto"  # send immediately, preview available afterwards
    REVIEW = "review"  # show mask, require approval before sending


#: The longest retention a policy accepts, in days: a hundred years, "practically forever". A date
#: that far ahead still fits a datetime; 999,999 days did not, and retention raised on every pass.
MAX_TTL_DAYS = 36_500

#: Entity types a new policy leaves unmasked. Dates are rarely identifying on their own and masking
#: them degrades answers; an admin who handles birth dates can switch DATE on.
OFF_BY_DEFAULT = frozenset({"DATE"})

#: Entity types a new policy masks: every type the library knows except the ones above. Derived
#: rather than listed, so a type added to `polanon.policy.ENTITY_TYPES` is on without a second edit
#: here. With two lists, a hub that reloaded between the edits recorded the new types as offered and
#: left them off.
DEFAULT_ENABLED_ENTITIES = ",".join(name for name in ENTITY_TYPES if name not in OFF_BY_DEFAULT)


class Policy(SQLModel, table=True):
    """Single-row admin policy. Load with policy.get_policy(session)."""

    id: int | None = Field(default=None, primary_key=True)
    hil_chat: HilMode = HilMode.AUTO
    hil_files: HilMode = HilMode.REVIEW
    judge_enabled: bool = True
    judge_fallback: JudgeFallback = JudgeFallback.SEND_STAGE1
    vault_ttl_days: int | None = 30  # None = keep forever
    history_ttl_days: int | None = None
    # Comma-separated entity types enabled for masking; see polanon.policy.ENTITY_TYPES.
    enabled_entities: str = DEFAULT_ENABLED_ENTITIES
    # Comma-separated entity types that existed when this policy was last reconciled with the code,
    # so a type a later release adds can be switched on without undoing what the admin unticked.
    # Empty on a policy saved before the field existed; see policy_loader.get_policy.
    entity_types_known: str = ""
    # newline-separated company names / custom strings always masked as ORG
    denylist: str = ""
    # newline-separated public names (platforms, tools) left unmasked when a guard or the judge
    # flags them as a name; see polanon.allowlist. Identifiers and the denylist are never affected.
    allowlist: str = "\n".join(WELL_KNOWN_NAMES)
    # Newline-separated names of polanon's WELL_KNOWN_NAMES this policy has been offered, so a name
    # a later release adds joins the allowlist without bringing back one the admin removed. Empty on
    # a policy saved before the field existed; see policy_loader.get_policy.
    well_known_names_known: str = ""
    updated_at: datetime = Field(default_factory=utcnow)
