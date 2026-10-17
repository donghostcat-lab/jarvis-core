# Jarvis Job

Company AI based on [`jarvis-core`](https://github.com/donghostcat-lab/jarvis-core).
Starter created from `jarvis-core/templates/jarvis-job`; see `CLAUDE.md` for scope and rules.

## Try it locally

```
pip install -r requirements.txt pytest
cp .env.example .env            # fill in the keys that IT approved
python -m pytest -q
python -m jarvis_job.cli        # terminal chat
```

## Structure

| Path | Purpose |
|---|---|
| `jarvis_job/config.py` | Settings from `.env`, persona of this profile |
| `jarvis_job/tools.py` | Core tools plus company tools |
| `jarvis_job/cli.py` | Terminal adapter for trying things out; chat adapters come next as thin adapters |
| `skills/job/` | Company skills (reviewed, pinned versions) |
| `personen.example.toml` | People and roles, copy to `data/personen.toml` |

Data, keys and memory live only on the company servers, never in this repo.
