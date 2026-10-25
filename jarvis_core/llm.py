"""Provider layer for plain chat models without tools.

Claude with tools runs through brain.py. The other models live here, all spoken to
over an OpenAI-compatible chat API:
- the local model (vLLM/Ollama, e.g. on a DGX Spark) for private mode and protect mode,
- Gemini as the fallback model when Claude fails or the daily budget is used up.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class ChatReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class ChatProvider(Protocol):
    name: str
    model: str

    async def complete(self, system: str, history: list[dict], user_text: str) -> ChatReply: ...


class OpenAICompatibleChat:
    def __init__(self, name: str, base_url: str, model: str, api_key: str | None = None,
                 timeout: float = 120.0, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport  # for tests

    async def complete(self, system: str, history: list[dict], user_text: str) -> ChatReply:
        messages = [{"role": "system", "content": system}]
        messages += [m for m in history if isinstance(m.get("content"), str)]
        messages.append({"role": "user", "content": user_text})
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json={"model": self.model, "messages": messages},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        usage = data.get("usage") or {}
        return ChatReply(
            text=(data["choices"][0]["message"]["content"] or "").strip(),
            input_tokens=usage.get("prompt_tokens", 0) or 0,
            output_tokens=usage.get("completion_tokens", 0) or 0,
        )
