"""Greenhouse job boards.

    https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true

Two traps, both verified against live responses, and either one silently produces jobs with
no description that then score zero:

  1. **`?content=true` is not optional.** Without it the `content` key is simply absent and
     the request still returns 200. A naive `job["content"]` yields nothing for every job on
     the board while everything looks like it worked.
  2. **`content` is entity-escaped HTML, not HTML.** The literal bytes are
     `&lt;div&gt;&lt;h2&gt;About...`, so a strip-tags pass finds no tags to strip and hands
     the scorer a wall of `&lt;p&gt;`. It must be unescaped exactly once -- twice corrupts
     the `&nbsp;` and `&amp;` that legitimately survive the first pass.
"""

from __future__ import annotations

import html

from .base import Posting, register, strip_html


class GreenhouseConnector:
    source = "greenhouse"
    handle_label = "Board token (the slug in job-boards.greenhouse.io/<token>)"

    def endpoint(self, handle: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{handle}/jobs?content=true"

    def parse(self, payload: object, handle: str) -> list[Posting]:
        # `.get("jobs", [])` is not enough: a board answering {"jobs": null} returns
        # None, not the default, and a sweep across four boards ends on the one that did.
        jobs = (payload.get("jobs") or []) if isinstance(payload, dict) else []
        postings = []
        for job in jobs:
            if not isinstance(job, dict):
                continue

            # `id` is the job *post* id -- the one in absolute_url and the retrieve
            # endpoint. `internal_job_id` is a different integer, is the requisition, and is
            # shared by several posts. Keyed on the wrong one, two roles collapse into one.
            # Both exceed 32 bits, so they stay strings.
            external_id = str(job.get("id") or "")

            # Unescape exactly once, then strip. See the module docstring.
            content = job.get("content") or ""
            description = strip_html(html.unescape(content)) if content else ""

            location = job.get("location") or {}
            # `offices[].location` is the normalised form ("London, England, United
            # Kingdom") where `location.name` is whatever the recruiter typed. Prefer the
            # clean one and fall back to the typed one.
            offices = job.get("offices") or []
            office = next(
                (o.get("location") for o in offices if isinstance(o, dict) and o.get("location")),
                None,
            )

            postings.append(
                Posting(
                    external_id=external_id,
                    title=(job.get("title") or "").strip(),
                    # Present on every board checked; the requested token is the fallback.
                    company=(job.get("company_name") or handle).strip(),
                    url=(job.get("absolute_url") or "").strip(),
                    description=description,
                    location=(office or location.get("name") or None),
                    # `first_published` is when it went up. `updated_at` moves on any edit,
                    # so it answers "has this changed", never "how old is this".
                    posted_at=job.get("first_published") or job.get("updated_at"),
                    extra={"requisition_id": job.get("requisition_id")},
                )
            )
        return postings


register(GreenhouseConnector())
