"""Create the schema and seed a profile so the walking skeleton has something to run with.

    python scripts/dev_bootstrap.py

Idempotent: safe to run more than once.

CV parsing lands in Phase 2. Until then the profile is seeded from this file, which is
honest about where the facts came from -- every seeded fact carries source="manual" and
status="accepted", and only accepted facts are ever used in an application.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import select  # noqa: E402

from localapply.config import get_settings  # noqa: E402
from localapply.db import models as m  # noqa: E402
from localapply.db.session import (  # noqa: E402
    create_all,
    dispose_engine,
    init_engine,
    session_factory,
)
from localapply.profile.facts import FactStatus  # noqa: E402

ACCEPTED = FactStatus.ACCEPTED.value

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_CV = REPO_ROOT / "evaluation" / "fixtures" / "sample-cv.txt"

PROFILE = {"full_name": "Haidar Farhat", "email": "you@example.com"}

#: SAFE_AUTOFILL facts. Keys match field_classifier.SAFE_PATTERNS.
IDENTITY_FACTS: list[tuple[str, str]] = [
    ("first_name", "Haidar"),
    ("last_name", "Farhat"),
    ("full_name", "Haidar Farhat"),
    ("email", "you@example.com"),
    ("phone", "+961 00 000 000"),
    ("city", "Beirut"),
    ("country", "Lebanon"),
    ("linkedin_url", "https://www.linkedin.com/in/example"),
    ("github_url", "https://github.com/haidar-farhat"),
    ("portfolio_url", "https://example.com"),
    ("current_title", "Software Engineer"),
    ("resume_path", str(SAMPLE_CV)),
]

#: REVIEW_REQUIRED drafts. Keys match the classifier's match labels, not field names, so one
#: draft covers every phrasing a site might use ("Expected salary", "Desired pay", "CTC").
#: These are proposals; the policy engine routes each to you before it is entered.
DRAFT_ANSWERS: list[tuple[str, str]] = [
    ("salary", "USD 4,500 / month"),
    ("work authorisation", "Yes"),
    ("relocation", "Open to remote; relocation negotiable"),
    ("availability", "One month"),
    ("experience claim", "3"),
    ("free-text narrative", "Placeholder — cover-letter generation lands in Phase 3."),
]


async def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    init_engine(settings)
    await create_all()

    async with session_factory()() as session:
        existing = (await session.execute(select(m.Profile).limit(1))).scalars().first()
        if existing is None:
            profile = m.Profile(**PROFILE)
            session.add(profile)
            await session.commit()
            print(f"Created profile {profile.full_name} <{profile.email}>")
        else:
            profile = existing
            print(f"Profile already exists: {profile.full_name}")

        result = await session.execute(
            select(m.ProfileFact).where(m.ProfileFact.profile_id == profile.id)
        )
        existing_facts = list(result.scalars().all())
        known = {f.key for f in existing_facts}

        # Never seed demo values over a real profile. These are placeholders -- an example
        # LinkedIn URL, example.com, a fixture CV path -- and the launcher runs this on
        # every start. They reached a real generated CV, which is exactly the failure this
        # guard exists to prevent.
        imported = [f for f in existing_facts if f.source != "manual"]
        if imported:
            print(
                f"Profile already has {len(imported)} imported fact(s); "
                "skipping demo seed so placeholders cannot reach your CV."
            )
            await dispose_engine()
            return

        added = 0
        for key, value in IDENTITY_FACTS:
            if key in known:
                continue
            session.add(
                m.ProfileFact(
                    profile_id=profile.id,
                    key=key,
                    value=value,
                    category="identity",
                    source="manual",
                    status=ACCEPTED,
                )
            )
            added += 1

        for key, value in DRAFT_ANSWERS:
            if key in known:
                continue
            session.add(
                m.ProfileFact(
                    profile_id=profile.id,
                    key=key,
                    value=value,
                    category="answer",
                    source="manual",
                    status=ACCEPTED,
                )
            )
            added += 1

        await session.commit()
        print(f"Added {added} profile facts.")

    await dispose_engine()

    print("\nReady. Start the API, then kick off a run:\n")
    print("  uvicorn localapply.main:app --reload --port 8000\n")
    print("  curl -X POST http://localhost:8000/agent/runs \\")
    print('       -H "Content-Type: application/json" \\')
    print('       -d \'{"start_url": "http://localhost:8000/fixtures/job.html",')
    print('            "job_title": "AI Engineer", "company": "Northwind Analytics"}\'')
    print("\nThen watch it at http://localhost:5173")


if __name__ == "__main__":
    asyncio.run(main())
