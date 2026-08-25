"""Per-subsystem health, for the dashboard's status strip."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ...config import Settings
from ...db.session import get_engine
from ...safety import KILL_SWITCH
from ..deps import (
    get_app_settings,
    get_resolved_reasoner,
    get_router,
    get_run_manager,
)

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


def _browser_status(runs, settings: Settings) -> dict:
    """Report an unusable event loop here rather than letting a run die on it."""
    from ...browser.session import BrowserManager

    try:
        problem = BrowserManager.check_event_loop()
    except RuntimeError:
        problem = None  # no running loop to inspect

    return {
        "ok": problem is None and runs is not None,
        "error": problem or (None if runs is not None else "no run manager"),
        "sessions": runs.browser.session_count if runs is not None else 0,
        "max_sessions": settings.max_browser_sessions,
        "headless": settings.headless,
        **({"error": problem} if problem else {}),
    }


async def _ai_status(settings: Settings, model_router, resolved: str) -> dict:
    """Report the AI engine honestly, including which model is actually resident.

    On an 8 GB card only one large model is loaded at a time, so "which one is in VRAM
    right now" is real operational information rather than a static config echo.
    """
    status: dict = {
        "ok": True,
        "reasoner": resolved,
        # Both, so "auto" is visible as a choice rather than looking like a hardcoded value.
        "configured": settings.reasoner,
    }
    if model_router is None:
        return status

    report = model_router.vram_report()
    status |= {
        "resident": report.resident,
        "vram_used_mb": report.used_mb,
        "vram_budget_mb": report.budget_mb,
        "vram_free_mb": report.free_mb,
        "model_swaps": model_router.stats.swaps,
        "mean_swap_ms": model_router.stats.mean_swap_ms,
    }

    if resolved == "ollama":
        try:
            reachable = await model_router.provider.health()
        except Exception:  # noqa: BLE001
            reachable = False
        status["ok"] = reachable
        if not reachable:
            status["error"] = (
                f"Ollama is not answering at {settings.ollama_base_url}. "
                "Start it, or set LA_REASONER=stub to run without a model."
            )
    return status


@router.get("/health")
async def health(
    settings: Settings = Depends(get_app_settings),
    runs=Depends(get_run_manager),
    model_router=Depends(get_router),
    resolved: str = Depends(get_resolved_reasoner),
) -> dict:
    db_ok, db_error = await _database_ok()
    redis_ok, redis_error = await _redis_ok(settings)

    # A health check that crashes when a subsystem is absent is the least useful thing in
    # the building: the moment it matters most is exactly the moment something is missing.
    # No run manager is reported as a missing run manager, not as a 500.
    handles = list(runs.runs.values()) if runs is not None else []
    active = [h.snapshot() for h in handles if h.status in {"running", "paused"}]
    waiting = [h.snapshot() for h in handles if h.status == "waiting_approval"]

    return {
        "status": "ok" if db_ok and runs is not None else "degraded",
        "subsystems": {
            "database": {"ok": db_ok, "error": db_error},
            "redis": {"ok": redis_ok, "error": redis_error},
            "browser": _browser_status(runs, settings),
            "ai": await _ai_status(settings, model_router, resolved),
        },
        "access": {
            # Whether this is reachable from anywhere but here, and whether that is safe.
            "token_required": bool(settings.api_token.strip()),
            "bound_to": settings.bind_host,
            "reachable_from_elsewhere": settings.bind_host not in {"127.0.0.1", "::1"},
        },
        "safety": {
            "kill_switch": KILL_SWITCH.status(),
            # The single most important thing on this page: whether a submit would be real.
            "dry_run": settings.dry_run,
        },
        "runs": {"active": active, "waiting_approval": waiting, "total": len(handles)},
    }
