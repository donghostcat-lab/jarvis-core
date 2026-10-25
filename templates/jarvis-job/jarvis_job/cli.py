"""Terminal channel for trying things out: python -m jarvis_job.cli

Speaks as the first person in data/people.toml (or "Admin"). A Slack channel comes later
(Socket Mode, no open port), also as a thin adapter around the same core; it finds people
with `UserDirectory.by_slack`.
"""

import asyncio
import logging

from jarvis_core.brain import Brain
from jarvis_core.chief import ChiefStore
from jarvis_core.core import Jarvis
from jarvis_core.journal import JournalStore
from jarvis_core.llm import OpenAICompatibleChat
from jarvis_core.memory import ShortTermMemory
from jarvis_core.skills import SkillLibrary, skill_dirs
from jarvis_core.state import StateStore
from jarvis_core.users import UserDirectory

from .config import PERSONA, Settings, load_settings
from .tools import build_tools


def build(s: Settings, client=None) -> tuple[Jarvis, UserDirectory]:
    """client: an Anthropic-compatible client; a fake in tests."""
    s.data_dir.mkdir(parents=True, exist_ok=True)
    people = s.data_dir / "people.toml"
    users = UserDirectory.from_toml(people) if people.exists() else UserDirectory.single("Admin", frozenset())
    if client is None:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=s.anthropic_api_key)
    skills = SkillLibrary.from_dirs(skill_dirs(s.skills_dir, "job"))
    journals = JournalStore(s.data_dir / "journal", s.timezone)
    chief = ChiefStore(s.data_dir / "jarvis.sqlite")
    tools = build_tools(journals, s.data_dir / "wiki", s.timezone, skills=skills, chief=chief)
    jarvis = Jarvis(
        brain=Brain(client, s.model, tools, s.timezone, skills=skills, persona=PERSONA),
        state=StateStore(s.data_dir / "jarvis.sqlite"),
        memory=ShortTermMemory(s.data_dir / "short-term.sqlite"),
        private_memory=ShortTermMemory(s.data_dir / "private.sqlite"),
        journals=journals,
        private_journals=JournalStore(s.data_dir / "private-journal", s.timezone),
        timezone=s.timezone,
        local_llm=(OpenAICompatibleChat("local", s.local_llm_url, s.local_llm_model)
                   if s.local_llm_url else None),
    )
    return jarvis, users


async def chat() -> None:
    jarvis, users = build(load_settings())
    user = next(iter(users))
    print(f"Jarvis Job: you are {user.name}. An empty line ends the chat.")
    while (text := input("> ").strip()):
        reply = await jarvis.handle(user, text, "cli")
        if reply.confirm_token:
            answer = input(f"{reply.question} [y/N] ").strip().lower() == "y"
            entry = jarvis.take_pending(user, reply.confirm_token)
            reply = await jarvis.resume(user, entry, answer) if entry else reply
        print(reply.text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(chat())
