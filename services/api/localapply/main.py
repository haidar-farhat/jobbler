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
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .ai.providers.ollama import OllamaProvider
from .ai.providers.stub import StubProvider
from .ai.reasoner import LLMReasoner, StubReasoner
from .ai.router import ModelRouter
from .api.routes import agent, approvals, documents, events, generate, health, profile
from .browser.executor import BrowserExecutor
from .browser.observer import Observer
from .browser.session import BrowserManager
from .config import get_settings
from .db.session import dispose_engine, init_engine
from .events.bus import EVENT_BUS
from .orchestrator.run_loop import RunManager
from .policy.engine import PolicyEngine

#: jobbler/ — the repo root, four levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = REPO_ROOT / "evaluation" / "fixtures"
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: Built React app, when someone has run `pnpm build`. Optional.
WEB_DIST = REPO_ROOT / "apps" / "web" / "dist"


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
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
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
    app.include_router(approvals.router)
    app.include_router(events.router)

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

    # The dashboard. The zero-build page in static/ needs no Node and is always available;
    # a built React app takes precedence when present. Serving the UI from the API means
    # "start the app" is one process, which is what the launcher depends on.
    if WEB_DIST.is_dir():
        app.mount("/app", StaticFiles(directory=str(WEB_DIST), html=True), name="web")

        @app.get("/", include_in_schema=False)
        async def root_built() -> RedirectResponse:
            return RedirectResponse("/app/")

    else:

        @app.get("/", include_in_schema=False)
        async def root() -> FileResponse:
            return FileResponse(STATIC_DIR / "dashboard.html")

    return app


app = create_app()
