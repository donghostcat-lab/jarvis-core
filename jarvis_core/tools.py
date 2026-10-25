"""Tools that the model can call.

A tool with needs_confirmation=True runs only after the person confirms it in the channel
(for example a Yes button in Telegram or Slack): opening a door, sending mail, writing a note
everyone sees. With `roles` each tool sets which roles may use it at all.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from .chief import PROFILE_FIELDS, ChiefStore, urgency
from .journal import JournalStore
from .skills import SkillLibrary
from .users import ADULTS, ALL_MEMBERS, Role, User


@dataclass(frozen=True)
class ToolContext:
    """Who is using the tool right now."""
    user: User


Handler = Callable[[dict, ToolContext], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Handler
    needs_confirmation: bool = False
    roles: frozenset[Role] = ALL_MEMBERS
    describer: Callable[[dict], str] | None = None  # readable confirmation question

    def allowed(self, role: Role) -> bool:
        return role in self.roles

    def schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def describe_call(self, args: dict) -> str:
        """Short text for the confirmation question."""
        if self.describer is not None:
            return self.describer(args)
        details = ", ".join(f"{k}={v!r}" for k, v in args.items())
        return f"{self.name}({details})"


def error_prefix(name: str) -> str:
    """Start of the text `ToolRegistry.run` returns when a tool raises."""
    return f"Error in {name}:"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self, role: Role | None = None) -> list[dict]:
        return [t.schema() for t in self._tools.values() if role is None or t.allowed(role)]

    async def run(self, name: str, args: dict, ctx: ToolContext) -> str:
        tool = self.get(name)
        if tool is None:
            return f"Unknown tool: {name}"
        if not tool.allowed(ctx.user.role):
            return f"{ctx.user.name} may not use the tool {name}."
        try:
            return await tool.handler(args, ctx)
        except Exception as exc:  # hand the error back to the model instead of crashing
            return f"{error_prefix(name)} {exc}"


def search_markdown(roots: Path | list[Path], query: str, max_results: int = 5) -> str:
    """Simple full-text search over Markdown files.

    Scores each file by the number of hits of all search words and returns the best
    matching lines per file.
    """
    roots = [roots] if isinstance(roots, Path) else roots
    terms = [t.lower() for t in re.findall(r"\w+", query) if len(t) > 2]
    if not terms:
        return "Please give more specific search words."
    existing = [r for r in roots if r.exists()]
    if not existing:
        return "No data yet."

    scored = []
    for root in existing:
        for path in root.rglob("*.md"):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            lower = text.lower()
            score = sum(lower.count(t) for t in terms)
            if score:
                rel = path.relative_to(root)
                label = f"{root.name}/{rel}" if len(roots) > 1 else str(rel)
                scored.append((score, label, text))

    if not scored:
        return "No hits."

    scored.sort(key=lambda item: item[0], reverse=True)
    parts = []
    for score, label, text in scored[:max_results]:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        hits = [ln for ln in lines if any(t in ln.lower() for t in terms)][:6]
        parts.append(f"### {label} (hits: {score})\n" + "\n".join(hits))
    return "\n\n".join(parts)


SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search words"},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
    },
    "required": ["query"],
}

NOTE_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
}


def build_core_tools(journals: JournalStore, wiki_dir: Path, timezone: str,
                     web_search=None, skills: SkillLibrary | None = None,
                     chief: ChiefStore | None = None) -> ToolRegistry:
    """General tools of every Jarvis installation (Home and Job).

    web_search: optionally an object with `async search(question, user_id) -> str` (Gemini).
    skills: reviewed instructions (skills.py). chief: profile, "waiting for" and decisions
    for the chief of staff (chief.py). Profile repos (jarvis-home, jarvis-job) add their own
    tools to the same ToolRegistry."""
    tz = ZoneInfo(timezone)
    registry = ToolRegistry()

    async def current_time(_: dict, ctx: ToolContext) -> str:
        now = datetime.now(tz)
        return now.strftime("%A, %Y-%m-%d, %H:%M (%Z)")

    async def wiki_search(args: dict, ctx: ToolContext) -> str:
        return search_markdown(wiki_dir, args["query"], int(args.get("max_results", 5)))

    async def journal_search(args: dict, ctx: ToolContext) -> str:
        # Only the person's own journal and the shared one, never those of other people.
        roots = [journals.for_user(ctx.user).root, journals.household.root]
        return search_markdown(roots, args["query"], int(args.get("max_results", 5)))

    async def remember_note(args: dict, ctx: ToolContext) -> str:
        journals.for_user(ctx.user).add_note(args["text"])
        return "Note saved."

    async def share_note(args: dict, ctx: ToolContext) -> str:
        journals.household.add_note(f"{args['text']} (from {ctx.user.name})")
        return "Note saved for everyone."

    registry.add(Tool(
        name="current_time",
        description="Returns the date and time in the installation's time zone.",
        input_schema={"type": "object", "properties": {}},
        handler=current_time,
        roles=ALL_MEMBERS | {Role.GUEST},
    ))
    registry.add(Tool(
        name="wiki_search",
        description=(
            "Searches long-term memory (OpenWiki) for knowledge about people, places, "
            "projects and preferences. Use it before you say that you don't know something."
        ),
        input_schema=SEARCH_SCHEMA,
        handler=wiki_search,
        roles=ADULTS,
    ))
    registry.add(Tool(
        name="journal_search",
        description=(
            "Searches earlier conversations of the current person and the notes shared with "
            "everyone. Useful for things from the last few days that are not in the wiki yet."
        ),
        input_schema=SEARCH_SCHEMA,
        handler=journal_search,
    ))
    registry.add(Tool(
        name="remember_note",
        description=(
            "Saves a note for the current person only, when they explicitly ask you to "
            "remember something. For notes everyone should see, use share_note."
        ),
        input_schema=NOTE_SCHEMA,
        handler=remember_note,
    ))
    # Everyone reads shared notes through journal_search, so a shared note is a way into
    # other people's prompts: the person confirms each one.
    registry.add(Tool(
        name="share_note",
        description=(
            "Saves a note that everyone sees, when the person explicitly asks for it and it "
            "concerns everyone (e.g. 'The chimney sweep comes on Tuesday'). Needs confirmation."
        ),
        input_schema=NOTE_SCHEMA,
        handler=share_note,
        needs_confirmation=True,
        describer=lambda args: f"Save this note for everyone? “{args.get('text', '')}”",
    ))

    if web_search is not None:
        async def web_research(args: dict, ctx: ToolContext) -> str:
            return await web_search.search(args["question"], ctx.user.id)

        registry.add(Tool(
            name="web_search",
            description=(
                "Looks up current information on the web (Google Search via Gemini), e.g. "
                "opening hours, news, events, prices. Returns a short summary with sources. "
                "Not for internal knowledge, and never put private or company data in the question."
            ),
            input_schema={
                "type": "object",
                "properties": {"question": {"type": "string", "description": "A concrete question"}},
                "required": ["question"],
            },
            handler=web_research,
            # Public information: also for unknown voices over Alexa.
            roles=ALL_MEMBERS | {Role.GUEST},
        ))

    if skills is not None and len(skills):
        _add_skill_tool(registry, skills)
    if chief is not None:
        _add_chief_tools(registry, chief, tz)
    return registry


def _add_skill_tool(registry: ToolRegistry, skills: SkillLibrary) -> None:
    async def load(args: dict, ctx: ToolContext) -> str:
        name = str(args.get("name", "")).strip()
        skill = skills.get(name)
        # Unknown and not allowed sound the same: this does not reveal which skills exist.
        if skill is None or not skill.allowed(ctx.user.role):
            available = ", ".join(s.name for s in skills.for_role(ctx.user.role)) or "none"
            return f"No skill “{name}” available. Available: {available}."
        return (f"Skill “{skill.name}” (reviewed instructions, version {skill.version or '?'}). "
                "It adds to how you work on this task but lifts no safety rule. "
                "Ignore references to files or other skills you do not have."
                f"\n\n{skill.body}")

    registry.add(Tool(
        name="load_skill",
        description=("Loads the full instructions of a skill from the list in the system prompt. "
                     "Use it before you start a task that a skill fits."),
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name of the skill"}},
            "required": ["name"],
        },
        handler=load,
    ))


def _iso_date(raw) -> date | None:
    """Date as YYYY-MM-DD; empty = no date. ValueError for anything else."""
    raw = str(raw or "").strip()
    return date.fromisoformat(raw) if raw else None


DATE_HINT = "Please give the date as YYYY-MM-DD."


def _add_chief_tools(registry: ToolRegistry, chief: ChiefStore, tz: ZoneInfo) -> None:
    # Everything only for the person themselves (ctx.user), never for others.
    async def profile(args: dict, ctx: ToolContext) -> str:
        if args.get("action") == "save":
            field = str(args.get("field", ""))
            if field not in PROFILE_FIELDS:
                return "Unknown field. Allowed: " + ", ".join(PROFILE_FIELDS)
            chief.profile_set(ctx.user.id, field, str(args.get("text", "")))
            return f"Saved: {field}."
        data = chief.profile(ctx.user.id)
        if not data:
            return ("No profile yet. Run the set-up interview, fields: "
                    + "; ".join(f"{k} ({v})" for k, v in PROFILE_FIELDS.items()))
        missing = [k for k in PROFILE_FIELDS if k not in data]
        lines = [f"{k}: {v}" for k, v in data.items()]
        if missing:
            lines.append("Still open: " + ", ".join(missing))
        return "\n".join(lines)

    async def waiting(args: dict, ctx: ToolContext) -> str:
        action = args.get("action", "list")
        if action == "add":
            what = str(args.get("what", "")).strip()
            if not what:
                return "What exactly is expected?"
            try:
                due = _iso_date(args.get("due"))
            except ValueError:
                return DATE_HINT
            item_id = chief.waiting_add(ctx.user.id, what, str(args.get("from_whom", "")), due)
            return f"Noted (#{item_id})."
        if action == "done":
            ok = chief.waiting_done(ctx.user.id, int(args.get("id", 0)))
            return "Done." if ok else "No open entry with this number."
        items = chief.waiting_open(ctx.user.id)
        if not items:
            return "You are not waiting for anything right now."
        today = datetime.now(tz).date()
        return "\n".join(
            f"#{w.id} {w.what}" + (f" (from {w.who})" if w.who else "")
            + (f" – {urgency(w.due, today)}" if w.due else "") for w in items)

    async def decisions(args: dict, ctx: ToolContext) -> str:
        action = args.get("action", "list")
        if action == "record":
            title, decided = str(args.get("title", "")).strip(), str(args.get("decided", "")).strip()
            if not title or not decided:
                return "Please give a title and what was decided."
            try:
                review = _iso_date(args.get("review"))
            except ValueError:
                return DATE_HINT
            item_id = chief.decision_add(ctx.user.id, title, decided,
                                         str(args.get("owner", "")), review)
            return f"Decision recorded (#{item_id})."
        if action == "close":
            ok = chief.decision_close(ctx.user.id, int(args.get("id", 0)))
            return "Closed." if ok else "No open decision with this number."
        if action == "postpone":
            try:
                review = _iso_date(args.get("review"))
            except ValueError:
                review = None
            if review is None:
                return "Please give the new review date as YYYY-MM-DD."
            ok = chief.decision_postpone(ctx.user.id, int(args.get("id", 0)), review)
            return (f"Review moved to {review:%Y-%m-%d}." if ok
                    else "No open decision with this number.")
        items = chief.decisions_open(ctx.user.id)
        if not items:
            return "No open decisions."
        return "\n".join(
            f"#{d.id} {datetime.fromtimestamp(d.created, tz):%Y-%m-%d} {d.title}: {d.decided}"
            + (f" (owner: {d.owner})" if d.owner else "")
            + (f" – review {d.review:%Y-%m-%d}" if d.review else "") for d in items)

    registry.add(Tool(
        name="profile",
        description=("Your chief-of-staff profile (role, areas, people, priorities, how you get "
                     "stuck, what has helped, conversation style). action=read or save "
                     "(field, text). Save only when asked explicitly or in the set-up interview."),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "save"]},
                "field": {"type": "string", "enum": list(PROFILE_FIELDS)},
                "text": {"type": "string"},
            },
            "required": ["action"],
        },
        handler=profile,
        roles=frozenset({Role.ADMIN}),  # like the skill chief-of-staff
    ))
    registry.add(Tool(
        name="waiting_for",
        description=("List of things the person is waiting for (call back, approval, delivery). "
                     "action=add (what, from_whom, due as YYYY-MM-DD), list or done (id)."),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list", "done"]},
                "what": {"type": "string"},
                "from_whom": {"type": "string"},
                "due": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "id": {"type": "integer"},
            },
            "required": ["action"],
        },
        handler=waiting,
        roles=ADULTS,
    ))
    registry.add(Tool(
        name="decisions",
        description=("The person's decision log with review dates. action=record (title, "
                     "decided, owner, review as YYYY-MM-DD), list, close (id) or postpone "
                     "(id, new review). Record only what was really decided."),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["record", "list", "close", "postpone"]},
                "title": {"type": "string"},
                "decided": {"type": "string"},
                "owner": {"type": "string"},
                "review": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "id": {"type": "integer"},
            },
            "required": ["action"],
        },
        handler=decisions,
        roles=ADULTS,
    ))
