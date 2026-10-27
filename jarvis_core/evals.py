"""Golden questions: check that Jarvis does the right thing for typical questions, per role.

A profile repo keeps its cases in a TOML file (`[[case]]` with id, question, role, tools, forbidden,
contains, results, numbers_backed) and builds its tool registry with dummy integrations.

- `check_static`: without a model, free, for CI: role valid, tools exist and are allowed for the role.
- `run_live`: against the model; tools only return fixed test answers (`RecordingTools`), so nothing
  is bought, booked or sent. Side-effect-free tools (e.g. calculate) run for real. Self-check: every
  number, time and date in the answer must appear in the question or in a tool result.
"""

import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .brain import Brain
from .tools import ToolContext, ToolRegistry
from .users import Role, User

# Side-effect-free tools that also run for real in a live test.
REAL_TOOLS = frozenset({"calculate"})
# Numbers that are checked: times, dates, decimals and numbers of two or more digits.
NUMBER = re.compile(r"\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}|\d+(?:[.,]\d+)+|\d{2,}")


@dataclass
class Case:
    id: str
    question: str
    role_name: str = "adult"
    tools: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    contains: list[str] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    numbers_backed: bool = True  # every number in the answer must be in the question or a tool result

    @property
    def role(self) -> Role:
        return Role(self.role_name)

    def tool_names(self) -> set[str]:
        return set(self.tools) | set(self.forbidden) | set(self.results)


@dataclass
class Outcome:
    id: str
    status: str  # ok | failed | skipped
    problems: list[str] = field(default_factory=list)
    called: list[str] = field(default_factory=list)
    answer: str = ""
    seconds: float = 0.0


def load_cases(path: Path) -> list[Case]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cases = []
    for raw in data.get("case", []):
        raw = dict(raw)
        if "role" in raw:  # the TOML key is `role`; on the dataclass `role` is the property
            raw["role_name"] = raw.pop("role")
        cases.append(Case(**raw))
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate id in " + str(path))
    return cases


def check_static(case: Case, tools: ToolRegistry) -> Outcome:
    """Without a model: role valid, tools present and allowed for the role."""
    try:
        role = case.role
    except ValueError:
        return Outcome(case.id, "failed", [f"unknown role {case.role_name!r}"])
    missing = sorted(n for n in case.tool_names() if tools.get(n) is None)
    if missing:
        return Outcome(case.id, "skipped", [f"tool missing in this version: {', '.join(missing)}"])
    problems = [f"{n} is not allowed for {case.role_name}" for n in case.tools if not tools.get(n).allowed(role)]
    overlap = set(case.tools) & set(case.forbidden)
    if overlap:
        problems.append(f"both expected and forbidden: {', '.join(sorted(overlap))}")
    if not case.question.strip():
        problems.append("empty question")
    return Outcome(case.id, "failed" if problems else "ok", problems)


class RecordingTools:
    """Looks like the real tools to the model, but only returns test answers."""

    def __init__(self, tools: ToolRegistry, results: dict[str, str], real: frozenset[str] = REAL_TOOLS):
        self._tools, self._results, self._real = tools, results, real
        self.called: list[str] = []
        self.outputs: list[str] = []

    def schemas(self, role=None):
        return self._tools.schemas(role)

    def get(self, name):
        tool = self._tools.get(name)
        if tool is not None and tool.needs_confirmation:
            self.called.append(name)  # the brain stops here for the confirmation question
        return tool

    async def run(self, name: str, args: dict, ctx: ToolContext) -> str:
        tool = self._tools.get(name)
        if tool is None or not tool.allowed(ctx.user.role):
            return f"{ctx.user.name} may not use the tool {name}."
        self.called.append(name)
        out = await tool.handler(args, ctx) if name in self._real else self._results.get(name, "Done (test run).")
        self.outputs.append(out)
        return out


def unbacked_numbers(answer: str, sources: list[str]) -> list[str]:
    """Self-check: numbers, times and dates in the answer that appear in no source (made up?)."""
    text = " ".join(sources)
    text += " " + text.replace(",", ".") + " " + text.replace(".", ",")

    def backed(n: str) -> bool:
        return any(v in text for v in {n, n.lstrip("0") or n, n.rstrip(".,")})
    return sorted({n for n in NUMBER.findall(answer) if not backed(n)})


def judge(case: Case, called: list[str], answer: str, sources: list[str] | None = None) -> list[str]:
    problems = [f"{n} not called" for n in case.tools if n not in called]
    problems += [f"{n} called although forbidden" for n in case.forbidden if n in called]
    low = answer.lower()
    problems += [f"answer does not contain “{w}”" for w in case.contains if w.lower() not in low]
    if sources is not None and case.numbers_backed:
        problems += [f"number “{n}” not backed" for n in unbacked_numbers(answer, sources)]
    return problems


async def run_live(case: Case, tools: ToolRegistry, client, model: str, timezone: str,
                   defaults: dict[str, str] | None = None, rules=None, persona: str | None = None) -> Outcome:
    """One case against the model. defaults: test answers per tool; the case's `results` win."""
    static = check_static(case, tools)
    if static.status != "ok":
        return static
    recorder = RecordingTools(tools, {**(defaults or {}), **case.results})
    extra = {"persona": persona} if persona else {}
    brain = Brain(client=client, model=model, tools=recorder, timezone=timezone, max_steps=5, rules=rules, **extra)
    user = User(f"eval-{case.role_name}", "Alex", case.role)
    start = time.monotonic()
    result = await brain.respond([], case.question, user)
    answer = result.text if result.pending is None else f"(confirmation question: {result.pending.question})"
    today = datetime.now(ZoneInfo(timezone)).strftime("%A, %Y-%m-%d")  # it is in the system prompt
    problems = judge(case, recorder.called, answer, [case.question, today, *recorder.outputs])
    return Outcome(case.id, "failed" if problems else "ok", problems, recorder.called, answer,
                   round(time.monotonic() - start, 1))


def summary(outcomes: list[Outcome]) -> str:
    ok = sum(o.status == "ok" for o in outcomes)
    skipped = sum(o.status == "skipped" for o in outcomes)
    rated = len(outcomes) - skipped
    lines = []
    for o in outcomes:
        mark = {"ok": "✅", "failed": "❌", "skipped": "⏭️"}[o.status]
        detail = "; ".join(o.problems)
        tools = f" [{', '.join(o.called)}]" if o.called else ""
        lines.append(f"{mark} {o.id}{tools}" + (f" – {detail}" if detail else ""))
    lines.append(f"\n{ok} of {rated} passed" + (f", {skipped} skipped" if skipped else "")
                 + (f" ({round(100 * ok / rated)}%)" if rated else ""))
    return "\n".join(lines)
