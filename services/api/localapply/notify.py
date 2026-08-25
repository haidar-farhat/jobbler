"""Telling you something is waiting.

An approval blocks its run until a person answers. Until now nothing said so, so a run
started before lunch was still parked at 6pm -- holding one of three capped browser
sessions, holding the page open, and waiting for someone who did not know they were needed.
That is not a missing convenience. It is a promise the app makes and does not keep.

Three deliveries, in the order a local-first app should try them:

  * **The dashboard** already knows -- it has the event stream. Nothing to build.
  * **The desktop**, via a native toast. Free, needs no account, works with no internet.
  * **A push service**, for when you are not at the machine. `ntfy` is the choice because
    it needs no account, no API key and no app store: you pick an unguessable topic and
    open a URL. Anything requiring registration would be a worse default for a tool whose
    whole premise is that it runs on your own hardware.

Two rules, both learned from how these go wrong:

  * **Delivery never blocks the run.** A notifier that raises, hangs, or takes eight seconds
    must not add eight seconds to an approval that a person is already waiting on. Every
    failure is caught and logged.
  * **Nothing sensitive leaves the machine.** A notification says a field needs approving.
    It does not say which value, because the values are your name, your phone number, your
    salary expectations -- and a push service is a third party.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

#: Long enough for a slow phone network, short enough that nobody waits on it. A person is
#: standing in front of an approval card while this runs.
DELIVERY_TIMEOUT = 5.0


@dataclass
class Notice:
    """What to say. Deliberately small, and deliberately vague about values.

    An approval notice names the *field*, never the value: "Expected salary needs you" is
    enough to act on, and "Expected salary: £85,000" is your salary expectations sent to a
    third-party push service.
    """

    title: str
    body: str
    #: Where to go. A tunnel address when there is one, so a phone can act on it.
    url: str | None = None
    #: low | normal | high. An approval is high; a finished run is normal.
    urgency: str = "normal"


class Notifier(Protocol):
    name: str

    async def send(self, notice: Notice) -> bool: ...


class NullNotifier:
    """Says nothing, successfully. The default, and what you get with nothing configured."""

    name = "none"

    async def send(self, notice: Notice) -> bool:  # noqa: ARG002
        return True


class DesktopNotifier:
    """A native toast on the machine the app is running on.

    Windows only for now, via PowerShell's BurntToast-free toast API, because that is the
    platform this app is developed and run on. Falls back to doing nothing rather than
    pretending: a notifier that silently no-ops on Linux is better than one that raises
    into a run loop.
    """

    name = "desktop"

    def available(self) -> bool:
        return sys.platform == "win32" and shutil.which("powershell") is not None

    async def send(self, notice: Notice) -> bool:
        if not self.available():
            return False
        script = _WINDOWS_TOAST.format(
            title=_ps_quote(notice.title), body=_ps_quote(notice.body)
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.wait_for(process.wait(), timeout=DELIVERY_TIMEOUT)
        except (OSError, TimeoutError, NotImplementedError) as exc:
            # NotImplementedError is the Windows SelectorEventLoop trap the browser layer
            # already documents: no subprocesses there. A toast is not worth failing over.
            logger.debug("desktop notification failed: %s", exc)
            return False
        return process.returncode == 0


def _ps_quote(text: str) -> str:
    """Single-quoted PowerShell, with the only escape that matters."""
    return "'" + " ".join((text or "").split()).replace("'", "''") + "'"


#: No module install, no BurntToast, no admin. Uses the toast API that ships with Windows.
_WINDOWS_TOAST = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] > $null; "
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
    "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
    "$n = $t.GetElementsByTagName('text'); "
    "$n.Item(0).AppendChild($t.CreateTextNode({title})) > $null; "
    "$n.Item(1).AppendChild($t.CreateTextNode({body})) > $null; "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    "'LocalApply').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
)


class NtfyNotifier:
    """Push to a phone through ntfy.sh, or a server you run yourself.

    No account and no API key: a topic is a secret you choose. That also means **anyone who
    knows the topic can read your notifications**, which is why nothing sensitive is ever in
    one and why the topic should be long and random rather than "job-alerts".
    """

    name = "ntfy"

    def __init__(self, topic: str, base_url: str = "https://ntfy.sh") -> None:
        self._topic = (topic or "").strip().strip("/")
        self._base = (base_url or "https://ntfy.sh").rstrip("/")

    def available(self) -> bool:
        return bool(self._topic)

    async def send(self, notice: Notice) -> bool:
        if not self.available():
            return False
        headers = {
            "Title": _ascii(notice.title),
            "Priority": {"low": "2", "normal": "3", "high": "5"}.get(notice.urgency, "3"),
        }
        if notice.url:
            headers["Click"] = notice.url
        try:
            async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT) as client:
                response = await client.post(
                    f"{self._base}/{self._topic}",
                    content=notice.body.encode("utf-8"),
                    headers=headers,
                )
            return response.is_success
        except httpx.HTTPError as exc:
            logger.debug("ntfy delivery failed: %s", exc)
            return False


def _ascii(text: str) -> str:
    """ntfy sends headers as latin-1; a curly quote in a title would 500 the request."""
    return " ".join((text or "").split()).encode("ascii", "replace").decode("ascii")[:120]


class Notifications:
    """Every configured way of telling you, tried in turn.

    Failures are recorded and never raised. This is called from inside the run loop, on the
    path of an approval a person is already waiting for, and the one outcome worse than "no
    notification" is "the run died trying to send one".
    """

    def __init__(self, notifiers: list[Notifier], *, minimum_urgency: str = "normal") -> None:
        self._notifiers = notifiers
        self._minimum = minimum_urgency
        self.sent = 0
        self.failed = 0

    @property
    def names(self) -> list[str]:
        return [n.name for n in self._notifiers]

    @property
    def enabled(self) -> bool:
        return bool(self._notifiers)

    def wants(self, urgency: str) -> bool:
        order = {"low": 0, "normal": 1, "high": 2}
        return order.get(urgency, 1) >= order.get(self._minimum, 1)

    async def send(self, notice: Notice) -> int:
        if not self._notifiers or not self.wants(notice.urgency):
            return 0
        delivered = 0
        for notifier in self._notifiers:
            try:
                if await notifier.send(notice):
                    delivered += 1
            except Exception as exc:  # noqa: BLE001 - see the class docstring
                logger.warning("notifier %s raised: %s", notifier.name, exc)
        self.sent += delivered
        self.failed += len(self._notifiers) - delivered
        return delivered


def build(settings) -> Notifications:
    """Assemble the notifiers from settings. Nothing configured means nothing is sent."""
    notifiers: list[Notifier] = []

    if settings.notify_desktop:
        desktop = DesktopNotifier()
        if desktop.available():
            notifiers.append(desktop)

    if settings.notify_ntfy_topic:
        notifiers.append(
            NtfyNotifier(settings.notify_ntfy_topic, settings.notify_ntfy_url)
        )

    return Notifications(notifiers, minimum_urgency=settings.notify_minimum_urgency)
