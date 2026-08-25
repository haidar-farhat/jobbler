"""Board connectors, against the shapes the boards actually return.

Every payload below is trimmed from a real response, not from documentation. On all three
boards the docs and the wire disagree in ways that fail *silently* -- the request returns
200, the job looks fine, and its description is empty, so it scores zero and quietly sinks
to the bottom of the board.

Those are exactly the failures a test has to catch, because nothing else will.
"""

from __future__ import annotations

import pytest
from localapply.jobs.connectors import (
    AshbyConnector,
    GreenhouseConnector,
    LeverConnector,
    connector_for,
)
from localapply.jobs.connectors.base import strip_html

# --------------------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------------------

#: `content` arrives entity-escaped: the literal bytes are `&lt;div&gt;`, not `<div>`.
GREENHOUSE = {
    "jobs": [
        {
            "id": 6136160004,
            "internal_job_id": 5196261004,
            "title": "Account Executive, Commercial",
            "company_name": "Vercel",
            "absolute_url": "https://job-boards.greenhouse.io/vercel/jobs/6136160004",
            "location": {"name": "Hybrid - London"},
            "offices": [{"location": "London, England, United Kingdom"}],
            "first_published": "2026-08-06T12:50:10-04:00",
            "updated_at": "2026-08-18T18:06:19-04:00",
            "requisition_id": "R-1234",
            "content": (
                "&lt;div class=&quot;content-intro&quot;&gt;&lt;h2&gt;About Vercel:&lt;/h2&gt;"
                "&lt;p&gt;We need strong &lt;b&gt;Python&lt;/b&gt; and Docker experience, "
                "plus FastAPI. Ampersand test: R&amp;amp;D.&lt;/p&gt;&lt;/div&gt;"
                + "&lt;p&gt;More detail about the role. &lt;/p&gt;" * 8
            ),
        }
    ]
}


def test_greenhouse_unescapes_before_stripping_tags():
    """The regression that produces a wall of `&lt;p&gt;` in the job description."""
    posting = GreenhouseConnector().parse(GREENHOUSE, "vercel")[0]

    assert "&lt;" not in posting.description
    assert "<p>" not in posting.description
    assert "Python" in posting.description
    assert "About Vercel" in posting.description


def test_greenhouse_does_not_unescape_twice():
    """A second pass corrupts the `&amp;` and `&nbsp;` that legitimately survive the first."""
    posting = GreenhouseConnector().parse(GREENHOUSE, "vercel")[0]
    assert "R&D" in posting.description


def test_greenhouse_keys_on_the_job_post_id_not_the_requisition():
    """`internal_job_id` is a different integer and several posts share one. Keyed on it,
    two distinct roles collapse into a single job."""
    posting = GreenhouseConnector().parse(GREENHOUSE, "vercel")[0]
    assert posting.external_id == "6136160004"
    assert posting.external_id != "5196261004"


def test_greenhouse_prefers_the_normalised_office_location():
    posting = GreenhouseConnector().parse(GREENHOUSE, "vercel")[0]
    assert posting.location == "London, England, United Kingdom"


def test_greenhouse_maps_the_rest_of_the_fields():
    posting = GreenhouseConnector().parse(GREENHOUSE, "vercel")[0]
    assert posting.title == "Account Executive, Commercial"
    assert posting.company == "Vercel"
    assert posting.url.endswith("/vercel/jobs/6136160004")
    # `first_published` is the posted date; `updated_at` moves on any edit.
    assert posting.posted_at == "2026-08-06T12:50:10-04:00"


def test_greenhouse_asks_for_the_content():
    """Without `?content=true` the description key is simply absent and the request still
    returns 200 -- every job on the board comes back empty and nothing looks wrong."""
    assert "content=true" in GreenhouseConnector().endpoint("vercel")


def test_a_greenhouse_job_with_no_content_is_not_usable():
    stripped = {"jobs": [dict(GREENHOUSE["jobs"][0], content=None)]}
    posting = GreenhouseConnector().parse(stripped, "vercel")[0]
    assert not posting.usable, "an empty description must not pass as a real posting"


# --------------------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------------------

#: The list endpoint returns a bare array. The title is in `text`. Two thirds of the job
#: text lives in `lists`, not in the field called "description".
LEVER = [
    {
        "id": "ac978161-6f46-4f6b-ad9e-a258e642751c",
        "text": "Aerostructures Design Engineer II",
        "hostedUrl": "https://jobs.lever.co/shieldai/ac978161-6f46-4f6b-ad9e-a258e642751c",
        "applyUrl": "https://jobs.lever.co/shieldai/ac978161/apply",
        "createdAt": 1711403416463,
        "country": "US",
        "workplaceType": "hybrid",
        "categories": {
            "location": "Wichita Metro Area",
            "allLocations": ["Wichita Metro Area", "San Diego, California", "Remote"],
            "team": "Engineering",
        },
        "descriptionPlain": "We are hiring an engineer to work on airframes.",
        "description": "<p>We are hiring an engineer to work on airframes.</p>",
        "lists": [
            {
                "text": "What you'll do",
                "content": "<li>Build things in <b>Python</b></li><li>Deploy with Docker</li>",
            },
            {
                "text": "Required qualifications",
                "content": "<li>Five years of FastAPI</li><li>Kubernetes</li>",
            },
        ],
        "additionalPlain": "Shield AI is an equal opportunity employer. " * 6,
    }
]


def test_lever_reads_the_title_from_text():
    """There is no `title` key on a Lever posting at all."""
    posting = LeverConnector().parse(LEVER, "shieldai")[0]
    assert posting.title == "Aerostructures Design Engineer II"


def test_lever_includes_the_lists_where_the_requirements_actually_are():
    """`descriptionPlain` is about a third of the posting. The other two thirds -- the
    responsibilities and requirements, and therefore every skill a scorer looks for -- are
    in `lists[].content`. Reading only the description field yields jobs that match nothing
    and look fine."""
    posting = LeverConnector().parse(LEVER, "shieldai")[0]

    assert "Python" in posting.description
    assert "Docker" in posting.description
    assert "FastAPI" in posting.description
    assert "Kubernetes" in posting.description
    assert "What you'll do" in posting.description
    assert "Required qualifications" in posting.description


def test_lever_survives_an_empty_description_field():
    """Blank on a small number of real postings. The lists are the fallback that stops
    those scoring zero, not an enrichment."""
    job = dict(LEVER[0], descriptionPlain="", description="")
    posting = LeverConnector().parse([job], "shieldai")[0]
    assert "Python" in posting.description


def test_lever_carries_the_slug_as_the_company():
    """The payload has no company field -- the endpoint is per-tenant."""
    posting = LeverConnector().parse(LEVER, "shieldai")[0]
    assert posting.company == "shieldai"


def test_lever_reads_epoch_milliseconds():
    """Seconds lands you in the year 58000."""
    posting = LeverConnector().parse(LEVER, "shieldai")[0]
    assert posting.posted_at is not None
    assert posting.posted_at.startswith("2024-03-25")


def test_lever_keeps_every_location():
    """`categories.location` alone loses all but the first; one real posting had eight."""
    posting = LeverConnector().parse(LEVER, "shieldai")[0]
    assert "Wichita Metro Area" in posting.location
    assert "San Diego, California" in posting.location


def test_lever_keeps_the_iso_country_which_is_the_reliable_one():
    posting = LeverConnector().parse(LEVER, "shieldai")[0]
    assert posting.extra["country"] == "US"


def test_lever_knows_about_the_eu_tenant_pool():
    """A slug that answers on one host 404s on the other; they are separate pools, not
    mirrors, so a 404 is not proof the board does not exist."""
    connector = LeverConnector()
    alternates = connector.alternates("shieldai")
    assert any("api.eu.lever.co" in url for url in alternates)


# --------------------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------------------

ASHBY = {
    "apiVersion": "1",
    "jobs": [
        {
            "id": "7458d4e9-da2e-47bd-98cb-adfda43d42b2",
            "title": "Engineering Manager",
            "location": "Remote - European Union",
            "isRemote": True,
            "workplaceType": "Remote",
            "team": "Engineering",
            "publishedAt": "2024-03-04T14:29:08.532+00:00",
            "jobUrl": "https://jobs.ashbyhq.com/linear/7458d4e9-da2e-47bd-98cb-adfda43d42b2",
            "applyUrl": "https://jobs.ashbyhq.com/linear/7458d4e9/application",
            "descriptionPlain": "We are looking for an engineering manager. " * 12,
            "descriptionHtml": "<p>We are looking for an engineering manager.</p>",
            "address": {"postalAddress": {"addressCountry": "NL"}},
        }
    ],
}


def test_ashby_uses_the_undocumented_id():
    """It appears in neither the docs' example JSON nor their field table."""
    posting = AshbyConnector().parse(ASHBY, "linear")[0]
    assert posting.external_id == "7458d4e9-da2e-47bd-98cb-adfda43d42b2"


def test_ashby_falls_back_to_the_url_segment_when_the_id_is_missing():
    """The documented way to arrive at the same value."""
    job = dict(ASHBY["jobs"][0])
    del job["id"]
    posting = AshbyConnector().parse({"jobs": [job]}, "linear")[0]
    assert posting.external_id == "7458d4e9-da2e-47bd-98cb-adfda43d42b2"


def test_ashby_carries_the_board_name_as_the_company():
    posting = AshbyConnector().parse(ASHBY, "linear")[0]
    assert posting.company == "linear"


def test_ashby_reads_the_plain_description():
    posting = AshbyConnector().parse(ASHBY, "linear")[0]
    assert "engineering manager" in posting.description
    assert "<p>" not in posting.description
    assert posting.usable


# --------------------------------------------------------------------------------------
# Shared behaviour
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["greenhouse", "lever", "ashby"])
def test_every_board_is_registered_and_names_its_own_handle(source):
    connector = connector_for(source)
    assert connector is not None
    assert connector.source == source
    # Every board calls the handle something different, and asking for "the handle" without
    # saying which is how you get a 404 you cannot debug.
    assert len(connector.handle_label) > 10
    assert "acme" in connector.endpoint("acme")


def test_an_unknown_board_is_not_invented():
    assert connector_for("linkedin") is None


@pytest.mark.parametrize(
    ("payload", "connector"),
    [
        (None, GreenhouseConnector()),
        ({}, GreenhouseConnector()),
        ({"jobs": None}, GreenhouseConnector()),
        ({"jobs": ["not a dict"]}, GreenhouseConnector()),
        ([], LeverConnector()),
        (["not a dict"], LeverConnector()),
        ({"jobs": [{}]}, AshbyConnector()),
    ],
)
def test_a_malformed_response_yields_nothing_rather_than_raising(payload, connector):
    """A board is a third party. Its response is untrusted input like any other, and a
    shape nobody expected must not end a sweep across four boards."""
    assert isinstance(connector.parse(payload, "acme"), list)


def test_strip_html_keeps_paragraphs_apart():
    """Without block-tag handling, "</p><p>" fuses two paragraphs and every heading runs
    into the sentence after it -- one enormous unreadable line."""
    text = strip_html("<h2>About</h2><p>First para.</p><p>Second para.</p>")
    assert "About\nFirst para." in text or "About\n\nFirst para." in text
    assert "First para.Second" not in text


def test_strip_html_turns_list_items_into_bullets():
    text = strip_html("<ul><li>Python</li><li>Docker</li></ul>")
    assert "- Python" in text
    assert "- Docker" in text


# --------------------------------------------------------------------------------------
# Against the real boards
#
# Opt in with `-m live`. Everything above is a fixed payload, which proves the mapping is
# right for the shape it was written against -- and proves nothing about whether the boards
# still return that shape. These three run once against public boards and check the one
# thing a fixed payload never can: that the field names have not moved.
#
# The assertion that matters is `short`: an empty description is how every one of these
# connectors fails silently, and the whole scoring pipeline downstream reads zero from it.
# --------------------------------------------------------------------------------------

LIVE_BOARDS = [("greenhouse", "vercel"), ("lever", "palantir"), ("ashby", "linear")]


@pytest.mark.live
@pytest.mark.parametrize(("source", "handle"), LIVE_BOARDS)
async def test_the_field_names_have_not_moved(source, handle):
    import httpx
    from localapply.jobs.connectors.base import get_json

    connector = connector_for(source)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        payload = await get_json(client, connector.endpoint(handle))

    postings = connector.parse(payload, handle)
    assert postings, f"{source} returned nothing this connector could read"

    short = [p for p in postings if len(p.description) < 200]
    assert not short, (
        f"{len(short)}/{len(postings)} {source} postings have no readable description -- "
        "the description field has almost certainly moved"
    )

    first = postings[0]
    assert first.external_id and first.title and first.url.startswith("http")
    assert first.company
    # No HTML and no entities survive into text a scorer reads and a person may see.
    assert "&lt;" not in first.description
    assert "<p>" not in first.description
