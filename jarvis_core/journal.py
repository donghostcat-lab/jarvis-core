"""Journal: jedes Gespräch als Markdown, eine Datei pro Tag.

Das Journal ist die Rohquelle, aus der OpenWiki später das Langzeitgedächtnis baut.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


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
        path = self.root / "notizen.md"
        if not path.exists():
            path.write_text("# Notizen\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- {now:%Y-%m-%d %H:%M}: {text.strip()}\n")
        return path


HOUSEHOLD = "haushalt"


class JournalStore:
    """Ein Journal pro Person plus ein gemeinsames für den Haushalt.

    Layout: <root>/<personen-id>/JJJJ/MM/JJJJ-MM-TT.md und <root>/haushalt/...
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
        return self._get(HOUSEHOLD, "Haushalt")
