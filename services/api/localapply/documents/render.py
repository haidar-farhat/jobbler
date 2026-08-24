"""DocumentPlan -> HTML -> PDF.

PDF generation reuses the Chromium that Playwright already installs for the browser agent,
rather than adding a second rendering stack. One dependency, real PDF output, and the
document looks exactly like the HTML you can preview in the dashboard.

Rendering is the last step *after* `assert_grounded`. Nothing reaches a page that has not
already been checked against your accepted facts.
"""

from __future__ import annotations

import html
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .generator import DocumentPlan
from .taxonomy import group_skills, is_presentable

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_environment = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

TEMPLATES = {
    "master_cv": "cv.html",
    "tailored_cv": "cv.html",
    "cover_letter": "cover_letter.html",
}


#: Order of the contact line. City and country are joined into one place, because
#: "Beirut | Lebanon" as two pipe-separated items reads like two separate facts.
_CONTACT_ORDER = ("email", "phone", "location", "linkedin_url", "github_url", "portfolio_url")


def _tidy_url(value: str) -> str:
    """Show a link the way a CV does: no scheme, no trailing slash, no "www."."""
    text = value.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if text.lower().startswith("www."):
        text = text[4:]
    return text


def build_contact_line(contact: dict[str, str]) -> str:
    """One plain-text line, pipe separated.

    Literal characters rather than CSS `::after` content: generated content is not document
    text, and a contact line is the one thing a parser must never lose.
    """
    place = ", ".join(
        part for part in (contact.get("city"), contact.get("country")) if part
    )
    values = {**contact, "location": place}

    parts = []
    for key in _CONTACT_ORDER:
        value = (values.get(key) or "").strip()
        if not value:
            continue
        parts.append(_tidy_url(value) if key.endswith("_url") else value)
    return "  |  ".join(parts)


def render_html(plan: DocumentPlan) -> str:
    template = _environment.get_template(TEMPLATES.get(plan.kind, "cv.html"))

    # Skills are grouped for the CV. Junk ("OOP", "Data Structures & Algorithms") and
    # phrases ("Python-based AI workflows") are filtered here rather than in the profile,
    # because they may be perfectly reasonable facts -- they are just not CV skill entries.
    skills_section = next(
        (s for s in plan.sections if s.heading == "Skills"), None
    )
    skill_groups: list[tuple[str, list[str]]] = []
    if skills_section is not None:
        presentable = [
            item.text for item in skills_section.items if is_presentable(item.text)
        ]
        skill_groups = group_skills(presentable)

    return template.render(
        plan=plan,
        sections=plan.visible_sections(),
        contact=plan.contact,
        contact_line=build_contact_line(plan.contact),
        skill_groups=skill_groups,
        # The contact block is grounded through a hidden section; never shown as a heading.
        body_sections=[s for s in plan.visible_sections() if s.heading != "Contact"],
        esc=html.escape,
    )


async def render_pdf(plan: DocumentPlan, output_path: Path, browser_manager=None) -> Path:
    """Print the plan to a PDF using Chromium.

    Uses its own short-lived browser rather than a session from `BrowserManager`: document
    rendering must not consume one of the capped agent browser slots, and it has no business
    sharing a context with a page that has visited a job site.
    """
    from playwright.async_api import async_playwright

    markup = render_html(plan)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await (await browser.new_context()).new_page()
            await page.set_content(markup, wait_until="load")
            await page.pdf(
                path=str(output_path),
                format="A4",
                print_background=True,
                margin={"top": "13mm", "bottom": "13mm", "left": "14mm", "right": "14mm"},
            )
        finally:
            await browser.close()

    return output_path
