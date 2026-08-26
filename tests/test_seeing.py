"""Letting the agent look at the page.

The observer walks the accessibility tree and hands the model a table. That works, and
running it against real boards showed what it misses: eleven of Greenhouse's seventy elements
have no accessible name at all, and its dropdowns are buttons with hidden inputs behind them.
Eleven blank rows the model is asked to choose between.

The load-bearing assertion is not that vision helps. It is that **sight does not become a
new way to act**: the model may look at the page and must still answer with a ref. A
coordinate would be unaddressable, unverifiable, and exactly what a prompt-injected page
would try to induce.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from localapply.ai.interface import ModelRole
from localapply.ai.reasoner import REASONER_SYSTEM_PROMPT, ReasoningContext
from localapply.ai.router import DEFAULT_MODELS, ModelRouter, use_one_model
from localapply.ai.seeing import (
    MAX_IMAGE_BYTES,
    SEEING_SYSTEM_PROMPT,
    SeeingReasoner,
    load_screenshot,
)
from localapply.contracts import ActionType, ElementRole, Observation, ObservedElement

REPLY = '{"action": "click", "target_ref": "e2", "confidence": 0.9, "reason": "the live one"}'


def observation(settings, *, with_screenshot: bool = True) -> Observation:
    screenshot_id = uuid4() if with_screenshot else None
    if with_screenshot:
        settings.ensure_dirs()
        # A one-pixel PNG. The bytes only have to exist and be readable.
        (settings.screenshot_dir / f"{screenshot_id}.png").write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                "1f15c4890000000a49444154789c6300010000050001"
                "0d0a2db40000000049454e44ae426082"
            )
        )
    return Observation(
        run_id=uuid4(),
        url="https://example.com/apply",
        title="Apply",
        screenshot_id=screenshot_id,
        elements=[
            ObservedElement(ref="e1", role=ElementRole.BUTTON, name="Submit"),
            ObservedElement(ref="e2", role=ElementRole.BUTTON, name="Submit"),
        ],
        untrusted_text="Two buttons say Submit.",
    )


class Router:
    """Records which call was made and with what."""

    def __init__(self, reply: str = REPLY) -> None:
        self.reply = reply
        self.generate_calls: list[dict] = []
        self.vision_calls: list[dict] = []

    async def generate(self, prompt, *, system=None, **kw):
        self.generate_calls.append({"prompt": prompt, "system": system})
        return self.reply

    async def vision(self, prompt, image, *, system=None, **kw):
        self.vision_calls.append({"prompt": prompt, "image": image, "system": system})
        return self.reply


# --------------------------------------------------------------------------------------
# Sight is for perception; refs stay the only vocabulary for action
# --------------------------------------------------------------------------------------


async def test_the_model_is_shown_the_page(settings):
    router = Router()
    decision = await SeeingReasoner(router, settings).decide(
        observation(settings), ReasoningContext(goal="apply")
    )

    assert len(router.vision_calls) == 1
    assert router.vision_calls[0]["image"], "the screenshot must actually be sent"
    assert decision.action is ActionType.CLICK
    assert decision.target_ref == "e2"


async def test_a_coordinate_cannot_be_expressed(settings):
    """The architecture rests on this. A coordinate is unaddressable, unverifiable, and a
    prompt-injected page could induce a click anywhere on screen."""
    router = Router('{"action": "click", "x": 450, "y": 320, "confidence": 1.0, "reason": "go"}')
    decision = await SeeingReasoner(router, settings).decide(
        observation(settings), ReasoningContext(goal="apply")
    )
    assert decision.action is ActionType.ASK_USER


async def test_a_ref_that_was_never_observed_is_still_rejected(settings):
    """Seeing the page does not widen what may be named."""
    router = Router('{"action": "click", "target_ref": "e99", "confidence": 1.0, "reason": "go"}')
    decision = await SeeingReasoner(router, settings).decide(
        observation(settings), ReasoningContext(goal="apply")
    )
    assert decision.action is ActionType.ASK_USER
    assert "e99" in decision.reason


def test_the_target_ref_pattern_admits_no_coordinate():
    """Belt and braces on the contract itself, not just the parser."""
    from localapply.contracts import Decision
    from pydantic import ValidationError

    for attempt in ("450,320", "(450, 320)", "x=450", "button.submit"):
        with pytest.raises(ValidationError):
            Decision(action=ActionType.CLICK, target_ref=attempt, confidence=1.0, reason="go")


# --------------------------------------------------------------------------------------
# A page can write instructions in pixels
# --------------------------------------------------------------------------------------


def test_the_model_is_told_the_image_is_untrusted():
    """An attacker who cannot get text past a fence can render the same sentence into a
    screenshot."""
    assert "stranger controls" in SEEING_SYSTEM_PROMPT
    assert "drawn in pixels" in SEEING_SYSTEM_PROMPT
    assert "UNTRUSTED_WEB_CONTENT" in SEEING_SYSTEM_PROMPT


def test_the_text_rules_still_apply():
    """The seeing prompt extends the reasoner's rules rather than replacing them -- every
    constraint on the element table is still in force."""
    assert REASONER_SYSTEM_PROMPT in SEEING_SYSTEM_PROMPT


async def test_the_system_prompt_actually_reaches_the_provider(settings):
    """`vision()` silently dropped `system` while `generate()` honoured it, so a caller that
    passed one got a model with no instructions at all -- which looks like a model ignoring
    its rules rather than one that was never given any."""
    import inspect

    from localapply.ai.providers.ollama import OllamaProvider

    signature = inspect.signature(OllamaProvider.vision)
    assert "system" in signature.parameters

    source = inspect.getsource(OllamaProvider.vision)
    assert 'payload["system"] = system' in source


# --------------------------------------------------------------------------------------
# Degrading rather than failing
# --------------------------------------------------------------------------------------


async def test_no_screenshot_falls_back_to_the_text_path(settings):
    """Being blind is the old behaviour, and far better than refusing to decide."""
    router = Router()
    decision = await SeeingReasoner(router, settings).decide(
        observation(settings, with_screenshot=False), ReasoningContext(goal="apply")
    )

    assert router.vision_calls == []
    assert len(router.generate_calls) == 1
    assert decision.action is ActionType.CLICK


def test_a_missing_file_is_not_an_error(settings):
    """The row says there is a screenshot; the disk disagrees. That is a reason to use the
    table, not to end a run."""
    view = Observation(run_id=uuid4(), url="https://x", title="x", screenshot_id=uuid4())
    assert load_screenshot(view, settings) is None


def test_an_enormous_screenshot_is_skipped_rather_than_sent(settings):
    """An image that fills the context window pushes out the element table -- which is the
    part the model has to answer from."""
    settings.ensure_dirs()
    screenshot_id = uuid4()
    (settings.screenshot_dir / f"{screenshot_id}.png").write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"0" * (MAX_IMAGE_BYTES + 1)
    )
    view = Observation(run_id=uuid4(), url="https://x", title="x", screenshot_id=screenshot_id)

    # No Pillow in this project, so there is nothing to resize with -- and it declines
    # rather than pretending.
    assert load_screenshot(view, settings) is None


async def test_a_dead_model_asks_rather_than_crashing(settings):
    class Dead:
        async def generate(self, *a, **kw):
            raise ConnectionError("Ollama is not running")

        async def vision(self, *a, **kw):
            raise ConnectionError("Ollama is not running")

    decision = await SeeingReasoner(Dead(), settings).decide(
        observation(settings), ReasoningContext(goal="apply")
    )
    assert decision.action is ActionType.ASK_USER
    assert "Ollama" in decision.reason


# --------------------------------------------------------------------------------------
# One model, no swap
# --------------------------------------------------------------------------------------


async def test_one_model_serving_both_roles_never_swaps():
    """The measurement that decides whether this feature is usable: a swap costs 3-10 s, and
    the loop would pay it on every single observation."""
    from localapply.ai.providers.stub import StubProvider

    router = ModelRouter(StubProvider(), vram_budget_mb=8151)
    use_one_model(router, "qwen2.5vl:7b")

    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.VISION)
    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.VISION)

    assert router.stats.swaps == 1, "one load, and then nothing"


async def test_two_different_models_still_swap():
    """The exclusive-load guarantee must survive the optimisation: a genuinely different
    model still unloads the incumbent, because they cannot both fit."""
    from localapply.ai.providers.stub import StubProvider

    router = ModelRouter(StubProvider(), vram_budget_mb=8151)
    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.VISION)

    assert router.stats.swaps == 2
    assert DEFAULT_MODELS[ModelRole.REASON].name != DEFAULT_MODELS[ModelRole.VISION].name


async def test_the_shared_model_is_reported_honestly():
    from localapply.ai.providers.stub import StubProvider

    router = ModelRouter(StubProvider(), vram_budget_mb=8151)
    use_one_model(router, "qwen2.5vl:7b")
    await router.ensure_loaded(ModelRole.VISION)

    report = router.vram_report()
    assert "qwen2.5vl:7b" in report.resident
    # One model, counted once -- not twice for two roles.
    assert report.used_mb == DEFAULT_MODELS[ModelRole.VISION].vram_mb


def test_vision_is_off_by_default(settings):
    """It needs a model most people will not have installed, and being silently slower is
    worse than being explicitly off."""
    assert settings.vision is False


async def test_a_missing_target_is_caught_at_both_layers(settings):
    """The parser rejects it so the retry loop can correct a formatting slip, and policy
    denies it so a parser change can never make it reachable. Neither layer is load-bearing
    alone."""
    from localapply.contracts import Decision, PolicyOutcome
    from localapply.policy.capabilities import capabilities_for
    from localapply.policy.engine import PolicyEngine
    from localapply.policy.rules import RunContext

    view = observation(settings)

    # Layer one: the parser turns it into a question.
    from localapply.ai.reasoner import LLMReasoner

    parsed = LLMReasoner.parse(
        '{"action": "click", "x": 450, "y": 320, "confidence": 1.0, "reason": "go"}', view
    )
    assert parsed.action is ActionType.ASK_USER
    assert "target_ref" in parsed.reason

    # Layer two: even constructed directly, policy denies it.
    verdict = PolicyEngine().evaluate(
        Decision(action=ActionType.CLICK, confidence=1.0, reason="go"),
        view,
        RunContext(agent="application", capabilities=capabilities_for("application")),
    )
    assert verdict.outcome is PolicyOutcome.DENY
    assert verdict.rule_id == "R002_MISSING_TARGET"
