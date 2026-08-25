"""Lever postings.

    https://api.lever.co/v0/postings/{slug}?mode=json

Three traps, all verified against live responses:

  1. **The title lives in `text`.** There is no `title` key on a posting at all. Reading
     `title` gives you nothing for every job on the board.
  2. **`descriptionPlain` is only about a third of the job.** Measured across 430 postings on
     one board, the other two thirds live in `lists[].content` -- which is where the
     responsibilities and requirements actually are, and therefore where every skill a
     scorer is looking for actually is. A connector that reads only `descriptionPlain`
     produces jobs that match nothing and look fine. `additionalPlain` holds the closing
     boilerplate. The full text is all four concatenated.
  3. **`createdAt` is epoch milliseconds**, undocumented, and not a publish date -- one board
     carries values from 2009 alongside current ones. It is kept for reference and not
     trusted for freshness.

There is no company field: the endpoint is per-tenant, so the slug is the company, and the
slug is a machine handle ("shieldai") rather than a display name ("Shield AI").
"""

from __future__ import annotations

from datetime import UTC, datetime

from .base import Posting, register, strip_html

#: EU-hosted customers are a separate tenant pool, not a mirror: a slug that answers on one
#: host 404s on the other. Both are tried, US first.
HOSTS = ("https://api.lever.co", "https://api.eu.lever.co")


def full_text(job: dict) -> str:
    """Everything a scorer needs, which is not what the "description" field contains.

    Order matters for readability: opening, then each list with its heading, then the
    closing boilerplate -- which is the order a person reads the posting in.
    """
    parts: list[str] = []
    opening = (job.get("descriptionPlain") or "").strip()
    if opening:
        parts.append(opening)

    for section in job.get("lists") or []:
        if not isinstance(section, dict):
            continue
        heading = (section.get("text") or "").strip()
        body = strip_html(section.get("content") or "")
        if heading and body:
            parts.append(f"{heading}\n{body}")
        elif heading or body:
            parts.append(heading or body)

    closing = (job.get("additionalPlain") or "").strip()
    if closing:
        parts.append(closing)

    # `descriptionPlain` is blank on a small number of postings, so the lists are not an
    # enrichment here -- they are the fallback that stops those scoring zero.
    return "\n\n".join(parts).strip()


class LeverConnector:
    source = "lever"
    handle_label = "Company slug (the segment in jobs.lever.co/<slug>)"

    #: Which host answered last, so a board found on the EU pool is not re-probed every run.
    _host_for: dict[str, str] = {}

    def endpoint(self, handle: str) -> str:
        host = self._host_for.get(handle, HOSTS[0])
        return f"{host}/v0/postings/{handle}?mode=json"

    def alternates(self, handle: str) -> list[str]:
        """The other tenant pool, tried when the first answers 404."""
        used = self._host_for.get(handle, HOSTS[0])
        return [f"{h}/v0/postings/{handle}?mode=json" for h in HOSTS if h != used]

    def remember_host(self, handle: str, url: str) -> None:
        for host in HOSTS:
            if url.startswith(host):
                self._host_for[handle] = host
                return

    def parse(self, payload: object, handle: str) -> list[Posting]:
        # The list endpoint returns a bare array, not an object with a `jobs` key.
        jobs = payload if isinstance(payload, list) else []
        postings = []
        for job in jobs:
            if not isinstance(job, dict):
                continue

            categories = job.get("categories") or {}
            # `allLocations` keeps multi-site roles whole; `location` alone loses all but
            # the first, and one observed posting had eight.
            everywhere = categories.get("allLocations") or []
            location = ", ".join(everywhere) if everywhere else categories.get("location")

            postings.append(
                Posting(
                    external_id=str(job.get("id") or ""),
                    # `text`, not `title`. See the module docstring.
                    title=(job.get("text") or "").strip(),
                    # Absent from the payload; the slug is all there is.
                    company=handle,
                    url=(job.get("hostedUrl") or "").strip(),
                    description=full_text(job),
                    location=location,
                    posted_at=_epoch_ms(job.get("createdAt")),
                    extra={
                        # ISO 3166-1 alpha-2, and far more reliable than parsing the
                        # free-text location, which is unnormalised per customer.
                        "country": job.get("country"),
                        "workplace_type": job.get("workplaceType"),
                        "team": categories.get("team"),
                    },
                )
            )
        return postings


def _epoch_ms(value: object) -> str | None:
    """Milliseconds since the epoch, as an integer. Seconds lands you in the year 58000."""
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


register(LeverConnector())
