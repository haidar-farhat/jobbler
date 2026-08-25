"""What every board connector must produce, and the one HTTP client they share.

The client is here rather than in each connector so the ToS posture is written once and
cannot drift: one timeout, no header spoofing, per-host pacing, and the kill switch checked
before every request.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ...safety import KILL_SWITCH, AutomationHalted

#: Long enough for a slow board, short enough that a hung request does not hold a search
#: open. Boards answer in well under a second when healthy.
REQUEST_TIMEOUT = 20.0

#: A description shorter than this is a stub, a redirect page, or a posting whose text lives
#: somewhere this connector did not look. Storing it would score zero and look like an
#: answer; refusing it makes the gap visible.
MIN_DESCRIPTION_CHARS = 200


@dataclass
class Posting:
    """One job as a board describes it. Every string here was written by a stranger."""

    #: Stable within the board. Combined with `source` it is the dedupe key.
    external_id: str
    title: str
    company: str
    url: str
    description: str
    location: str | None = None
    #: ISO 8601, as the board gave it. Kept as text: three boards, three formats, and none
    #: of them is worth normalising into a lie.
    posted_at: str | None = None
    #: Anything board-specific worth keeping but not worth a column.
    extra: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.external_id and self.title and self.url) and (
            len(self.description) >= MIN_DESCRIPTION_CHARS
        )


class Connector(Protocol):
    """A board. Named by its `source`, which is also what lands in `jobs.source`."""

    source: str
    #: Shown in the UI when asking for a board handle, because every board calls it
    #: something different and none of them call it "handle".
    handle_label: str

    def endpoint(self, handle: str) -> str: ...

    def parse(self, payload: object, handle: str) -> list[Posting]: ...


_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_RE = re.compile(r"\n{3,}")


def strip_html(markup: str) -> str:
    """HTML to readable text, for text a scorer reads and a person may see.

    Block tags become newlines first. Without that, "</p><p>" collapses two paragraphs into
    one run-on line and every heading fuses into the sentence after it -- which then reads
    as one enormous paragraph in the job description panel.
    """
    if not markup:
        return ""
    text = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", markup)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


async def get_json(client: httpx.AsyncClient, url: str) -> object:
    """One GET, no retry, no header games.

    Deliberately not setting a User-Agent, a Referer, or anything else: the posture is to be
    an obvious, ordinary client of a public endpoint, not to look like a browser. A board
    that wants to refuse this can, and that refusal is respected.
    """
    if KILL_SWITCH.engaged:
        raise AutomationHalted(KILL_SWITCH.reason or "Automation stopped.")
    response = await client.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


BOARDS: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    BOARDS[connector.source] = connector
    return connector


def connector_for(source: str) -> Connector | None:
    return BOARDS.get(source)
