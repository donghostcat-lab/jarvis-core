"""Journal: every conversation as Markdown, one file per day.

The journal is the raw source from which OpenWiki later builds long-term memory.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# File and folder names on disk stay as in v0.1.0, so existing journals keep working.
NOTES_FILE = "notizen.md"
HOUSEHOLD = "haushalt"


class Journal:
    def __init__(self, root: Path, timezone: str, user_name: str):
        self.root = root
        self.tz = ZoneInfo(timezone)
        self.user_name = user_name
        self.root.mkdir(parents=True, exist_ok=True)

    def _day_file(self, now: datetime) -> Path:
        folder = self.root / f"{now:%Y}" / f"{now:%m}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{now:%Y-%m-%d}.md"
        if not path.exists():
            path.write_text(f"# Journal {now:%Y-%m-%d}\n", encoding="utf-8")
        return path

    def log_exchange(self, channel: str, user_text: str, reply: str) -> None:
        now = datetime.now(self.tz)
        entry = (
            f"\n## {now:%H:%M} · {channel}\n\n"
            f"**{self.user_name}:** {user_text.strip()}\n\n"
            f"**Jarvis:** {reply.strip()}\n"
        )
        with self._day_file(now).open("a", encoding="utf-8") as f:
            f.write(entry)

    def add_note(self, text: str) -> Path:
        now = datetime.now(self.tz)
        path = self.root / NOTES_FILE
        if not path.exists():
            path.write_text("# Notes\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- {now:%Y-%m-%d %H:%M}: {text.strip()}\n")
        return path


class JournalStore:
    """One journal per person plus a shared one for everyone (the household or the team).

    Layout: <root>/<person-id>/YYYY/MM/YYYY-MM-DD.md and <root>/haushalt/...
    """

    def __init__(self, root: Path, timezone: str):
        self.root = root
        self.timezone = timezone
        self._cache: dict[str, Journal] = {}

    def _get(self, key: str, name: str) -> Journal:
        if key not in self._cache:
            self._cache[key] = Journal(self.root / key, self.timezone, name)
        return self._cache[key]

    def for_user(self, user) -> Journal:
        return self._get(user.id, user.name)

    @property
    def household(self) -> Journal:
        """The shared journal: notes for everyone."""
        return self._get(HOUSEHOLD, "Everyone")
