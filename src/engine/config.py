"""Typed settings, read once at startup and validated eagerly.

Nothing outside this module reads os.environ.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # core
    app_env: str = "development"
    log_level: str = "info"

    # storage
    database_url: str = "sqlite:///data/engine.db"
    data_dir: Path = Path("data")

    # ingestion
    apis_guru_list_url: str = "https://api.apis.guru/v2/list.json"
    ingest_spec_limit: int = 100
    # Guards against one enormous API (Azure, AWS) swamping the corpus. GitHub has
    # ~845 operations, so this must sit above that or the demo endpoints get cut.
    ingest_max_endpoints_per_api: int = 1200

    # llm
    llm_provider: str = "gemini"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.6-flash"
    # Embeddings are configured separately from the chat model: retrieval runs
    # locally so the corpus can be embedded without an API quota, while planning
    # still uses the hosted model.
    embedding_provider: str = "local"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimensions: int = 384
    llm_timeout_seconds: int = 60
    embedding_batch_size: int = 50
    # Free-tier embedding quota is per-minute; pacing requests is cheaper than
    # absorbing 429s and backing off.
    embedding_request_delay_ms: int = 2000
    planner_max_repair_iterations: int = 3

    # search
    search_top_k: int = 20
    rrf_k: int = 60
    # Synonym expansion trades precision for recall and the trade is not
    # obviously positive — it helped one probe query and hurt another. Day 3
    # benchmarks both settings against the labeled query set instead of
    # guessing from anecdotes.
    search_expand_synonyms: bool = True

    # execution
    http_timeout_seconds: int = 30
    http_max_retries: int = 3
    http_backoff_base_ms: int = 250
    execution_allowlist_only: bool = True
    ssrf_block_private_ranges: bool = True
    queue_max_deliveries: int = 5
    queue_lease_seconds: int = 300
    execution_allowlist: str = "httpbin.org,slack.com,github.com"

    # credentials
    # OAuth 2.0 client credentials. These identify *this application* to the
    # provider and cannot be generated here — the user registers an app with
    # the provider and pastes the pair in. The secret never leaves the server.
    google_client_id: str = ""
    google_client_secret: str = ""
    oauth_redirect_uri: str = "http://localhost:3000/oauth/callback"

    credential_encryption_key: str = ""
    credential_key_version: int = 1

    # dev-only
    dev_slack_webhook_url: str = ""
    dev_github_token: str = ""

    @field_validator("data_dir", mode="after")
    @classmethod
    def _absolutize(cls, v: Path) -> Path:
        return v if v.is_absolute() else REPO_ROOT / v

    @property
    def specs_dir(self) -> Path:
        return self.data_dir / "specs"

    @property
    def embeddings_path(self) -> Path:
        return self.data_dir / "embeddings.npz"

    @property
    def allowlisted_providers(self) -> set[str]:
        return {p.strip() for p in self.execution_allowlist.split(",") if p.strip()}

    @property
    def sqlalchemy_url(self) -> str:
        """Resolve a relative sqlite path against the repo root."""
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            raw = self.database_url[len(prefix) :]
            path = Path(raw)
            if not path.is_absolute():
                path = REPO_ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True)
            return f"{prefix}{path}"
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.specs_dir.mkdir(parents=True, exist_ok=True)
    return settings


settings: Settings = get_settings()

__all__ = ["Settings", "get_settings", "settings", "REPO_ROOT", "Field"]
