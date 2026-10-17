"""Skills: geprüfte Anleitungen als Markdown, die Jarvis bei Bedarf lädt.

Aufbau eines Skills (ein Ordner pro Skill):
    <ordner>/<name>/SKILL.md     Originaldatei, unverändert übernommen
    <ordner>/<name>/jarvis.toml  unsere Angaben: Rollen, Quelle, Version, Lizenz

Geladen werden die eingebauten Skills von jarvis-core (`builtin_skills/`, z. B.
chief-of-staff) und die Skills des Profil-Repos (`skills/<profil>/` in jarvis-home bzw.
jarvis-job). Bei gleichem Namen gewinnt der eingebaute Skill. Jarvis sieht im System-Prompt nur Name und
Beschreibung und holt den vollen Text erst mit dem Werkzeug `skill_laden`.

Fremde Skills werden vor der Übernahme gelesen, mit fester Version ins Repo kopiert und
nicht automatisch aktualisiert. Ohne jarvis.toml wird ein Skill nicht geladen.
"""

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .users import ADULTS, ALL_MEMBERS, Role

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ROLE_GROUPS = {"erwachsene": ADULTS, "alle": ALL_MEMBERS}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    roles: frozenset[Role]
    source: str = ""
    version: str = ""
    license: str = ""

    def allowed(self, role: Role) -> bool:
        return role in self.roles


def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Frontmatter (nur einfache `schlüssel: wert`-Zeilen der obersten Ebene) und Text."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue  # verschachtelte Angaben (z. B. metadata:) brauchen wir nicht
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, text[end + 4:].lstrip("\n")


def _roles(raw) -> frozenset[Role]:
    if isinstance(raw, str):
        if raw in _ROLE_GROUPS:
            return _ROLE_GROUPS[raw]
        raw = [raw]
    return frozenset(Role(r) for r in raw)


def load_skill(folder: Path) -> Skill:
    meta, body = parse_skill_md((folder / "SKILL.md").read_text(encoding="utf-8"))
    conf = tomllib.loads((folder / "jarvis.toml").read_text(encoding="utf-8"))
    name = meta.get("name") or folder.name
    if not _NAME.match(name):
        raise ValueError(f"ungültiger Skill-Name {name!r}")
    description = conf.get("beschreibung") or meta.get("description", "")
    if not description:
        raise ValueError(f"Skill {name} hat keine Beschreibung")
    roles = _roles(conf.get("rollen", "erwachsene"))
    if Role.GAST in roles:
        raise ValueError(f"Skill {name}: Gäste bekommen keine Skills")
    return Skill(name=name, description=description, body=body, roles=roles,
                 source=conf.get("quelle", ""), version=str(conf.get("version", "")),
                 license=conf.get("lizenz", ""))


class SkillLibrary:
    def __init__(self, skills: list[Skill] | None = None) -> None:
        self._skills = {s.name: s for s in skills or []}
        self.errors: list[str] = []

    @classmethod
    def from_dirs(cls, roots: list[Path]) -> "SkillLibrary":
        lib = cls()
        for root in roots:
            if not root.is_dir():
                continue
            for folder in sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file()):
                if not (folder / "jarvis.toml").is_file():
                    lib.errors.append(f"{folder.name}: jarvis.toml fehlt, nicht geladen")
                    continue
                try:
                    skill = load_skill(folder)
                except (ValueError, tomllib.TOMLDecodeError, OSError) as exc:
                    lib.errors.append(f"{folder.name}: {exc}")
                    continue
                if skill.name in lib._skills:
                    lib.errors.append(f"{skill.name}: doppelt, {folder} ignoriert")
                    continue
                lib._skills[skill.name] = skill
        for err in lib.errors:
            log.warning("Skill nicht geladen: %s", err)
        return lib

    def __len__(self) -> int:
        return len(self._skills)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def for_role(self, role: Role) -> list[Skill]:
        return [s for _, s in sorted(self._skills.items()) if s.allowed(role)]

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def catalog(self, role: Role) -> str:
        """Kurzliste für den System-Prompt; leer, wenn die Rolle keine Skills hat."""
        skills = self.for_role(role)
        if not skills:
            return ""
        lines = "\n".join(f"  - {s.name}: {s.description[:300]}" for s in skills)
        return ("- Skills (Anleitungen für bestimmte Aufgaben). Passt eine zur Anfrage, lade sie\n"
                "  zuerst mit skill_laden und arbeite danach:\n" + lines + "\n")


BUILTIN_SKILLS = Path(__file__).resolve().parent / "builtin_skills"


def skill_dirs(base: Path, profile: str) -> list[Path]:
    """Eingebaute Skills des Kerns, danach die des Profils (z. B. skills/home)."""
    return [BUILTIN_SKILLS, base / profile]
