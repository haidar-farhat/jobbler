"""A provider that returns whatever you tell it to.

Lets every model-dependent path be tested deterministically -- including the paths that only
happen when a model misbehaves: unparseable output, an invented ref, prose that claims a
skill the profile does not have. Those are the cases that matter most and are the hardest to
provoke from a real model on demand.
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class ScriptedProvider:
    """Replies come from a queue; the last one repeats once the queue is empty."""

    name = "scripted"

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.prompts: list[str] = []
        self.systems: list[str | None] = []
        self.loaded: list[str] = []
        self.unloaded: list[str] = []
        #: Set to raise from generate(), to exercise the "model is down" path.
        self.fail_with: Exception | None = None

    async def load_model(self, model: str) -> None:
        self.loaded.append(model)

    async def unload_model(self, model: str) -> None:
        self.unloaded.append(model)

    async def generate(self, prompt: str, *, system: str | None = None, **kw) -> str:
        self.prompts.append(prompt)
        self.systems.append(system)
        if self.fail_with is not None:
            raise self.fail_with
        if not self.replies:
            return ""
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def stream(self, prompt: str, *, system: str | None = None, **kw) -> AsyncIterator[str]:
        yield await self.generate(prompt, system=system, **kw)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [0.0 for _ in documents]

    async def vision(self, prompt: str, image: bytes, **kw) -> str:
        return await self.generate(prompt, **kw)

    async def health(self) -> bool:
        return self.fail_with is None
