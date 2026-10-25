import asyncio
import csv
import json

import httpx
import pytest

from jarvis_core.decider import Choice, Noul, Score, SystemOneClient
from jarvis_core.router import (HashEmbedder, Router, RouterLog, RouterObserver, ShadowDecider,
                           is_local_url)


def run(coro):
    return asyncio.run(coro)


def server(answers=None, seen=None, status=200, model="laya:multilingual"):
    """A fake System One server (answer format like Kev/ollaya/Jev)."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                         "body": body})
        if status != 200:
            return httpx.Response(status)
        default = {
            "sensitive": {"type": "noul", "noul": 0.91},
            "intent": {"type": "choice", "choice": "knowledge", "confidence": 0.4,
                        "probabilities": {"knowledge": 0.7, "other": 0.3}},
            "effort": {"type": "score", "score": 1.2, "confidence": 0.3,
                        "legend": {"0": "simple: x", "1": "medium: y", "2": "complex: z"},
                        "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}},
        }
        chosen = answers or default
        return httpx.Response(200, json={
            "model": model, "answers": {k: v for k, v in chosen.items() if k in body["questions"]},
            "usage": {"input_tokens": 50}})
    return httpx.MockTransport(handler)


def test_client_request_and_parsing():
    seen = []
    client = SystemOneClient("http://decider:11435/", "laya:multilingual", api_key="k",
                             transport=server(seen=seen))
    result = run(client.decide("Ich habe Rückenschmerzen", {
        "sensitive": Noul("Sensitive?"),
        "intent": Choice("Intent?", {"knowledge": "Knowledge", "other": "Else"}),
        "effort": Score("Effort?", ["simple: x", "medium: y", "complex: z"]),
    }))
    req = seen[0]
    assert req["url"] == "http://decider:11435/v1/systemone" and req["auth"] == "Bearer k"
    assert req["body"]["model"] == "laya:multilingual"
    assert req["body"]["questions"]["intent"] == {
        "type": "choice", "instructions": "Intent?",
        "criteria": {"knowledge": "Knowledge", "other": "Else"}}
    a = result.answers
    assert a["sensitive"].value == pytest.approx(0.91)
    assert a["intent"].value == "knowledge" and a["intent"].probabilities["knowledge"] == 0.7
    assert a["effort"].probabilities == {"simple: x": 0.1, "medium: y": 0.6, "complex: z": 0.3}
    assert result.latency_ms >= 0


@pytest.mark.parametrize("url,local", [
    ("http://decider:11435", True),           # Docker service name
    ("http://localhost:11435", True),
    ("http://127.0.0.1:8009", True),
    ("http://192.168.1.20:11435", True),
    ("http://10.0.0.2:8000", True),
    ("http://100.101.102.103:11435", True),   # Tailscale
    ("http://spark.tail1234.ts.net:11435", True),
    ("https://api.typesafe.ai", False),
    ("https://kev.example.modal.run", False),
    ("http://8.8.8.8:11435", False),
    ("", False),
])
def test_is_local_url(url, local):
    assert is_local_url(url) is local


@pytest.fixture
def setup(tmp_path):
    log = RouterLog(tmp_path / "router.sqlite")
    return log, Router(HashEmbedder())


def observe(observer, *messages):
    async def go():
        for user, text, private in messages:
            observer.observe(user, "telegram", text, private)
            await observer.drain()
    run(go())


def test_local_shadow_sees_everything_and_is_compared(setup, tmp_path):
    log, router = setup
    seen = []
    shadow = ShadowDecider(SystemOneClient("http://decider:11435", "laya:multilingual",
                                           transport=server(seen=seen)))
    assert shadow.cloud is False
    observer = RouterObserver(router, log, shadow=shadow)
    observe(observer, ("oliver", "Ich habe Rückenschmerzen", False),
            ("oliver", "Something very private", True))
    assert len(seen) == 2 and set(seen[0]["body"]["questions"]) == {"sensitive", "intent", "effort"}
    rows = log._db.execute("SELECT sensitive_p, intent, complexity, error FROM shadow").fetchall()
    assert rows[0] == (pytest.approx(0.91), "knowledge", "medium", None)

    log.label_last("oliver", sensitive=True)
    stats = log.stats()
    assert "Shadow decider laya:multilingual: 2 requests, 0 errors" in stats
    assert "sent in private mode: 1, of which detected: 1 (100%)" in stats.split("Shadow")[1]
    assert "on corrected labels: precision 100%" in stats

    out = tmp_path / "r.csv"
    log.export_csv(out)
    exported = list(csv.DictReader(out.open(encoding="utf-8")))
    assert exported[0]["shadow_intent"] == "knowledge" and exported[1]["text"] == ""


def test_cloud_shadow_never_sees_private_or_sensitive(setup):
    log, router = setup
    seen = []
    shadow = ShadowDecider(SystemOneClient("https://api.typesafe.ai", "jev-1.14",
                                           transport=server(seen=seen)))
    assert shadow.cloud is True
    observer = RouterObserver(router, log, shadow=shadow)
    observe(observer,
            ("oliver", "Something in private mode", True),        # private → never to the cloud
            ("oliver", "Ich habe Rückenschmerzen", False),      # sensitive locally → never to the cloud
            ("oliver", "Schreib Tomaten auf die Liste", False))  # harmless → allowed
    assert [s["body"]["state"] for s in seen] == ["Schreib Tomaten auf die Liste"]
    assert "sensitive" not in seen[0]["body"]["questions"]
    row = log._db.execute("SELECT cloud, sensitive_p, intent FROM shadow").fetchone()
    assert row == (1, None, "knowledge")
    assert "(cloud)" in log.stats()


def test_shadow_error_is_logged_and_never_breaks(setup):
    log, router = setup
    shadow = ShadowDecider(SystemOneClient("http://decider:11435", "laya:multilingual",
                                           transport=server(status=503)))
    observer = RouterObserver(router, log, shadow=shadow)
    observe(observer, ("oliver", "Hallo", False))
    assert log._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    error = log._db.execute("SELECT error FROM shadow").fetchone()[0]
    assert "HTTPStatusError" in error
    assert "1 requests, 1 errors" in log.stats()


def test_score_without_legend_maps_by_index(setup):
    log, router = setup
    answers = {"effort": {"type": "score", "score": 1.8, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}}}
    shadow = ShadowDecider(SystemOneClient("http://decider:11435", "m", transport=server(answers=answers)))
    observe(RouterObserver(router, log, shadow=shadow), ("oliver", "Plane unseren Urlaub", False))
    assert log._db.execute("SELECT complexity FROM shadow").fetchone()[0] == "complex"
