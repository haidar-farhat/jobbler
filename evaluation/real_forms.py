"""What the observer actually sees on a real application form.

Every verification in this project so far is against `evaluation/fixtures/job.html` -- a page
we wrote ourselves. The ref enumeration, the field classifier, the page-kind heuristics and
the reasoner's element table have all been proven against our own idea of what a form looks
like. That is the single largest unknown in the codebase, and it is the kind of unknown that
is discovered in month three rather than announced.

This harness closes it. It opens real, public application forms from boards that publish
their listings as JSON, observes each one exactly as a run would, and reports every place
reality and the fixture disagree.

**It observes. It does not apply.** No typing, no clicking, no submitting -- one navigation
and a read of the accessibility tree, which is the same thing `jobs/ingest.py` already does
and no more than a browser does by visiting. Nothing is sent to an employer and no profile
data is involved at any point.

    python evaluation/real_forms.py                    # a default sample
    python evaluation/real_forms.py <url> [<url> ...]  # specific forms
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api"))

from localapply.browser.session import BrowserManager  # noqa: E402
from localapply.config import get_settings  # noqa: E402
from localapply.contracts import ElementRole  # noqa: E402
from localapply.policy.field_classifier import FieldClass, classify  # noqa: E402

#: Public application forms, one per platform we can read. Chosen because their listings are
#: already reachable through the connectors, so nothing here needs a login or a workaround.
DEFAULT_FORMS = [
    "https://job-boards.greenhouse.io/vercel",
    "https://jobs.lever.co/palantir",
    "https://jobs.ashbyhq.com/linear",
]

#: How many seconds to let a page settle. Real forms load their fields with JavaScript, and
#: a fixture -- which does not -- hides exactly that difference.
SETTLE_MS = 3000


async def look(manager: BrowserManager, url: str) -> dict:
    """Open one page, observe it, and report what was there."""
    from uuid import uuid4

    from localapply.browser.observer import Observer

    session = await manager.new_session()
    try:
        await session.page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Real forms render their fields after load. Observing too early sees an empty page
        # and reports "no elements", which looks like a bug in the observer.
        await session.page.wait_for_timeout(SETTLE_MS)

        observation = await Observer(get_settings()).observe(
            session, uuid4(), screenshot=False
        )
    finally:
        await manager.close_session(session.session_id)

    roles = Counter(e.role.value for e in observation.elements)
    classified = Counter(classify(e).field_class.value for e in observation.elements)

    return {
        "url": url,
        "landed": observation.url,
        "title": observation.title,
        "page_kind": observation.page_kind.value,
        "elements": len(observation.elements),
        "visible": sum(1 for e in observation.elements if e.visible),
        "required": sum(1 for e in observation.elements if e.required),
        "roles": dict(roles),
        "classified": dict(classified),
        "text_chars": len(observation.untrusted_text),
        "unnamed": [e.ref for e in observation.elements if not e.name.strip()],
        "names": [
            (e.ref, e.role.value, e.name[:60], classify(e).field_class.value)
            for e in observation.elements
            if e.visible and e.role is not ElementRole.HEADING
        ][:40],
    }


def report(seen: dict) -> None:
    print(f"\n== {seen['url']}")
    if seen.get("error"):
        print(f"   FAILED  {seen['error']}")
        return

    if seen["landed"] != seen["url"]:
        print(f"   landed   {seen['landed']}")
    print(f"   title    {seen['title'][:70]}")
    print(f"   kind     {seen['page_kind']}")
    print(f"   elements {seen['elements']} ({seen['visible']} visible, "
          f"{seen['required']} required)")
    print(f"   text     {seen['text_chars']} chars")
    print(f"   roles    {seen['roles']}")
    print(f"   classes  {seen['classified']}")

    if seen["unnamed"]:
        # A ref the model cannot describe is a ref it cannot choose sensibly.
        print(f"   !! {len(seen['unnamed'])} element(s) with no accessible name: "
              f"{seen['unnamed'][:8]}")

    print("   what the model would be shown:")
    for ref, role, name, field_class in seen["names"][:20]:
        flag = "  <-- never autofill" if field_class == FieldClass.NEVER_AUTOFILL.value else (
            "  <-- needs review" if field_class == FieldClass.REVIEW_REQUIRED.value else ""
        )
        print(f"      {ref:<5} {role:<10} {name}{flag}")


async def main(urls: list[str]) -> int:
    settings = get_settings()
    problem = BrowserManager.check_event_loop()
    if problem:
        print(problem)
        return 1

    manager = BrowserManager(settings)
    results = []
    try:
        for url in urls:
            try:
                results.append(await look(manager, url))
            except Exception as exc:  # noqa: BLE001 - one bad page must not end the sweep
                results.append({"url": url, "error": f"{exc.__class__.__name__}: {exc}"})
            # The same courtesy the connectors extend. One page at a time, unhurried.
            await asyncio.sleep(settings.ingest_min_interval_s)
    finally:
        await manager.stop()

    for seen in results:
        report(seen)

    print("\n== what this says about the fixture")
    working = [r for r in results if not r.get("error")]
    if not working:
        print("   nothing loaded, so nothing learned")
        return 1

    unnamed = sum(len(r["unnamed"]) for r in working)
    if unnamed:
        print(f"   {unnamed} unnamed element(s) across {len(working)} pages -- the fixture "
              "names every element, so the model has never had to deal with this")
    kinds = {r["page_kind"] for r in working}
    print(f"   page kinds seen: {sorted(kinds)}")
    return 0


if __name__ == "__main__":
    targets = sys.argv[1:] or DEFAULT_FORMS
    raise SystemExit(asyncio.run(main(targets)))
