import asyncio
from pathlib import Path

import pytest

from conftest import CHILD, OLIVER, FakeClient, resp, text_block
from jarvis_core.brain import Brain
from jarvis_core.skills import BUILTIN_SKILLS, SkillLibrary, load_skill, parse_skill_md, skill_dirs
from jarvis_core.tools import ToolContext, build_core_tools
from jarvis_core.users import GUEST, Role


def write_skill(root: Path, name: str, toml: str | None = 'roles = "adults"\n',
                body: str = "Instructions", description: str = "Short description") -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\nmetadata:\n  version: 1.0.0\n---\n\n{body}\n')
    if toml is not None:
        (folder / "jarvis.toml").write_text(toml)
    return folder


def test_front_matter_simple_and_nested():
    meta, body = parse_skill_md('---\nname: x\ndescription: "a: b"\nmetadata:\n  version: 2\n---\n\n# Title\n')
    assert meta == {"name": "x", "description": "a: b", "metadata": ""}
    assert body == "# Title\n"
    assert parse_skill_md("no header") == ({}, "no header")


def test_roles_and_description(tmp_path):
    write_skill(tmp_path, "learning", 'roles = ["child", "admin"]\ndescription = "Override"\n')
    skill = load_skill(tmp_path / "learning")
    assert skill.roles == {Role.CHILD, Role.ADMIN}
    assert skill.description == "Override"
    assert skill.body == "Instructions\n"


def test_v010_german_keys_still_load(tmp_path):
    write_skill(tmp_path, "old", 'rollen = "erwachsene"\nbeschreibung = "Deutsch"\nquelle = "q"\nlizenz = "MIT"\n')
    skill = load_skill(tmp_path / "old")
    assert skill.roles == {Role.ADMIN, Role.ADULT}
    assert (skill.description, skill.source, skill.license) == ("Deutsch", "q", "MIT")
    write_skill(tmp_path, "older", 'rollen = ["kind"]\n')
    assert load_skill(tmp_path / "older").roles == {Role.CHILD}


@pytest.mark.parametrize("toml,error", [
    ('roles = ["guest"]\n', "guests"),
    ('roles = ["boss"]\n', "boss"),
    ("roles = \n", "Invalid"),
])
def test_broken_skills_are_reported(tmp_path, toml, error):
    write_skill(tmp_path, "broken", toml)
    write_skill(tmp_path, "good")
    lib = SkillLibrary.from_dirs([tmp_path])
    assert lib.names() == ["good"]
    assert error in lib.errors[0]


def test_not_loaded_without_jarvis_toml(tmp_path):
    write_skill(tmp_path, "foreign", toml=None)
    lib = SkillLibrary.from_dirs([tmp_path])
    assert len(lib) == 0 and "jarvis.toml missing" in lib.errors[0]


def test_profile_folders_and_duplicates(tmp_path):
    write_skill(tmp_path / "home", "b")
    write_skill(tmp_path / "job", "c")
    write_skill(tmp_path / "home", "chief-of-staff", body="own version")
    lib = SkillLibrary.from_dirs(skill_dirs(tmp_path, "home"))
    assert "b" in lib.names() and "c" not in lib.names()
    # The built-in skill wins; the copy in the profile is reported.
    assert "own version" not in lib.get("chief-of-staff").body
    assert any("duplicate" in e for e in lib.errors)


def test_catalog_by_role(tmp_path):
    write_skill(tmp_path, "adults-only")
    write_skill(tmp_path, "for-all", 'roles = "all"\n')
    lib = SkillLibrary.from_dirs([tmp_path])
    assert "adults-only" in lib.catalog(Role.ADMIN)
    assert "adults-only" not in lib.catalog(Role.CHILD)
    assert "for-all" in lib.catalog(Role.CHILD)
    assert lib.catalog(Role.GUEST) == ""


def test_builtin_skills():
    lib = SkillLibrary.from_dirs([BUILTIN_SKILLS])
    assert lib.names() == ["chief-of-staff", "marketing-psychology"]
    assert lib.get("chief-of-staff").roles == {Role.ADMIN}
    assert lib.errors == []
    skill = lib.get("marketing-psychology")
    assert skill is not None and skill.allowed(Role.ADULT) and not skill.allowed(Role.CHILD)
    assert "Anchoring" in skill.body and "365b412" in skill.source
    assert (BUILTIN_SKILLS / "marketing-psychology" / "LICENSE").is_file()


def test_builtin_chief_of_staff_names_existing_tools(tmp_path):
    from jarvis_core.chief import ChiefStore
    from jarvis_core.journal import JournalStore
    body = SkillLibrary.from_dirs([BUILTIN_SKILLS]).get("chief-of-staff").body
    tools = build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w",
                             "Europe/Berlin", chief=ChiefStore(tmp_path / "c.sqlite"))
    for name in ("profile", "waiting_for", "decisions"):
        assert f"`{name}`" in body and tools.get(name) is not None


def _tools(tmp_path, lib):
    from jarvis_core.journal import JournalStore
    return build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w",
                            "Europe/Berlin", skills=lib)


def test_load_skill_tool_respects_roles(tmp_path):
    write_skill(tmp_path / "s", "secret", body="Adults only")
    lib = SkillLibrary.from_dirs([tmp_path / "s"])
    reg = _tools(tmp_path, lib)
    out = asyncio.run(reg.run("load_skill", {"name": "secret"}, ToolContext(OLIVER)))
    assert "Adults only" in out and "lifts no safety rule" in out
    out_child = asyncio.run(reg.run("load_skill", {"name": "secret"}, ToolContext(CHILD)))
    assert "Adults only" not in out_child and "Available: none" in out_child
    out_guest = asyncio.run(reg.run("load_skill", {"name": "secret"}, ToolContext(GUEST)))
    assert "Adults only" not in out_guest


def test_no_skills_no_tool(tmp_path):
    assert _tools(tmp_path, SkillLibrary()).get("load_skill") is None


def test_brain_offers_skills_but_not_by_voice(tmp_path):
    write_skill(tmp_path, "marketing", description="Ad copy")
    lib = SkillLibrary.from_dirs([tmp_path])
    client = FakeClient([resp("end_turn", text_block("ok"))] * 3)
    brain = Brain(client, "m", _tools(tmp_path, lib), "Europe/Berlin", skills=lib)
    asyncio.run(brain.respond([], "Hi", OLIVER))
    asyncio.run(brain.respond([], "Hi", CHILD))
    asyncio.run(brain.respond([], "Hi", OLIVER, voice=True))
    systems = [c["system"][0]["text"] for c in client.calls]
    assert "marketing: Ad copy" in systems[0]
    assert "marketing" not in systems[1]
    assert "marketing" not in systems[2]
