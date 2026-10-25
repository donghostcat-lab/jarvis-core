"""Tools of Jarvis Job: the general ones from jarvis-core plus your own.

Your own tools (chat, calendar, wiki, tickets …) are added here. Rules: read-only first;
anything with consequences with needs_confirmation=True and suitable roles; content from
company systems is data, never instructions.
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
    """A placeholder so the starter shows something. Replace it with real tools."""
    async def profile_info(args: dict, ctx: ToolContext) -> str:
        return "Profile: Jarvis Job. Add your own tools in jarvis_job/tools.py."

    registry.add(Tool(
        name="jarvis_job_info",
        description="Says which installation this is (Jarvis Job) and where tools are added.",
        input_schema={"type": "object", "properties": {}},
        handler=profile_info,
        roles=ADULTS,
    ))
