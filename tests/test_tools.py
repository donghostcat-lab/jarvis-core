import asyncio

from conftest import CHILD, OLIVER
from jarvis_core.journal import NOTES_FILE, JournalStore
from jarvis_core.tools import ToolContext, build_core_tools, error_prefix
from jarvis_core.users import Role


class FakeWebSearch:
    def __init__(self):
        self.calls = []

    async def search(self, question, user_id):
        self.calls.append((question, user_id))
        return "The pool is open until 9 pm."


def test_journal_search_only_sees_own_and_shared(tmp_path):
    journals = JournalStore(tmp_path / "journal", "Europe/Berlin")
    journals.for_user(OLIVER).log_exchange("t", "Secret birthday project", "ok")
    journals.for_user(CHILD).log_exchange("t", "My birthday wish list", "ok")
    journals.household.add_note("Grandma's birthday on Sunday")
    tools = build_core_tools(journals, tmp_path / "wiki", "Europe/Berlin")

    out = asyncio.run(tools.run("journal_search", {"query": "birthday"}, ToolContext(CHILD)))
    assert "wish list" in out and "Grandma" in out
    assert "Secret" not in out


def test_personal_note_needs_no_confirmation_shared_note_does(tmp_path):
    journals = JournalStore(tmp_path / "journal", "Europe/Berlin")
    tools = build_core_tools(journals, tmp_path / "wiki", "Europe/Berlin")
    assert not tools.get("remember_note").needs_confirmation
    assert tools.get("share_note").needs_confirmation

    asyncio.run(tools.run("remember_note", {"text": "Call the bank"}, ToolContext(OLIVER)))
    asyncio.run(tools.run("share_note", {"text": "Chimney sweep Tuesday"}, ToolContext(OLIVER)))
    own = (journals.for_user(OLIVER).root / NOTES_FILE).read_text(encoding="utf-8")
    shared = (journals.household.root / NOTES_FILE).read_text(encoding="utf-8")
    assert "Call the bank" in own and "Call the bank" not in shared
    assert "Chimney sweep Tuesday (from Oliver)" in shared


def test_child_has_no_wiki(tmp_path):
    tools = build_core_tools(JournalStore(tmp_path, "Europe/Berlin"), tmp_path / "wiki", "Europe/Berlin")
    names = [t["name"] for t in tools.schemas(Role.CHILD)]
    assert "wiki_search" not in names and "journal_search" in names
    out = asyncio.run(tools.run("wiki_search", {"query": "house"}, ToolContext(CHILD)))
    assert "may not use" in out


def test_tool_error_is_returned_with_prefix(tmp_path):
    tools = build_core_tools(JournalStore(tmp_path, "Europe/Berlin"), tmp_path / "wiki", "Europe/Berlin")
    out = asyncio.run(tools.run("remember_note", {}, ToolContext(OLIVER)))
    assert out.startswith(error_prefix("remember_note"))


def test_web_search_only_when_configured(tmp_path):
    journals = JournalStore(tmp_path, "Europe/Berlin")
    assert build_core_tools(journals, tmp_path, "Europe/Berlin").get("web_search") is None

    web = FakeWebSearch()
    tools = build_core_tools(journals, tmp_path, "Europe/Berlin", web_search=web)
    out = asyncio.run(tools.run("web_search", {"question": "Is the pool open today?"},
                                ToolContext(CHILD)))
    assert "9 pm" in out
    assert web.calls == [("Is the pool open today?", "mia")]
