#!/usr/bin/env python3
"""Hooks für Claude Code in diesem Repo (eingetragen in .claude/settings.json).

  guard.py bash    PreToolUse für Bash:
                   - vor `git commit`: blockiert, wenn Geheimnisse oder private Daten
                     im Diff stehen (Schlüssel, .env, data/, Dienstkonto, eigene Begriffe)
                   - vor `git push`: blockiert, wenn die Tests nicht grün sind
  guard.py merged  PostToolUse nach einem PR-Merge: erinnert an die Roadmap-Seite

Exit-Code 2 blockiert den Befehl; die Meldung auf stderr bekommt Claude zu sehen.
Eigene Begriffe (z. B. Straßenname) stehen in .claude/verbotene-begriffe.txt; die Datei
ist in .gitignore, damit die Begriffe selbst nie ins Repo gelangen.
Eine Zeile mit „guard: ok“ wird übersprungen (für bewusste Ausnahmen).
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROADMAP_URL = "https://claude.ai/artifact/R63XbmKYty5DD5RW9QfGYX"
TERMS_FILE = Path(".claude") / "verbotene-begriffe.txt"
SKIP_MARK = "guard: ok"

SECRET_PATTERNS = [
    ("Anthropic-Schlüssel", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-artiger Schlüssel", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("Google-API-Schlüssel", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Telegram-Bot-Token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("GitHub-Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("privater Schlüssel", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Zugangsdaten in Umgebungsvariable",
     re.compile(r"^\s*[A-Z0-9_]*(?:KEY|TOKEN|PASSWORD|SECRET)[A-Z0-9_]*\s*=\s*[^\s#]{8,}")),
]

GIT_CMD = r"\bgit\s+(?:-C\s+(?P<dir>\S+)\s+)?(?:-c\s+\S+\s+)*"
COMMIT_RE = re.compile(GIT_CMD + r"commit\b")
PUSH_RE = re.compile(GIT_CMD + r"push\b")


def forbidden_path(path: str) -> str | None:
    """Grund, warum die Datei nie ins Repo darf, sonst None."""
    p = Path(path)
    if p.name == ".env":
        return ".env enthält Zugangsdaten"
    if p.parts and p.parts[0] in ("data", "wiki"):
        return f"{p.parts[0]}/ enthält private Daten der Familie"
    if "dienstkonto" in p.name.lower() or p.suffix in (".pem", ".key"):
        return "Schlüsseldatei"
    if p.name == TERMS_FILE.name:
        return "Liste verbotener Begriffe"
    return None


def find_secrets(text: str, terms: list[str] = ()) -> list[tuple[int, str]]:
    """(Zeilennummer, Art) für jede verdächtige Zeile."""
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
                hits.append((no, "verbotener Begriff"))
    return hits


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Hinzugefügte Zeilen aus `git diff -U0` je Datei, mit Zeilennummer im neuen Stand."""
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
    path = repo / TERMS_FILE
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]


def scan_repo(repo: Path) -> list[str]:
    """Prüft alle Änderungen, die ein Commit mitnehmen könnte: geänderte und neue Dateien.

    Bewusst großzügig (nicht nur der Index), weil Befehle wie `git add -A && git commit`
    erst nach dem Hook etwas zum Index hinzufügen.
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
    """Nur eingreifen, wenn der Befehl dieses Repo betrifft."""
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
            print("Commit blockiert – mögliche Geheimnisse oder private Daten:\n  "
                  + "\n  ".join(problems)
                  + "\nEntfernen oder, falls harmlos, die Zeile mit „guard: ok“ markieren.",
                  file=sys.stderr)
            return 2
    push = PUSH_RE.search(command)
    if push and (repo := _target_repo(push, cwd)):
        run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x"], cwd=repo,
                             capture_output=True, text=True, check=False, timeout=540)
        if run.returncode != 0:
            tail = "\n".join((run.stdout + run.stderr).strip().splitlines()[-25:])
            print(f"Push blockiert – Tests sind nicht grün:\n{tail}", file=sys.stderr)
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
            "PR in jarvis-core gemergt. Laut CLAUDE.md: Version in pyproject.toml und "
            "jarvis_core/__init__.py anheben, Tag vX.Y.Z setzen, in den Profil-Repos die "
            "Abhängigkeit anheben und die Roadmap in jarvis-home (docs/roadmap.html) "
            f"nachziehen und unter {ROADMAP_URL} neu veröffentlichen."),
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
