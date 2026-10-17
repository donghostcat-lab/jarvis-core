import asyncio
from pathlib import Path

import pytest

from conftest import KIND, OLIVER, FakeClient, resp, text_block
from jarvis_core.brain import Brain
from jarvis_core.skills import BUILTIN_SKILLS, SkillLibrary, load_skill, parse_skill_md, skill_dirs
from jarvis_core.tools import ToolContext, build_core_tools
from jarvis_core.users import GUEST, Role



def write_skill(root: Path, name: str, toml: str | None = 'rollen = "erwachsene"\n',
                body: str = "Anleitung", description: str = "Kurzbeschreibung") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\nmetadata:\n  version: 1.0.0\n---\n\n{body}\n')
    if toml is not None:
        (folder / "jarvis.toml").write_text(toml)
    return folder


def test_frontmatter_einfach_und_verschachtelt():
    meta, body = parse_skill_md('---\nname: x\ndescription: "a: b"\nmetadata:\n  version: 2\n---\n\n# Titel\n')
    assert meta == {"name": "x", "description": "a: b", "metadata": ""}
    assert body == "# Titel\n"
    assert parse_skill_md("ohne Kopf") == ({}, "ohne Kopf")


def test_rollen_und_beschreibung(tmp_path):
    write_skill(tmp_path, "lernen", 'rollen = ["kind", "admin"]\nbeschreibung = "Deutsch"\n')
    skill = load_skill(tmp_path / "lernen")
    assert skill.roles == {Role.KIND, Role.ADMIN}
    assert skill.description == "Deutsch"
    assert skill.body == "Anleitung\n"


@pytest.mark.parametrize("toml,fehler", [
    ('rollen = ["gast"]\n', "Gäste"),
    ('rollen = ["chef"]\n', "chef"),
    ("rollen = \n", "Invalid"),
])
def test_fehlerhafte_skills_werden_gemeldet(tmp_path, toml, fehler):
    write_skill(tmp_path, "kaputt", toml)
    write_skill(tmp_path, "gut")
    lib = SkillLibrary.from_dirs([tmp_path])
    assert lib.names() == ["gut"]
    assert fehler in lib.errors[0]


def test_ohne_jarvis_toml_nicht_geladen(tmp_path):
    write_skill(tmp_path, "fremd", toml=None)
    lib = SkillLibrary.from_dirs([tmp_path])
    assert len(lib) == 0 and "jarvis.toml fehlt" in lib.errors[0]


def test_profil_ordner_und_doppelte(tmp_path):
    write_skill(tmp_path / "home", "b")
    write_skill(tmp_path / "job", "c")
    write_skill(tmp_path / "home", "chief-of-staff", body="eigene Fassung")
    lib = SkillLibrary.from_dirs(skill_dirs(tmp_path, "home"))
    assert "b" in lib.names() and "c" not in lib.names()
    # Eingebauter Skill gewinnt, die Kopie im Profil wird gemeldet.
    assert "eigene Fassung" not in lib.get("chief-of-staff").body
    assert any("doppelt" in e for e in lib.errors)


def test_katalog_nach_rolle(tmp_path):
    write_skill(tmp_path, "nur-erwachsene")
    write_skill(tmp_path, "fuer-alle", 'rollen = "alle"\n')
    lib = SkillLibrary.from_dirs([tmp_path])
    assert "nur-erwachsene" in lib.catalog(Role.ADMIN)
    assert "nur-erwachsene" not in lib.catalog(Role.KIND)
    assert "fuer-alle" in lib.catalog(Role.KIND)
    assert lib.catalog(Role.GAST) == ""


def test_eingebaute_skills():
    lib = SkillLibrary.from_dirs([BUILTIN_SKILLS])
    assert lib.names() == ["chief-of-staff", "marketing-psychology"]
    assert lib.get("chief-of-staff").roles == {Role.ADMIN}
    assert lib.errors == []
    skill = lib.get("marketing-psychology")
    assert skill is not None and skill.allowed(Role.ERWACHSEN) and not skill.allowed(Role.KIND)
    assert "Anchoring" in skill.body and "365b412" in skill.source
    assert (BUILTIN_SKILLS / "marketing-psychology" / "LICENSE").is_file()


def _tools(tmp_path, lib):
    from jarvis_core.journal import JournalStore
    return build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w",
                       "Europe/Berlin", skills=lib)


def test_werkzeug_skill_laden_mit_rechten(tmp_path):
    write_skill(tmp_path / "s", "geheim", body="Nur für Erwachsene")
    lib = SkillLibrary.from_dirs([tmp_path / "s"])
    reg = _tools(tmp_path, lib)
    out = asyncio.run(reg.run("skill_laden", {"name": "geheim"}, ToolContext(OLIVER)))
    assert "Nur für Erwachsene" in out and "hebt aber keine" in out
    out_kind = asyncio.run(reg.run("skill_laden", {"name": "geheim"}, ToolContext(KIND)))
    assert "Nur für Erwachsene" not in out_kind and "Verfügbar: keine" in out_kind
    out_gast = asyncio.run(reg.run("skill_laden", {"name": "geheim"}, ToolContext(GUEST)))
    assert "Nur für Erwachsene" not in out_gast


def test_ohne_skills_kein_werkzeug(tmp_path):
    assert _tools(tmp_path, SkillLibrary()).get("skill_laden") is None


def test_brain_bietet_skills_an_aber_nicht_per_sprache(tmp_path):
    write_skill(tmp_path, "marketing", description="Werbetexte")
    lib = SkillLibrary.from_dirs([tmp_path])
    client = FakeClient([resp("end_turn", text_block("ok"))] * 3)
    brain = Brain(client, "m", _tools(tmp_path, lib), "Europe/Berlin", skills=lib)
    asyncio.run(brain.respond([], "Hi", OLIVER))
    asyncio.run(brain.respond([], "Hi", KIND))
    asyncio.run(brain.respond([], "Hi", OLIVER, voice=True))
    systems = [c["system"][0]["text"] for c in client.calls]
    assert "marketing: Werbetexte" in systems[0]
    assert "marketing" not in systems[1]
    assert "marketing" not in systems[2]


