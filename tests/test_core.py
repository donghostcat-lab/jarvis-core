import asyncio

import pytest

from conftest import CHILD, OLIVER, FakeClient, resp, text_block, tool_block, usage
from jarvis_core.brain import Brain
from jarvis_core.core import FALLBACK_NOTE, Jarvis
from jarvis_core.journal import NOTES_FILE, JournalStore
from jarvis_core.llm import ChatReply
from jarvis_core.memory import ShortTermMemory
from jarvis_core.state import StateStore
from jarvis_core.tools import Tool, ToolRegistry, build_core_tools
from jarvis_core.users import ADULTS


class FakeChat:
    def __init__(self, name, reply="local answer", fail=False):
        self.name = name
        self.model = f"{name}-model"
        self.reply = reply
        self.fail = fail
        self.calls = []

    async def complete(self, system, history, user_text):
        self.calls.append((system, history, user_text))
        if self.fail:
            raise RuntimeError("unreachable")
        return ChatReply(self.reply, 10, 5)


def make_jarvis(tmp_path, responses, local_llm=None, fallback=None, budget=0, registry=None):
    log = []

    async def door(args, ctx):
        log.append(("door", ctx.user.id))
        return "Door open"

    reg = registry or ToolRegistry()
    reg.add(Tool("door", "", {"type": "object"}, door, needs_confirmation=True, roles=ADULTS))
    client = FakeClient(responses)
    jarvis = Jarvis(
        brain=Brain(client, "claude-test", reg, "Europe/Berlin"),
        state=StateStore(tmp_path / "jarvis.sqlite"),
        memory=ShortTermMemory(tmp_path / "short.sqlite"),
        private_memory=ShortTermMemory(tmp_path / "private.sqlite"),
        journals=JournalStore(tmp_path / "journal", "Europe/Berlin"),
        private_journals=JournalStore(tmp_path / "private-journal", "Europe/Berlin"),
        timezone="Europe/Berlin",
        local_llm=local_llm,
        fallback=fallback,
        daily_token_budget=budget,
    )
    return jarvis, client, log


def run(coro):
    return asyncio.run(coro)


def test_answer_goes_to_memory_and_personal_journal(tmp_path):
    jarvis, _, _ = make_jarvis(tmp_path, [resp("end_turn", text_block("Hello"), usage=usage(10, 2))])
    reply = run(jarvis.handle(OLIVER, "Hi", "telegram"))
    assert reply.text == "Hello" and reply.confirm_token is None
    assert jarvis.memory.history("oliver")[-1]["content"] == "Hello"
    assert jarvis.memory.history("mia") == []
    assert list((tmp_path / "journal" / "oliver").rglob("*.md"))
    assert jarvis.state.tokens_on(jarvis.today(), "claude") == 12


def test_private_mode_survives_restart_and_never_uses_claude(tmp_path):
    jarvis, client, _ = make_jarvis(tmp_path, [])
    jarvis.toggle_private(OLIVER)

    local = FakeChat("local")
    restarted, client, _ = make_jarvis(tmp_path, [], local_llm=local, fallback=FakeChat("gemini"))
    reply = run(restarted.handle(OLIVER, "Something private", "telegram"))
    assert reply.text == "local answer"
    assert client.calls == [] and local.calls
    assert list((tmp_path / "private-journal" / "oliver").rglob("*.md"))
    assert not (tmp_path / "journal" / "oliver").exists()


def test_private_mode_does_not_fall_back_to_cloud(tmp_path):
    gemini = FakeChat("gemini")
    jarvis, client, _ = make_jarvis(tmp_path, [], local_llm=FakeChat("local", fail=True),
                                    fallback=gemini)
    jarvis.toggle_private(OLIVER)
    reply = run(jarvis.handle(OLIVER, "Something private", "telegram"))
    assert "cannot be reached" in reply.text
    assert gemini.calls == [] and client.calls == []


def test_confirmation_flow_persists_and_runs_once(tmp_path):
    jarvis, _, log = make_jarvis(tmp_path, [
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("The door is open")),
    ])
    ask = run(jarvis.handle(OLIVER, "Open the door", "telegram"))
    assert ask.confirm_token and log == []

    # An old button with the wrong token leaves the current question in place.
    assert jarvis.take_pending(OLIVER, "wrong") is None

    # Restart: the open question is in SQLite and can be confirmed afterwards.
    restarted, _, log = make_jarvis(tmp_path, [resp("end_turn", text_block("The door is open"))])
    entry = restarted.take_pending(OLIVER, ask.confirm_token)
    assert entry is not None
    final = run(restarted.resume(OLIVER, entry, True))
    assert final.text == "The door is open" and log == [("door", "oliver")]
    assert restarted.take_pending(OLIVER, ask.confirm_token) is None


def test_confirmation_only_by_owner(tmp_path):
    jarvis, _, log = make_jarvis(tmp_path, [
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("The door is open")),
    ])
    ask = run(jarvis.handle(OLIVER, "Open the door", "telegram"))
    assert jarvis.take_pending(CHILD, ask.confirm_token) is None
    entry = jarvis.take_pending(OLIVER, ask.confirm_token)
    final = run(jarvis.resume(OLIVER, entry, True))
    assert final.text == "The door is open" and log == [("door", "oliver")]
    assert jarvis.take_pending(OLIVER, ask.confirm_token) is None


def test_expired_confirmation_is_rejected(tmp_path):
    jarvis, _, log = make_jarvis(tmp_path, [resp("tool_use", tool_block("t1", "door", {}))])
    ask = run(jarvis.handle(OLIVER, "Open the door", "telegram"))
    jarvis.state.pending_ttl = -1
    assert jarvis.take_pending(OLIVER, ask.confirm_token) is None
    assert log == []


def test_shared_note_waits_for_confirmation(tmp_path):
    journals = JournalStore(tmp_path / "journal", "Europe/Berlin")
    tools = build_core_tools(journals, tmp_path / "wiki", "Europe/Berlin")
    jarvis, _, _ = make_jarvis(tmp_path, [
        resp("tool_use", tool_block("t1", "share_note", {"text": "Chimney sweep on Tuesday"})),
        resp("end_turn", text_block("Saved for everyone")),
    ], registry=tools)
    notes = journals.household.root / NOTES_FILE
    ask = run(jarvis.handle(OLIVER, "Tell everyone the chimney sweep comes on Tuesday", "telegram"))
    assert ask.question == "Save this note for everyone? “Chimney sweep on Tuesday”"
    assert not notes.exists()
    final = run(jarvis.resume(OLIVER, jarvis.take_pending(OLIVER, ask.confirm_token), True))
    assert final.text == "Saved for everyone"
    assert "Chimney sweep on Tuesday (from Oliver)" in notes.read_text(encoding="utf-8")


def test_claude_error_uses_fallback(tmp_path):
    gemini = FakeChat("gemini", reply="Answer from Gemini")
    jarvis, _, _ = make_jarvis(tmp_path, [RuntimeError("API down")], fallback=gemini)
    reply = run(jarvis.handle(OLIVER, "What time is it?", "telegram"))
    assert reply.text == "Answer from Gemini" + FALLBACK_NOTE
    assert jarvis.memory.history("oliver")[-1]["content"] == "Answer from Gemini"
    assert jarvis.state.tokens_on(jarvis.today(), "gemini") == 15


def test_claude_error_without_fallback(tmp_path):
    jarvis, _, _ = make_jarvis(tmp_path, [RuntimeError("API down")])
    reply = run(jarvis.handle(OLIVER, "Hi", "telegram"))
    assert "went wrong" in reply.text


@pytest.mark.parametrize("with_fallback", [True, False])
def test_budget_exhausted(tmp_path, with_fallback):
    gemini = FakeChat("gemini", reply="Gemini here") if with_fallback else None
    jarvis, client, _ = make_jarvis(tmp_path, [], fallback=gemini, budget=100)
    jarvis.state.record_usage(jarvis.today(), "oliver", "claude", "claude-test", 90, 20)
    reply = run(jarvis.handle(OLIVER, "Hi", "telegram"))
    assert client.calls == []
    if with_fallback:
        assert reply.text.startswith("Gemini here")
    else:
        assert "daily budget" in reply.text
    assert "Daily budget for Claude: 110 of 100" in jarvis.usage_report()


def test_guest_has_no_conversation_memory(tmp_path):
    from jarvis_core.users import GUEST
    jarvis, client, _ = make_jarvis(tmp_path, [
        resp("end_turn", text_block("Answer 1")),
        resp("end_turn", text_block("Answer 2")),
    ])
    run(jarvis.handle(GUEST, "Question from person A", "alexa", voice=True))
    run(jarvis.handle(GUEST, "What was asked before?", "alexa", voice=True))
    assert len(client.calls[1]["messages"]) == 1
    assert jarvis.memory.history("guest") == []
    assert list((tmp_path / "journal" / "guest").rglob("*.md"))
