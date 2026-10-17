"""Chief of Staff: Speicher, Werkzeuge, Morgenbrief, Lebenszeichen, Nachjustieren, Wiedereinstieg."""

import asyncio
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import KIND, OLIVER, FakeClient, resp, text_block
from jarvis_core.brain import Brain
from jarvis_core.chief import ChiefStore, urgency
from jarvis_core.core import Jarvis
from jarvis_core.journal import JournalStore
from jarvis_core.memory import ShortTermMemory
from jarvis_core.state import StateStore
from jarvis_core.tools import ToolContext, ToolRegistry, build_core_tools
from jarvis_core.users import Role, User

TZ = ZoneInfo("Europe/Berlin")
MARIA = User("maria", "Maria", Role.ERWACHSEN, telegram_ids=frozenset({3}))
TODAY = date(2026, 11, 3)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def chief(tmp_path):
    return ChiefStore(tmp_path / "jarvis.sqlite")


# ---------- Speicher ----------

def test_profil_felder_und_erstellzeit(chief):
    chief.profile_set("oliver", "rolle", " Data Scientist ")
    created = chief.profile_created("oliver")
    chief.profile_set("oliver", "haengen", "zu viele Baustellen")
    assert chief.profile("oliver") == {"rolle": "Data Scientist", "haengen": "zu viele Baustellen"}
    assert chief.profile_created("oliver") == created
    assert chief.profile("maria") == {}
    with pytest.raises(ValueError):
        chief.profile_set("oliver", "passwort", "x")


def test_warte_auf_nur_eigene(chief):
    a = chief.waiting_add("oliver", "Angebot Dachdecker", "Fa. Meier", TODAY)
    chief.waiting_add("maria", "Paket", "", None)
    assert [w.what for w in chief.waiting_open("oliver")] == ["Angebot Dachdecker"]
    assert not chief.waiting_done("maria", a)
    assert chief.waiting_done("oliver", a)
    assert chief.waiting_open("oliver") == []


def test_faellig_und_wiedervorlage(chief):
    chief.waiting_add("oliver", "überfällig", due=TODAY - timedelta(days=2))
    chief.waiting_add("oliver", "bald", due=TODAY + timedelta(days=3))
    chief.waiting_add("oliver", "später", due=TODAY + timedelta(days=10))
    chief.waiting_add("oliver", "ohne Datum")
    assert [w.what for w in chief.waiting_due("oliver", TODAY)] == ["überfällig", "bald"]
    chief.decision_add("oliver", "Server", "Strato behalten", "Oliver", TODAY)
    chief.decision_add("oliver", "Später", "x", review=TODAY + timedelta(days=1))
    d = chief.decisions_to_review("oliver", TODAY)
    assert [x.title for x in d] == ["Server"]
    assert not chief.decision_close("maria", d[0].id)
    assert chief.decision_close("oliver", d[0].id)


@pytest.mark.parametrize("delta,text", [
    (-2, "⚠️ seit 2 Tagen überfällig"), (-1, "⚠️ seit 1 Tag überfällig"),
    (0, "⚠️ heute fällig"), (1, "morgen fällig"), (5, "fällig 08.11."),
])
def test_dringlichkeit(delta, text):
    assert urgency(TODAY + timedelta(days=delta), TODAY) == text


# ---------- Werkzeuge ----------

def tools(tmp_path, chief):
    return build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w",
                       "Europe/Berlin", chief=chief)


def call(reg, name, args, user=OLIVER):
    return run(reg.run(name, args, ToolContext(user)))


def test_profil_werkzeug(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "Einrichtungs-Interview" in call(reg, "profil", {"aktion": "lesen"})
    assert "Unbekanntes Feld" in call(reg, "profil", {"aktion": "speichern", "feld": "x", "text": "y"})
    call(reg, "profil", {"aktion": "speichern", "feld": "rolle", "text": "Zeitnahme"})
    out = call(reg, "profil", {"aktion": "lesen"})
    assert "rolle: Zeitnahme" in out and "Noch offen:" in out


def test_warte_auf_werkzeug(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "JJJJ-MM-TT" in call(reg, "warte_auf", {"aktion": "merken", "was": "x", "bis": "morgen"})
    assert "Gemerkt (#1)" in call(reg, "warte_auf", {"aktion": "merken", "was": "Rückruf",
                                                     "von_wem": "Bank", "bis": "2020-01-01"})
    assert "Rückruf (von Bank) – ⚠️ seit" in call(reg, "warte_auf", {"aktion": "liste"})
    # Andere Personen können fremde Einträge weder sehen noch erledigen.
    assert "nichts" in call(reg, "warte_auf", {"aktion": "liste"}, MARIA)
    assert "Keinen" in call(reg, "warte_auf", {"aktion": "erledigt", "id": 1}, MARIA)
    assert call(reg, "warte_auf", {"aktion": "erledigt", "id": 1}) == "Erledigt."


def test_entscheidungen_werkzeug(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "Titel" in call(reg, "entscheidungen", {"aktion": "festhalten", "titel": "x"})
    call(reg, "entscheidungen", {"aktion": "festhalten", "titel": "Router", "entschieden": "erst beobachten",
                                 "verantwortlich": "Oliver", "wiedervorlage": "2026-12-01"})
    out = call(reg, "entscheidungen", {"aktion": "liste"})
    assert "Router: erst beobachten" in out and "Wiedervorlage 01.12.2026" in out
    assert call(reg, "entscheidungen", {"aktion": "abschliessen", "id": 1}) == "Abgeschlossen."


def test_profil_nur_fuer_admin(tmp_path, chief):
    reg = tools(tmp_path, chief)
    assert "profil" not in {x["name"] for x in reg.schemas(Role.ERWACHSEN)}
    assert "warte_auf" in {x["name"] for x in reg.schemas(Role.ERWACHSEN)}


def test_wiedervorlage_verschieben(tmp_path, chief):
    reg = tools(tmp_path, chief)
    call(reg, "entscheidungen", {"aktion": "festhalten", "titel": "A", "entschieden": "B",
                                 "wiedervorlage": TODAY.isoformat()})
    assert "JJJJ-MM-TT" in call(reg, "entscheidungen", {"aktion": "verschieben", "id": 1})
    assert "Keine" in call(reg, "entscheidungen", {"aktion": "verschieben", "id": 1,
                                                    "wiedervorlage": "2027-01-01"}, MARIA)
    assert "01.01.2027" in call(reg, "entscheidungen", {"aktion": "verschieben", "id": 1,
                                                         "wiedervorlage": "2027-01-01"})
    assert chief.decisions_to_review("oliver", TODAY) == []


def test_kinder_haben_keine_chief_werkzeuge(tmp_path, chief):
    reg = tools(tmp_path, chief)
    names = {s["name"] for s in reg.schemas(Role.KIND)}
    assert not {"profil", "warte_auf", "entscheidungen"} & names
    assert "darf" in call(reg, "warte_auf", {"aktion": "liste"}, KIND)


# ---------- Morgenbrief ----------


# ---------- Zeitplaner: Lebenszeichen und Nachjustieren ----------

class FakeNotifier:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def send_text(self, user, text):
        self.sent.append((user.id, text))
        return self.ok


def at(*args):
    return datetime(*args, tzinfo=TZ)


# ---------- Selbsttest ----------


# ---------- Wiedereinstieg ----------

def make_jarvis(tmp_path, n=1):
    client = FakeClient([resp("end_turn", text_block("ok"))] * n)
    state = StateStore(tmp_path / "jarvis.sqlite")
    jarvis = Jarvis(
        brain=Brain(client, "m", ToolRegistry(), "Europe/Berlin"),
        state=state, memory=ShortTermMemory(tmp_path / "kurz.sqlite"),
        private_memory=ShortTermMemory(tmp_path / "privat.sqlite"),
        journals=JournalStore(tmp_path / "journal", "Europe/Berlin"),
        private_journals=JournalStore(tmp_path / "pj", "Europe/Berlin"),
        timezone="Europe/Berlin")
    return jarvis, client, state


def _away(state, user, days):
    state.touch(user.id, time.time() - days * 86400)


def system_of(client, i):
    return client.calls[i]["system"][0]["text"]


def test_wiedereinstieg_nach_pause(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path, n=3)
    run(jarvis.handle(OLIVER, "Hallo", "telegram"))  # erste Nachricht überhaupt: kein Hinweis
    run(jarvis.handle(OLIVER, "Und?", "telegram"))  # gleich danach: kein Hinweis
    assert "nicht hier" not in system_of(client, 0) + system_of(client, 1)
    _away(state, OLIVER, 12)
    run(jarvis.handle(OLIVER, "Wieder da", "telegram"))
    assert "Oliver war 12 Tage nicht hier" in system_of(client, 2)


def test_sprache_verbraucht_den_wiedereinstieg_nicht(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path, n=2)
    _away(state, OLIVER, 6)
    run(jarvis.handle(OLIVER, "Alexa, frag Jarvis …", "alexa", voice=True))
    assert "nicht hier" not in system_of(client, 0)
    run(jarvis.handle(OLIVER, "Wieder da", "telegram"))
    assert "Oliver war 6 Tage nicht hier" in system_of(client, 1)


def test_kein_wiedereinstieg_fuer_kinder(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path)
    _away(state, KIND, 10)
    run(jarvis.handle(KIND, "Hi", "telegram"))
    assert "nicht hier" not in system_of(client, 0)


def test_privatmodus_zaehlt_als_kontakt(tmp_path):
    jarvis, client, state = make_jarvis(tmp_path)
    _away(state, OLIVER, 10)
    state.set_private(OLIVER.id, True)
    run(jarvis.handle(OLIVER, "privat", "telegram"))  # ohne lokales Modell, aber Kontakt
    assert state.last_contact(OLIVER.id) > time.time() - 60
