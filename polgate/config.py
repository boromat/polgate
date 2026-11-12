"""Application settings, loaded from environment / .env via pydantic-settings."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration, read from the environment and the .env file."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # security
    #: Signs session cookies. Empty: a key is generated once and kept beside the database.
    secret_key: str = ""
    vault_key: str = (
        ""  # Fernet key (base64). Empty -> generated at startup and stored in data/vault.key
    )
    #: Send session cookies only over HTTPS, and add HSTS. Turn on for any real deployment.
    cookie_secure: bool = False
    session_max_age_days: int = 14
    #: Failed sign-ins tolerated per account inside the lockout window before it is refused.
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    # cloud LLM (OpenAI-compatible; OpenRouter by default)
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-opus-5.5"

    # stage 2 judge
    judge_backend: str = "mock"  # mock | ollama | remote
    ollama_base_url: str = "http://localhost:11434"
    ollama_judge_model: str = "qwen2.5:1.5b"
    judge_timeout_seconds: float = 20.0

    # first admin
    admin_username: str = "admin"
    #: First administrator's password. Empty: one is generated and written beside the database, to
    #: the file `initial-admin-password`, readable by the service account only.
    admin_password: str = ""

    # storage / retention
    #: A queued message whose stream never started is dropped after this long.
    pending_turn_ttl_minutes: int = 60
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'polgate.db'}"
    upload_dir: Path = BASE_DIR / "data" / "uploads"

    # context budget
    #: Characters of earlier conversation replayed to the provider on each turn. Older turns are
    #: dropped rather than growing the request until the model refuses it.
    history_budget_chars: int = 24_000
    #: Characters of attached document text sent per file, per turn. A long PDF would otherwise be
    #: resent in full with every single message.
    attachment_budget_chars: int = 60_000

    # search in attachments (RAG): a file over the budget above sends the passages that match the
    # question instead of its first characters
    #: Embedding model at the endpoint below. Empty switches the search off, so no text goes to an
    #: embeddings endpoint until somebody chooses one.
    embedding_model: str = ""
    #: OpenAI-compatible API root for /embeddings. Empty: the chat provider's `llm_base_url`.
    embedding_base_url: str = ""
    #: Key for that endpoint. Empty: the chat key when the chat endpoint is used, otherwise none, so
    #: the chat provider's key never travels to another host.
    embedding_api_key: str = ""
    #: Passages sent to the embeddings endpoint per request.
    embedding_batch_size: int = 64
    #: Target length of one passage, in characters.
    retrieval_chunk_chars: int = 1_200
    #: Passages indexed per file. The rest of a longer file is not searched, and the employee and
    #: the model are told so.
    retrieval_max_chunks_per_file: int = 500

    # NLP
    spacy_model: str = "pl_core_news_md"

    # UI
    default_language: str = "pl"  # pl | en

    def embedding_endpoint(self) -> tuple[str, str]:
        """Return the API root and the key for /embeddings.

        Without an endpoint of its own the chat provider's is used, key included. With one, only
        its own key is sent: the chat key belongs to the chat provider and goes nowhere else.
        """
        if not self.embedding_base_url:
            return self.llm_base_url, self.embedding_api_key or self.llm_api_key
        return self.embedding_base_url, self.embedding_api_key


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
