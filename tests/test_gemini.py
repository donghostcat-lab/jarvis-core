import asyncio
import json

import httpx

from jarvis_core.gemini import GEMINI_API, GeminiSearch
from jarvis_core.llm import OpenAICompatibleChat


def test_search_returns_text_sources_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{
                "content": {"parts": [{"text": "Das Hallenbad hat "}, {"text": "bis 21 Uhr offen."}]},
                "groundingMetadata": {"groundingChunks": [
                    {"web": {"uri": "https://bonn.de/bad", "title": "bonn.de"}},
                    {"web": {"uri": "https://bonn.de/bad", "title": "bonn.de"}},
                ]},
            }],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 8},
        })

    usage = []
    search = GeminiSearch("schluessel", "gemini-test",
                          on_usage=lambda *a: usage.append(a),
                          transport=httpx.MockTransport(handler))
    out = asyncio.run(search.search("Hallenbad Beuel heute?", "oliver"))

    assert seen["url"] == f"{GEMINI_API}/models/gemini-test:generateContent"
    assert seen["key"] == "schluessel"
    assert seen["body"]["tools"] == [{"google_search": {}}]
    assert out.startswith("Das Hallenbad hat bis 21 Uhr offen.")
    assert out.count("https://bonn.de/bad") == 1
    assert usage == [("oliver", "gemini", "gemini-test", 12, 8)]


def test_search_without_candidates():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    out = asyncio.run(GeminiSearch("k", "m", transport=transport).search("?", "oliver"))
    assert "keine Antwort" in out


def test_openai_compatible_chat_sends_system_history_and_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": " Antwort "}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        })

    chat = OpenAICompatibleChat("gemini", "https://example.test/v1/", "m", api_key="k",
                                transport=httpx.MockTransport(handler))
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": [{"type": "x"}]}]
    reply = asyncio.run(chat.complete("SYS", history, "b"))

    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert [m["content"] for m in seen["body"]["messages"]] == ["SYS", "a", "b"]
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ("Antwort", 7, 3)
