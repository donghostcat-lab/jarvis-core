import asyncio

from conftest import KIND, OLIVER
from jarvis_core.journal import JournalStore
from jarvis_core.tools import ToolContext, build_core_tools


class FakeWebSearch:
    def __init__(self):
        self.calls = []

    async def search(self, question, user_id):
        self.calls.append((question, user_id))
        return "Hallenbad hat bis 21 Uhr offen."


def test_journal_search_only_sees_own_and_household(tmp_path):
    journals = JournalStore(tmp_path / "journal", "Europe/Berlin")
    journals.for_user(OLIVER).log_exchange("t", "Geheimprojekt Geburtstag", "ok")
    journals.for_user(KIND).log_exchange("t", "Mein Geburtstag Wunschzettel", "ok")
    journals.household.add_note("Geburtstag von Oma am Sonntag")
    tools = build_core_tools(journals, tmp_path / "wiki", "Europe/Berlin")

    out = asyncio.run(tools.run("journal_search", {"query": "Geburtstag"}, ToolContext(KIND)))
    assert "Wunschzettel" in out and "Oma" in out
    assert "Geheimprojekt" not in out


def test_note_for_household(tmp_path):
    journals = JournalStore(tmp_path / "journal", "Europe/Berlin")
    tools = build_core_tools(journals, tmp_path / "wiki", "Europe/Berlin")
    asyncio.run(tools.run("notiz_merken", {"text": "Schornsteinfeger Dienstag", "fuer_alle": True},
                          ToolContext(OLIVER)))
    notes = (journals.household.root / "notizen.md").read_text(encoding="utf-8")
    assert "Schornsteinfeger Dienstag (von Oliver)" in notes


def test_child_has_no_wiki(tmp_path):
    tools = build_core_tools(JournalStore(tmp_path, "Europe/Berlin"), tmp_path / "wiki", "Europe/Berlin")
    from jarvis_core.users import Role
    names = [t["name"] for t in tools.schemas(Role.KIND)]
    assert "wiki_search" not in names and "journal_search" in names
    out = asyncio.run(tools.run("wiki_search", {"query": "Haus"}, ToolContext(KIND)))
    assert "nicht benutzen" in out


def test_web_research_only_when_configured(tmp_path):
    journals = JournalStore(tmp_path, "Europe/Berlin")
    assert build_core_tools(journals, tmp_path, "Europe/Berlin").get("web_recherche") is None

    web = FakeWebSearch()
    tools = build_core_tools(journals, tmp_path, "Europe/Berlin", web_search=web)
    out = asyncio.run(tools.run("web_recherche", {"frage": "Hallenbad Beuel heute?"},
                                ToolContext(KIND)))
    assert "21 Uhr" in out
    assert web.calls == [("Hallenbad Beuel heute?", "mia")]
