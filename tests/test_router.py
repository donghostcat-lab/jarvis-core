import asyncio
import csv

import numpy as np
import pytest

from conftest import OLIVER, FakeClient, resp, text_block
from jarvis_core.brain import Brain
from jarvis_core.core import PROTECT_NOTE, Jarvis
from jarvis_core.journal import JournalStore
from jarvis_core.llm import ChatReply
from jarvis_core.memory import ShortTermMemory
from jarvis_core.router import _RULES, HashEmbedder, Router, RouterLog, RouterObserver, _main
from jarvis_core.state import StateStore
from jarvis_core.tools import ToolRegistry
from jarvis_core.users import GUEST


@pytest.fixture(scope="module")
def router():
    return Router(HashEmbedder())


@pytest.mark.parametrize("text,rule", [
    ("Ich habe Rückenschmerzen", "health"),
    ("Termin beim Hausarzt", "health"),
    ("I need to see a doctor", "health"),
    ("Kontoauszug prüfen", "finance"),
    ("Die Steuer ist fällig", "finance"),
    ("My salary is too low", "finance"),
    ("Was ist meine PIN", "credentials"),
    ("I forgot my password", "credentials"),
    ("Wir haben gestritten", "relationship"),
    ("Brief vom Amtsgericht", "legal"),
    ("Pinguine im Zoo", None),
    ("Stell die Heizungssteuerung um", None),
    ("Schreib Tomaten auf die Liste", None),
    ("Put tomatoes on the list", None),
])
def test_rules(text, rule):
    hits = [name for name, rx in _RULES.items() if rx.search(text)]
    assert hits == ([rule] if rule else [])


def test_classify_sensitive_and_normal(router):
    assert router.classify("Ich habe Rückenschmerzen seit einer Woche").sensitive
    assert router.classify("Mir geht es psychisch nicht gut").reasons == ["health"]
    assert router.classify("Ich mache mir Sorgen um unsere Ehe").sensitive  # only via the model
    normal = router.classify("Schreib Tomaten auf die Liste")
    assert not normal.sensitive and normal.sensitive_score < 0.5


def test_classify_intent_and_complexity(router):
    d = router.classify("Schreib Tomaten auf die Liste")
    assert (d.intent, d.complexity) == ("shopping", "simple")
    assert router.classify("Wann kommt der Papiermüll?").intent == "waste"
    assert router.classify("Wie spät ist es?").intent == "time"
    long = router.classify("Erkläre mir bitte, wie eine Wärmepumpe funktioniert und ob sie sich lohnt")
    assert long.complexity == "complex"
    assert router.classify("Explain how a heat pump works").complexity == "complex"
    assert d.embedder == "hash-3gram" and d.latency_ms >= 0


def test_falls_back_when_model_unavailable():
    class Broken:
        name = "broken"

        def embed(self, texts):
            raise OSError("download blocked")

    r = Router(Broken())
    assert r.classify("Wie spät ist es?").embedder == "hash-3gram"


def test_log_stats_label_and_export(tmp_path, router):
    log = RouterLog(tmp_path / "router.sqlite")
    log.add("oliver", "telegram", "Schreib Milch auf die Liste", False, router.classify("Schreib Milch auf die Liste"))
    log.add("oliver", "telegram", "Mein Blutdruck ist hoch", True, router.classify("Mein Blutdruck ist hoch"))
    log.add("maria", "alexa", "Wir sind pleite", False, router.classify("Wir sind pleite"))
    assert log.label_last("maria", sensitive=True) == "Wir sind pleite"
    assert log.label_last("nobody", sensitive=True) is None

    stats = log.stats()
    assert "3 messages" in stats
    assert "sent in private mode: 1, of which detected: 1 (100%)" in stats
    assert "corrected labels: 1" in stats
    assert "shopping" in stats and "hash-3gram" in stats

    out = tmp_path / "router.csv"
    assert log.export_csv(out) == 3
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[1]["private"] == "1" and rows[1]["text"] == ""   # private text stays out
    assert rows[2]["label"] == "1"
    log.export_csv(out, include_private=True)
    assert list(csv.DictReader(out.open(encoding="utf-8")))[1]["text"] == "Mein Blutdruck ist hoch"


def test_empty_stats(tmp_path):
    assert "no messages" in RouterLog(tmp_path / "r.sqlite").stats()


class LocalChat:
    name, model = "local", "local-test"

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def complete(self, system, history, user_text):
        self.calls.append((history, user_text))
        if self.fail:
            raise RuntimeError("unreachable")
        return ChatReply("answered privately")


def make_jarvis(tmp_path, responses, observer, protect=False, local_llm=None):
    client = FakeClient(responses)
    jarvis = Jarvis(
        brain=Brain(client, "claude-test", ToolRegistry(), "Europe/Berlin"),
        state=StateStore(tmp_path / "jarvis.sqlite"),
        memory=ShortTermMemory(tmp_path / "short.sqlite"),
        private_memory=ShortTermMemory(tmp_path / "private.sqlite"),
        journals=JournalStore(tmp_path / "journal", "Europe/Berlin"),
        private_journals=JournalStore(tmp_path / "private-journal", "Europe/Berlin"),
        timezone="Europe/Berlin",
        local_llm=LocalChat() if local_llm is None else local_llm,
        router=observer,
        protect=protect,
    )
    return jarvis, client


def test_observer_logs_without_changing_answers(tmp_path, router):
    log = RouterLog(tmp_path / "router.sqlite")
    observer = RouterObserver(router, log)
    jarvis, _ = make_jarvis(tmp_path, [resp("end_turn", text_block("Get well soon"))], observer)

    async def scenario():
        normal = await jarvis.handle(OLIVER, "Ich habe Rückenschmerzen", "telegram")
        jarvis.toggle_private(OLIVER)
        private = await jarvis.handle(OLIVER, "Something very private", "telegram")
        await observer.drain()
        return normal, private

    normal, private = asyncio.run(scenario())
    # Observing changes nothing: the sensitive message still went to Claude as usual.
    assert normal.text == "Get well soon" and private.text == "answered privately"
    # Both observations run in parallel threads; the order in the log is open.
    rows = {r[0]: r[1:] for r in log._db.execute("SELECT text, private, sensitive FROM decisions")}
    assert rows["Ich habe Rückenschmerzen"] == (0, 1)
    assert rows["Something very private"][0] == 1 and len(rows) == 2


def test_broken_router_never_breaks_answer(tmp_path):
    class Exploding:
        def classify(self, text):
            raise RuntimeError("broken")

    observer = RouterObserver(Exploding(), RouterLog(tmp_path / "router.sqlite"))
    jarvis, _ = make_jarvis(tmp_path, [resp("end_turn", text_block("Answer"))], observer)

    async def scenario():
        reply = await jarvis.handle(OLIVER, "Hello", "telegram")
        await observer.drain()
        return reply

    assert asyncio.run(scenario()).text == "Answer"


# ---------- Protect mode ----------

def test_protect_mode_keeps_sensitive_messages_local(tmp_path, router):
    log = RouterLog(tmp_path / "router.sqlite")
    local = LocalChat()
    jarvis, client = make_jarvis(tmp_path, [resp("end_turn", text_block("From Claude"))],
                                 RouterObserver(router, log), protect=True, local_llm=local)

    sensitive = asyncio.run(jarvis.handle(OLIVER, "I have had back pain for a week", "telegram"))
    assert sensitive.text == "answered privately" + PROTECT_NOTE
    assert client.calls == []
    # Kept in the private memory and journal, so it never reaches a later cloud prompt.
    assert jarvis.private_memory.history("oliver")[0]["content"] == "I have had back pain for a week"
    assert jarvis.memory.history("oliver") == []
    assert list((tmp_path / "private-journal" / "oliver").rglob("*.md"))

    normal = asyncio.run(jarvis.handle(OLIVER, "Put tomatoes on the list", "telegram"))
    assert normal.text == "From Claude" and len(local.calls) == 1
    assert all("back pain" not in str(m) for m in client.calls[0]["messages"])
    # protect mode logs each decision before answering
    assert log._db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2


def test_protect_mode_fails_closed_when_router_breaks(tmp_path):
    class Exploding:
        def classify(self, text):
            raise RuntimeError("broken")

    observer = RouterObserver(Exploding(), RouterLog(tmp_path / "router.sqlite"))
    jarvis, client = make_jarvis(tmp_path, [], observer, protect=True)
    reply = asyncio.run(jarvis.handle(OLIVER, "Hello", "telegram"))
    assert reply.text.startswith("answered privately") and client.calls == []


@pytest.mark.parametrize("local_llm,text", [
    (False, "no local model is set up yet"),
    (LocalChat(fail=True), "cannot be reached"),
])
def test_protect_mode_never_falls_back_to_the_cloud(tmp_path, router, local_llm, text):
    observer = RouterObserver(router, RouterLog(tmp_path / "router.sqlite"))
    jarvis, client = make_jarvis(tmp_path, [], observer, protect=True,
                                 local_llm=local_llm or None)
    if local_llm is False:
        jarvis.local_llm = None
    reply = asyncio.run(jarvis.handle(OLIVER, "Ich habe Rückenschmerzen", "telegram"))
    assert text in reply.text and PROTECT_NOTE not in reply.text
    assert client.calls == []


def test_protect_mode_guest_leaves_no_private_history(tmp_path, router):
    local = LocalChat()
    observer = RouterObserver(router, RouterLog(tmp_path / "router.sqlite"))
    jarvis, _ = make_jarvis(tmp_path, [], observer, protect=True, local_llm=local)
    asyncio.run(jarvis.handle(GUEST, "Ich habe Rückenschmerzen", "alexa", voice=True))
    asyncio.run(jarvis.handle(GUEST, "Welche Schmerzen hatte die Person vorhin?", "alexa", voice=True))
    assert [history for history, _ in local.calls] == [[], []]
    assert jarvis.private_memory.history("guest") == []


def test_protect_mode_needs_a_router(tmp_path):
    with pytest.raises(ValueError, match="needs a router"):
        make_jarvis(tmp_path, [], None, protect=True)


def test_cli_export(tmp_path, router, capsys):
    db = tmp_path / "router.sqlite"
    RouterLog(db).add("oliver", "telegram", "Hallo", False, router.classify("Hallo"))
    assert _main(["export", str(db), str(tmp_path / "x.csv")]) == 0
    assert "1 rows" in capsys.readouterr().out
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
