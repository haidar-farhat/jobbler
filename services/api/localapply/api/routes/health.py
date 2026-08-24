"""Per-subsystem health, for the dashboard's status strip."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ...config import Settings
from ...db.session import get_engine
from ...safety import KILL_SWITCH
from ..deps import get_app_settings, get_run_manager

router = APIRouter(tags=["health"])


async def _database_ok() -> tuple[bool, str | None]:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # noqa: BLE001 - health checks report, never raise
        return False, f"{exc.__class__.__name__}: {exc}"


async def _redis_ok(settings: Settings) -> tuple[bool, str | None]:
    try:
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url)
        try:
            await client.ping()
            return True, None
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        return False, f"{exc.__class__.__name__}: {exc}"


@router.get("/health")
async def health(
    settings: Settings = Depends(get_app_settings),
    runs=Depends(get_run_manager),
) -> dict:
    db_ok, db_error = await _database_ok()
    redis_ok, redis_error = await _redis_ok(settings)

    active = [h.snapshot() for h in runs.runs.values() if h.status in {"running", "paused"}]
    waiting = [h.snapshot() for h in runs.runs.values() if h.status == "waiting_approval"]

    return {
        "status": "ok" if db_ok else "degraded",
        "subsystems": {
            "database": {"ok": db_ok, "error": db_error},
            "redis": {"ok": redis_ok, "error": redis_error},
            "browser": {
                "ok": True,
                "sessions": runs.browser.session_count,
                "max_sessions": settings.max_browser_sessions,
            },
            "ai": {"ok": True, "reasoner": settings.reasoner},
        },
        "safety": {
            "kill_switch": KILL_SWITCH.status(),
            # The single most important thing on this page: whether a submit would be real.
            "dry_run": settings.dry_run,
        },
        "runs": {"active": active, "waiting_approval": waiting, "total": len(runs.runs)},
    }
