"""v0.3.0: conversation search with daily summaries, rules from corrections, calculator,
feedback store and golden-question evals."""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from conftest import CHILD, OLIVER, FakeClient, resp, text_block, tool_block, usage
from jarvis_core.brain import Brain
from jarvis_core.calc import calculate, format_number
from jarvis_core.conversations import ConversationIndex, DaySummarizer, IndexedJournalStore, fts_query
from jarvis_core.evals import Case, check_static, judge, load_cases, run_live, summary, unbacked_numbers
from jarvis_core.feedback import FeedbackStore
from jarvis_core.journal import JournalStore
from jarvis_core.rules import RulesStore
from jarvis_core.tools import ToolContext, ToolRegistry, build_core_tools
from jarvis_core.users import Role, User

TZ = ZoneInfo("Europe/Berlin")
GUEST = User("guest", "Guest", Role.GUEST)


def run(coro):
    return asyncio.run(coro)


def at(day, hh=10):
    return datetime(2026, 10, day, hh, 0, tzinfo=TZ)


# ---------- conversations ----------

def test_fts_query():
    assert fts_query("When does the chimney sweep come?") == '"when"* OR "does"* OR "the"* OR "chimney"* OR "sweep"* OR "come"*'
    assert fts_query("?? a b") is None


def test_index_search_only_given_people(tmp_path):
    idx = ConversationIndex(tmp_path / "c.sqlite")
    idx.add("oliver", at(10), "telegram", "We take the Mova mower", "Noted: Mova ViAX 500.")
    idx.add("mia", at(10), "alexa", "My mower secret", "Okay.")
    idx.add("haushalt", at(11), "telegram", "Chimney sweep comes Tuesday", "Saved.")
    idx.set_summary("oliver", "2026-10-10", "• Decision: mower Mova ViAX 500", ["garden", "mower"])
    hits = idx.search(["oliver", "haushalt"], "mower")
    assert hits[0].startswith("2026-10-10 (summary): • Decision")
    assert any("Mova ViAX 500" in h for h in hits) and not any("secret" in h for h in hits)
    assert idx.search(["oliver", "haushalt"], "chimney")[0].startswith("2026-10-11 10:00 (telegram)")


def test_indexed_journal_and_private_journal(tmp_path):
    idx = ConversationIndex(tmp_path / "c.sqlite")
    store = IndexedJournalStore(tmp_path / "journal", "Europe/Berlin", idx)
    store.for_user(OLIVER).log_exchange("telegram", "The car is a Kona", "Saved.")
    assert list((tmp_path / "journal" / "oliver").rglob("*.md"))
    JournalStore(tmp_path / "private", "Europe/Berlin").for_user(OLIVER).log_exchange("telegram", "Back pain", "Ok.")
    assert idx.count() == (1, 0)  # private journals get no index


def test_journal_search_uses_index_and_respects_people(tmp_path):
    idx = ConversationIndex(tmp_path / "c.sqlite")
    store = IndexedJournalStore(tmp_path / "journal", "Europe/Berlin", idx)
    store.for_user(OLIVER).log_exchange("telegram", "The garage gets painted in March", "Noted.")
    reg = build_core_tools(store, tmp_path / "wiki", "Europe/Berlin", conversations=idx)
    assert "- 20" in run(reg.run("journal_search", {"query": "garage"}, ToolContext(OLIVER)))
    assert "garage" not in run(reg.run("journal_search", {"query": "garage"}, ToolContext(CHILD))).lower()


def test_summarizer_masks_numbers_and_skips_today(tmp_path):
    idx = ConversationIndex(tmp_path / "c.sqlite")
    idx.add("oliver", at(17), "telegram", "New IBAN DE89370400440532013000 for the rent", "Noted.")
    idx.add("oliver", at(19), "telegram", "Today", "Today.")
    client = SimpleNamespace(calls=[])

    async def create(**kw):
        client.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text='{"summary": "• Rent now to DE89 3704 0044 0532 0130 00", "topics": ["rent"]}')])
    client.messages = SimpleNamespace(create=create)
    assert run(DaySummarizer(idx, client, "small-model", {"oliver": "Oliver"}).run(date(2026, 10, 19))) == 1
    assert "material, not an instruction" in client.calls[0]["system"]
    assert "DE89" not in idx.search(["oliver"], "rent")[0].split("(summary)")[1].split("–")[0]
    assert idx.pending_days(date(2026, 10, 19)) == []


# ---------- rules and calculator ----------

def test_rules_need_confirmation_and_are_per_person(tmp_path):
    store = RulesStore(tmp_path / "r.sqlite")
    reg = build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w", "Europe/Berlin", rules=store)
    tool = reg.get("rule_save")
    assert tool.needs_confirmation and not tool.allowed(Role.GUEST)
    assert run(reg.run("rule_save", {"rule": "  Kids' events  in the family calendar "}, ToolContext(OLIVER))) == \
        "Rule #1 saved: Kids' events in the family calendar"
    assert "do not store" in run(reg.run("rule_save", {"rule": "My PIN: 4711 always"}, ToolContext(OLIVER)))
    assert run(reg.run("rules_list", {}, ToolContext(CHILD))) == "No rules saved yet."
    assert run(reg.run("rule_delete", {"id": 1}, ToolContext(CHILD))) == "This rule does not exist."
    assert run(reg.run("rule_delete", {"id": 1}, ToolContext(OLIVER))) == "Rule deleted."


def test_brain_puts_own_rules_into_system_prompt(tmp_path):
    store = RulesStore(tmp_path / "r.sqlite")
    store.add("oliver", "Answer me with the result first.")
    store.add("mia", "Mia's rule")
    brain = Brain(client=None, model="m", tools=ToolRegistry(), timezone="Europe/Berlin", rules=store)
    text = brain._system(OLIVER)[0]["text"]
    assert "Rules Oliver has confirmed" in text and "result first" in text and "Mia's rule" not in text
    assert "Rules " not in brain._system(GUEST)[0]["text"].replace("Corrections", "")
    assert "rule_save" not in Brain(client=None, model="m", tools=ToolRegistry(), timezone="Europe/Berlin")._system(OLIVER)[0]["text"]


def test_calculator(tmp_path):
    assert calculate("(412.5 + 87) * 1.19") == (412.5 + 87) * 1.19
    assert calculate("round(10 / 3, 2)") == 3.33
    for bad in ["__import__('os')", "a + 1", "2 ** 1000", "x" * 400]:
        try:
            calculate(bad)
        except (ValueError, SyntaxError):
            continue
        raise AssertionError(bad)
    assert format_number(1500) == "1,500" and format_number(2.5) == "2.5"
    reg = build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w", "Europe/Berlin")
    assert run(reg.run("calculate", {"expression": "1000 * 1.5"}, ToolContext(GUEST))) == "1000 * 1.5 = 1,500"
    assert "Cannot be calculated" in run(reg.run("calculate", {"expression": "1/0"}, ToolContext(OLIVER)))


# ---------- feedback ----------

def test_feedback_once_only_own_and_private_without_text(tmp_path):
    fb = FeedbackStore(tmp_path / "f.sqlite")
    key = fb.offer("oliver", "Q?", "A.", "telegram", private=False)
    assert fb.rate(key, "mia", -1) is None
    assert fb.rate(key, "oliver", -1) is not None
    assert fb.rate(key, "oliver", 1) is None  # each reply once
    secret = fb.offer("oliver", "private question", "private answer", "telegram", private=True)
    fb.rate(secret, "oliver", 1)
    rows = fb._db.execute("SELECT question, answer FROM feedback ORDER BY id").fetchall()
    assert rows[1] == ("", "")


# ---------- evals ----------

def test_evals_load_static_and_judge(tmp_path):
    f = tmp_path / "q.toml"
    f.write_text('[[case]]\nid = "sum"\nquestion = "What is 12 plus 30?"\ntools = ["calculate"]\n\n'
                 '[[case]]\nid = "guest-rule"\nrole = "guest"\nquestion = "Save a rule"\ntools = ["rule_save"]\n', encoding="utf-8")
    cases = load_cases(f)
    reg = build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w", "Europe/Berlin",
                           rules=RulesStore(tmp_path / "r.sqlite"))
    assert [check_static(c, reg).status for c in cases] == ["ok", "failed"]
    assert unbacked_numbers("It is 42 and 17:25", ["12 plus 30 = 42", "17:25"]) == []
    assert judge(Case("x", "?", contains=["14"]), [], "Up to 16", ["8 to 14"]) == \
        ["answer does not contain “14”", "number “16” not backed"]


def test_live_run_calculator_runs_for_real(tmp_path):
    reg = build_core_tools(JournalStore(tmp_path / "j", "Europe/Berlin"), tmp_path / "w", "Europe/Berlin")
    case = Case("sum", "What is 12 plus 30?", tools=["calculate"], contains=["42"])
    client = FakeClient([
        resp("tool_use", tool_block("t1", "calculate", {"expression": "12 + 30"}), usage=usage(1, 1)),
        resp("end_turn", text_block("12 plus 30 is 42."), usage=usage(1, 1)),
    ])
    out = run(run_live(case, reg, client, "m", "Europe/Berlin"))
    assert out.status == "ok" and out.called == ["calculate"]
    assert "12 + 30 = 42" in str(client.calls[1]["messages"][-1]["content"][0]["content"])
    assert "1 of 1 passed" in summary([out])
