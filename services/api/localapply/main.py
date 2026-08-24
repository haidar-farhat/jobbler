"""Composition root and FastAPI app.

Every dependency is constructed here and nowhere else, so swapping the stub reasoner for a
real model, or Playwright for something else, is a change to this file alone.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .ai.providers.ollama import OllamaProvider
from .ai.providers.stub import StubProvider
from .ai.reasoner import LLMReasoner, StubReasoner
from .ai.router import ModelRouter
from .api.routes import agent, approvals, events, health, profile
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


def build_run_manager(settings) -> RunManager:
    provider = (
        OllamaProvider(settings.ollama_base_url)
        if settings.reasoner == "ollama"
        else StubProvider()
    )
    router = ModelRouter(provider, vram_budget_mb=settings.vram_budget_mb)
    reasoner = LLMReasoner(router) if settings.reasoner == "ollama" else StubReasoner()

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
    app.state.runs = build_run_manager(settings)

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
    # whole stack is `docker compose up` + `uvicorn` + `pnpm dev`, with no third server and
    # no live job site involved.
    if FIXTURES_DIR.is_dir():
        app.mount("/fixtures", StaticFiles(directory=str(FIXTURES_DIR)), name="fixtures")

    return app


app = create_app()
