import asyncio

import pytest

from conftest import KIND, OLIVER, FakeClient, resp, text_block, tool_block, usage
from jarvis_core.brain import Brain
from jarvis_core.core import FALLBACK_NOTE, Jarvis
from jarvis_core.journal import JournalStore
from jarvis_core.llm import ChatReply
from jarvis_core.memory import ShortTermMemory
from jarvis_core.state import StateStore
from jarvis_core.tools import Tool, ToolRegistry
from jarvis_core.users import ADULTS


class FakeChat:
    def __init__(self, name, reply="lokale Antwort", fail=False):
        self.name = name
        self.model = f"{name}-model"
        self.reply = reply
        self.fail = fail
        self.calls = []

    async def complete(self, system, history, user_text):
        self.calls.append((system, history, user_text))
        if self.fail:
            raise RuntimeError("nicht erreichbar")
        return ChatReply(self.reply, 10, 5)


def make_jarvis(tmp_path, responses, local_llm=None, fallback=None, budget=0):
    log = []

    async def door(args, ctx):
        log.append(("door", ctx.user.id))
        return "Tür offen"

    reg = ToolRegistry()
    reg.add(Tool("door", "", {"type": "object"}, door, needs_confirmation=True, roles=ADULTS))
    client = FakeClient(responses)
    jarvis = Jarvis(
        brain=Brain(client, "claude-test", reg, "Europe/Berlin"),
        state=StateStore(tmp_path / "jarvis.sqlite"),
        memory=ShortTermMemory(tmp_path / "kurz.sqlite"),
        private_memory=ShortTermMemory(tmp_path / "privat.sqlite"),
        journals=JournalStore(tmp_path / "journal", "Europe/Berlin"),
        private_journals=JournalStore(tmp_path / "privat-journal", "Europe/Berlin"),
        timezone="Europe/Berlin",
        local_llm=local_llm,
        fallback=fallback,
        daily_token_budget=budget,
    )
    return jarvis, client, log


def run(coro):
    return asyncio.run(coro)


def test_answer_goes_to_memory_and_personal_journal(tmp_path):
    jarvis, _, _ = make_jarvis(tmp_path, [resp("end_turn", text_block("Hallo"), usage=usage(10, 2))])
    reply = run(jarvis.handle(OLIVER, "Hi", "telegram"))
    assert reply.text == "Hallo" and reply.confirm_token is None
    assert jarvis.memory.history("oliver")[-1]["content"] == "Hallo"
    assert jarvis.memory.history("mia") == []
    assert list((tmp_path / "journal" / "oliver").rglob("*.md"))
    assert jarvis.state.tokens_on(jarvis.today(), "claude") == 12


def test_private_mode_survives_restart_and_never_uses_claude(tmp_path):
    jarvis, client, _ = make_jarvis(tmp_path, [])
    jarvis.toggle_private(OLIVER)

    local = FakeChat("lokal")
    restarted, client, _ = make_jarvis(tmp_path, [], local_llm=local, fallback=FakeChat("gemini"))
    reply = run(restarted.handle(OLIVER, "Etwas Privates", "telegram"))
    assert reply.text == "lokale Antwort"
    assert client.calls == [] and local.calls
    assert list((tmp_path / "privat-journal" / "oliver").rglob("*.md"))
    assert not (tmp_path / "journal" / "oliver").exists()


def test_private_mode_does_not_fall_back_to_cloud(tmp_path):
    gemini = FakeChat("gemini")
    jarvis, client, _ = make_jarvis(tmp_path, [], local_llm=FakeChat("lokal", fail=True),
                                    fallback=gemini)
    jarvis.toggle_private(OLIVER)
    reply = run(jarvis.handle(OLIVER, "Etwas Privates", "telegram"))
    assert "nicht erreichbar" in reply.text
    assert gemini.calls == [] and client.calls == []


def test_confirmation_flow_persists_and_runs_once(tmp_path):
    jarvis, _, log = make_jarvis(tmp_path, [
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("Tür ist offen")),
    ])
    ask = run(jarvis.handle(OLIVER, "Tür auf", "telegram"))
    assert ask.confirm_token and log == []

    # Ein alter Button mit falschem Token lässt die aktuelle Frage stehen.
    assert jarvis.take_pending(OLIVER, "falsch") is None

    # Neustart: die offene Frage liegt in SQLite und lässt sich danach bestätigen.
    restarted, _, log = make_jarvis(tmp_path, [resp("end_turn", text_block("Tür ist offen"))])
    entry = restarted.take_pending(OLIVER, ask.confirm_token)
    assert entry is not None
    final = run(restarted.resume(OLIVER, entry, True))
    assert final.text == "Tür ist offen" and log == [("door", "oliver")]
    assert restarted.take_pending(OLIVER, ask.confirm_token) is None


def test_confirmation_only_by_owner(tmp_path):
    jarvis, _, log = make_jarvis(tmp_path, [
        resp("tool_use", tool_block("t1", "door", {})),
        resp("end_turn", text_block("Tür ist offen")),
    ])
    ask = run(jarvis.handle(OLIVER, "Tür auf", "telegram"))
    assert jarvis.take_pending(KIND, ask.confirm_token) is None
    entry = jarvis.take_pending(OLIVER, ask.confirm_token)
    final = run(jarvis.resume(OLIVER, entry, True))
    assert final.text == "Tür ist offen" and log == [("door", "oliver")]
    assert jarvis.take_pending(OLIVER, ask.confirm_token) is None


def test_expired_confirmation_is_rejected(tmp_path):
    jarvis, _, log = make_jarvis(tmp_path, [resp("tool_use", tool_block("t1", "door", {}))])
    ask = run(jarvis.handle(OLIVER, "Tür auf", "telegram"))
    jarvis.state.pending_ttl = -1
    assert jarvis.take_pending(OLIVER, ask.confirm_token) is None
    assert log == []


def test_claude_error_uses_fallback(tmp_path):
    gemini = FakeChat("gemini", reply="Antwort von Gemini")
    jarvis, _, _ = make_jarvis(tmp_path, [RuntimeError("API down")], fallback=gemini)
    reply = run(jarvis.handle(OLIVER, "Wie spät ist es?", "telegram"))
    assert reply.text == "Antwort von Gemini" + FALLBACK_NOTE
    assert jarvis.memory.history("oliver")[-1]["content"] == "Antwort von Gemini"
    assert jarvis.state.tokens_on(jarvis.today(), "gemini") == 15


def test_claude_error_without_fallback(tmp_path):
    jarvis, _, _ = make_jarvis(tmp_path, [RuntimeError("API down")])
    reply = run(jarvis.handle(OLIVER, "Hi", "telegram"))
    assert "schiefgegangen" in reply.text


@pytest.mark.parametrize("with_fallback", [True, False])
def test_budget_exhausted(tmp_path, with_fallback):
    gemini = FakeChat("gemini", reply="Gemini hier") if with_fallback else None
    jarvis, client, _ = make_jarvis(tmp_path, [], fallback=gemini, budget=100)
    jarvis.state.record_usage(jarvis.today(), "oliver", "claude", "claude-test", 90, 20)
    reply = run(jarvis.handle(OLIVER, "Hi", "telegram"))
    assert client.calls == []
    if with_fallback:
        assert reply.text.startswith("Gemini hier")
    else:
        assert "Tagesbudget" in reply.text
    assert "Tagesbudget Claude: 110 von 100" in jarvis.usage_report()


def test_guest_has_no_conversation_memory(tmp_path):
    from jarvis_core.users import GUEST
    jarvis, client, _ = make_jarvis(tmp_path, [
        resp("end_turn", text_block("Antwort 1")),
        resp("end_turn", text_block("Antwort 2")),
    ])
    run(jarvis.handle(GUEST, "Frage von Person A", "alexa", voice=True))
    run(jarvis.handle(GUEST, "Was wurde vorhin gefragt?", "alexa", voice=True))
    assert len(client.calls[1]["messages"]) == 1
    assert jarvis.memory.history("gast") == []
    assert list((tmp_path / "journal" / "gast").rglob("*.md"))
