import asyncio
import csv

import numpy as np
import pytest

from conftest import OLIVER, FakeClient, resp, text_block
from jarvis_core.brain import Brain
from jarvis_core.core import Jarvis
from jarvis_core.journal import JournalStore
from jarvis_core.llm import ChatReply
from jarvis_core.memory import ShortTermMemory
from jarvis_core.router import _RULES, HashEmbedder, Router, RouterLog, RouterObserver, _main
from jarvis_core.state import StateStore
from jarvis_core.tools import ToolRegistry


@pytest.fixture(scope="module")
def router():
    return Router(HashEmbedder())


@pytest.mark.parametrize("text,rule", [
    ("Ich habe Rückenschmerzen", "gesundheit"),
    ("Termin beim Hausarzt", "gesundheit"),
    ("Kontoauszug prüfen", "finanzen"),
    ("Die Steuer ist fällig", "finanzen"),
    ("Was ist meine PIN", "zugangsdaten"),
    ("Wir haben gestritten", "beziehung"),
    ("Brief vom Amtsgericht", "recht"),
    ("Pinguine im Zoo", None),
    ("Stell die Heizungssteuerung um", None),
    ("Schreib Tomaten auf die Liste", None),
])
def test_rules(text, rule):
    hits = [name for name, rx in _RULES.items() if rx.search(text)]
    assert hits == ([rule] if rule else [])


def test_classify_sensitive_and_normal(router):
    assert router.classify("Ich habe Rückenschmerzen seit einer Woche").sensitive
    assert router.classify("Mir geht es psychisch nicht gut").reasons == ["gesundheit"]
    assert router.classify("Ich mache mir Sorgen um unsere Ehe").sensitive  # nur über das Modell
    normal = router.classify("Schreib Tomaten auf die Liste")
    assert not normal.sensitive and normal.sensitive_score < 0.5


def test_classify_intent_and_complexity(router):
    d = router.classify("Schreib Tomaten auf die Liste")
    assert (d.intent, d.complexity) == ("einkauf", "einfach")
    assert router.classify("Wann kommt der Papiermüll?").intent == "muell"
    assert router.classify("Wie spät ist es?").intent == "zeit"
    long = router.classify("Erkläre mir bitte, wie eine Wärmepumpe funktioniert und ob sie sich lohnt")
    assert long.complexity == "komplex"
    assert d.embedder == "hash-3gram" and d.latency_ms >= 0


def test_falls_back_when_model_unavailable():
    class Broken:
        name = "kaputt"

        def embed(self, texts):
            raise OSError("Download gesperrt")

    r = Router(Broken())
    assert r.classify("Wie spät ist es?").embedder == "hash-3gram"


def test_log_stats_label_and_export(tmp_path, router):
    log = RouterLog(tmp_path / "router.sqlite")
    log.add("oliver", "telegram", "Schreib Milch auf die Liste", False, router.classify("Schreib Milch auf die Liste"))
    log.add("oliver", "telegram", "Mein Blutdruck ist hoch", True, router.classify("Mein Blutdruck ist hoch"))
    log.add("maria", "alexa", "Wir sind pleite", False, router.classify("Wir sind pleite"))
    assert log.label_last("maria", sensitive=True) == "Wir sind pleite"
    assert log.label_last("niemand", sensitive=True) is None

    stats = log.stats()
    assert "3 Nachrichten" in stats
    assert "im Privatmodus gesendet: 1, davon erkannt: 1 (100%)" in stats
    assert "korrigierte Labels: 1" in stats
    assert "einkauf" in stats and "hash-3gram" in stats

    out = tmp_path / "router.csv"
    assert log.export_csv(out) == 3
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[1]["private"] == "1" and rows[1]["text"] == ""   # privater Text bleibt draußen
    assert rows[2]["label"] == "1"
    log.export_csv(out, include_private=True)
    assert list(csv.DictReader(out.open(encoding="utf-8")))[1]["text"] == "Mein Blutdruck ist hoch"


def test_empty_stats(tmp_path):
    assert "noch keine" in RouterLog(tmp_path / "r.sqlite").stats()


class LocalChat:
    name, model = "lokal", "lokal-test"

    async def complete(self, system, history, user_text):
        return ChatReply("privat beantwortet")


def make_jarvis(tmp_path, responses, observer):
    return Jarvis(
        brain=Brain(FakeClient(responses), "claude-test", ToolRegistry(), "Europe/Berlin"),
        state=StateStore(tmp_path / "jarvis.sqlite"),
        memory=ShortTermMemory(tmp_path / "kurz.sqlite"),
        private_memory=ShortTermMemory(tmp_path / "privat.sqlite"),
        journals=JournalStore(tmp_path / "journal", "Europe/Berlin"),
        private_journals=JournalStore(tmp_path / "privat-journal", "Europe/Berlin"),
        timezone="Europe/Berlin",
        local_llm=LocalChat(),
        router=observer,
    )


def test_observer_logs_without_changing_answers(tmp_path, router):
    log = RouterLog(tmp_path / "router.sqlite")
    observer = RouterObserver(router, log)
    jarvis = make_jarvis(tmp_path, [resp("end_turn", text_block("Gute Besserung"))], observer)

    async def scenario():
        normal = await jarvis.handle(OLIVER, "Ich habe Rückenschmerzen", "telegram")
        jarvis.toggle_private(OLIVER)
        private = await jarvis.handle(OLIVER, "Etwas ganz Privates", "telegram")
        await observer.drain()
        return normal, private

    normal, private = asyncio.run(scenario())
    # Beobachtung ändert nichts: sensible Nachricht ging trotzdem normal an Claude.
    assert normal.text == "Gute Besserung" and private.text == "privat beantwortet"
    # Beide Beobachtungen laufen parallel in Threads; die Reihenfolge im Protokoll ist offen.
    rows = {r[0]: r[1:] for r in log._db.execute("SELECT text, private, sensitive FROM decisions")}
    assert rows["Ich habe Rückenschmerzen"] == (0, 1)
    assert rows["Etwas ganz Privates"][0] == 1 and len(rows) == 2


def test_broken_router_never_breaks_answer(tmp_path):
    class Exploding:
        def classify(self, text):
            raise RuntimeError("kaputt")

    observer = RouterObserver(Exploding(), RouterLog(tmp_path / "router.sqlite"))
    jarvis = make_jarvis(tmp_path, [resp("end_turn", text_block("Antwort"))], observer)

    async def scenario():
        reply = await jarvis.handle(OLIVER, "Hallo", "telegram")
        await observer.drain()
        return reply

    assert asyncio.run(scenario()).text == "Antwort"


def test_cli_export(tmp_path, router, capsys):
    db = tmp_path / "router.sqlite"
    RouterLog(db).add("oliver", "telegram", "Hallo", False, router.classify("Hallo"))
    assert _main(["export", str(db), str(tmp_path / "x.csv")]) == 0
    assert "1 Zeilen" in capsys.readouterr().out
    assert _main([]) == 1


def test_hash_embedder_shape():
    v = HashEmbedder(dim=64).embed(["abc", "abc", "xyz"])
    assert v.shape == (3, 64) and np.array_equal(v[0], v[1]) and v[0].sum() > 0


def test_prepare_runs_once_across_threads():
    import threading

    class Counting(HashEmbedder):
        calls = 0

        def embed(self, texts):
            Counting.calls += 1
            return super().embed(texts)

    r = Router(Counting())
    threads = [threading.Thread(target=r._prepare) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seed_calls = Counting.calls
    r._prepare()
    assert Counting.calls == seed_calls == 2 + len(__import__("jarvis_core.router_seeds").router_seeds.INTENTS)
