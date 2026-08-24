"""Ollama-backed provider. Wired for Phase 4; unexercised in the walking skeleton.

Note `keep_alive`: it is how a model is actually evicted from an 8 GB card. `keep_alive=0`
on a generate call tells Ollama to unload immediately after responding, which is what makes
the router's exclusive-load discipline real rather than aspirational.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator

import httpx

from ..interface import ProviderUnavailable


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base_url}{path}", json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Ollama at {self._base_url} is unreachable: {exc}") from exc

    async def load_model(self, model: str) -> None:
        # An empty prompt with a long keep_alive warms the model without generating.
        await self._post("/api/generate", {"model": model, "prompt": "", "keep_alive": "30m"})

    async def unload_model(self, model: str) -> None:
        # keep_alive=0 evicts immediately -- the actual VRAM reclamation.
        await self._post("/api/generate", {"model": model, "prompt": "", "keep_alive": 0})

    async def generate(self, prompt: str, *, system: str | None = None, **kw) -> str:
        payload = {
            "model": kw.get("model"),
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": kw.get("temperature", 0.1)},
        }
        if system:
            payload["system"] = system
        data = await self._post("/api/generate", payload)
        return data.get("response", "")

    async def stream(self, prompt: str, *, system: str | None = None, **kw) -> AsyncIterator[str]:
        payload = {"model": kw.get("model"), "prompt": prompt, "stream": True}
        if system:
            payload["system"] = system
        async with (
            httpx.AsyncClient(timeout=self._timeout) as client,
            client.stream("POST", f"{self._base_url}/api/generate", json=payload) as response,
        ):
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if chunk.get("response"):
                    yield chunk["response"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        data = await self._post(
            "/api/embed", {"model": "nomic-embed-text", "input": texts}
        )
        return data.get("embeddings", [])

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        raise NotImplementedError("Reranking lands with the RAG work in Phase 4.")

    async def vision(self, prompt: str, image: bytes, **kw) -> str:
        data = await self._post(
            "/api/generate",
            {
                "model": kw.get("model"),
                "prompt": prompt,
                "images": [base64.b64encode(image).decode()],
                "stream": False,
            },
        )
        return data.get("response", "")

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                return response.status_code == 200
        except httpx.HTTPError:
            return False
