"""Tests for the Claude Code hooks in .claude/hooks/guard.py."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guard.py"
spec = importlib.util.spec_from_file_location("guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

# Example values are put together so this file itself does not trigger the hook.
FAKE_ANTHROPIC = "sk-" + "ant-" + "a1B2" * 8
FAKE_TELEGRAM = "123456789" + ":" + "A" * 35
FAKE_GOOGLE = "AI" + "za" + "x" * 35


@pytest.mark.parametrize("line", [
    f"key = '{FAKE_ANTHROPIC}'",
    f"TELEGRAM_BOT_TOKEN={FAKE_TELEGRAM}",
    f"gemini: {FAKE_GOOGLE}",
    "-----BEGIN " + "PRIVATE KEY-----",
    "BRING_PASSWORD=" + "secret123",
])
def test_finds_secrets(line):
    assert guard.find_secrets(line)


@pytest.mark.parametrize("line", [
    "ANTHROPIC_API_KEY=",
    "BRING_PASSWORD=  # leave empty",
    'bring_password=os.environ.get("BRING_PASSWORD", "") or None,',
    "No secret here.",
])
def test_harmless_lines(line):
    assert guard.find_secrets(line) == []


def test_forbidden_terms_and_exception():
    text = "We live at Musterstraße 1\nMusterstraße 1  # guard: ok"
    assert guard.find_secrets(text, ["musterstraße"]) == [(1, "forbidden term")]


@pytest.mark.parametrize("path,forbidden", [
    (".env", True), (".env.example", False), ("data/people.toml", True),
    ("google-dienstkonto.json", True), ("jarvis/core.py", False),
    (".claude/forbidden-terms.txt", True), (".claude/verbotene-begriffe.txt", True),
])
def test_forbidden_paths(path, forbidden):
    assert (guard.forbidden_path(path) is not None) is forbidden


def test_added_lines_with_line_numbers():
    diff = ("+++ b/a.py\n@@ -1,0 +3,2 @@\n+one\n+two\n"
            "+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n")
    assert guard.added_lines(diff) == {"a.py": [(3, "one"), (4, "two")]}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.org")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("Hello\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "start")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _run(mode, event):
    return subprocess.run([sys.executable, str(GUARD), mode], input=json.dumps(event),
                          capture_output=True, text=True)


def test_commit_with_secret_is_blocked(repo):
    (repo / "config.py").write_text(f"KEY = '{FAKE_ANTHROPIC}'\n")
    run = _run("bash", {"tool_input": {"command": "git add -A && git commit -m x"},
                        "cwd": str(repo)})
    assert run.returncode == 2
    assert "config.py:1: Anthropic key" in run.stderr


@pytest.mark.parametrize("terms_file", ["forbidden-terms.txt", "verbotene-begriffe.txt"])
def test_commit_with_own_term_is_blocked(repo, terms_file):
    (repo / ".claude").mkdir()
    (repo / ".claude" / terms_file).write_text("# address\nMusterweg\n")
    (repo / ".gitignore").write_text(f".claude/{terms_file}\n")
    (repo / "README.md").write_text("Hello\nWe live on Musterweg.\n")
    run = _run("bash", {"tool_input": {"command": "git commit -am x"}, "cwd": str(repo)})
    assert run.returncode == 2
    assert "README.md:2: forbidden term" in run.stderr


def test_harmless_commit_and_other_repo_pass(repo, tmp_path_factory):
    (repo / "README.md").write_text("Hello world\n")
    assert _run("bash", {"tool_input": {"command": "git commit -am x"},
                         "cwd": str(repo)}).returncode == 0
    other = tmp_path_factory.mktemp("other")
    _git(other, "init", "-q")
    (other / "x.py").write_text(f"KEY = '{FAKE_ANTHROPIC}'\n")
    assert _run("bash", {"tool_input": {"command": "git commit -am x"},
                         "cwd": str(other)}).returncode == 0


def test_other_commands_untouched(repo):
    (repo / "config.py").write_text(f"KEY = '{FAKE_ANTHROPIC}'\n")
    assert _run("bash", {"tool_input": {"command": "git status"},
                         "cwd": str(repo)}).returncode == 0


def test_push_blocked_on_red_tests(repo):
    (repo / "test_red.py").write_text("def test_red():\n    assert False\n")
    run = _run("bash", {"tool_input": {"command": "git push -u origin main"},
                        "cwd": str(repo)})
    assert run.returncode == 2
    assert "the tests are not green" in run.stderr


def test_merge_reminds_about_release():
    run = _run("merged", {"tool_response": {"sha": "abc", "merged": True}})
    out = json.loads(run.stdout)
    assert "tag vX.Y.Z" in out["hookSpecificOutput"]["additionalContext"]
    assert _run("merged", {"tool_response": "Merge failed"}).stdout == ""
