"""Who is allowed to talk to this API.

Until this, anyone was. `GET /profile` returned every accepted fact -- name, email, phone,
address, salary answers -- to whatever asked, and an adversarial review demonstrated a
working exfiltration through exactly that: a job posting that redirected the ingesting
browser to `http://localhost./profile` and read the whole fact set back out.

The URL guard that allowed the redirect is fixed. This is the other half: the reason it
*worked* is that the endpoint answers strangers.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from localapply.security import PUBLIC_PATHS, TokenGuard, is_loopback, new_token

TOKEN = "test-token-not-a-real-secret"


@pytest.fixture
def guard():
    return TokenGuard(TOKEN)


# --------------------------------------------------------------------------------------
# The decision, on its own
# --------------------------------------------------------------------------------------


def test_a_stranger_with_no_token_is_refused(guard):
    assert not guard.check(path="/profile", peer="10.0.0.5", presented=None).allowed


def test_a_stranger_with_the_wrong_token_is_refused(guard):
    assert not guard.check(path="/profile", peer="10.0.0.5", presented="guess").allowed


def test_a_stranger_with_the_token_is_allowed(guard):
    assert guard.check(path="/profile", peer="10.0.0.5", presented=TOKEN).allowed


def test_this_machine_needs_no_token(guard):
    """Requiring one to use the app on the machine it runs on means a login screen for a
    local dashboard, and people turn those off."""
    assert guard.check(path="/profile", peer="127.0.0.1", presented=None).allowed


def test_loopback_can_be_made_to_need_one_too():
    strict = TokenGuard(TOKEN, require_on_loopback=True)
    assert not strict.check(path="/profile", peer="127.0.0.1", presented=None).allowed
    assert strict.check(path="/profile", peer="127.0.0.1", presented=TOKEN).allowed


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "localhost", "127.0.0.53", "LOCALHOST."])
def test_every_form_of_this_machine_is_recognised(peer):
    """A browser sends whichever of these it feels like, and they are the same machine."""
    assert is_loopback(peer)


@pytest.mark.parametrize("peer", ["10.0.0.5", "192.168.1.9", "8.8.8.8", "", None, "evil.com"])
def test_nothing_else_is_mistaken_for_this_machine(peer):
    assert not is_loopback(peer)


@pytest.mark.parametrize("path", sorted(PUBLIC_PATHS))
def test_health_and_docs_answer_without_a_token(guard, path):
    """A health check that needs a secret is one nobody can use when things are wrong --
    which is exactly when it is needed."""
    assert guard.check(path=path, peer="10.0.0.5", presented=None).allowed


def test_no_token_configured_means_no_gate():
    """The old behaviour, kept reachable on purpose: someone running with no token set is
    running the app the way it worked before, and should not be locked out of it."""
    assert TokenGuard("").check(path="/profile", peer="10.0.0.5", presented=None).allowed


def test_a_generated_token_is_long_enough_that_guessing_is_not_a_strategy():
    token = new_token()
    assert len(token) >= 32
    assert token != new_token()


def test_the_token_is_compared_in_constant_time():
    """`==` on a secret leaks its prefix to anyone who can time the response, and over a
    private network that is a real attack rather than a theoretical one."""
    import inspect

    source = inspect.getsource(TokenGuard.check)
    assert "compare_digest" in source
    assert "presented == self._token" not in source


# --------------------------------------------------------------------------------------
# Through the app
# --------------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def guarded(settings):
    """The app with a token set, answering as if from another machine."""
    from localapply.events.bus import EventBus
    from localapply.main import create_app

    settings.api_token = TOKEN
    app = create_app()
    app.state.settings = settings
    app.state.bus = EventBus()
    app.state.runs = None
    # `create_app` reads the settings at build time, so the guard is installed from the
    # process settings; re-point it at this test's token.
    from localapply.security import TokenGuard as Guard

    app.state.token_guard = Guard(TOKEN)

    # `client=` is what makes this a request from somewhere else. Without it the peer is
    # loopback and every one of these tests passes for the wrong reason.
    transport = ASGITransport(app=app, client=("10.0.0.5", 1234))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_the_profile_is_not_readable_by_a_stranger(guarded, settings):
    """The endpoint the demonstrated exfiltration read."""
    if not guarded._transport.app.state.token_guard.enabled:
        pytest.skip("no token configured in this build")
    response = await guarded.get("/profile")
    assert response.status_code == 401
    # The refusal says nothing about what exists behind it.
    assert "token" in response.json()["detail"].lower()
    assert "profile" not in response.json()["detail"].lower()


async def test_health_still_answers_a_stranger(guarded):
    """What a launcher polls, and what tells you the app is up at all."""
    assert (await guarded.get("/health")).status_code == 200


async def test_the_token_opens_the_door(guarded):
    response = await guarded.get("/profile", headers={"X-LocalApply-Token": TOKEN})
    assert response.status_code == 200


async def test_a_bearer_token_works_too(guarded):
    """Because that is what every client already knows how to send."""
    response = await guarded.get("/profile", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


async def test_the_query_parameter_works_for_the_event_stream(guarded):
    """EventSource cannot set headers. This is the one case, and it is documented as such
    rather than quietly accepted."""
    response = await guarded.get(f"/profile?token={TOKEN}")
    assert response.status_code == 200


async def test_a_new_route_is_behind_the_gate_by_default(guarded):
    """Middleware, not a per-route dependency. A dependency has to be remembered on every
    route, and the one most likely to be forgotten is the new one nobody has reviewed."""
    for path in ("/jobs", "/searches", "/generate", "/documents", "/approvals"):
        assert (await guarded.get(path)).status_code == 401, path


# --------------------------------------------------------------------------------------
# The ordering constraint
# --------------------------------------------------------------------------------------


def test_binding_to_a_network_without_a_token_refuses_to_start(settings):
    """The one hard ordering constraint in the roadmap: auth ships before remote access.
    Binding to anything but loopback with no token turns a local app into a public one,
    quietly, the moment someone edits a config line."""
    from localapply.main import _refuse_unguarded_remote_access

    settings.api_token = ""
    settings.bind_host = "0.0.0.0"

    with pytest.raises(RuntimeError) as caught:
        _refuse_unguarded_remote_access(settings)
    assert "entire profile" in str(caught.value)


def test_binding_to_a_network_with_a_token_is_fine(settings):
    from localapply.main import _refuse_unguarded_remote_access

    settings.api_token = TOKEN
    settings.bind_host = "0.0.0.0"
    _refuse_unguarded_remote_access(settings)


def test_loopback_without_a_token_is_still_fine(settings):
    """The default, and the way the app has always run."""
    from localapply.main import _refuse_unguarded_remote_access

    settings.api_token = ""
    settings.bind_host = "127.0.0.1"
    _refuse_unguarded_remote_access(settings)
