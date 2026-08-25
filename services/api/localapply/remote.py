"""Reaching this machine from your phone.

The goal is small and specific: an approval is waiting, you are not at the desk, and you
want to answer it. Everything here serves that and nothing more.

**Why a private network and not a port forward.** Forwarding a port puts an API holding your
name, phone number, address and salary expectations on the public internet, protected by one
token. A private network means only devices you have paired can reach it at all, and the
token is the second lock rather than the only one.

**Why Tailscale is the recommended way in, when the plan said WireGuard.** Tailscale *is*
WireGuard -- same protocol, same encryption -- with the one part raw WireGuard leaves to you
solved: getting a packet from a phone on mobile data to a machine behind a home router. Raw
WireGuard needs a forwarded port, a static address or dynamic DNS, and a manual key
exchange; behind carrier-grade NAT it may not be possible at all. Generating keys is the
easy five percent of that problem, and this module does not pretend otherwise.

Raw WireGuard is still supported for people who already run it. What this module refuses to
do is hand-roll the crypto: keys come from the real `wg` binary or not at all.

**Nothing here opens anything.** It finds the address you already have, checks the
configuration is safe to expose, and tells you the URL to type into a phone. Binding to it
is a deliberate flag on the launcher, and the app refuses to start bound to a network with
no token set.
"""

from __future__ import annotations

import ipaddress
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from urllib.parse import quote

#: Tailscale hands every machine an address in this range. Recognising it is what lets the
#: launcher say "this is your tunnel address" rather than "here are four network cards".
TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")

#: The interface a hand-rolled WireGuard setup conventionally uses.
WIREGUARD_NAMES = ("wg0", "wg-localapply")


@dataclass
class Tunnel:
    """A way in that already exists on this machine."""

    kind: str  # tailscale | wireguard | lan
    address: str
    #: How sure we are this is reachable from elsewhere, in words rather than a number.
    detail: str = ""

    @property
    def private(self) -> bool:
        """Is this a private network, as opposed to a plain LAN address?

        A LAN address works from the sofa and not from the train, and more importantly it is
        reachable by everything else on that network -- a guest, a smart TV, a compromised
        laptop. It is offered, clearly labelled, and never recommended.
        """
        return self.kind in {"tailscale", "wireguard"}


@dataclass
class Readiness:
    """Whether this machine is safe to expose, and what is missing if not."""

    tunnels: list[Tunnel] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.problems and bool(self.tunnels)

    @property
    def best(self) -> Tunnel | None:
        """The one to recommend: a private network before a LAN address, always."""
        private = [t for t in self.tunnels if t.private]
        return (private or self.tunnels or [None])[0]

    def as_dict(self) -> dict:
        return {
            "ready": self.ready,
            "tunnels": [
                {"kind": t.kind, "address": t.address, "detail": t.detail,
                 "private": t.private}
                for t in self.tunnels
            ],
            "problems": self.problems,
            "advice": self.advice,
        }


def tailscale_address() -> str | None:
    """This machine's Tailscale address, if Tailscale is running.

    Asked of the binary rather than of the network interfaces: an address in the Tailscale
    range on a machine where Tailscale is stopped is an address nothing answers on, and
    telling someone to use it would send them to a URL that times out.
    """
    binary = shutil.which("tailscale")
    if not binary:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - a fixed binary found on PATH, no shell
            [binary, "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    first = (result.stdout or "").strip().splitlines()
    return first[0].strip() if first else None


def local_addresses() -> list[str]:
    """Every address this machine answers on, loopback excluded.

    `getaddrinfo` on the hostname rather than reading interfaces, because it needs no extra
    dependency and returns what the machine believes it is -- which is what a phone will
    resolve too.
    """
    found: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return found
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            continue
        if parsed.is_loopback or address in found:
            continue
        found.append(address)
    return found


def find_tunnels() -> list[Tunnel]:
    """Every way in, best first."""
    tunnels: list[Tunnel] = []

    address = tailscale_address()
    if address:
        tunnels.append(
            Tunnel(
                kind="tailscale",
                address=address,
                detail="Only devices signed into your tailnet can reach this.",
            )
        )

    for candidate in local_addresses():
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if parsed in TAILSCALE_RANGE:
            # Already reported above, or Tailscale is installed but stopped.
            continue
        tunnels.append(
            Tunnel(
                kind="lan",
                address=candidate,
                detail=(
                    "Anything else on this network can reach it too -- a guest, a smart TV, "
                    "a laptop someone else owns. Fine at home; not a substitute for a tunnel."
                ),
            )
        )
    return tunnels


def check(settings) -> Readiness:
    """Is this machine ready to answer your phone, and is doing so safe?

    Every problem here is a refusal to recommend, not a refusal to run -- except the token,
    which `main._refuse_unguarded_remote_access` turns into an actual refusal at startup.
    """
    readiness = Readiness(tunnels=find_tunnels())

    if not settings.api_token.strip():
        readiness.problems.append(
            "No API token. Anyone who could reach this port would be able to read your "
            "whole profile."
        )
        readiness.advice.append("Run LocalApply.exe once; it generates one into .env.")

    if not readiness.tunnels:
        readiness.problems.append("No network address found besides this machine itself.")
        readiness.advice.append(
            "Install Tailscale and run `tailscale up` on this machine and on your phone. "
            "It is WireGuard underneath and needs no router configuration."
        )
    elif not any(t.private for t in readiness.tunnels):
        readiness.advice.append(
            "Only a LAN address was found, which works at home and not on mobile data. "
            "Tailscale gives you an address that works from anywhere."
        )

    if settings.dry_run is False:
        readiness.advice.append(
            "DRY_RUN is off, so an approval from your phone sends a real application."
        )

    return readiness


def pairing_url(tunnel: Tunnel, port: int, token: str) -> str:
    """The one URL to type into a phone.

    The token rides in the query string on purpose. The alternative is asking someone to
    type a 43-character secret into a mobile keyboard, which people get wrong twice and then
    give up on -- and the dashboard stores it and strips it from the address bar on arrival,
    so it lives in the URL for exactly one request.
    """
    # `safe=""` matters: `quote` leaves `/` alone by default. A generated token never
    # contains one -- `token_urlsafe` uses `-` and `_` -- but a hand-set token can, and it
    # would arrive as a different string with no indication that anything went wrong.
    return f"http://{tunnel.address}:{port}/?token={quote(token, safe='')}"


def summary(readiness: Readiness, port: int, token: str) -> list[str]:
    """What to tell a person, in the order they need to hear it."""
    lines: list[str] = []
    for problem in readiness.problems:
        lines.append(f"! {problem}")
    for note in readiness.advice:
        lines.append(f"  {note}")

    best = readiness.best
    if best is not None and not readiness.problems:
        lines.append(f"  Open this on your phone:  {pairing_url(best, port, token)}")
        if not best.private:
            lines.append(f"  {best.detail}")
    return lines


# --------------------------------------------------------------------------------------
# Raw WireGuard, for people who already run it
# --------------------------------------------------------------------------------------


def wireguard_available() -> bool:
    return shutil.which("wg") is not None


def generate_keypair() -> tuple[str, str] | None:
    """A WireGuard keypair, from the real `wg` binary.

    Deliberately not implemented in Python. X25519 key generation is well specified and
    short enough to be tempting, and hand-rolled crypto in a file nobody will audit is
    exactly the kind of thing that looks fine for years. If `wg` is not installed, this
    says so instead of improvising.
    """
    binary = shutil.which("wg")
    if not binary:
        return None
    try:
        private = subprocess.run(  # noqa: S603 - a fixed binary found on PATH, no shell
            [binary, "genkey"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        public = subprocess.run(  # noqa: S603 - same
            [binary, "pubkey"], input=private, capture_output=True, text=True,
            timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return private, public
