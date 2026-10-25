import asyncio

from conftest import CHILD, OLIVER, FakeClient, resp, text_block, tool_block, usage
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
        return "Door open"

    reg.add(Tool("harmless", "", {"type": "object"}, harmless))
    reg.add(Tool("door", "", {"type": "object"}, door, needs_confirmation=True, roles=ADULTS))
    return reg


def make_brain(client, log):
    return Brain(client, "test-model", make_registry(log), "Europe/Berlin")


def test_plain_answer():
    client = FakeClient([resp("end_turn", text_block("Hello Oliver"))])
    result = asyncio.run(make_brain(client, []).respond([], "Hi", OLIVER))
    assert result.text == "Hello Oliver"
    assert result.pending is None
    assert "Oliver" in client.calls[0]["system"][0]["text"]


def test_tool_loop_runs_tool_and_returns_result():
    log = []
    client = FakeClient([
        resp("tool_use", tool_block("t1", "harmless", {"x": 1})),
        resp("end_turn", text_block("Done")),
    ])
    result = asyncio.run(make_brain(client, log).respond([], "do something", OLIVER))
    assert result.text == "Done"
    assert log == [("harmless", {"x": 1})]
    last = client.calls[-1]["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}


def test_confirmation_pauses_and_approval_runs_tool():
    log = []
    client = FakeClient([
        resp("tool_use", text_block("Opening"), tool_block("t1", "harmless", {}),
             tool_block("t2", "door", {"door": "front"})),
        resp("end_turn", text_block("The door is open")),
    ])
    brain = make_brain(client, log)
    first = asyncio.run(brain.respond([], "Open the door", OLIVER))
    assert first.pending is not None
    assert log == [("harmless", {})]  # the door has NOT been opened yet

    final = asyncio.run(brain.resume(first.pending, True, OLIVER))
    assert final.text == "The door is open"
    assert log == [("harmless", {}), ("door", {"door": "front"}, "oliver")]
    results = client.calls[-1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]


def test_rejection_does_not_run_tool():
    log = []
    client = FakeClient([
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("Okay, not opened")),
    ])
    brain = make_brain(client, log)
    first = asyncio.run(brain.respond([], "Open the door", OLIVER))
    final = asyncio.run(brain.resume(first.pending, False, OLIVER))
    assert log == []
    assert final.text == "Okay, not opened"
    rejected = client.calls[-1]["messages"][-1]["content"][0]
    assert rejected["is_error"] is True


def test_child_does_not_see_or_run_adult_tool():
    log = []
    client = FakeClient([
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("I can't do that for you")),
    ])
    result = asyncio.run(make_brain(client, log).respond([], "Open the door", CHILD))
    assert [t["name"] for t in client.calls[0]["tools"]] == ["harmless"]
    assert "suitable for children" in client.calls[0]["system"][0]["text"]
    # Even if the model tries anyway: no question, no execution.
    assert result.pending is None and log == []
    denied = client.calls[-1]["messages"][-1]["content"][0]["content"]
    assert "may not use" in denied


def test_usage_is_summed_over_the_loop():
    client = FakeClient([
        resp("tool_use", tool_block("t1", "harmless", {}), usage=usage(100, 10)),
        resp("end_turn", text_block("ok"), usage=usage(150, 20)),
    ])
    result = asyncio.run(make_brain(client, []).respond([], "x", OLIVER))
    assert (result.usage.input_tokens, result.usage.output_tokens) == (250, 30)


def test_search_markdown(tmp_path):
    (tmp_path / "house.md").write_text("# House\nHeating oil is delivered in November.\n", encoding="utf-8")
    (tmp_path / "garden.md").write_text("# Garden\nThe mower runs on Mondays.\n", encoding="utf-8")
    out = search_markdown(tmp_path, "heating oil delivery")
    assert "house.md" in out and "garden.md" not in out
    assert search_markdown(tmp_path, "spaceship") == "No hits."


def test_persona_per_profile():
    client = FakeClient([resp("end_turn", text_block("ok"))])
    brain = Brain(client, "m", make_registry([]), "Europe/Berlin", persona="the team's assistant")
    asyncio.run(brain.respond([], "Hi", OLIVER))
    assert "You are Jarvis, the team's assistant." in client.calls[0]["system"][0]["text"]
