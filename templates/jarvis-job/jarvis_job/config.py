"""Einstellungen aus Umgebungsvariablen (.env)."""

import os
from dataclasses import dataclass
from pathlib import Path

PERSONA = "der Assistent des Teams"


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    model: str
    local_llm_url: str | None
    local_llm_model: str
    data_dir: Path
    timezone: str
    skills_dir: Path


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("JARVIS_DATA_DIR", "/data"))
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() or None,
        model=os.environ.get("JARVIS_MODEL", "").strip() or "claude-sonnet-5-5",
        local_llm_url=os.environ.get("LOCAL_LLM_URL", "").strip() or None,
        local_llm_model=os.environ.get("LOCAL_LLM_MODEL", "").strip() or "local-model",
        data_dir=data_dir,
        timezone=os.environ.get("JARVIS_TIMEZONE", "Europe/Berlin"),
        skills_dir=Path(os.environ.get("JARVIS_SKILLS_DIR", "").strip()
                        or Path(__file__).resolve().parents[1] / "skills"),
    )
