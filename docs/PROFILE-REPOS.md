# Creating a profile repo

A profile repo is a private deployment of Jarvis (for example the family assistant or a
company AI). It depends on a pinned tag of `jarvis-core` and adds channels, tools and skills.

## From the template

1. Copy `templates/jarvis-job/` into a new, empty private repository in the organisation that
   will own it (for a company AI: the company's GitHub organisation, never a personal account).
2. Replace `CLAUDE.md` with the real project context (team, systems, hardware, roles, rules).
   The template version is deliberately generic.
3. `pip install -r requirements.txt pytest && python -m pytest -q` – the starter test must pass.
4. Copy `.env.example` to `.env` (never commit it) and `personen.example.toml` to
   `data/personen.toml`.
5. `python -m jarvis_job.cli` gives a terminal chat to try the profile.
6. Copy the Claude Code hooks from `jarvis-core/.claude/` (secret scanner before commits,
   tests before pushes) and add the organisation's own forbidden terms to
   `.claude/verbotene-begriffe.txt` (gitignored).

## Upgrading the core

Bump the tag in `requirements.txt`, run the tests, read the version history in
`jarvis-core/CLAUDE.md` for interface changes.

## Contributing back

Generic improvements (useful for every profile) go to `jarvis-core` as a pull request.
Company-specific code, data, prompts, skills and integrations stay in the profile repo.

## Known gaps in v0.1.0

- `Brain` speaks the Anthropic Messages API only. Local-first deployments need a tool loop
  over OpenAI-compatible local models (Ollama, MLX, vLLM).
- `User` knows Telegram and Alexa ids, not yet Slack (or other chat) ids.
- Role names come from the family profile (`admin`, `erwachsen`, `kind`, `gast`); teams need
  their own roles or a mapping.
