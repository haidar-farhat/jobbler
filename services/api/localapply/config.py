"""Settings. All environment variables use the `LA_` prefix (see `.env.example`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: jobbler/ -- four levels up from services/api/localapply/config.py.
REPO_ROOT = Path(__file__).resolve().parents[3]


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

    # --- Access ---
    #: Generated on first run and written to .env. Empty disables the gate entirely, which
    #: is only sane while nothing but this machine can reach the API.
    api_token: str = ""
    #: Off by default: requiring a token to use the app on the machine it runs on means a
    #: login screen for a local dashboard, and people turn those off. The launcher turns it
    #: ON automatically whenever the API binds to anything but loopback.
    require_token_on_loopback: bool = False
    #: What the API binds to. Loopback only, until something deliberately changes it -- a
    #: local-first app has no business listening on 0.0.0.0 by default.
    bind_host: str = "127.0.0.1"

    # --- Telling you something is waiting ---
    #: A native toast on this machine. Free, no account, works offline.
    notify_desktop: bool = True
    #: An ntfy topic, for reaching a phone. No account and no API key -- which also means
    #: anyone who knows the topic can read your notifications, so make it long and random
    #: and never put a value in a notification.
    notify_ntfy_topic: str = ""
    notify_ntfy_url: str = "https://ntfy.sh"
    #: low | normal | high. "high" means approvals only, which is what most people want
    #: after the first week.
    notify_minimum_urgency: str = "normal"
    #: What a notification links to. Set this to your tunnel address once remote access is
    #: set up, so tapping the notification opens the approval rather than a dead link.
    public_url: str = ""

    # --- Reading job postings ---
    #: Minimum gap between two fetches of the same host. The first throttling of any kind in
    #: this package, and the reason it exists is the stated posture: a tool that reads other
    #: people's sites paces itself rather than being paced by them.
    ingest_min_interval_s: float = 5.0
    #: Off by default, and it matters. This API has no authentication, so a posting that
    #: redirects the browser to http://localhost:8000/profile would land the entire accepted
    #: fact set in a job description -- from where it flows into a prompt and then into a PDF
    #: bound for a stranger. Turn it on only to point the ingester at a local test server.
    ingest_allow_loopback: bool = False

    # --- AI ---
    #: "auto" (default) uses the local model when Ollama is running with one installed, and
    #: falls back to the scripted reasoner when it is not. Set "ollama" or "stub" to force
    #: one. Defaulting to "stub" meant a machine with a model sitting right there quietly
    #: ignored it, which is not a sensible default for anyone who bothered to install one.
    reasoner: str = "auto"
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
