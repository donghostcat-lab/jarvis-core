"""Short-term memory: the last conversation turns per person in SQLite."""

import sqlite3
import time
from pathlib import Path


class ShortTermMemory:
    def __init__(self, path: Path, max_turns: int = 20):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS turns ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT NOT NULL,"
            " role TEXT NOT NULL,"
            " content TEXT NOT NULL,"
            " ts REAL NOT NULL)"
        )
        self._db.commit()
        self._max_messages = max_turns * 2

    def add_exchange(self, user_id: str, user_text: str, reply: str) -> None:
        now = time.time()
        self._db.executemany(
            "INSERT INTO turns (user_id, role, content, ts) VALUES (?, ?, ?, ?)",
            [(user_id, "user", user_text, now), (user_id, "assistant", reply, now)],
        )
        self._db.commit()

    def history(self, user_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT role, content FROM turns WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, self._max_messages),
        ).fetchall()
        messages = [{"role": r, "content": c} for r, c in reversed(rows)]
        # The API expects the history to start with a user message.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    def clear(self, user_id: str) -> None:
        self._db.execute("DELETE FROM turns WHERE user_id = ?", (user_id,))
        self._db.commit()
