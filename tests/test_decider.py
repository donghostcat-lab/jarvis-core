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
    """Nachgebauter System-One-Server (Antwortformat wie Kev/ollaya/Jev)."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                         "body": body})
        if status != 200:
            return httpx.Response(status)
        default = {
            "sensibel": {"type": "noul", "noul": 0.91},
            "absicht": {"type": "choice", "choice": "wissen", "confidence": 0.4,
                        "probabilities": {"wissen": 0.7, "sonstiges": 0.3}},
            "aufwand": {"type": "score", "score": 1.2, "confidence": 0.3,
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
        "sensibel": Noul("Sensitive?"),
        "absicht": Choice("Intent?", {"wissen": "Knowledge", "sonstiges": "Else"}),
        "aufwand": Score("Effort?", ["simple: x", "medium: y", "complex: z"]),
    }))
    req = seen[0]
    assert req["url"] == "http://decider:11435/v1/systemone" and req["auth"] == "Bearer k"
    assert req["body"]["model"] == "laya:multilingual"
    assert req["body"]["questions"]["absicht"] == {
        "type": "choice", "instructions": "Intent?",
        "criteria": {"wissen": "Knowledge", "sonstiges": "Else"}}
    a = result.answers
    assert a["sensibel"].value == pytest.approx(0.91)
    assert a["absicht"].value == "wissen" and a["absicht"].probabilities["wissen"] == 0.7
    assert a["aufwand"].probabilities == {"simple: x": 0.1, "medium: y": 0.6, "complex: z": 0.3}
    assert result.latency_ms >= 0


@pytest.mark.parametrize("url,local", [
    ("http://decider:11435", True),           # Docker-Dienstname
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
            ("oliver", "Etwas ganz Privates", True))
    assert len(seen) == 2 and set(seen[0]["body"]["questions"]) == {"sensibel", "absicht", "aufwand"}
    rows = log._db.execute("SELECT sensitive_p, intent, complexity, error FROM shadow").fetchall()
    assert rows[0] == (pytest.approx(0.91), "wissen", "mittel", None)

    log.label_last("oliver", sensitive=True)
    stats = log.stats()
    assert "Schatten-Entscheider laya:multilingual: 2 Anfragen, 0 Fehler" in stats
    assert "im Privatmodus gesendet: 1, davon erkannt: 1 (100%)" in stats.split("Schatten")[1]
    assert "auf korrigierten Labels: Präzision 100%" in stats

    out = tmp_path / "r.csv"
    log.export_csv(out)
    exported = list(csv.DictReader(out.open(encoding="utf-8")))
    assert exported[0]["shadow_intent"] == "wissen" and exported[1]["text"] == ""


def test_cloud_shadow_never_sees_private_or_sensitive(setup):
    log, router = setup
    seen = []
    shadow = ShadowDecider(SystemOneClient("https://api.typesafe.ai", "jev-1.14",
                                           transport=server(seen=seen)))
    assert shadow.cloud is True
    observer = RouterObserver(router, log, shadow=shadow)
    observe(observer,
            ("oliver", "Etwas im Privatmodus", True),            # privat → nie in die Cloud
            ("oliver", "Ich habe Rückenschmerzen", False),      # lokal sensibel → nie in die Cloud
            ("oliver", "Schreib Tomaten auf die Liste", False))  # unkritisch → darf
    assert [s["body"]["state"] for s in seen] == ["Schreib Tomaten auf die Liste"]
    assert "sensibel" not in seen[0]["body"]["questions"]
    row = log._db.execute("SELECT cloud, sensitive_p, intent FROM shadow").fetchone()
    assert row == (1, None, "wissen")
    assert "(Cloud)" in log.stats()


def test_shadow_error_is_logged_and_never_breaks(setup):
    log, router = setup
    shadow = ShadowDecider(SystemOneClient("http://decider:11435", "laya:multilingual",
                                           transport=server(status=503)))
    observer = RouterObserver(router, log, shadow=shadow)
    observe(observer, ("oliver", "Hallo", False))
    assert log._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    error = log._db.execute("SELECT error FROM shadow").fetchone()[0]
    assert "HTTPStatusError" in error
    assert "1 Anfragen, 1 Fehler" in log.stats()


def test_score_without_legend_maps_by_index(setup):
    log, router = setup
    answers = {"aufwand": {"type": "score", "score": 1.8, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}}}
    shadow = ShadowDecider(SystemOneClient("http://decider:11435", "m", transport=server(answers=answers)))
    observe(RouterObserver(router, log, shadow=shadow), ("oliver", "Plane unseren Urlaub", False))
    assert log._db.execute("SELECT complexity FROM shadow").fetchone()[0] == "komplex"
