"""Skills: reviewed instructions as Markdown that Jarvis loads when needed.

Layout of a skill (one folder per skill):
    <folder>/<name>/SKILL.md     the original file, taken over unchanged
    <folder>/<name>/jarvis.toml  our settings: roles, source, version, license

Loaded are the built-in skills of jarvis-core (`builtin_skills/`, e.g. chief-of-staff) and
the skills of the profile repo (`skills/<profile>/` in jarvis-home or jarvis-job). On equal
names the built-in skill wins. Jarvis sees only name and description in the system prompt
and fetches the full text with the tool `load_skill`.

Third-party skills are read before they are taken over, copied into the repo at a fixed
version and never updated automatically. A skill without jarvis.toml is not loaded.

jarvis.toml keys: `roles` ("adults", "all" or a list of roles), `description` (optional,
overrides the one in SKILL.md), `source`, `version`, `license`. The German keys of v0.1.0
(`rollen`, `beschreibung`, `quelle`, `lizenz`, groups "erwachsene" and "alle") are still read.
"""

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .users import ADULTS, ALL_MEMBERS, Role

log = logging.getLogger(__name__)

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ROLE_GROUPS = {"adults": ADULTS, "all": ALL_MEMBERS, "erwachsene": ADULTS, "alle": ALL_MEMBERS}


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
    """Front matter (only simple top-level `key: value` lines) and body."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if not line or line[0].isspace() or ":" not in line:
            continue  # nested settings (e.g. metadata:) are not needed
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
    get = lambda key, old, default="": conf.get(key, conf.get(old, default))  # noqa: E731
    name = meta.get("name") or folder.name
    if not _NAME.match(name):
        raise ValueError(f"invalid skill name {name!r}")
    description = get("description", "beschreibung") or meta.get("description", "")
    if not description:
        raise ValueError(f"skill {name} has no description")
    roles = _roles(get("roles", "rollen", "adults"))
    if Role.GUEST in roles:
        raise ValueError(f"skill {name}: guests get no skills")
    return Skill(name=name, description=description, body=body, roles=roles,
                 source=get("source", "quelle"), version=str(conf.get("version", "")),
                 license=get("license", "lizenz"))


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
                    lib.errors.append(f"{folder.name}: jarvis.toml missing, not loaded")
                    continue
                try:
                    skill = load_skill(folder)
                except (ValueError, tomllib.TOMLDecodeError, OSError) as exc:
                    lib.errors.append(f"{folder.name}: {exc}")
                    continue
                if skill.name in lib._skills:
                    lib.errors.append(f"{skill.name}: duplicate, {folder} ignored")
                    continue
                lib._skills[skill.name] = skill
        for err in lib.errors:
            log.warning("Skill not loaded: %s", err)
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
        """Short list for the system prompt; empty if the role has no skills."""
        skills = self.for_role(role)
        if not skills:
            return ""
        lines = "\n".join(f"  - {s.name}: {s.description[:300]}" for s in skills)
        return ("- Skills (instructions for particular tasks). If one fits the request, load it\n"
                "  first with load_skill and then follow it:\n" + lines + "\n")


BUILTIN_SKILLS = Path(__file__).resolve().parent / "builtin_skills"


def skill_dirs(base: Path, profile: str) -> list[Path]:
    """Built-in skills of the core, then those of the profile (e.g. skills/home)."""
    return [BUILTIN_SKILLS, base / profile]
