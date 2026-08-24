"""The 8 GB VRAM constraint, encoded as behaviour.

These assert the router genuinely serialises model loading rather than assuming a GPU large
enough to hold everything -- the single most consequential adaptation to the measured
hardware.
"""

from __future__ import annotations

import asyncio

import pytest
from localapply.ai.interface import ModelRole
from localapply.ai.providers.stub import StubProvider
from localapply.ai.router import DEFAULT_MODELS, ModelRouter, ModelSpec, VramExceeded

BUDGET = 8151


@pytest.fixture
def provider():
    return StubProvider()


@pytest.fixture
def router(provider):
    return ModelRouter(provider, vram_budget_mb=BUDGET)


async def test_first_load_loads_nothing_else(router, provider):
    await router.ensure_loaded(ModelRole.REASON)
    assert provider.loaded == [DEFAULT_MODELS[ModelRole.REASON].name]
    assert provider.unloaded == []


async def test_switching_role_unloads_the_incumbent_first(router, provider):
    """The behaviour that makes 8 GB workable: reasoning and vision are never co-resident."""
    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.VISION)

    assert provider.unloaded == [DEFAULT_MODELS[ModelRole.REASON].name]
    assert provider.loaded == [
        DEFAULT_MODELS[ModelRole.REASON].name,
        DEFAULT_MODELS[ModelRole.VISION].name,
    ]


async def test_reasoning_and_vision_never_resident_together(router):
    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.VISION)

    report = router.vram_report()
    assert DEFAULT_MODELS[ModelRole.REASON].name not in report.resident
    assert DEFAULT_MODELS[ModelRole.VISION].name in report.resident


async def test_repeated_use_of_one_role_does_not_reload(router, provider):
    for _ in range(5):
        await router.ensure_loaded(ModelRole.REASON)
    assert len(provider.loaded) == 1
    assert router.stats.swaps == 1


async def test_embeddings_are_pinned_and_never_evict_the_reasoner(router, provider):
    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.EMBED)
    await router.ensure_loaded(ModelRole.REASON)

    # The embed call must not have caused a swap.
    assert router.stats.swaps == 1
    assert provider.unloaded == []


async def test_vram_report_stays_within_budget(router):
    await router.ensure_loaded(ModelRole.REASON)
    report = router.vram_report()
    assert report.used_mb <= BUDGET
    assert report.free_mb == BUDGET - report.used_mb


async def test_model_too_large_for_the_card_is_refused(provider):
    """Better a clear error at load time than an opaque CUDA OOM mid-application."""
    oversized = dict(DEFAULT_MODELS)
    oversized[ModelRole.REASON] = ModelSpec("llama3.3:70b", ModelRole.REASON, 40_000)
    router = ModelRouter(provider, vram_budget_mb=BUDGET, models=oversized)

    with pytest.raises(VramExceeded, match="over the"):
        await router.ensure_loaded(ModelRole.REASON)


async def test_concurrent_requests_do_not_race_a_swap(router, provider):
    """Two coroutines wanting different models must not both think they own the GPU."""
    await asyncio.gather(
        router.ensure_loaded(ModelRole.REASON),
        router.ensure_loaded(ModelRole.VISION),
        router.ensure_loaded(ModelRole.REASON),
    )
    # Loads and unloads must interleave strictly: every unload precedes the next load.
    assert len(provider.loaded) - len(provider.unloaded) == 1


async def test_generate_ensures_the_reasoning_model(router, provider):
    await router.generate("hello")
    assert provider.loaded == [DEFAULT_MODELS[ModelRole.REASON].name]
    assert provider.calls[-1] == ("generate", DEFAULT_MODELS[ModelRole.REASON].name)


async def test_nothing_is_reported_resident_before_anything_loads(router):
    """Found by running the launcher: /health named two pinned models as resident that were
    not even installed. Operational status has to describe what is really in VRAM."""
    report = router.vram_report()
    assert report.resident == []
    assert report.used_mb == 0


async def test_a_pinned_model_appears_only_after_it_is_used(router, provider):
    await router.ensure_loaded(ModelRole.REASON)
    assert DEFAULT_MODELS[ModelRole.EMBED].name not in router.vram_report().resident

    await router.ensure_loaded(ModelRole.EMBED)
    report = router.vram_report()
    assert DEFAULT_MODELS[ModelRole.EMBED].name in report.resident
    assert report.used_mb == (
        DEFAULT_MODELS[ModelRole.REASON].vram_mb + DEFAULT_MODELS[ModelRole.EMBED].vram_mb
    )


async def test_a_swapped_out_model_stops_being_reported(router):
    await router.ensure_loaded(ModelRole.REASON)
    await router.ensure_loaded(ModelRole.VISION)

    resident = router.vram_report().resident
    assert DEFAULT_MODELS[ModelRole.VISION].name in resident
    assert DEFAULT_MODELS[ModelRole.REASON].name not in resident
