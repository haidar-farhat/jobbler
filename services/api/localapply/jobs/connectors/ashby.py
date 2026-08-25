"""Ashby job boards.

    https://api.ashbyhq.com/posting-api/job-board/{board_name}

The kindest of the three to read: `descriptionPlain` really is the whole posting in plain
text, and it was non-empty on every record sampled.

Two things to know. The primary key, `id`, is **undocumented** -- it appears in neither the
docs' example JSON nor their field table, so anyone coding from the documentation alone
would not know it exists. It is the final segment of `jobUrl`, which is the fallback. And
like Lever, there is no company field: the board name you asked for is the company.
"""

from __future__ import annotations

from .base import Posting, register


class AshbyConnector:
    source = "ashby"
    handle_label = "Job board name (the segment in jobs.ashbyhq.com/<name>)"

    def endpoint(self, handle: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{handle}"

    def parse(self, payload: object, handle: str) -> list[Posting]:
        # `.get("jobs", [])` is not enough: a board answering {"jobs": null} returns
        # None, not the default, and a sweep across four boards ends on the one that did.
        jobs = (payload.get("jobs") or []) if isinstance(payload, dict) else []
        postings = []
        for job in jobs:
            if not isinstance(job, dict):
                continue

            url = (job.get("jobUrl") or "").strip()
            # Undocumented but present on every record; the URL's last segment is the same
            # value and is the documented way to arrive at it.
            external_id = str(job.get("id") or "") or url.rstrip("/").rsplit("/", 1)[-1]

            address = (job.get("address") or {}).get("postalAddress") or {}
            # Each inner key is optional and the set varies per job; postalAddress can even
            # be empty, so the human-readable string stays the primary.
            structured = ", ".join(
                part
                for part in (
                    address.get("addressLocality"),
                    address.get("addressRegion"),
                    address.get("addressCountry"),
                )
                if part
            )

            postings.append(
                Posting(
                    external_id=external_id,
                    title=(job.get("title") or "").strip(),
                    company=handle,
                    url=url,
                    description=(job.get("descriptionPlain") or "").strip(),
                    location=(job.get("location") or structured or None),
                    # "Last published", not "created" -- re-publishing a role resets it, so
                    # it is not a job's age. `discovered_at` on our own row is.
                    posted_at=job.get("publishedAt"),
                    extra={
                        "workplace_type": job.get("workplaceType"),
                        "is_remote": job.get("isRemote"),
                        "team": job.get("team"),
                        "apply_url": job.get("applyUrl"),
                    },
                )
            )
        return postings


register(AshbyConnector())
