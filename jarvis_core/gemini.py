"""Gemini: web search with Google Search grounding, and the fallback model.

Needs an API key from Google AI Studio with billing enabled. The Gemini Pro
subscription has no API access. On the free API tier Google may use inputs to
improve its products, so use the paid tier.
Gemini is a cloud model: private conversations never go here.
"""

import logging
from typing import Callable

import httpx

from .llm import OpenAICompatibleChat

log = logging.getLogger(__name__)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_OPENAI_URL = f"{GEMINI_API}/openai"

SEARCH_INSTRUCTION = (
    "Answer the question briefly in {language} from a current web search. "
    "Give concrete facts (dates, times, prices). If the search finds nothing "
    "reliable, say so."
)

# (user_id, provider, model, input_tokens, output_tokens)
UsageCallback = Callable[[str, str, str, int, int], None]

NO_ANSWER = "The web search returned no answer."


class GeminiSearch:
    provider = "gemini"

    def __init__(self, api_key: str, model: str, on_usage: UsageCallback | None = None,
                 timeout: float = 30.0, max_sources: int = 5, language: str = "English",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self.model = model
        self.on_usage = on_usage
        self.timeout = timeout
        self.max_sources = max_sources
        self.language = language  # language of the answer, e.g. "German"
        self._transport = transport  # for tests

    async def search(self, question: str, user_id: str) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": SEARCH_INSTRUCTION.format(language=self.language)}]},
            "contents": [{"role": "user", "parts": [{"text": question}]}],
            "tools": [{"google_search": {}}],
        }
        async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
            resp = await client.post(
                f"{GEMINI_API}/models/{self.model}:generateContent",
                json=body,
                headers={"x-goog-api-key": self.api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        meta = data.get("usageMetadata") or {}
        if self.on_usage:
            self.on_usage(user_id, self.provider, self.model,
                          meta.get("promptTokenCount", 0) or 0,
                          meta.get("candidatesTokenCount", 0) or 0)

        candidates = data.get("candidates") or []
        if not candidates:
            return NO_ANSWER
        cand = candidates[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()

        sources, seen = [], set()
        for chunk in (cand.get("groundingMetadata") or {}).get("groundingChunks", []):
            web = chunk.get("web") or {}
            uri = web.get("uri")
            if uri and uri not in seen:
                seen.add(uri)
                sources.append(f"- {web.get('title') or uri}: {uri}")
        out = text or NO_ANSWER
        if sources:
            out += "\n\nSources:\n" + "\n".join(sources[:self.max_sources])
        return out


def gemini_chat(api_key: str, model: str) -> OpenAICompatibleChat:
    """Gemini over the OpenAI-compatible interface, as the fallback model without tools."""
    return OpenAICompatibleChat("gemini", GEMINI_OPENAI_URL, model, api_key=api_key, timeout=60.0)
