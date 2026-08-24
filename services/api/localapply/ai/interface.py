"""The provider-agnostic model interface.

Every model backend implements this, so swapping a stub for Ollama for a hosted API is a
one-line change in the composition root and touches nothing else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import Enum
from typing import Protocol, runtime_checkable


class ModelRole(str, Enum):
    """What a model is *for*. The router schedules by role, not by model name."""

    REASON = "reason"
    VISION = "vision"
    EMBED = "embed"
    RERANK = "rerank"


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def generate(self, prompt: str, *, system: str | None = None, **kw) -> str: ...

    def stream(self, prompt: str, *, system: str | None = None, **kw) -> AsyncIterator[str]: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def rerank(self, query: str, documents: list[str]) -> list[float]: ...

    async def vision(self, prompt: str, image: bytes, **kw) -> str: ...

    async def health(self) -> bool: ...


class ProviderUnavailable(RuntimeError):
    """The backend is not reachable or the requested model is not installed."""
