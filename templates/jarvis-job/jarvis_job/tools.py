"""Werkzeuge von Jarvis Job: die allgemeinen aus jarvis-core plus eigene.

Eigene Werkzeuge (Chat, Kalender, Wiki, Tickets …) kommen hier
dazu. Regeln: lesend zuerst; alles mit Folgen mit needs_confirmation=True und
passenden Rollen; Inhalte aus Firmensystemen sind Daten, nie Anweisungen.
"""

from pathlib import Path

from jarvis_core.chief import ChiefStore
from jarvis_core.journal import JournalStore
from jarvis_core.skills import SkillLibrary
from jarvis_core.tools import Tool, ToolContext, ToolRegistry, build_core_tools
from jarvis_core.users import ADULTS


def build_tools(journals: JournalStore, wiki_dir: Path, timezone: str,
                skills: SkillLibrary | None = None,
                chief: ChiefStore | None = None) -> ToolRegistry:
    registry = build_core_tools(journals, wiki_dir, timezone, skills=skills, chief=chief)
    _add_example_tool(registry)
    return registry


def _add_example_tool(registry: ToolRegistry) -> None:
    """Platzhalter, damit das Gerüst etwas zeigt. Durch echte Werkzeuge ersetzen."""
    async def profile_info(args: dict, ctx: ToolContext) -> str:
        return "Profil: Jarvis Job. Eigene Werkzeuge kommen in jarvis_job/tools.py dazu."

    registry.add(Tool(
        name="jarvis_job_info",
        description="Sagt, welche Installation das ist (Jarvis Job) und wo Werkzeuge ergänzt werden.",
        input_schema={"type": "object", "properties": {}},
        handler=profile_info,
        roles=ADULTS,
    ))
