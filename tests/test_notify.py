"""Telling you something is waiting.

An approval blocks its run until a person answers. Until this, nothing said so -- a run
started before lunch was still parked at six, holding one of three capped browser sessions
and waiting for someone who did not know they were needed.

Two properties carry the weight, and both are about failure rather than success: a notifier
that breaks must not break the run, and a notification must not carry your salary
expectations to a third party.
"""

from __future__ import annotations

import pytest
from localapply.notify import (
    DesktopNotifier,
    Notice,
    Notifications,
    NtfyNotifier,
    NullNotifier,
    build,
)


class Recorder:
    """A notifier that remembers, so a test can read what would have been sent."""

    name = "recorder"

    def __init__(self, *, succeed: bool = True) -> None:
        self.notices: list[Notice] = []
        self._succeed = succeed

    async def send(self, notice: Notice) -> bool:
        self.notices.append(notice)
        return self._succeed


class Exploding:
    name = "exploding"

    async def send(self, notice: Notice) -> bool:
        raise RuntimeError("the push service is on fire")


class Hanging:
    name = "hanging"

    async def send(self, notice: Notice) -> bool:
        import asyncio

        await asyncio.sleep(30)
        return True


# --------------------------------------------------------------------------------------
# Nothing may break the run
# --------------------------------------------------------------------------------------


async def test_a_notifier_that_raises_does_not_reach_the_caller():
    """This is called from inside the run loop, on the path of an approval a person is
    already waiting for. The one outcome worse than "no notification" is "the run died
    trying to send one"."""
    notifications = Notifications([Exploding()])
    assert await notifications.send(Notice("Hi", "There")) == 0
    assert notifications.failed == 1


async def test_one_broken_notifier_does_not_stop_the_others():
    recorder = Recorder()
    notifications = Notifications([Exploding(), recorder])

    assert await notifications.send(Notice("Hi", "There")) == 1
    assert len(recorder.notices) == 1


async def test_nothing_configured_sends_nothing_and_succeeds():
    """The default. A run must work with no notifier at all."""
    assert await Notifications([]).send(Notice("Hi", "There")) == 0
    assert await NullNotifier().send(Notice("Hi", "There")) is True


async def test_a_failed_delivery_is_counted_rather_than_hidden():
    notifications = Notifications([Recorder(succeed=False)])
    await notifications.send(Notice("Hi", "There"))
    assert (notifications.sent, notifications.failed) == (0, 1)


# --------------------------------------------------------------------------------------
# What is said
# --------------------------------------------------------------------------------------


async def test_an_urgency_below_the_threshold_is_not_sent():
    """"Approvals only" is what most people want after the first week."""
    recorder = Recorder()
    notifications = Notifications([recorder], minimum_urgency="high")

    await notifications.send(Notice("Run finished", "All done", urgency="normal"))
    assert recorder.notices == []

    await notifications.send(Notice("Needs you", "Approve this", urgency="high"))
    assert len(recorder.notices) == 1


def test_a_notice_carries_a_link_so_a_phone_can_act_on_it():
    notice = Notice("Needs you", "Approve", url="https://tunnel.example/")
    assert notice.url == "https://tunnel.example/"


def test_the_run_loop_names_the_field_and_never_the_value():
    """A notification says a field needs approving. It does not say which value, because
    the values are your name, your phone number and your salary expectations -- and a push
    service is a third party."""
    import ast
    import inspect

    from localapply.orchestrator.run_loop import RunManager

    # Parsed rather than string-searched: a docstring that mentions the function would fool
    # a substring check, and the property being tested is about the actual call.
    tree = ast.parse(inspect.getsource(RunManager._await_approval).lstrip())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_tell_the_user"
    ]
    assert calls, "the approval path must say something"

    arguments = " ".join(ast.unparse(arg) for arg in calls[0].args)
    assert "element.name" in arguments, "the field is what a person needs to act"
    # The values here are your name, your phone number and your salary expectations.
    assert "decision.value" not in arguments
    assert "edited_value" not in arguments


# --------------------------------------------------------------------------------------
# ntfy
# --------------------------------------------------------------------------------------


def test_ntfy_is_off_until_a_topic_is_set():
    assert not NtfyNotifier("").available()
    assert NtfyNotifier("some-long-random-topic").available()


async def test_ntfy_posts_the_body_to_the_topic(monkeypatch):
    captured = {}

    class FakeResponse:
        is_success = True

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            captured.update(url=url, content=content, headers=headers)
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    sent = await NtfyNotifier("my-secret-topic").send(
        Notice("Needs you", "Approve the salary field", url="https://tunnel/", urgency="high")
    )

    assert sent is True
    assert captured["url"] == "https://ntfy.sh/my-secret-topic"
    assert captured["content"] == b"Approve the salary field"
    assert captured["headers"]["Priority"] == "5"
    assert captured["headers"]["Click"] == "https://tunnel/"


async def test_ntfy_titles_survive_a_curly_quote(monkeypatch):
    """Headers go out as latin-1; a curly quote in a title would 500 the request."""
    from localapply.notify import _ascii

    assert _ascii("Haydar’s application — needs you").isascii()


async def test_an_unreachable_push_service_is_a_false_not_a_crash(monkeypatch):
    import httpx

    class Failing:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx, "AsyncClient", Failing)
    assert await NtfyNotifier("topic").send(Notice("Hi", "There")) is False


# --------------------------------------------------------------------------------------
# Desktop
# --------------------------------------------------------------------------------------


def test_the_desktop_notifier_declines_rather_than_pretends(monkeypatch):
    """A notifier that silently no-ops on an unsupported platform is better than one that
    raises into a run loop -- but it must report that it did nothing."""
    monkeypatch.setattr("sys.platform", "linux")
    assert not DesktopNotifier().available()


async def test_an_unavailable_desktop_notifier_returns_false(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert await DesktopNotifier().send(Notice("Hi", "There")) is False


def test_the_toast_script_quotes_what_it_is_given():
    """The title and body are ours, but a job title reaches them, and a job title is written
    by a stranger."""
    from localapply.notify import _ps_quote

    quoted = _ps_quote("It's a 'test'")
    assert quoted.startswith("'") and quoted.endswith("'")
    assert "''" in quoted


# --------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------


def test_nothing_configured_builds_a_silent_notifier(settings):
    settings.notify_desktop = False
    settings.notify_ntfy_topic = ""
    assert build(settings).enabled is False


def test_a_topic_turns_push_on(settings):
    settings.notify_desktop = False
    settings.notify_ntfy_topic = "some-long-random-topic"
    assert "ntfy" in build(settings).names


@pytest.mark.parametrize(
    ("minimum", "urgency", "wanted"),
    [("low", "low", True), ("normal", "low", False), ("high", "normal", False),
     ("high", "high", True), ("normal", "high", True)],
)
def test_the_urgency_threshold(minimum, urgency, wanted):
    assert Notifications([], minimum_urgency=minimum).wants(urgency) is wanted
