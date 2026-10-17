"""Dauerhafter Zustand in SQLite: Privatmodus, offene Bestätigungen, Verbrauch.

Liegt bewusst nicht im Arbeitsspeicher: Nach einem Neustart soll niemand, der
im Privatmodus war, plötzlich unbemerkt wieder mit einem Cloud-Modell sprechen.
"""

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from .brain import PendingAction

PENDING_TTL = 15 * 60  # Sekunden; ältere Bestätigungsfragen gelten als abgelaufen


class StateStore:
    def __init__(self, path: Path, pending_ttl: float = PENDING_TTL):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS private_mode (user_id TEXT PRIMARY KEY);"
            "CREATE TABLE IF NOT EXISTS pending ("
            " user_id TEXT PRIMARY KEY, token TEXT NOT NULL, channel TEXT NOT NULL,"
            " payload TEXT NOT NULL, ts REAL NOT NULL);"
            "CREATE TABLE IF NOT EXISTS usage ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL, user_id TEXT NOT NULL,"
            " provider TEXT NOT NULL, model TEXT NOT NULL,"
            " input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, ts REAL NOT NULL);"
            "CREATE INDEX IF NOT EXISTS usage_day ON usage (day);"
            # Letzte Nachricht per Text (Telegram, auch privat); Sprache zählt nicht.
            "CREATE TABLE IF NOT EXISTS last_contact (user_id TEXT PRIMARY KEY, ts REAL NOT NULL);"
        )
        self._db.commit()
        self.pending_ttl = pending_ttl

    # ---------- Letzter Kontakt (für den Wiedereinstieg) ----------

    def touch(self, user_id: str, ts: float | None = None) -> None:
        self._db.execute("INSERT OR REPLACE INTO last_contact (user_id, ts) VALUES (?, ?)",
                         (user_id, time.time() if ts is None else ts))
        self._db.commit()

    def last_contact(self, user_id: str) -> float | None:
        row = self._db.execute("SELECT ts FROM last_contact WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else None

    # ---------- Privatmodus ----------

    def is_private(self, user_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM private_mode WHERE user_id = ?", (user_id,)).fetchone()
        return row is not None

    def set_private(self, user_id: str, on: bool) -> None:
        if on:
            self._db.execute("INSERT OR IGNORE INTO private_mode (user_id) VALUES (?)", (user_id,))
        else:
            self._db.execute("DELETE FROM private_mode WHERE user_id = ?", (user_id,))
        self._db.commit()

    # ---------- Offene Bestätigungen (eine pro Person) ----------

    def put_pending(self, user_id: str, token: str, channel: str, pending: PendingAction) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO pending (user_id, token, channel, payload, ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, token, channel, json.dumps(asdict(pending)), time.time()),
        )
        self._db.commit()

    def pop_pending(self, user_id: str, token: str) -> tuple[str, PendingAction] | None:
        """Holt und löscht die offene Aktion, wenn Token passt und sie nicht abgelaufen ist."""
        row = self._db.execute(
            "SELECT token, channel, payload, ts FROM pending WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None or row[0] != token:
            return None
        self.clear_pending(user_id)
        if time.time() - row[3] > self.pending_ttl:
            return None
        return row[1], PendingAction(**json.loads(row[2]))

    def clear_pending(self, user_id: str) -> None:
        self._db.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
        self._db.commit()

    # ---------- Verbrauch ----------

    def record_usage(self, day: str, user_id: str, provider: str, model: str,
                     input_tokens: int, output_tokens: int) -> None:
        self._db.execute(
            "INSERT INTO usage (day, user_id, provider, model, input_tokens, output_tokens, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (day, user_id, provider, model, input_tokens, output_tokens, time.time()),
        )
        self._db.commit()

    def tokens_on(self, day: str, provider: str | None = None) -> int:
        sql = "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM usage WHERE day = ?"
        params: tuple = (day,)
        if provider is not None:
            sql += " AND provider = ?"
            params += (provider,)
        return self._db.execute(sql, params).fetchone()[0]

    def usage_on(self, day: str) -> list[tuple[str, int, int]]:
        """(Anbieter, Eingabe-Tokens, Ausgabe-Tokens) pro Anbieter für einen Tag."""
        return self._db.execute(
            "SELECT provider, SUM(input_tokens), SUM(output_tokens) FROM usage"
            " WHERE day = ? GROUP BY provider ORDER BY provider", (day,)
        ).fetchall()
