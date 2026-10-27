"""Corrections become rules: confirmed rules per person that Jarvis follows in every reply.

If someone corrects Jarvis in principle ("No, that's how we do it"), Jarvis asks whether to save it
as a rule. It is stored only after the confirmation button (needs_confirmation). Rules apply only to
the person who confirmed them and can be listed and deleted at any time. Guests cannot save rules.
"""

import re
import sqlite3
import time
from pathlib import Path

from .users import ALL_MEMBERS, Role, User

MAX_RULES = 30
SECRET = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b|\b(?:\d[ -]?){13,19}\b"
                    r"|\b(password|passcode|passwort|kennwort|pin|tan|puk)\b\s*[:=]?\s*\S{3,}", re.I)

RULES_HINT = (
    "\nCorrections and numbers:\n"
    "- If {user} corrects you in principle (\"No, that's how we do it\", \"always\", \"never again\"), briefly "
    "ask whether you should save that as a rule, then use rule_save with one short, general sentence.\n"
    "- Calculate sums, percentages, averages and time spans with the calculate tool, not in your head.\n"
)


class RulesStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, person TEXT NOT NULL, "
                       "text TEXT NOT NULL, created REAL NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def add(self, person: str, text: str) -> int:
        with self._db() as db:
            return db.execute("INSERT INTO rules (person, text, created) VALUES (?, ?, ?)",
                              (person, text, time.time())).lastrowid

    def list(self, person: str) -> list[tuple[int, str]]:
        with self._db() as db:
            return db.execute("SELECT id, text FROM rules WHERE person = ? ORDER BY id", (person,)).fetchall()

    def delete(self, person: str, rule_id: int) -> bool:
        with self._db() as db:
            return db.execute("DELETE FROM rules WHERE person = ? AND id = ?", (person, rule_id)).rowcount > 0

    def note(self, user: User) -> str:
        """Text for the system prompt: the hint plus the person's confirmed rules (none for guests)."""
        text = RULES_HINT.format(user=user.name)
        mine = self.list(user.id) if user.role is not Role.GUEST else []
        if mine:
            text += f"Rules {user.name} has confirmed (always follow them):\n" + "".join(f"- {t}\n" for _, t in mine)
        return text


def add_rule_tools(registry, store: RulesStore) -> None:
    """rule_save (needs confirmation), rules_list and rule_delete, for members only."""
    from .tools import Tool, ToolContext  # late import: tools imports this module

    async def save(args: dict, ctx: ToolContext) -> str:
        text = " ".join(str(args.get("rule", "")).split())[:300]
        if not text:
            return "Empty rule."
        if SECRET.search(text):
            return "That looks like a password, PIN or account number. I do not store things like that."
        if len(store.list(ctx.user.id)) >= MAX_RULES:
            return f"{MAX_RULES} rules are already saved. Please delete one with rule_delete first."
        return f"Rule #{store.add(ctx.user.id, text)} saved: {text}"

    async def show(args: dict, ctx: ToolContext) -> str:
        mine = store.list(ctx.user.id)
        return "\n".join(f"#{i}: {t}" for i, t in mine) if mine else "No rules saved yet."

    async def delete(args: dict, ctx: ToolContext) -> str:
        return "Rule deleted." if store.delete(ctx.user.id, int(args["id"])) else "This rule does not exist."

    registry.add(Tool(
        name="rule_save",
        description=("Saves a rule for the asking person that you always follow from now on, e.g. "
                     "\"Always put events in the family calendar\". Only after a correction and if the person "
                     "agrees; one short, general sentence. The person confirms with a button."),
        input_schema={"type": "object", "properties": {"rule": {"type": "string"}}, "required": ["rule"]},
        handler=save, needs_confirmation=True, roles=ALL_MEMBERS,
        describer=lambda a: f"Save as a rule: “{a.get('rule')}”",
    ))
    registry.add(Tool(
        name="rules_list", description="Shows the saved rules of the asking person with their numbers.",
        input_schema={"type": "object", "properties": {}}, handler=show, roles=ALL_MEMBERS,
    ))
    registry.add(Tool(
        name="rule_delete", description="Deletes one of your own rules by its number from rules_list.",
        input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        handler=delete, roles=ALL_MEMBERS,
    ))
