"""A browser session, and the private ref -> element map that makes ADR 0002 work.

The map lives here and is exposed only through `resolve()`. The reasoner never sees it; the
executor can only ask "what is e17?" and gets `None` if e17 is not currently valid.

Refs are cleared and rebuilt on every observation, so a ref from a previous page fails closed
rather than resolving to whatever element now occupies that position.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from playwright.async_api import Browser, BrowserContext, Locator, Page, async_playwright

from ..config import Settings


class BrowserSession:
    """One page under agent control."""

    def __init__(self, page: Page, settings: Settings, session_id: UUID | None = None) -> None:
        self.session_id = session_id or uuid4()
        self._page = page
        self._settings = settings
        #: The authoritative set of refs valid *right now*. Private on purpose.
        self._known_refs: set[str] = set()
        self._epoch = 0

    @property
    def page(self) -> Page:
        return self._page

    @property
    def epoch(self) -> int:
        """Increments on every observation. Useful for spotting stale-ref bugs in the log."""
        return self._epoch

    def rebind(self, refs: set[str]) -> None:
        """Replace the valid ref set. Called by the observer, and by nothing else."""
        self._known_refs = set(refs)
        self._epoch += 1

    def invalidate(self) -> None:
        """Drop every ref -- e.g. after a navigation. Everything fails closed until the next
        observation."""
        self._known_refs.clear()

    def resolve(self, ref: str) -> Locator | None:
        """Ref -> Locator, or None if the ref is unknown or stale.

        This is the only way to get from a model-supplied string to something Playwright can
        act on, and it is bounded by what the observer actually enumerated.
        """
        if ref not in self._known_refs:
            return None
        return self._page.locator(f"[data-la-ref={ref!r}]")

    def knows(self, ref: str) -> bool:
        return ref in self._known_refs


class BrowserManager:
    """Owns the Playwright lifecycle and caps concurrent sessions (design doc §27)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: dict[UUID, BrowserContext] = {}
        self._sessions: dict[UUID, BrowserSession] = {}

    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._settings.headless)

    async def stop(self) -> None:
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @property
    def running(self) -> bool:
        return self._browser is not None

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    async def new_session(self) -> BrowserSession:
        if self._browser is None:
            await self.start()
        assert self._browser is not None
        if len(self._sessions) >= self._settings.max_browser_sessions:
            raise RuntimeError(
                f"Session cap reached ({self._settings.max_browser_sessions}). "
                "Close a session before opening another."
            )
        context = await self._browser.new_context(viewport={"width": 1440, "height": 900})
        context.set_default_timeout(self._settings.browser_timeout_ms)
        page = await context.new_page()
        session = BrowserSession(page, self._settings)
        self._contexts[session.session_id] = context
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: UUID) -> BrowserSession | None:
        return self._sessions.get(session_id)

    async def close_session(self, session_id: UUID) -> None:
        self._sessions.pop(session_id, None)
        context = self._contexts.pop(session_id, None)
        if context is not None:
            await context.close()
