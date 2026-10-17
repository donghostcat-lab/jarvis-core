"""Chief of Staff: der Zustand hinter dem Skill `chief-of-staff` (SQLite).

Nach der „Anatomy“ aus chief-of-staff-2: Der Skill ist nur die Anleitung, der
aktuelle Stand liegt hier.
- Profil: wer die Person ist, ihre Bereiche, wie sie hängen bleibt, was geholfen hat
  (einmal im Einrichtungs-Interview erfragt, danach nachjustiert).
- Warte auf: was von wem bis wann kommen soll; Überfälliges meldet der Morgenbrief.
- Entscheidungen: was entschieden wurde, von wem umgesetzt, wann wieder anschauen.
Alles gehört genau einer Person; niemand sieht die Einträge anderer.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PROFILE_FIELDS = {
    "rolle": "Was du machst, beruflich und privat",
    "bereiche": "Deine Lebens- und Arbeitsbereiche, um die sich Jarvis kümmern soll",
    "personen": "Die wichtigsten Menschen und wofür sie stehen",
    "prioritaeten": "Was in den nächsten Monaten wirklich zählt",
    "haengen": "Wie du typischerweise hängen bleibst (ehrlich, nicht geschönt)",
    "geholfen": "Was dir früher geholfen hat",
    "stil": "Wie Jarvis mit dir reden soll",
}


@dataclass(frozen=True)
class Waiting:
    id: int
    what: str
    who: str
    due: date | None


@dataclass(frozen=True)
class Decision:
    id: int
    title: str
    decided: str
    owner: str
    review: date | None
    created: float


def _date(raw: str | None) -> date | None:
    return date.fromisoformat(raw) if raw else None


class ChiefStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS cos_profile ("
            " user_id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL,"
            " updated REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS cos_waiting ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, what TEXT NOT NULL,"
            " who TEXT NOT NULL DEFAULT '', due TEXT, created REAL NOT NULL, done REAL);"
            "CREATE TABLE IF NOT EXISTS cos_decisions ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, title TEXT NOT NULL,"
            " decided TEXT NOT NULL, owner TEXT NOT NULL DEFAULT '', review TEXT,"
            " created REAL NOT NULL, closed REAL);"
        )
        self._db.commit()

    # ---------- Profil ----------

    def profile(self, user_id: str) -> dict[str, str]:
        row = self._db.execute("SELECT data FROM cos_profile WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def profile_created(self, user_id: str) -> float | None:
        row = self._db.execute("SELECT created FROM cos_profile WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    def profile_set(self, user_id: str, field: str, text: str) -> None:
        if field not in PROFILE_FIELDS:
            raise ValueError(f"Unbekanntes Profilfeld {field!r}")
        data = self.profile(user_id)
        data[field] = text.strip()
        now = time.time()
        self._db.execute(
            "INSERT INTO cos_profile (user_id, data, created, updated) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
            (user_id, json.dumps(data, ensure_ascii=False), now, now))
        self._db.commit()

    # ---------- Warte auf ----------

    def waiting_add(self, user_id: str, what: str, who: str = "", due: date | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO cos_waiting (user_id, what, who, due, created) VALUES (?, ?, ?, ?, ?)",
            (user_id, what.strip(), who.strip(), due.isoformat() if due else None, time.time()))
        self._db.commit()
        return cur.lastrowid

    def waiting_open(self, user_id: str) -> list[Waiting]:
        rows = self._db.execute(
            "SELECT id, what, who, due FROM cos_waiting WHERE user_id = ? AND done IS NULL"
            " ORDER BY due IS NULL, due, id", (user_id,)).fetchall()
        return [Waiting(i, w, who, _date(d)) for i, w, who, d in rows]

    def waiting_done(self, user_id: str, item_id: int) -> bool:
        cur = self._db.execute(
            "UPDATE cos_waiting SET done = ? WHERE id = ? AND user_id = ? AND done IS NULL",
            (time.time(), item_id, user_id))
        self._db.commit()
        return cur.rowcount > 0

    # ---------- Entscheidungen ----------

    def decision_add(self, user_id: str, title: str, decided: str, owner: str = "",
                     review: date | None = None) -> int:
        cur = self._db.execute(
            "INSERT INTO cos_decisions (user_id, title, decided, owner, review, created)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, title.strip(), decided.strip(), owner.strip(),
             review.isoformat() if review else None, time.time()))
        self._db.commit()
        return cur.lastrowid

    def decisions_open(self, user_id: str) -> list[Decision]:
        rows = self._db.execute(
            "SELECT id, title, decided, owner, review, created FROM cos_decisions"
            " WHERE user_id = ? AND closed IS NULL ORDER BY review IS NULL, review, id",
            (user_id,)).fetchall()
        return [Decision(i, t, d, o, _date(r), c) for i, t, d, o, r, c in rows]

    def decision_close(self, user_id: str, item_id: int) -> bool:
        cur = self._db.execute(
            "UPDATE cos_decisions SET closed = ? WHERE id = ? AND user_id = ? AND closed IS NULL",
            (time.time(), item_id, user_id))
        self._db.commit()
        return cur.rowcount > 0

    def decision_postpone(self, user_id: str, item_id: int, review: date) -> bool:
        cur = self._db.execute(
            "UPDATE cos_decisions SET review = ? WHERE id = ? AND user_id = ? AND closed IS NULL",
            (review.isoformat(), item_id, user_id))
        self._db.commit()
        return cur.rowcount > 0

    # ---------- Für den Morgenbrief ----------

    def waiting_due(self, user_id: str, today: date, days: int = 3) -> list[Waiting]:
        """Überfällig oder in den nächsten `days` Tagen fällig."""
        return [w for w in self.waiting_open(user_id)
                if w.due is not None and (w.due - today).days <= days]

    def decisions_to_review(self, user_id: str, today: date) -> list[Decision]:
        return [d for d in self.decisions_open(user_id) if d.review is not None and d.review <= today]


def urgency(due: date, today: date) -> str:
    """Je näher die Frist, desto lauter (chief-of-staff-2, Planner)."""
    days = (due - today).days
    if days < 0:
        return f"⚠️ seit {-days} {'Tag' if days == -1 else 'Tagen'} überfällig"
    if days == 0:
        return "⚠️ heute fällig"
    if days == 1:
        return "morgen fällig"
    return f"fällig {due:%d.%m.}"
