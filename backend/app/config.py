"""Runtime configuration. Every secret comes from the environment; nothing is hard-coded."""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Runtime connection uses rag_app (NOBYPASSRLS). Migrations use rag_owner.
    database_url: str = "postgresql+psycopg://rag_app:change-me-app@localhost:5433/rag"
    owner_database_url: str = "postgresql+psycopg://rag_owner:change-me-owner@localhost:5433/rag"

    file_storage_root: str = "./data/files"
    session_secret: str = "dev-only-session-secret"
    session_ttl_hours: int = 12
    cookie_secure: bool = False

    demo_mode: bool = True
    embedding_provider: str = "local"  # local | hash
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384

    # Cloud model provider (KTD4). Keys are SecretStr so repr, model_dump and logs never show them.
    llm_provider: Literal["openai", "anthropic"] = DEFAULT_LLM_PROVIDER
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    # Resolved key: the first non-empty of OPENAI_KEY, then OPENAI_API_KEY (Compose injects both, maybe empty).
    openai_key: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_reasoning_effort: str = "none"  # empty means: send no reasoning setting

    # Per-purpose model call timeouts, all below the turn deadline.
    llm_timeout_interpret_seconds: float = 20
    llm_timeout_clarify_seconds: float = 15
    llm_timeout_extract_seconds: float = 30
    llm_timeout_answer_seconds: float = 30
    llm_timeout_verify_seconds: float = 20
    llm_timeout_test_seconds: float = 20

    # Turn limits (KTD8, KTD13).
    turn_deadline_seconds: int = 45
    extract_sync_max_versions: int = 6
    extract_async_max_pending: int = 50
    extract_doc_char_budget: int = 60_000

    max_upload_mb: int = 50
    max_pages: int = 300
    max_batch_files: int = 20
    max_render_pixels: int = 40_000_000
    max_docx_uncompressed_mb: int = 100
    job_timeout_seconds: int = 900
    job_lease_seconds: int = 120
    job_max_attempts: int = 3

    ocr_languages: str = "heb+eng"
    ocr_dpi: int = 300
    ocr_timeout_seconds: int = 60  # per Tesseract call; a hung call fails the page instead of the whole job


    @field_validator("llm_provider", "openai_model", mode="before")
    @classmethod
    def _empty_means_default(cls, value, info):
        if isinstance(value, str) and not value.strip():
            return cls.model_fields[info.field_name].default
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _resolve_openai_key(self) -> "Settings":
        for candidate in (self.openai_key, self.openai_api_key):
            if candidate.get_secret_value().strip():
                self.openai_api_key = SecretStr(candidate.get_secret_value().strip())
                break
        else:
            self.openai_api_key = SecretStr("")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
