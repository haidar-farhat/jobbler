"""Settings. All environment variables use the `LA_` prefix (see `.env.example`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: jobbler/ -- four levels up from services/api/localapply/config.py.
REPO_ROOT = Path(__file__).resolve().parents[3]

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LA_",
        # Anchored to the repo root, not the working directory: alembic runs from
        # services/api, uvicorn from services/api, pytest from the root, and all three must
        # read the same .env. A bare ".env" silently resolves to a different file per caller.
        env_file=(REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage ---
    database_url: str = "postgresql+asyncpg://localapply:localapply@localhost:5433/localapply"
    redis_url: str = "redis://localhost:6380/0"
    data_dir: Path = REPO_ROOT / "var"

    # --- Safety -------------------------------------------------------------------
    #: When true, SUBMIT actions are recorded and simulated but never actually clicked.
    #: This is the last line of defence between a bug and a real job application.
    dry_run: bool = True
    #: Hard ceiling on executed actions per run. A runaway loop hits this and stops.
    max_actions_per_run: int = 120
    #: Decisions below this confidence require human approval rather than executing.
    min_decision_confidence: float = 0.60

    # --- Browser ---
    headless: bool = False
    browser_timeout_ms: int = 15_000
    max_browser_sessions: int = 3

    # --- AI ---
    #: "stub" = scripted deterministic reasoner (walking skeleton). "ollama" = local models.
    reasoner: str = "stub"
    ollama_base_url: str = "http://localhost:11434"
    #: Measured on the target machine: RTX 5070 Laptop, 8151 MiB. Models load *sequentially*
    #: because a reasoning model (~5 GB) and a vision model (~4 GB) cannot be co-resident.
    vram_budget_mb: int = 8151

    # --- Server ---
    cors_origins: list[str] = ["http://localhost:5173"]

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    def ensure_dirs(self) -> None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
