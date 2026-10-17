"""Familienmitglieder, ihre Rollen und ihre Kennungen in den Zugängen.

Die Familie steht in einer TOML-Datei (siehe familie.example.toml). Fehlt sie,
gibt es genau eine Person: JARVIS_USER_NAME als Admin mit ALLOWED_CHAT_IDS.
"""

import re
import tomllib
from dataclasses import dataclass
from datetime import time
from enum import Enum
from pathlib import Path


class Role(str, Enum):
    ADMIN = "admin"
    ERWACHSEN = "erwachsen"
    KIND = "kind"
    GAST = "gast"  # unbekannte Stimme, z. B. später über Alexa


ALL_MEMBERS = frozenset({Role.ADMIN, Role.ERWACHSEN, Role.KIND})
ADULTS = frozenset({Role.ADMIN, Role.ERWACHSEN})

# "haushalt" ist der Ordner für gemeinsames Wissen, "gast" die unbekannte Stimme.
RESERVED_IDS = frozenset({"haushalt", "gast"})
_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")


@dataclass(frozen=True)
class User:
    id: str  # wird als Ordnername benutzt, daher nur a-z, 0-9, _ und -
    name: str
    role: Role
    language: str = "Deutsch"
    telegram_ids: frozenset[int] = frozenset()
    alexa_person_ids: frozenset[str] = frozenset()
    calendar_id: str | None = None          # eigener Google-Kalender (meist die Gmail-Adresse)
    brief_weekday: time | None = time(7, 0)  # Morgenbrief Mo–Fr; None = aus
    brief_weekend: time | None = time(7, 0)  # Morgenbrief Sa/So; None = aus


def _brief_time(raw, default: time | None) -> time | None:
    """„07:15“ → 07:15; „aus“/false → None; fehlt → default."""
    if raw is None:
        return default
    if raw is False or str(raw).strip().lower() in ("aus", "nein", "off", ""):
        return None
    return time.fromisoformat(str(raw).strip())


# Wer über Alexa spricht, ohne dass Alexa die Stimme erkennt.
GUEST = User(id="gast", name="Gast", role=Role.GAST)


def slugify(name: str) -> str:
    table = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower().translate(table)).strip("-")
    return slug or "person"


class UserDirectory:
    def __init__(self, users: list[User]):
        self._by_id: dict[str, User] = {}
        self._by_telegram: dict[int, User] = {}
        self._by_alexa: dict[str, User] = {}
        for user in users:
            if not _ID_PATTERN.match(user.id) or user.id in RESERVED_IDS:
                raise ValueError(f"Ungültige Personen-ID: {user.id!r}")
            if user.id in self._by_id:
                raise ValueError(f"Personen-ID doppelt vergeben: {user.id}")
            self._by_id[user.id] = user
            for tid in user.telegram_ids:
                if tid in self._by_telegram:
                    raise ValueError(f"Telegram-ID {tid} ist zwei Personen zugeordnet")
                self._by_telegram[tid] = user
            for pid in user.alexa_person_ids:
                if pid in self._by_alexa:
                    raise ValueError(f"Alexa-Stimme {pid} ist zwei Personen zugeordnet")
                self._by_alexa[pid] = user

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

    def admins(self) -> list[User]:
        return [u for u in self._by_id.values() if u.role is Role.ADMIN]

    @classmethod
    def from_toml(cls, path: Path) -> "UserDirectory":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        users = []
        for entry in data.get("person", []):
            name = entry["name"]
            weekday = _brief_time(entry.get("morgenbrief"), time(7, 0))
            users.append(User(
                id=entry.get("id") or slugify(name),
                name=name,
                role=Role(entry.get("rolle", "erwachsen")),
                language=entry.get("sprache", "Deutsch"),
                telegram_ids=frozenset(int(x) for x in entry.get("telegram", [])),
                alexa_person_ids=frozenset(str(x) for x in entry.get("alexa", [])),
                calendar_id=entry.get("kalender") or None,
                brief_weekday=weekday,
                brief_weekend=_brief_time(entry.get("morgenbrief_wochenende"), weekday),
            ))
        return cls(users)

    @classmethod
    def single(cls, name: str, telegram_ids: frozenset[int]) -> "UserDirectory":
        return cls([User(id=slugify(name), name=name, role=Role.ADMIN,
                         telegram_ids=telegram_ids)])
