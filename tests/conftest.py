"""Shared test fixtures.

Tests run against SQLite rather than Postgres so the whole suite executes with no Docker and
no network. The models avoid dialect-specific types precisely to make this possible.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "evaluation" / "fixtures"

JOB_FIXTURE_URL = (FIXTURES / "job.html").as_uri()
APPLY_FIXTURE_URL = (FIXTURES / "apply.html").as_uri()


def _configure_environment(tmp_path: Path) -> None:
    os.environ["LA_DATABASE_URL"] = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    os.environ["LA_DATA_DIR"] = str(tmp_path)
    os.environ["LA_DRY_RUN"] = "true"
    os.environ["LA_HEADLESS"] = "true"
    os.environ["LA_REASONER"] = "stub"


@pytest.fixture(scope="session", autouse=True)
def _environment(tmp_path_factory):
    _configure_environment(tmp_path_factory.mktemp("localapply"))
    from localapply.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings():
    from localapply.config import get_settings

    return get_settings()


@pytest_asyncio.fixture(autouse=True)
async def database(settings):
    from localapply.db.session import create_all, dispose_engine, init_engine

    settings.ensure_dirs()
    init_engine(settings)
    await create_all()
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def armed_kill_switch():
    """Every test starts with automation armed, and leaves it that way."""
    from localapply.safety import KILL_SWITCH

    KILL_SWITCH.reset()
    yield
    KILL_SWITCH.reset()


# --------------------------------------------------------------------------------------
# Contract builders — used by the pure unit tests, which need no browser and no database.
# --------------------------------------------------------------------------------------


@pytest.fixture
def make_element():
    from localapply.contracts import ElementRole, ObservedElement

    def _make(ref="e1", name="First name", role=ElementRole.TEXTBOX, **kw):
        return ObservedElement(ref=ref, role=role, name=name, **kw)

    return _make


@pytest.fixture
def make_observation():
    from localapply.contracts import Observation, PageKind

    def _make(elements=(), *, page_kind=PageKind.APPLICATION_FORM, text="", url="file:///x"):
        return Observation(
            run_id=uuid4(),
            url=url,
            title="Apply",
            page_kind=page_kind,
            elements=list(elements),
            untrusted_text=text,
        )

    return _make


@pytest.fixture
def application_context():
    from localapply.policy.capabilities import capabilities_for
    from localapply.policy.rules import RunContext

    return RunContext(agent="application", capabilities=capabilities_for("application"))


@pytest.fixture
def engine():
    from localapply.policy.engine import PolicyEngine

    return PolicyEngine()


# --------------------------------------------------------------------------------------
# Browser-backed fixtures
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def browser_manager(settings):
    """Skips the test rather than failing when Playwright browsers are not installed."""
    from localapply.browser.session import BrowserManager

    manager = BrowserManager(settings)
    try:
        await manager.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Playwright browser unavailable ({exc.__class__.__name__}). "
                    "Run: playwright install chromium")
    yield manager
    await manager.stop()


@pytest_asyncio.fixture
async def run_manager(settings, browser_manager):
    from localapply.ai.reasoner import StubReasoner
    from localapply.browser.executor import BrowserExecutor
    from localapply.browser.observer import Observer
    from localapply.events.bus import EventBus
    from localapply.orchestrator.run_loop import RunManager
    from localapply.policy.engine import PolicyEngine

    return RunManager(
        settings=settings,
        browser=browser_manager,
        observer=Observer(settings),
        reasoner=StubReasoner(),
        policy=PolicyEngine(),
        executor=BrowserExecutor(settings),
        bus=EventBus(),
    )


@pytest.fixture
def seeded_context():
    """A reasoning context with the same shape dev_bootstrap.py seeds."""
    from localapply.ai.reasoner import ReasoningContext

    return ReasoningContext(
        goal="Complete this job application.",
        profile={
            "first_name": "Haidar",
            "last_name": "Farhat",
            "full_name": "Haidar Farhat",
            "email": "you@example.com",
            "phone": "+961 00 000 000",
            "city": "Beirut",
            "linkedin_url": "https://www.linkedin.com/in/example",
            "github_url": "https://github.com/example",
            "resume_path": str(FIXTURES / "sample-cv.txt"),
        },
        drafts={
            "salary": "USD 4,500 / month",
            "work authorisation": "Yes",
            "availability": "One month",
            "free-text narrative": "Placeholder.",
        },
    )
