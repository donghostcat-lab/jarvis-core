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


def test_geruest_antwortet_mit_job_persona(tmp_path):
    client = FakeClient("Hallo")
    jarvis, users = build(settings(tmp_path), client=client)
    user = next(iter(users))
    reply = asyncio.run(jarvis.handle(user, "Hi", "cli"))
    assert reply.text == "Hallo"
    system = client.calls[0]["system"][0]["text"]
    assert "Du bist Jarvis, der Assistent des Teams." in system
    assert "chief-of-staff" in system  # eingebauter Skill des Kerns
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert {"jarvis_job_info", "skill_laden", "warte_auf"} <= names
