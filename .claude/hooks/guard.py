#!/usr/bin/env python3
"""Hooks for Claude Code in this repo (registered in .claude/settings.json).

  guard.py bash    PreToolUse for Bash:
                   - before `git commit`: blocks if secrets or private data are in the
                     diff (keys, .env, data/, service account, your own terms)
                   - before `git push`: blocks if the tests are not green
  guard.py merged  PostToolUse after a PR merge: a reminder about the release steps

Exit code 2 blocks the command; Claude sees the message on stderr.
Your own terms (e.g. a street name) go in .claude/forbidden-terms.txt (the v0.1.0 name
.claude/verbotene-begriffe.txt is still read); the file is in .gitignore, so the terms
themselves never reach the repo.
A line containing "guard: ok" is skipped (for deliberate exceptions).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROADMAP_URL = "https://claude.ai/artifact/R63XbmKYty5DD5RW9QfGYX"
TERMS_FILE = Path(".claude") / "forbidden-terms.txt"
OLD_TERMS_FILE = Path(".claude") / "verbotene-begriffe.txt"
SKIP_MARK = "guard: ok"

SECRET_PATTERNS = [
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("credentials in an environment variable",
     re.compile(r"^\s*[A-Z0-9_]*(?:KEY|TOKEN|PASSWORD|SECRET)[A-Z0-9_]*\s*=\s*[^\s#]{8,}")),
]

GIT_CMD = r"\bgit\s+(?:-C\s+(?P<dir>\S+)\s+)?(?:-c\s+\S+\s+)*"
COMMIT_RE = re.compile(GIT_CMD + r"commit\b")
PUSH_RE = re.compile(GIT_CMD + r"push\b")


def forbidden_path(path: str) -> str | None:
    """The reason why the file may never go into the repo, else None."""
    p = Path(path)
    if p.name == ".env":
        return ".env holds credentials"
    if p.parts and p.parts[0] in ("data", "wiki"):
        return f"{p.parts[0]}/ holds private data"
    if "dienstkonto" in p.name.lower() or p.suffix in (".pem", ".key"):
        return "key file"
    if p.name in (TERMS_FILE.name, OLD_TERMS_FILE.name):
        return "list of forbidden terms"
    return None


def find_secrets(text: str, terms: list[str] = ()) -> list[tuple[int, str]]:
    """(line number, kind) for each suspicious line."""
    hits = []
    lowered_terms = [t.lower() for t in terms if t.strip()]
    for no, line in enumerate(text.splitlines(), 1):
        if SKIP_MARK in line:
            continue
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append((no, label))
                break
        else:
            low = line.lower()
            if any(t in low for t in lowered_terms):
                hits.append((no, "forbidden term"))
    return hits


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Added lines from `git diff -U0` per file, with the line number in the new version."""
    result: dict[str, list[tuple[int, str]]] = {}
    current = None
    line_no = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:]
            current = None if name == "/dev/null" else name.removeprefix("b/")
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if current and raw.startswith("+"):
            result.setdefault(current, []).append((line_no, raw[1:]))
            line_no += 1
    return result


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=False).stdout


def load_terms(repo: Path) -> list[str]:
    terms = []
    for path in (repo / TERMS_FILE, repo / OLD_TERMS_FILE):
        if path.exists():
            terms += [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                      if l.strip() and not l.startswith("#")]
    return terms


def scan_repo(repo: Path) -> list[str]:
    """Checks every change a commit could take along: changed and new files.

    Deliberately generous (not only the index), because commands like
    `git add -A && git commit` add to the index only after the hook has run.
    """
    terms = load_terms(repo)
    problems = []
    changed = set(_git(repo, "diff", "HEAD", "--name-only").split())
    changed |= set(_git(repo, "diff", "--cached", "--name-only").split())
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").split()
    for path in sorted(changed | set(untracked)):
        reason = forbidden_path(path)
        if reason and (repo / path).exists():
            problems.append(f"{path}: {reason}")
    diff = _git(repo, "diff", "HEAD", "-U0", "--no-color")
    for path, lines in added_lines(diff).items():
        text = "\n".join(l for _, l in lines)
        for idx, label in find_secrets(text, terms):
            problems.append(f"{path}:{lines[idx - 1][0]}: {label}")
    for path in untracked:
        file = repo / path
        if forbidden_path(path) or not file.is_file() or file.stat().st_size > 1_000_000:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for no, label in find_secrets(text, terms):
            problems.append(f"{path}:{no}: {label}")
    return problems


def _target_repo(command_match: re.Match, cwd: str) -> Path | None:
    """Only step in when the command concerns this repo."""
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
    base = Path(cwd or ".")
    target = base / command_match.group("dir") if command_match.group("dir") else base
    top = subprocess.run(["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, check=False).stdout.strip()
    if not top or Path(top).resolve() != project.resolve():
        return None
    return project


def on_bash(event: dict) -> int:
    command = (event.get("tool_input") or {}).get("command", "")
    cwd = event.get("cwd", "")
    commit = COMMIT_RE.search(command)
    if commit and (repo := _target_repo(commit, cwd)):
        problems = scan_repo(repo)
        if problems:
            print("Commit blocked: possible secrets or private data:\n  "
                  + "\n  ".join(problems)
                  + "\nRemove them or, if harmless, mark the line with \"guard: ok\".",
                  file=sys.stderr)
            return 2
    push = PUSH_RE.search(command)
    if push and (repo := _target_repo(push, cwd)):
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=repo,
                             capture_output=True, text=True, check=False, timeout=540)
        if run.returncode != 0:
            tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-25:])
            print(f"Push blocked: the tests are not green:\n{tail}", file=sys.stderr)
            return 2
    return 0


def on_merged(event: dict) -> int:
    response = json.dumps(event.get("tool_response", ""))
    if '\\"merged\\":true' not in response and '"merged": true' not in response \
            and "successfully merged" not in response:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            "PR merged in jarvis-core. Per CLAUDE.md: raise the version in pyproject.toml and "
            "jarvis_core/__init__.py, have tag vX.Y.Z set as a release, raise the dependency "
            "in the profile repos, update the roadmap in jarvis-home (docs/roadmap.html) "
            f"and publish it again at {ROADMAP_URL}."),
    }}))
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if mode == "bash":
        return on_bash(event)
    if mode == "merged":
        return on_merged(event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
