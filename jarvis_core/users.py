"""People, their roles and their ids in each channel.

People are listed in a TOML file (see `people.example.toml` in a profile repo). Without the
file there is exactly one person: an admin with the given name and Telegram ids.

Since v0.2.0 the keys and role values are English. The German keys and values of v0.1.0
(`rolle`, `sprache`, `kalender`, `morgenbrief`, `erwachsen`, `kind`, `gast`, `mitglied`) are
still read, so existing files keep working; new files use the English names.
"""

import re
import tomllib
from dataclasses import dataclass
from datetime import time
from enum import Enum
from pathlib import Path


class Role(str, Enum):
    ADMIN = "admin"
    ADULT = "adult"
    CHILD = "child"
    GUEST = "guest"  # an unknown voice, e.g. over Alexa

    # v0.1.0 names, kept as aliases of the same members.
    ERWACHSEN = "adult"
    KIND = "child"
    GAST = "guest"

    @classmethod
    def _missing_(cls, value):
        return _ROLE_ALIASES.get(str(value).strip().lower())


# Older values from v0.1.0 files, and `member` for team profiles (a team member is an adult).
_ROLE_ALIASES = {"erwachsen": Role.ADULT, "kind": Role.CHILD, "gast": Role.GUEST,
                 "member": Role.ADULT, "mitglied": Role.ADULT}

ALL_MEMBERS = frozenset({Role.ADMIN, Role.ADULT, Role.CHILD})
ADULTS = frozenset({Role.ADMIN, Role.ADULT})

# "haushalt" is the folder of the shared journal (name kept on disk from v0.1.0),
# "guest" (formerly "gast") the unknown voice.
RESERVED_IDS = frozenset({"haushalt", "guest", "gast"})
_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")
_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")

DEFAULT_LANGUAGE = "English"


@dataclass(frozen=True)
class User:
    id: str  # used as a folder name, so only a-z, 0-9, _ and -
    name: str
    role: Role
    language: str = DEFAULT_LANGUAGE
    telegram_ids: frozenset[int] = frozenset()
    alexa_person_ids: frozenset[str] = frozenset()
    slack_ids: frozenset[str] = frozenset()  # Slack member ids (U…, W…)
    calendar_id: str | None = None          # own Google calendar (usually the Gmail address)
    brief_weekday: time | None = time(7, 0)  # morning brief Mon–Fri; None = off
    brief_weekend: time | None = time(7, 0)  # morning brief Sat/Sun; None = off


def _brief_time(raw, default: time | None) -> time | None:
    """"07:15" → 07:15; "off"/false → None; missing → default."""
    if raw is None:
        return default
    if raw is False or str(raw).strip().lower() in ("off", "no", "aus", "nein", ""):
        return None
    return time.fromisoformat(str(raw).strip())


# Whoever speaks over Alexa without Alexa recognising the voice.
GUEST = User(id="guest", name="Guest", role=Role.GUEST)


def slugify(name: str) -> str:
    table = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower().translate(table)).strip("-")
    return slug or "person"


def _field(entry: dict, key: str, old_key: str, default=None):
    """The English key wins; the v0.1.0 German key is read when the English one is missing."""
    return entry.get(key, entry.get(old_key, default))


class UserDirectory:
    def __init__(self, users: list[User]):
        self._by_id: dict[str, User] = {}
        self._by_telegram: dict[int, User] = {}
        self._by_alexa: dict[str, User] = {}
        self._by_slack: dict[str, User] = {}
        for user in users:
            if not _ID_PATTERN.match(user.id) or user.id in RESERVED_IDS:
                raise ValueError(f"Invalid person id: {user.id!r}")
            if user.id in self._by_id:
                raise ValueError(f"Person id used twice: {user.id}")
            self._by_id[user.id] = user
            for tid in user.telegram_ids:
                if tid in self._by_telegram:
                    raise ValueError(f"Telegram id {tid} is assigned to two people")
                self._by_telegram[tid] = user
            for pid in user.alexa_person_ids:
                if pid in self._by_alexa:
                    raise ValueError(f"Alexa voice {pid} is assigned to two people")
                self._by_alexa[pid] = user
            for sid in user.slack_ids:
                if not _SLACK_ID.match(sid):
                    raise ValueError(f"Invalid Slack id for {user.id}: {sid!r}")
                if sid in self._by_slack:
                    raise ValueError(f"Slack id {sid} is assigned to two people")
                self._by_slack[sid] = user

    def __iter__(self):
        return iter(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)

    def get(self, user_id: str) -> User | None:
        return self._by_id.get(user_id)

    def by_telegram(self, telegram_id: int) -> User | None:
        return self._by_telegram.get(telegram_id)

    def by_alexa(self, person_id: str) -> User | None:
        return self._by_alexa.get(person_id)

    def by_slack(self, slack_id: str) -> User | None:
        return self._by_slack.get(slack_id)

    def admins(self) -> list[User]:
        return [u for u in self._by_id.values() if u.role is Role.ADMIN]

    @classmethod
    def from_toml(cls, path: Path, default_language: str = DEFAULT_LANGUAGE) -> "UserDirectory":
        """default_language: for people without `language`, e.g. "German" in a German household."""
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        users = []
        for entry in data.get("person", []):
            name = entry["name"]
            weekday = _brief_time(_field(entry, "morning_brief", "morgenbrief"), time(7, 0))
            role = _field(entry, "role", "rolle", Role.ADULT.value)
            try:
                role = Role(role)
            except ValueError:
                raise ValueError(f"Unknown role for {name}: {role!r}") from None
            users.append(User(
                id=entry.get("id") or slugify(name),
                name=name,
                role=role,
                language=_field(entry, "language", "sprache", default_language),
                telegram_ids=frozenset(int(x) for x in entry.get("telegram", [])),
                alexa_person_ids=frozenset(str(x) for x in entry.get("alexa", [])),
                slack_ids=frozenset(str(x).strip() for x in entry.get("slack", [])),
                calendar_id=_field(entry, "calendar", "kalender") or None,
                brief_weekday=weekday,
                brief_weekend=_brief_time(
                    _field(entry, "morning_brief_weekend", "morgenbrief_wochenende"), weekday),
            ))
        return cls(users)

    @classmethod
    def single(cls, name: str, telegram_ids: frozenset[int]) -> "UserDirectory":
        return cls([User(id=slugify(name), name=name, role=Role.ADMIN,
                         telegram_ids=telegram_ids)])
