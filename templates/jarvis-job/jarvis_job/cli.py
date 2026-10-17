"""Terminal-Zugang zum Ausprobieren: python -m jarvis_job.cli

Spricht als erste Person aus data/personen.toml (oder „Admin“). Später
kommt ein Slack-Zugang dazu (Socket Mode, kein offener Port), ebenfalls als dünner
Adapter um denselben Kern.
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
    """client: Anthropic-kompatibler Client; in Tests ein Fake."""
    s.data_dir.mkdir(parents=True, exist_ok=True)
    people = s.data_dir / "personen.toml"
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
        memory=ShortTermMemory(s.data_dir / "kurzzeit.sqlite"),
        private_memory=ShortTermMemory(s.data_dir / "privat.sqlite"),
        journals=journals,
        private_journals=JournalStore(s.data_dir / "privat-journal", s.timezone),
        timezone=s.timezone,
        local_llm=(OpenAICompatibleChat("lokal", s.local_llm_url, s.local_llm_model)
                   if s.local_llm_url else None),
    )
    return jarvis, users


async def chat() -> None:
    jarvis, users = build(load_settings())
    user = next(iter(users))
    print(f"Jarvis Job – du bist {user.name}. Leere Zeile beendet.")
    while (text := input("> ").strip()):
        reply = await jarvis.handle(user, text, "cli")
        if reply.confirm_token:
            answer = input(f"{reply.question} [j/N] ").strip().lower() == "j"
            entry = jarvis.take_pending(user, reply.confirm_token)
            reply = await jarvis.resume(user, entry, answer) if entry else reply
        print(reply.text)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(chat())
