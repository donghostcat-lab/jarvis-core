"""Feedback on answers: 👍/👎 under a reply, with an optional reason for 👎.

The channel (Telegram, Slack, app) shows the buttons; this store keeps the ratings. Purpose: see
where Jarvis gets it wrong and turn that into golden questions (evals.py) and rules (rules.py). In private mode only the rating is
stored, never the text of question or answer - private conversations stay in the private journal.
"""

import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass
class Rated:
    """A reply that can still be rated (in memory only)."""
    user_id: str
    question: str
    answer: str
    channel: str


class FeedbackStore:
    def __init__(self, path: Path, keep_open: int = 300):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS feedback ("
            " id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, created REAL NOT NULL,"
            " rating INTEGER NOT NULL, question TEXT, answer TEXT, reason TEXT, channel TEXT)")
        self._db.commit()
        # Open replies live in memory only: old buttons expire after a restart.
        self._open: OrderedDict[str, Rated] = OrderedDict()
        self._keep = keep_open
        self._next = 0

    # ---------- open replies ----------

    def offer(self, user_id: str, question: str, answer: str, channel: str, private: bool) -> str:
        """Remembers a reply and returns the key for the buttons."""
        self._next += 1
        key = f"{int(time.time()) % 100000}-{self._next}"
        if private:
            question = answer = ""
        self._open[key] = Rated(user_id, question[:2000], answer[:4000], channel)
        while len(self._open) > self._keep:
            self._open.popitem(last=False)
        return key

    def rate(self, key: str, user_id: str, rating: int) -> int | None:
        """Stores 👍 (+1) or 👎 (-1). Only the person who asked; each reply once."""
        item = self._open.get(key)
        if item is None or item.user_id != user_id:
            return None
        del self._open[key]
        cur = self._db.execute(
            "INSERT INTO feedback (user_id, created, rating, question, answer, channel) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, time.time(), 1 if rating > 0 else -1, item.question, item.answer, item.channel))
        self._db.commit()
        return cur.lastrowid

    def add_reason(self, feedback_id: int, user_id: str, reason: str) -> bool:
        cur = self._db.execute("UPDATE feedback SET reason = ? WHERE id = ? AND user_id = ?",
                               (reason.strip()[:1000], feedback_id, user_id))
        self._db.commit()
        return cur.rowcount == 1

    # ---------- Evaluation ----------

    def report(self, timezone: str, days: int = 30, last: int = 5) -> str:
        since = time.time() - days * 86400
        rows = self._db.execute(
            "SELECT user_id, SUM(rating > 0), SUM(rating < 0) FROM feedback WHERE created >= ? "
            "GROUP BY user_id ORDER BY user_id", (since,)).fetchall()
        if not rows:
            return f"No feedback in the last {days} days yet."
        up = sum(r[1] for r in rows)
        down = sum(r[2] for r in rows)
        lines = [f"Feedback of the last {days} days: 👍 {up} · 👎 {down} "
                 f"({round(100 * up / (up + down))}% good)"]
        lines += [f"- {user}: 👍 {u} · 👎 {d}" for user, u, d in rows]
        bad = self._db.execute(
            "SELECT created, user_id, question, reason FROM feedback WHERE rating < 0 AND created >= ? "
            "ORDER BY created DESC LIMIT ?", (since, last)).fetchall()
        if bad:
            tz = ZoneInfo(timezone)
            lines.append("\nRecently wrong:")
            for created, user, question, reason in bad:
                when = datetime.fromtimestamp(created, tz).strftime("%d %b %H:%M")
                q = f"“{question[:120]}”" if question else "(private mode)"
                lines.append(f"- {when} {user}: {q}" + (f"\n  Reason: {reason}" if reason else ""))
            lines.append("\nGood candidates for evals/questions.toml.")
        return "\n".join(lines)
