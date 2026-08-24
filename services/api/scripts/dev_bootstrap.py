"""Create the schema and seed a profile so the walking skeleton has something to run with.

    python scripts/dev_bootstrap.py

Idempotent: safe to run more than once.

CV parsing lands in Phase 2. Until then the profile is seeded from this file, which is
honest about where the facts came from -- every seeded fact carries source="manual" and
verified=True, and only verified facts are ever used in an application.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import select  # noqa: E402

from localapply.config import get_settings  # noqa: E402
from localapply.db import models as m  # noqa: E402
from localapply.db.session import create_all, dispose_engine, init_engine, session_factory  # noqa: E402

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
        known = {f.key for f in result.scalars().all()}

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
                    verified=True,
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
                    verified=True,
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
