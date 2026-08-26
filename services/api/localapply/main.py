"""Composition root and FastAPI app.

Every dependency is constructed here and nowhere else, so swapping the stub reasoner for a
real model, or Playwright for something else, is a change to this file alone.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .ai.providers.ollama import OllamaProvider
from .ai.providers.stub import StubProvider
from .ai.reasoner import LLMReasoner, StubReasoner
from .ai.router import ModelRouter
from .api.routes import (
    agent,
    approvals,
    backup,
    documents,
    events,
    generate,
    health,
    jobs,
    profile,
    searches,
)
from .browser.executor import BrowserExecutor
from .browser.observer import Observer
from .browser.session import BrowserManager
from .config import get_settings
from .db.session import dispose_engine, init_engine
from .events.bus import EVENT_BUS
from .notify import build as build_notifications
from .orchestrator.run_loop import RunManager
from .policy.engine import PolicyEngine
from .security import is_loopback

#: jobbler/ — the repo root, four levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "evaluation" / "fixtures"
STATIC_DIR = Path(__file__).resolve().parent / "static"


async def resolve_reasoner(settings) -> str:
    """Turn `LA_REASONER=auto` into a concrete choice by looking for a usable model.

    Explicit settings are honoured exactly, including "ollama" when Ollama is down -- if you
    asked for the model, a silent downgrade to the scripted reasoner would be worse than a
    red light on the dashboard telling you it is missing.
    """
    configured = (settings.reasoner or "auto").strip().lower()
    if configured != "auto":
        return configured

    try:
        provider = OllamaProvider(settings.ollama_base_url)
        if not await provider.health():
            return "stub"
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(f"{settings.ollama_base_url}/api/tags")
            models = response.json().get("models", [])
        return "ollama" if models else "stub"
    except Exception:  # noqa: BLE001 - detection must never stop the app starting
        return "stub"


def build_model_router(settings, resolved: str) -> ModelRouter:
    """One router per process. Its exclusive lock is what keeps two coroutines from racing
    a model swap on a single 8 GB card, so there must not be a second instance."""
    provider = (
        OllamaProvider(settings.ollama_base_url) if resolved == "ollama" else StubProvider()
    )
    return ModelRouter(provider, vram_budget_mb=settings.vram_budget_mb)


def build_run_manager(settings, router: ModelRouter, resolved: str) -> RunManager:
    reasoner = LLMReasoner(router) if resolved == "ollama" else StubReasoner()

    return RunManager(
        settings=settings,
        browser=BrowserManager(settings),
        observer=Observer(settings),
        reasoner=reasoner,
        policy=PolicyEngine(),
        executor=BrowserExecutor(settings),
        bus=EVENT_BUS,
        # An approval blocks its run until a person answers. Without this nothing said
        # so, and a run started before lunch was still parked at six.
        notifier=build_notifications(settings),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Before anything binds or connects. A refusal here is the whole point.
    _refuse_unguarded_remote_access(settings)
    settings.ensure_dirs()
    init_engine(settings)

    app.state.settings = settings
    app.state.bus = EVENT_BUS
    # Resolved once at startup: probing on every request would add latency to /health and
    # could flip the reasoner mid-run.
    resolved = await resolve_reasoner(settings)
    app.state.reasoner = resolved
    app.state.router = build_model_router(settings, resolved)
    app.state.runs = build_run_manager(settings, app.state.router, resolved)

    yield

    await app.state.runs.stop_all("Server shutting down")
    await dispose_engine()


def _install_token_gate(app: FastAPI, settings) -> None:
    """Put the whole API behind one token, except this machine talking to itself.

    Middleware rather than a per-route dependency, deliberately. A dependency has to be
    added to every route, which means the next route someone writes is unprotected by
    default -- and the route most likely to be forgotten is the new one nobody has reviewed.
    A gate everything passes through fails closed instead.
    """
    from .security import Gate, TokenGuard, refuse, token_from

    guard = TokenGuard(
        settings.api_token,
        require_on_loopback=settings.require_token_on_loopback,
    )
    app.state.token_guard = guard

    @app.middleware("http")
    async def gate(request, call_next):
        verdict: Gate = guard.check(
            path=request.url.path,
            peer=request.client.host if request.client else None,
            presented=token_from(request),
        )
        if not verdict.allowed:
            error = refuse()
            return JSONResponse(
                status_code=error.status_code,
                content={"detail": error.detail},
                headers=error.headers,
            )
        return await call_next(request)


def _refuse_unguarded_remote_access(settings) -> None:
    """Do not start listening on a network without a token.

    This is the one ordering constraint in the whole roadmap. `GET /profile` returns every
    accepted fact -- name, phone, address, salary answers -- and remote access is a phase
    that is already planned. Binding to anything but loopback with no token turns a local
    app into a public one, quietly, at the moment someone edits a config line.
    """
    host = (settings.bind_host or "").strip()
    if not host or is_loopback(host):
        return
    if settings.api_token.strip():
        return
    raise RuntimeError(
        f"Refusing to start: LA_BIND_HOST is {host!r}, which is reachable from other "
        "machines, but LA_API_TOKEN is empty. Anyone who can reach this port could read "
        "your entire profile. Set a token first."
    )


def _register_error_handlers(app: FastAPI) -> None:
    """Map the pipeline's domain errors onto HTTP once, centrally.

    Registering these here is what lets every jobs handler stay a thin shell: a step that
    asks for an illegal move raises, and the caller gets a 409 whose body names the states
    that *are* legal from where the job actually is -- rather than each handler wrapping
    every call in a try/except and paraphrasing the machine.
    """
    from fastapi.responses import JSONResponse

    from .jobs.ingest import UnsafeURL
    from .jobs.pipeline import WrongState
    from .orchestrator.state_machine import InvalidTransition

    async def _conflict(_request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    async def _bad_request(_request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.add_exception_handler(InvalidTransition, _conflict)
    app.add_exception_handler(WrongState, _conflict)
    app.add_exception_handler(UnsafeURL, _bad_request)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="LocalApply",
        version="0.1.0",
        description="Local-first autonomous job-application workstation.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(profile.router)
    app.include_router(documents.router)
    app.include_router(generate.router)
    app.include_router(agent.router)
    app.include_router(jobs.router)
    app.include_router(searches.router)
    app.include_router(backup.router)
    app.include_router(approvals.router)
    app.include_router(events.router)

    _install_token_gate(app, settings)
    _register_error_handlers(app)

    # Screenshots the observer captured, for the dashboard's live view.
    settings.ensure_dirs()
    app.mount(
        "/screenshots",
        StaticFiles(directory=str(settings.screenshot_dir)),
        name="screenshots",
    )

    # The practice job posting the walking skeleton runs against. Served from here so the
    # whole stack is one process plus two containers, with no live job site involved.
    if FIXTURES_DIR.is_dir():
        app.mount("/fixtures", StaticFiles(directory=str(FIXTURES_DIR)), name="fixtures")

    # The dashboard. One UI, served from here, needing no Node -- so "start the app" is one
    # process, which is what the launcher depends on.
    #
    # There used to be a second one: a React app in apps/web that this would serve instead
    # whenever `apps/web/dist` existed. It was deleted, and the branch with it. Two reasons.
    # It had become a strict subset -- no Jobs board, no watched boards, no outcomes, no
    # entry editor, no backup -- while calling endpoints whose shapes had since moved. And
    # the swap was silent: running `pnpm build` once would have replaced a working dashboard
    # with one missing most of the application, and nothing would have said why.
    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    return app


app = create_app()
