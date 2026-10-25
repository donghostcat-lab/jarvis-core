import asyncio
from pathlib import Path
from types import SimpleNamespace

from jarvis_job.cli import build
from jarvis_job.config import Settings


class FakeClient:
    def __init__(self, text):
        self.calls = []
        self.messages = self
        self.text = text

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=self.text)], usage=None)


def settings(tmp_path: Path) -> Settings:
    return Settings(anthropic_api_key=None, model="m", local_llm_url=None, local_llm_model="x",
                    data_dir=tmp_path, timezone="Europe/Berlin",
                    skills_dir=Path(__file__).resolve().parents[1] / "skills")


def test_starter_answers_with_job_persona(tmp_path):
    client = FakeClient("Hello")
    jarvis, users = build(settings(tmp_path), client=client)
    user = next(iter(users))
    reply = asyncio.run(jarvis.handle(user, "Hi", "cli"))
    assert reply.text == "Hello"
    system = client.calls[0]["system"][0]["text"]
    assert "You are Jarvis, the team's assistant." in system
    assert "chief-of-staff" in system  # built-in skill of the core
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert {"jarvis_job_info", "load_skill", "waiting_for", "share_note"} <= names
