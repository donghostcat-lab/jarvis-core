"""Gemini: Websuche mit Google-Search-Grounding und Ausweichmodell.

Braucht einen API-Schlüssel aus Google AI Studio mit aktivierter Abrechnung.
Das Gemini-Pro-Abo enthält keinen API-Zugang. Bei der kostenlosen API-Stufe
darf Google Eingaben zur Produktverbesserung nutzen, deshalb die bezahlte nehmen.
Gemini ist ein Cloud-Modell: private Gespräche gehen nie hierher.
"""

import logging
from typing import Callable

import httpx

from .llm import OpenAICompatibleChat

log = logging.getLogger(__name__)

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_OPENAI_URL = f"{GEMINI_API}/openai"

SEARCH_INSTRUCTION = (
    "Beantworte die Frage knapp auf Deutsch anhand einer aktuellen Websuche. "
    "Nenne konkrete Fakten (Daten, Uhrzeiten, Preise). Wenn die Suche nichts "
    "Verlässliches liefert, sag das."
)

# (user_id, anbieter, modell, eingabe_tokens, ausgabe_tokens)
UsageCallback = Callable[[str, str, str, int, int], None]


class GeminiSearch:
    provider = "gemini"

    def __init__(self, api_key: str, model: str, on_usage: UsageCallback | None = None,
                 timeout: float = 30.0, max_sources: int = 5,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key = api_key
        self.model = model
        self.on_usage = on_usage
        self.timeout = timeout
        self.max_sources = max_sources
        self._transport = transport  # für Tests

    async def search(self, question: str, user_id: str) -> str:
        body = {
            "systemInstruction": {"parts": [{"text": SEARCH_INSTRUCTION}]},
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
            return "Die Websuche hat keine Antwort geliefert."
        cand = candidates[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()

        sources, seen = [], set()
        for chunk in (cand.get("groundingMetadata") or {}).get("groundingChunks", []):
            web = chunk.get("web") or {}
            uri = web.get("uri")
            if uri and uri not in seen:
                seen.add(uri)
                sources.append(f"- {web.get('title') or uri}: {uri}")
        out = text or "Die Websuche hat keine Antwort geliefert."
        if sources:
            out += "\n\nQuellen:\n" + "\n".join(sources[:self.max_sources])
        return out


def gemini_chat(api_key: str, model: str) -> OpenAICompatibleChat:
    """Gemini über die OpenAI-kompatible Schnittstelle, als Ausweichmodell ohne Werkzeuge."""
    return OpenAICompatibleChat("gemini", GEMINI_OPENAI_URL, model, api_key=api_key, timeout=60.0)
