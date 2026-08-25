"""Who is allowed to talk to this API.

Until now: anyone. `GET /profile` returned every accepted fact -- name, email, phone,
address, salary answers -- to whatever asked. On a machine where nothing else is listening
that is defensible. It stops being defensible the moment the API is reachable from anywhere
else, and reaching it from a phone is a phase that is already planned.

It is also not theoretical. An adversarial review demonstrated a working exfiltration: a job
posting that redirected the ingesting browser to `http://localhost./profile` and read the
whole fact set back out. The URL guard that allowed it is fixed, but the reason it *worked*
is that `/profile` answers anyone.

The design is deliberately the smallest thing that is actually a boundary:

  * **One token, generated on first run.** A single-user local app has no accounts to model,
    and inventing users would be a login screen guarding a database only you can reach.
  * **Compared in constant time.** A token compared with `==` leaks its prefix to anyone who
    can time the response, which over a private network is a real attack rather than a
    theoretical one.
  * **Loopback is exempt by default.** Requiring a token to use the app on the machine it
    runs on would mean a login screen for a local dashboard, and people turn those off. The
    exemption is what makes the token viable everywhere else -- and it is refused the moment
    the API binds to anything but loopback.
"""

from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass

from fastapi import HTTPException, Request

#: 32 bytes of urlsafe base64. Long enough that guessing is not a strategy, short enough to
#: paste into a phone once.
TOKEN_BYTES = 32

HEADER = "X-LocalApply-Token"
#: EventSource cannot set headers, so the live event stream has no way to send one. The
#: query parameter exists for that single case and is documented as such rather than
#: quietly accepted everywhere.
QUERY_PARAM = "token"

#: Reachable without a token when `require_token_on_loopback` is off: this machine talking
#: to itself. Anything else -- a phone, a laptop, a tunnel -- needs the token.
_LOCAL = frozenset({"127.0.0.1", "::1", "localhost"})

#: Answerable without a token, always. `/health` is what a launcher polls to know the app is
#: up, and what tells you the app is running at all; a health check that needs a secret is a
#: health check nobody can use when things are wrong.
PUBLIC_PATHS = frozenset({"/health", "/openapi.json", "/docs", "/redoc",
                          "/docs/oauth2-redirect"})


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def is_loopback(host: str | None) -> bool:
    """Is this request from the machine the app is running on?

    Names are matched literally and addresses numerically, because a browser will send
    either and they are the same machine.
    """
    if not host:
        return False
    candidate = host.strip().lower().rstrip(".")
    if candidate in _LOCAL:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


@dataclass
class Gate:
    """The decision about one request, and why."""

    allowed: bool
    reason: str = ""


class TokenGuard:
    """Holds the token and answers "may this request proceed".

    Framework-free on purpose: it is a pure function of a token, a path, and a peer address,
    which is what makes it testable without a client and reusable from somewhere other than
    HTTP middleware.
    """

    def __init__(
        self, token: str | None, *, require_on_loopback: bool = False, enabled: bool = True
    ) -> None:
        self._token = (token or "").strip()
        self._require_on_loopback = require_on_loopback
        self._enabled = enabled and bool(self._token)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def token(self) -> str:
        return self._token

    def check(self, *, path: str, peer: str | None, presented: str | None) -> Gate:
        if not self._enabled:
            return Gate(True, "no token configured")
        if path in PUBLIC_PATHS or path.startswith("/screenshots/"):
            return Gate(True, "public")
        if is_loopback(peer) and not self._require_on_loopback:
            return Gate(True, "loopback")
        if not presented:
            return Gate(False, "no token")
        # Constant time. `==` on a secret leaks its prefix to anyone who can time the
        # response, and over a private network that is a real attack.
        if not secrets.compare_digest(presented, self._token):
            return Gate(False, "wrong token")
        return Gate(True, "token")


def token_from(request: Request) -> str | None:
    header = request.headers.get(HEADER)
    if header:
        return header.strip()
    # `Authorization: Bearer <token>` as well, because that is what every client already
    # knows how to send.
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.query_params.get(QUERY_PARAM)


def refuse() -> HTTPException:
    """One refusal, whatever went wrong.

    Deliberately identical for a missing token, a wrong token, and a path that does not
    exist behind the gate: an error that distinguishes them tells an unauthenticated caller
    what this app has, which is the one thing they should not learn from the door.
    """
    return HTTPException(
        status_code=401,
        detail="This request needs your LocalApply token.",
        headers={"WWW-Authenticate": "Bearer"},
    )
