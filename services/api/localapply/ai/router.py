"""Model router for a single 8 GB GPU.

The design doc assumes a reasoning model, a vision model, and embeddings all resident at
once. Measured hardware says otherwise: an RTX 5070 Laptop has **8151 MiB**, and a 7-8B Q4
reasoning model alone is ~5 GB with a vision model another ~4 GB. They cannot coexist.

So loading is **exclusive and sequential**: one large model resident at a time, guarded by an
async lock, with small embeddings pinned. A role change costs a real unload/load cycle
(3-10 s), which makes model scheduling an *orchestration* concern -- the run loop should batch
work by role rather than alternating, or it will thrash.

This lands now, with stub models behind it, so the cost is visible in the architecture from
the start instead of surfacing as a mystery latency in Phase 4.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .interface import LLMProvider, ModelRole


@dataclass(frozen=True)
class ModelSpec:
    name: str
    role: ModelRole
    vram_mb: int
    #: Pinned models stay resident and are excluded from the exclusive-load dance.
    pinned: bool = False


#: Sensible defaults for an 8 GB card. Sizes are approximate Q4 footprints.
DEFAULT_MODELS: dict[ModelRole, ModelSpec] = {
    ModelRole.REASON: ModelSpec("qwen2.5:7b-instruct-q4_K_M", ModelRole.REASON, 5_000),
    ModelRole.VISION: ModelSpec("qwen2.5vl:7b-q4_K_M", ModelRole.VISION, 4_400),
    ModelRole.EMBED: ModelSpec("nomic-embed-text", ModelRole.EMBED, 300, pinned=True),
    ModelRole.RERANK: ModelSpec("bge-reranker-v2-m3", ModelRole.RERANK, 600, pinned=True),
}


@dataclass
class SwapStats:
    swaps: int = 0
    total_swap_ms: int = 0
    last_swap_ms: int = 0

    @property
    def mean_swap_ms(self) -> int:
        return self.total_swap_ms // self.swaps if self.swaps else 0


@dataclass
class VramReport:
    budget_mb: int
    resident: list[str] = field(default_factory=list)
    used_mb: int = 0

    @property
    def free_mb(self) -> int:
        return max(0, self.budget_mb - self.used_mb)


class VramExceeded(RuntimeError):
    pass


class ModelRouter:
    """Routes work to a provider, ensuring the right model is resident first."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        vram_budget_mb: int,
        models: dict[ModelRole, ModelSpec] | None = None,
    ) -> None:
        self._provider = provider
        self._budget_mb = vram_budget_mb
        self._models = dict(models or DEFAULT_MODELS)
        self._lock = asyncio.Lock()
        #: The one non-pinned model currently loaded.
        self._resident: ModelRole | None = None
        self._pinned: set[ModelRole] = {r for r, s in self._models.items() if s.pinned}
        self.stats = SwapStats()

    @property
    def provider(self) -> LLMProvider:
        return self._provider

    def spec(self, role: ModelRole) -> ModelSpec:
        return self._models[role]

    def _pinned_mb(self) -> int:
        return sum(self._models[r].vram_mb for r in self._pinned)

    async def ensure_loaded(self, role: ModelRole) -> ModelSpec:
        """Make `role` the resident model, unloading the incumbent if it is a different one.

        Serialised by a lock: two coroutines must never race a model swap on one GPU.
        """
        spec = self._models[role]

        if role in self._pinned:
            return spec

        async with self._lock:
            if self._resident is role:
                return spec

            required = spec.vram_mb + self._pinned_mb()
            if required > self._budget_mb:
                raise VramExceeded(
                    f"Model {spec.name!r} needs {spec.vram_mb} MB plus {self._pinned_mb()} MB "
                    f"pinned, over the {self._budget_mb} MB budget. Use a smaller quantisation."
                )

            started = time.perf_counter()
            if self._resident is not None:
                await self._unload(self._models[self._resident])
            await self._load(spec)
            elapsed = int((time.perf_counter() - started) * 1000)

            self._resident = role
            self.stats.swaps += 1
            self.stats.last_swap_ms = elapsed
            self.stats.total_swap_ms += elapsed
            return spec

    async def _load(self, spec: ModelSpec) -> None:
        loader = getattr(self._provider, "load_model", None)
        if loader is not None:
            await loader(spec.name)

    async def _unload(self, spec: ModelSpec) -> None:
        unloader = getattr(self._provider, "unload_model", None)
        if unloader is not None:
            await unloader(spec.name)

    def vram_report(self) -> VramReport:
        resident = [self._models[r].name for r in sorted(self._pinned, key=lambda r: r.value)]
        used = self._pinned_mb()
        if self._resident is not None:
            resident.append(self._models[self._resident].name)
            used += self._models[self._resident].vram_mb
        return VramReport(budget_mb=self._budget_mb, resident=resident, used_mb=used)

    # --- Convenience wrappers: ensure the model, then call through. ---------------------

    async def generate(self, prompt: str, *, system: str | None = None, **kw) -> str:
        spec = await self.ensure_loaded(ModelRole.REASON)
        return await self._provider.generate(prompt, system=system, model=spec.name, **kw)

    async def vision(self, prompt: str, image: bytes, **kw) -> str:
        spec = await self.ensure_loaded(ModelRole.VISION)
        return await self._provider.vision(prompt, image, model=spec.name, **kw)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        await self.ensure_loaded(ModelRole.EMBED)
        return await self._provider.embed(texts)

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        await self.ensure_loaded(ModelRole.RERANK)
        return await self._provider.rerank(query, documents)
