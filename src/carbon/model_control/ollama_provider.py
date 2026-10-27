"""Ollama runtime provider implementation."""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from carbon.interfaces.models import (
    ChatMessage,
    ModelCapability,
    ModelMetadata,
)
from carbon.interfaces.protocols import IModelProvider

logger = logging.getLogger(__name__)


class OllamaProvider(IModelProvider):
    """Client for Ollama local inference API."""

    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout_seconds: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def is_healthy(self) -> bool:
        """Check if Ollama server daemon is responding."""
        try:
            async with self._client() as client:
                res = await client.get("/api/version")
                return res.status_code == 200
        except Exception as e:
            logger.warning(f"Ollama health check failed: {e}")
            return False

    async def list_available_models(self) -> list[ModelMetadata]:
        """Fetch list of installed models from Ollama."""
        models: list[ModelMetadata] = []
        try:
            async with self._client() as client:
                res = await client.get("/api/tags")
                if res.status_code != 200:
                    return models
                data = res.json()
                for item in data.get("models", []):
                    name = item.get("name", "")
                    details = item.get("details", {})
                    caps_raw = item.get("capabilities", ["completion"])

                    capabilities: list[ModelCapability] = []
                    for c in caps_raw:
                        try:
                            capabilities.append(ModelCapability(c.lower()))
                        except ValueError:
                            pass

                    # Default completion / chat if not explicitly listed
                    if ModelCapability.CHAT not in capabilities:
                        capabilities.append(ModelCapability.CHAT)

                    metadata = ModelMetadata(
                        id=name,
                        name=name,
                        provider="ollama",
                        family=details.get("family", "unknown"),
                        parameter_size=details.get("parameter_size", "unknown"),
                        quantization=details.get("quantization_level", "unknown"),
                        context_length=details.get("context_length", 262144),
                        memory_footprint_mb=int(item.get("size", 0) / (1024 * 1024)),
                        capabilities=capabilities,
                        is_active=False,
                        is_healthy=True,
                    )
                    models.append(metadata)
        except Exception as e:
            logger.error(f"Failed to list Ollama models: {e}")
        return models

    async def generate_stream(
        self, model_id: str, prompt: str, system: str | None = None
    ) -> AsyncGenerator[str, None]:
        """Stream generated text tokens for a prompt."""
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "stream": True,
        }
        if system:
            payload["system"] = system

        async with self._client() as client:
            async with client.stream("POST", "/api/generate", json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    raise RuntimeError(
                        f"Ollama generate error {response.status_code}: {error_text.decode('utf-8')}"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        resp = chunk.get("response")
                        if resp:
                            yield resp
                    except json.JSONDecodeError:
                        continue

    async def generate_stream_events(
        self, model_id: str, prompt: str, system: str | None = None
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream structured chunks including thinking and response tokens."""
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "stream": True,
        }
        if system:
            payload["system"] = system

        async with self._client() as client:
            async with client.stream("POST", "/api/generate", json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    raise RuntimeError(
                        f"Ollama generate error {response.status_code}: {error_text.decode('utf-8')}"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        yield {
                            "response": chunk.get("response", ""),
                            "thinking": chunk.get("thinking", ""),
                            "done": chunk.get("done", False),
                        }
                    except json.JSONDecodeError:
                        continue

    async def chat_stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream chat completion chunks with optional tool schemas."""
        formatted_messages = [{"role": m.role, "content": m.content} for m in messages]
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        async with self._client() as client:
            async with client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    raise RuntimeError(
                        f"Ollama chat error {response.status_code}: {error_text.decode('utf-8')}"
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        yield chunk
                    except json.JSONDecodeError:
                        continue

    async def load_model(self, model_id: str) -> bool:
        """Warm up model into memory."""
        try:
            async with self._client() as client:
                # Issue empty prompt to load model into memory
                res = await client.post(
                    "/api/generate",
                    json={"model": model_id, "prompt": "", "keep_alive": "5m"},
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Failed to load model '{model_id}': {e}")
            return False

    async def unload_model(self, model_id: str) -> bool:
        """Unload model from memory by setting keep_alive to 0."""
        try:
            async with self._client() as client:
                res = await client.post(
                    "/api/generate",
                    json={"model": model_id, "keep_alive": 0},
                )
                return res.status_code == 200
        except Exception as e:
            logger.error(f"Failed to unload model '{model_id}': {e}")
            return False
