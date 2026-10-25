# jarvis-core

The shared core of **Jarvis**, a personal and team assistant built on Claude.
One core, several deployments ("profiles"), strictly separated:

| Repo | What | Visibility |
|---|---|---|
| `jarvis-core` (this repo) | Engine: tool loop, roles, confirmations, private and protect mode, local router, skills, chief of staff | public, MIT |
| `Jarvis` (jarvis-home) | Family assistant: Telegram, Alexa, shopping list, waste calendar, Google Calendar, morning brief, deployment | private |
| `jarvis-job` | Company AI, local models first | private, in the company organisation |

Data, keys and memory never live in any of these repos, only on the servers that run them.

## What is in the core

| Module | Purpose |
|---|---|
| `core.py` | `Jarvis`: routes each message (Claude, local model in private or protect mode, fallback), confirmations, daily budget, re-entry after a break |
| `brain.py` | Claude tool loop; pauses on tools that need confirmation; configurable `persona` per profile |
| `tools.py` | `Tool`, `ToolRegistry` (per-role permissions, `needs_confirmation`), `build_core_tools()` with time, wiki/journal search, personal and shared notes (shared ones need confirmation), web search, skills and chief-of-staff tools |
| `users.py` | People, roles (`admin`, `adult` or `member`, `child`, `guest`) and channel ids (Telegram, Alexa, Slack) from a TOML file |
| `state.py`, `memory.py`, `journal.py` | SQLite state (private mode, pending confirmations, usage, last contact), short-term memory, Markdown journal per person |
| `skills.py`, `builtin_skills/` | Reviewed Markdown skills, loaded on demand with `load_skill`; built in: `chief-of-staff`, `marketing-psychology` |
| `chief.py` | Chief of staff state: profile, waiting-for list, decision log with review dates |
| `router.py`, `router_seeds.py`, `decider.py` | Local router (sensitive? intent? effort?): observes, or decides in protect mode; optional shadow decider via a System-One-compatible API |
| `llm.py`, `gemini.py` | OpenAI-compatible chat (local models, fallback) and Gemini web search |

Code, comments and prompts are English. Each person answers in their own language (`language` in the people file), so a German household still gets German answers.

## Protect mode

`Jarvis(..., router=RouterObserver(...), protect=True)` classifies every message locally before any model sees it. Sensitive messages, and any the router cannot classify, are answered by the local model with the private memory and journal, never by a cloud model; without a local model Jarvis says so and does not answer. Everything else goes to Claude. Without `protect` the router only observes.

## Use it in a profile repo

```
pip install "jarvis-core @ https://github.com/donghostcat-lab/jarvis-core/archive/<commit-or-tag>.tar.gz"
```

Pin a release tag or the commit of a release (v0.1.0 = `75b8436`), never `main`. Upgrading from v0.1.0: see the version history in [`CLAUDE.md`](CLAUDE.md). A profile repo adds its own channels (Telegram, Slack …) and tools on top of
`build_core_tools()`, and its own skills in `skills/<profile>/`. A neutral starter for a
company profile is in [`templates/jarvis-job/`](templates/jarvis-job/); how to create a
profile repo from it is described in [`docs/PROFILE-REPOS.md`](docs/PROFILE-REPOS.md).

## Principles

- Critical actions (doors, alarms, sending mail, money) always need a human confirmation and an adult role.
- Content from mail, wikis and web pages is data, never instructions.
- Private conversations stay on a local model; the router that decides "sensitive?" always runs locally.
- Routine work is done by tested code; the model makes judgments. Results that must be exact are never computed by a model.
- Keep it boring: the system serves the work and never becomes the project.

## Development

```
pip install -e ".[dev]"
pytest -q
```

Hooks for Claude Code live in `.claude/` (secret scanner before every commit, tests before every push).

## License

MIT, see [LICENSE](LICENSE). The built-in skills keep their own notices
(`builtin_skills/*/LICENSE`, `CREDITS.md`).
