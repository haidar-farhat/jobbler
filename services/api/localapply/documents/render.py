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


def render_html(plan: DocumentPlan) -> str:
    template = _environment.get_template(TEMPLATES.get(plan.kind, "cv.html"))
    return template.render(
        plan=plan,
        sections=plan.visible_sections(),
        contact=plan.contact,
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
                margin={"top": "14mm", "bottom": "14mm", "left": "14mm", "right": "14mm"},
            )
        finally:
            await browser.close()

    return output_path
