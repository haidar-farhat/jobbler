"""Reaching this machine from your phone.

Nothing here opens anything. It finds the address you already have, checks the configuration
is safe to expose, and says what to type into a phone -- and the assertions that matter are
the refusals, because the failure mode is putting an API that holds your name, address and
salary expectations somewhere it should not be.
"""

from __future__ import annotations

import pytest
from localapply import remote
from localapply.remote import Readiness, Tunnel


@pytest.fixture
def exposed(settings):
    settings.api_token = "a-real-token"
    return settings


# --------------------------------------------------------------------------------------
# What counts as a way in
# --------------------------------------------------------------------------------------


def test_a_tailscale_address_is_a_private_network():
    assert Tunnel(kind="tailscale", address="100.64.1.2").private


def test_a_lan_address_is_not():
    """It works from the sofa and not from the train -- and everything else on that network
    can reach it: a guest, a smart TV, a laptop someone else owns."""
    assert not Tunnel(kind="lan", address="192.168.1.20").private


def test_a_private_network_is_preferred_over_a_lan_address():
    readiness = Readiness(
        tunnels=[Tunnel("lan", "192.168.1.20"), Tunnel("tailscale", "100.64.1.2")]
    )
    assert readiness.best.kind == "tailscale"


def test_a_lan_address_is_still_offered_when_it_is_all_there_is():
    readiness = Readiness(tunnels=[Tunnel("lan", "192.168.1.20")])
    assert readiness.best.address == "192.168.1.20"


# --------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------


def test_no_token_is_a_problem_not_a_warning(settings, monkeypatch):
    """The whole reason auth comes before remote access."""
    settings.api_token = ""
    monkeypatch.setattr(remote, "find_tunnels", lambda: [Tunnel("tailscale", "100.64.1.2")])

    readiness = remote.check(settings)
    assert not readiness.ready
    assert any("whole profile" in p for p in readiness.problems)


def test_no_way_in_is_a_problem_with_advice(exposed, monkeypatch):
    monkeypatch.setattr(remote, "find_tunnels", list)

    readiness = remote.check(exposed)
    assert not readiness.ready
    assert any("Tailscale" in a for a in readiness.advice)


def test_a_token_and_a_tunnel_is_ready(exposed, monkeypatch):
    monkeypatch.setattr(remote, "find_tunnels", lambda: [Tunnel("tailscale", "100.64.1.2")])
    assert remote.check(exposed).ready


def test_only_a_lan_address_is_ready_but_said_so(exposed, monkeypatch):
    """Not a refusal -- it genuinely works at home -- but it must not be presented as a
    tunnel."""
    monkeypatch.setattr(remote, "find_tunnels", lambda: [Tunnel("lan", "192.168.1.20")])

    readiness = remote.check(exposed)
    assert readiness.ready
    assert any("mobile data" in a for a in readiness.advice)


def test_a_live_submit_is_called_out(exposed, monkeypatch):
    """Approving from a phone is easy to do without thinking. If DRY_RUN is off, that tap
    sends a real application."""
    monkeypatch.setattr(remote, "find_tunnels", lambda: [Tunnel("tailscale", "100.64.1.2")])
    exposed.dry_run = False

    assert any("real application" in a for a in remote.check(exposed).advice)


# --------------------------------------------------------------------------------------
# Pairing
# --------------------------------------------------------------------------------------


def test_the_pairing_url_carries_the_token():
    """One URL to type, rather than a 43-character secret on a mobile keyboard."""
    url = remote.pairing_url(Tunnel("tailscale", "100.64.1.2"), 8000, "tok en/+")
    assert url.startswith("http://100.64.1.2:8000/?token=")
    # URL-encoded, or a token containing / or + silently becomes a different token.
    assert "tok%20en%2F%2B" in url


def test_the_dashboard_removes_the_token_from_the_address_bar():
    """Otherwise it lives in history, in a bookmark, and in any screenshot of the browser."""
    from pathlib import Path

    dashboard = (
        Path(__file__).resolve().parents[1]
        / "services" / "api" / "localapply" / "static" / "dashboard.html"
    ).read_text(encoding="utf-8")

    assert "params.delete(\"token\")" in dashboard
    assert "history.replaceState" in dashboard


def test_the_summary_leads_with_problems(exposed, monkeypatch):
    monkeypatch.setattr(remote, "find_tunnels", list)
    lines = remote.summary(remote.check(exposed), 8000, "token")
    assert lines[0].startswith("!")
    # And says nothing about where to connect, because there is nowhere safe to.
    assert not any("Open this on your phone" in line for line in lines)


def test_the_summary_gives_a_url_when_it_is_safe(exposed, monkeypatch):
    monkeypatch.setattr(remote, "find_tunnels", lambda: [Tunnel("tailscale", "100.64.1.2")])
    lines = remote.summary(remote.check(exposed), 8000, "token")
    assert any("100.64.1.2:8000" in line for line in lines)


# --------------------------------------------------------------------------------------
# Raw WireGuard
# --------------------------------------------------------------------------------------


def test_keys_come_from_the_real_binary_or_not_at_all(monkeypatch):
    """X25519 key generation is short enough to be tempting to hand-roll, and hand-rolled
    crypto in a file nobody will audit is exactly the kind of thing that looks fine for
    years."""
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    assert remote.generate_keypair() is None
    assert not remote.wireguard_available()


def test_no_crypto_is_implemented_here():
    import inspect

    source = inspect.getsource(remote)
    for smell in ("curve25519", "0x7fffffffffffffff", "def _scalarmult", "pow(", "% p"):
        assert smell not in source, f"looks like hand-rolled crypto: {smell}"


# --------------------------------------------------------------------------------------
# Detection does not lie
# --------------------------------------------------------------------------------------


def test_tailscale_that_is_installed_but_stopped_is_not_reported(monkeypatch):
    """An address in the Tailscale range on a machine where Tailscale is not running is an
    address nothing answers on, and offering it sends someone to a URL that times out."""
    monkeypatch.setattr(remote.shutil, "which", lambda name: "tailscale" if name == "tailscale" else None)

    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **kw: Failed())
    assert remote.tailscale_address() is None


def test_tailscale_missing_entirely_is_not_an_error(monkeypatch):
    monkeypatch.setattr(remote.shutil, "which", lambda name: None)
    assert remote.tailscale_address() is None


def test_loopback_is_never_offered_as_a_way_in(monkeypatch):
    monkeypatch.setattr(
        remote.socket, "getaddrinfo",
        lambda *a, **kw: [(0, 0, 0, "", ("127.0.0.1", 0)), (0, 0, 0, "", ("192.168.1.9", 0))],
    )
    assert remote.local_addresses() == ["192.168.1.9"]
