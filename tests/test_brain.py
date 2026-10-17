import asyncio

from conftest import KIND, OLIVER, FakeClient, resp, text_block, tool_block, usage
from jarvis_core.brain import Brain
from jarvis_core.tools import Tool, ToolRegistry, search_markdown
from jarvis_core.users import ADULTS


def make_registry(log):
    reg = ToolRegistry()

    async def harmless(args, ctx):
        log.append(("harmless", args))
        return "ok"

    async def door(args, ctx):
        log.append(("door", args, ctx.user.id))
        return "Tür offen"

    reg.add(Tool("harmless", "", {"type": "object"}, harmless))
    reg.add(Tool("door", "", {"type": "object"}, door, needs_confirmation=True, roles=ADULTS))
    return reg


def make_brain(client, log):
    return Brain(client, "test-model", make_registry(log), "Europe/Berlin")


def test_plain_answer():
    client = FakeClient([resp("end_turn", text_block("Hallo Oliver"))])
    result = asyncio.run(make_brain(client, []).respond([], "Hi", OLIVER))
    assert result.text == "Hallo Oliver"
    assert result.pending is None
    assert "Oliver" in client.calls[0]["system"][0]["text"]


def test_tool_loop_runs_tool_and_returns_result():
    log = []
    client = FakeClient([
        resp("tool_use", tool_block("t1", "harmless", {"x": 1})),
        resp("end_turn", text_block("Fertig")),
    ])
    result = asyncio.run(make_brain(client, log).respond([], "mach was", OLIVER))
    assert result.text == "Fertig"
    assert log == [("harmless", {"x": 1})]
    last = client.calls[-1]["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}


def test_confirmation_pauses_and_approval_runs_tool():
    log = []
    client = FakeClient([
        resp("tool_use", text_block("Ich öffne"), tool_block("t1", "harmless", {}),
             tool_block("t2", "door", {"door": "front"})),
        resp("end_turn", text_block("Tür ist offen")),
    ])
    brain = make_brain(client, log)
    first = asyncio.run(brain.respond([], "Tür auf", OLIVER))
    assert first.pending is not None
    assert log == [("harmless", {})]  # die Tür wurde noch NICHT geöffnet

    final = asyncio.run(brain.resume(first.pending, True, OLIVER))
    assert final.text == "Tür ist offen"
    assert log == [("harmless", {}), ("door", {"door": "front"}, "oliver")]
    results = client.calls[-1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]


def test_rejection_does_not_run_tool():
    log = []
    client = FakeClient([
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("Okay, nicht geöffnet")),
    ])
    brain = make_brain(client, log)
    first = asyncio.run(brain.respond([], "Tür auf", OLIVER))
    final = asyncio.run(brain.resume(first.pending, False, OLIVER))
    assert log == []
    assert final.text == "Okay, nicht geöffnet"
    rejected = client.calls[-1]["messages"][-1]["content"][0]
    assert rejected["is_error"] is True


def test_child_does_not_see_or_run_adult_tool():
    log = []
    client = FakeClient([
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("Das darf ich für dich nicht")),
    ])
    result = asyncio.run(make_brain(client, log).respond([], "Tür auf", KIND))
    assert [t["name"] for t in client.calls[0]["tools"]] == ["harmless"]
    assert "kindgerecht" in client.calls[0]["system"][0]["text"]
    # Auch wenn das Modell es trotzdem versucht: keine Rückfrage, keine Ausführung.
    assert result.pending is None and log == []
    denied = client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "darf" in denied


def test_usage_is_summed_over_the_loop():
    client = FakeClient([
        resp("tool_use", tool_block("t1", "harmless", {}), usage=usage(100, 10)),
        resp("end_turn", text_block("ok"), usage=usage(150, 20)),
    ])
    result = asyncio.run(make_brain(client, []).respond([], "x", OLIVER))
    assert (result.usage.input_tokens, result.usage.output_tokens) == (250, 30)


def test_search_markdown(tmp_path):
    (tmp_path / "haus.md").write_text("# Haus\nHeizöl wird im November geliefert.\n", encoding="utf-8")
    (tmp_path / "garten.md").write_text("# Garten\nMäher fährt montags.\n", encoding="utf-8")
    out = search_markdown(tmp_path, "Heizöl Lieferung")
    assert "haus.md" in out and "garten.md" not in out
    assert search_markdown(tmp_path, "Raumschiff") == "Keine Treffer."


def test_persona_pro_profil():
    client = FakeClient([resp("end_turn", text_block("ok"))])
    brain = Brain(client, "m", make_registry([]), "Europe/Berlin", persona="der Assistent des Teams")
    asyncio.run(brain.respond([], "Hi", OLIVER))
    assert "Du bist Jarvis, der Assistent des Teams." in client.calls[0]["system"][0]["text"]
