"""Chief of staff: store, tools and re-entry after a break."""

import asyncio
import json
import time
from datetime import date, timedelta

import pytest

from conftest import CHILD, OLIVER, FakeClient, resp, text_block
from jarvis_core.brain import Brain
from jarvis_core.chief import ChiefStore, urgency
from jarvis_core.core import Jarvis
from jarvis_core.journal import JournalStore
from jarvis_core.memory import ShortTermMemory
from jarvis_core.state import StateStore
from jarvis_core.tools import ToolContext, ToolRegistry, build_core_tools
from jarvis_core.users import Role, User

MARIA = User("maria", "Maria", Role.ADULT, telegram_ids=frozenset({3}))
TODAY = date(2026, 11, 3)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def chief(tmp_path):
    return ChiefStore(tmp_path / "jarvis.sqlite")


# ---------- Store ----------

def test_profile_fields_and_creation_time(chief):
    chief.profile_set("oliver", "role", " Data scientist ")
    created = chief.profile_created("oliver")
    chief.profile_set("oliver", "stuck", "too many things at once")
    assert chief.profile("oliver") == {"role": "Data scientist", "stuck": "too many things at once"}
    assert chief.profile_created("oliver") == created
    assert chief.profile("maria") == {}
    with pytest.raises(ValueError):
        chief.profile_set("oliver", "password", "x")


def test_v010_profile_fields_are_translated(chief):
    old = json.dumps({"rolle": "Zeitnahme", "haengen": "zu viele Baustellen"})
    chief._db.execute("INSERT INTO cos_profile VALUES ('oliver', ?, 0, 0)", (old,))
    assert chief.profile("oliver") == {"role": "Zeitnahme", "stuck": "zu viele Baustellen"}
    chief.profile_set("oliver", "style", "short")  # writes the English names back
    stored = json.loads(chief._db.execute("SELECT data FROM cos_profile").fetchone()[0])
    assert set(stored) == {"role", "stuck", "style"}


def test_waiting_for_only_own(chief):
    a = chief.waiting_add("oliver", "Roofer's quote", "Meier Ltd", TODAY)
    chief.waiting_add("maria", "Parcel", "", None)
    assert [w.what for w in chief.waiting_open("oliver")] == ["Roofer's quote"]
    assert not chief.waiting_done("maria", a)
    assert chief.waiting_done("oliver", a)
    assert chief.waiting_open("oliver") == []


def test_due_and_review(chief):
    chief.waiting_add("oliver", "overdue", due=TODAY - timedelta(days=2))
    chief.waiting_add("oliver", "soon", due=TODAY + timedelta(days=3))
    chief.waiting_add("oliver", "later", due=TODAY + timedelta(days=10))
    chief.waiting_add("oliver", "no date")
    assert [w.what for w in chief.waiting_due("oliver", TODAY)] == ["overdue", "soon"]
    chief.decision_add("oliver", "Server", "keep Strato", "Oliver", TODAY)
    chief.decision_add("oliver", "Later", "x", review=TODAY + timedelta(days=1))
    d = chief.decisions_to_review("oliver", TODAY)
    assert [x.title for x in d] == ["Server"]
    assert not chief.decision_close("maria", d[0].id)
    assert chief.decision_close("oliver", d[0].id)


@pytest.mark.parametrize("delta,text", [
    (-2, "⚠️ overdue by 2 days"), (-1, "⚠️ overdue by 1 day"),
    (0, "⚠️ due today"), (1, "due tomorrow"), (5, "due 08 Nov"),
])
def test_urgency(delta, text):
    assert urgency(TODAY + timedelta(days=delta), TODAY) == text


# ---------- Tools ----------

def tools(tmp_path, chief):
    return build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w",
                            "Europe/Berlin", chief=chief)


def call(reg, name, args, user=OLIVER):
    return run(reg.run(name, args, ToolContext(user)))


def test_profile_tool(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "set-up interview" in call(reg, "profile", {"action": "read"})
    assert "Unknown field" in call(reg, "profile", {"action": "save", "field": "x", "text": "y"})
    call(reg, "profile", {"action": "save", "field": "role", "text": "Timing"})
    out = call(reg, "profile", {"action": "read"})
    assert "role: Timing" in out and "Still open:" in out


def test_waiting_for_tool(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "YYYY-MM-DD" in call(reg, "waiting_for", {"action": "add", "what": "x", "due": "tomorrow"})
    assert "Noted (#1)" in call(reg, "waiting_for", {"action": "add", "what": "Call back",
                                                     "from_whom": "Bank", "due": "2020-01-01"})
    assert "Call back (from Bank) – ⚠️ overdue" in call(reg, "waiting_for", {"action": "list"})
    # Other people can neither see nor close someone else's entries.
    assert "not waiting" in call(reg, "waiting_for", {"action": "list"}, MARIA)
    assert "No open entry" in call(reg, "waiting_for", {"action": "done", "id": 1}, MARIA)
    assert call(reg, "waiting_for", {"action": "done", "id": 1}) == "Done."


def test_decisions_tool(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "title" in call(reg, "decisions", {"action": "record", "title": "x"})
    call(reg, "decisions", {"action": "record", "title": "Router", "decided": "observe first",
                            "owner": "Oliver", "review": "2026-12-01"})
    out = call(reg, "decisions", {"action": "list"})
    assert "Router: observe first" in out and "review 2026-12-01" in out
    assert call(reg, "decisions", {"action": "close", "id": 1}) == "Closed."


def test_profile_only_for_admin(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "profile" not in {x["name"] for x in reg.schemas(Role.ADULT)}
    assert "waiting_for" in {x["name"] for x in reg.schemas(Role.ADULT)}


def test_postpone_review(tmp_path, chief):
    reg = tools(tmp_path, chief)
    call(reg, "decisions", {"action": "record", "title": "A", "decided": "B",
                            "review": TODAY.isoformat()})
    assert "YYYY-MM-DD" in call(reg, "decisions", {"action": "postpone", "id": 1})
    assert "No open" in call(reg, "decisions", {"action": "postpone", "id": 1,
                                                "review": "2027-01-01"}, MARIA)
    assert "2027-01-01" in call(reg, "decisions", {"action": "postpone", "id": 1,
                                                   "review": "2027-01-01"})
    assert chief.decisions_to_review("oliver", TODAY) == []


def test_children_have_no_chief_tools(tmp_path, chief):
    reg = tools(tmp_path, chief)
    names = {s["name"] for s in reg.schemas(Role.CHILD)}
    assert not {"profile", "waiting_for", "decisions"} & names
    assert "may not use" in call(reg, "waiting_for", {"action": "list"}, CHILD)


# ---------- Re-entry ----------

def make_jarvis(tmp_path, n=1):
    client = FakeClient([resp("end_turn", text_block("ok"))] * n)
    state = StateStore(tmp_path / "jarvis.sqlite")
    jarvis = Jarvis(
        brain=Brain(client, "m", ToolRegistry(), "Europe/Berlin"),
        state=state, memory=ShortTermMemory(tmp_path / "short.sqlite"),
        private_memory=ShortTermMemory(tmp_path / "private.sqlite"),
        journals=JournalStore(tmp_path / "journal", "Europe/Berlin"),
        private_journals=JournalStore(tmp_path / "pj", "Europe/Berlin"),
        timezone="Europe/Berlin")
    return jarvis, client, state


def _away(state, user, days):
    state.touch(user.id, time.time() - days * 86400)


def system_of(client, i):
    return client.calls[i]["system"][0]["text"]


def test_reentry_after_break(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path, n=3)
    run(jarvis.handle(OLIVER, "Hello", "telegram"))  # very first message: no hint
    run(jarvis.handle(OLIVER, "And?", "telegram"))  # right after: no hint
    assert "has not been here" not in system_of(client, 0) + system_of(client, 1)
    _away(state, OLIVER, 12)
    run(jarvis.handle(OLIVER, "Back again", "telegram"))
    assert "Oliver has not been here for 12 days" in system_of(client, 2)


def test_voice_does_not_use_up_the_reentry(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path, n=2)
    _away(state, OLIVER, 6)
    run(jarvis.handle(OLIVER, "Alexa, ask Jarvis …", "alexa", voice=True))
    assert "has not been here" not in system_of(client, 0)
    run(jarvis.handle(OLIVER, "Back again", "telegram"))
    assert "Oliver has not been here for 6 days" in system_of(client, 1)


def test_no_reentry_for_children(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path)
    _away(state, CHILD, 10)
    run(jarvis.handle(CHILD, "Hi", "telegram"))
    assert "has not been here" not in system_of(client, 0)


def test_private_mode_counts_as_contact(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path)
    _away(state, OLIVER, 10)
    state.set_private(OLIVER.id, True)
    run(jarvis.handle(OLIVER, "private", "telegram"))  # no local model, but still contact
    assert state.last_contact(OLIVER.id) > time.time() - 60
