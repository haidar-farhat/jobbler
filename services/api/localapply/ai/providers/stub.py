"""A provider with no model behind it.

Records load/unload calls so the router's exclusive-swap behaviour can be asserted in tests
without a GPU, and returns fixed values so nothing downstream has to special-case its absence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class StubProvider:
    name = "stub"

    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.unloaded: list[str] = []
        self.calls: list[tuple[str, str]] = []

    async def load_model(self, model: str) -> None:
        self.loaded.append(model)

    async def unload_model(self, model: str) -> None:
        self.unloaded.append(model)

    async def generate(self, prompt: str, *, system: str | None = None, **kw) -> str:
        self.calls.append(("generate", kw.get("model", "")))
        return '{"action": "finish", "confidence": 1.0, "reason": "stub provider"}'

    async def stream(self, prompt: str, *, system: str | None = None, **kw) -> AsyncIterator[str]:
        yield await self.generate(prompt, system=system, **kw)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(("embed", ""))
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append(("rerank", ""))
        return [0.0 for _ in documents]

    async def vision(self, prompt: str, image: bytes, **kw) -> str:
        self.calls.append(("vision", kw.get("model", "")))
        return "stub vision response"

    async def health(self) -> bool:
        return True
