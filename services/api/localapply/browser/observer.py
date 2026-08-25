"""The Observe layer: live page -> `Observation`.

The observer *describes*. It makes no decisions, scores nothing, and never acts. Its one
privileged job is assigning opaque refs (ADR 0002) and telling the session which are valid.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from playwright.async_api import Error as PlaywrightError

from ..config import Settings
from ..contracts import ElementRole, Observation, ObservedElement, PageKind
from .session import BrowserSession

# --------------------------------------------------------------------------------------
# Element enumeration.
#
# Runs in the page. Clears any previous stamps, walks interactive elements in DOM order,
# stamps each with data-la-ref, and reports accessible metadata. Kept as one evaluate() call
# so the ref set is a consistent snapshot rather than racing a mutating page.
# --------------------------------------------------------------------------------------
_ENUMERATE_JS = r"""
() => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role="button"]', '[role="link"]', '[role="checkbox"]',
    '[role="radio"]', '[role="combobox"]', '[contenteditable="true"]'
  ].join(',');

  const previous = document.querySelectorAll('[data-la-ref]');
  for (const el of previous) el.removeAttribute('data-la-ref');

  const accessibleName = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();

    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const target = document.getElementById(labelledBy);
      if (target && target.innerText.trim()) return target.innerText.trim();
    }
    if (el.id) {
      const label = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (label && label.innerText.trim()) return label.innerText.trim();
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping.innerText.trim()) return wrapping.innerText.trim();

    const placeholder = el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return placeholder.trim();

    const text = (el.innerText || '').trim();
    if (text) return text;

    const nameAttr = el.getAttribute('name');
    if (nameAttr && nameAttr.trim()) return nameAttr.trim();

    return (el.getAttribute('title') || '').trim();
  };

  const roleOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const explicit = (el.getAttribute('role') || '').toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();

    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textarea';
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'input') {
      if (type === 'file') return 'file_input';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['submit', 'button', 'reset', 'image'].includes(type)) return 'button';
      return 'textbox';
    }
    if (['button', 'link', 'checkbox', 'radio', 'combobox'].includes(explicit)) return explicit;
    return 'other';
  };

  const elements = [];
  let counter = 0;

  for (const el of document.querySelectorAll(SELECTOR)) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;

    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    const visible =
      style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      style.opacity !== '0' &&
      (rect.width > 0 || rect.height > 0);

    const ref = 'e' + (++counter);
    el.setAttribute('data-la-ref', ref);

    let options = [];
    if (el.tagName.toLowerCase() === 'select') {
      options = Array.from(el.options).map(o => o.textContent.trim()).filter(Boolean).slice(0, 50);
    }

    let value = null;
    if ('value' in el && typeof el.value === 'string') value = el.value.slice(0, 200);
    if (type === 'checkbox' || type === 'radio') value = el.checked ? 'checked' : 'unchecked';

    elements.push({
      ref,
      role: roleOf(el),
      name: accessibleName(el).slice(0, 300),
      value,
      input_type: el.tagName.toLowerCase() === 'input' ? (type || 'text') : null,
      enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
      required: el.required === true || el.getAttribute('aria-required') === 'true',
      visible,
      options
    });
  }

  return {
    title: document.title || '',
    url: window.location.href,
    text: (document.body ? document.body.innerText : '').slice(0, 20000),
    elements,
    // A captcha *challenge*, not a captcha *script*. Every modern application form loads
    // reCAPTCHA v3, which runs invisibly and asks the visitor for nothing -- and the old
    // check, "is there a recaptcha iframe", was true on every single one of them. Verified
    // against real Greenhouse and Ashby forms: both carry an `anchor` frame with
    // `size=invisible`, and both were being refused as CAPTCHA walls. That made the agent
    // unusable on every real site it would ever be pointed at.
    //
    // What actually requires a person:
    //   * a `bframe` -- the "select all the traffic lights" popup -- when it is on screen;
    //   * an anchor frame that is NOT size=invisible: the v2 "I'm not a robot" checkbox,
    //     which has to be clicked.
    // An invisible v3 frame, and the little badge in the corner, require nothing.
    has_captcha_frame: (() => {
      const frames = Array.from(document.querySelectorAll('iframe')).filter(
        f => /recaptcha|hcaptcha|turnstile/i.test(f.src || '')
             || /captcha/i.test(f.title || '')
      );
      return frames.some(f => {
        const src = f.src || '';
        if (/[?&]size=invisible/i.test(src)) return false;
        const rect = f.getBoundingClientRect();
        const style = window.getComputedStyle(f);
        const onScreen = style.visibility !== 'hidden' && style.display !== 'none'
                         && rect.width > 0 && rect.height > 0;
        if (!onScreen) return false;
        // The challenge popup, whatever its size.
        if (/\/bframe/i.test(src)) return true;
        // A visible checkbox widget. The v3 badge is inert, so it is excluded by the
        // size=invisible test above rather than by guessing at its dimensions.
        return true;
      });
    })()
  };
}
"""

#: Text that means a person is being challenged, as opposed to text that merely mentions
#: the word. Every application form carries "protected by reCAPTCHA" boilerplate in its
#: footer, and matching a bare "captcha" on that refused every real form on the internet.
_CAPTCHA_TEXT = re.compile(
    r"((i'?m|you'?re|you are) not a robot|verify (that )?you (are|'re) (a )?human|are you a robot"
    r"|select all (images|squares)|complete the (captcha|security check)"
    r"|captcha (challenge|verification) (failed|required))",
    re.IGNORECASE,
)
_CONFIRM_TEXT = re.compile(
    r"(thank you for applying|application (has been )?(received|submitted|sent)"
    r"|we'?ve received your application|successfully submitted)",
    re.IGNORECASE,
)
_LOGIN_TEXT = re.compile(r"\b(sign in|log in|login)\b", re.IGNORECASE)


#: A page with this many links and nothing to type into is a list of jobs. Eight is well
#: above any application form (which has a handful of navigation links at most) and well
#: below any real board, which lists dozens.
MANY_LINKS = 8


def infer_page_kind(
    url: str, title: str, text: str, elements: list[ObservedElement], captcha_frame: bool
) -> PageKind:
    """Cheap deterministic classification.

    Intentionally *not* a model call: page kind gates policy rule R013 (CAPTCHA/login pause),
    and a security-relevant signal should not be something a page can talk its way out of.
    A vision model may later refine this, but never below this floor.
    """
    if captcha_frame or _CAPTCHA_TEXT.search(text[:4000]):
        return PageKind.CAPTCHA

    has_password = any(e.input_type == "password" for e in elements)
    if has_password:
        return PageKind.LOGIN
    if _LOGIN_TEXT.search(title):
        return PageKind.LOGIN

    if _CONFIRM_TEXT.search(text[:4000]):
        return PageKind.CONFIRMATION

    fillable = [
        e
        for e in elements
        if e.role
        in {
            ElementRole.TEXTBOX,
            ElementRole.TEXTAREA,
            ElementRole.COMBOBOX,
            ElementRole.FILE_INPUT,
        }
        and e.visible
    ]
    # A CV upload is the one thing only an application form has.
    has_file_input = any(e.role is ElementRole.FILE_INPUT for e in elements)
    if has_file_input:
        return PageKind.APPLICATION_FORM

    links = [e for e in elements if e.role is ElementRole.LINK and e.visible]
    typeable = [
        e for e in fillable if e.role in {ElementRole.TEXTBOX, ElementRole.TEXTAREA}
    ]

    # Checked *before* the form test, and this order is the fix. A real Ashby board has
    # four comboboxes -- Department, Employment, Location, Location Type -- which are
    # filters, and "three or more fillable things" called that page an application form.
    # The run loop transitions to FORM_ANALYZED on that, so a listing page advanced the
    # state machine as though it were a form to fill in.
    #
    # What actually separates them is not how many widgets there are but what they are: a
    # listing is dominated by links and has almost nothing to type into.
    if len(links) >= MANY_LINKS and len(typeable) <= 1:
        return PageKind.JOB_LISTING

    if len(fillable) >= 3:
        return PageKind.APPLICATION_FORM

    if re.search(r"/jobs?/(view|detail)|/careers?/", url, re.IGNORECASE):
        return PageKind.JOB_DETAIL

    # Kept as a weaker fallback. It rarely fires on a real board: listing links are job
    # *titles* ("Account Executive, Commercial"), which do not contain the word "job" --
    # which is why a real Greenhouse board came back UNKNOWN before the rule above existed.
    job_links = sum(1 for e in links if "job" in e.name.lower())
    if job_links >= 3:
        return PageKind.JOB_LISTING

    return PageKind.UNKNOWN


class Observer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def observe(
        self, session: BrowserSession, run_id: UUID, *, screenshot: bool = True
    ) -> Observation:
        page = session.page

        try:
            raw = await page.evaluate(_ENUMERATE_JS)
        except PlaywrightError as exc:
            # A page mid-navigation is a normal condition, not a failure. Report an empty
            # observation; the run loop will simply observe again.
            session.invalidate()
            return Observation(
                run_id=run_id,
                url=page.url,
                title="",
                page_kind=PageKind.ERROR,
                untrusted_text=f"(page could not be read: {exc.__class__.__name__})",
            )

        elements = [ObservedElement(**item) for item in raw["elements"]]

        # Hand the session the authoritative ref set. Everything not in here fails closed.
        session.rebind({e.ref for e in elements})

        screenshot_id: UUID | None = None
        if screenshot:
            screenshot_id = await self._capture(page)

        return Observation(
            run_id=run_id,
            url=raw["url"],
            title=raw["title"],
            page_kind=infer_page_kind(
                raw["url"], raw["title"], raw["text"], elements, raw["has_captcha_frame"]
            ),
            screenshot_id=screenshot_id,
            elements=elements,
            untrusted_text=raw["text"],
        )

    async def _capture(self, page) -> UUID | None:
        screenshot_id = uuid4()
        self._settings.ensure_dirs()
        path = self._settings.screenshot_dir / f"{screenshot_id}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
        except PlaywrightError:
            return None
        return screenshot_id
