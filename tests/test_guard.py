"""Tests für die Claude-Code-Hooks in .claude/hooks/guard.py."""

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

# Beispielwerte zusammengesetzt, damit diese Datei selbst den Hook nicht auslöst.
FAKE_ANTHROPIC = "sk-" + "ant-" + "a1B2" * 8
FAKE_TELEGRAM = "123456789" + ":" + "A" * 35
FAKE_GOOGLE = "AI" + "za" + "x" * 35


@pytest.mark.parametrize("line", [
    f"key = '{FAKE_ANTHROPIC}'",
    f"TELEGRAM_BOT_TOKEN={FAKE_TELEGRAM}",
    f"gemini: {FAKE_GOOGLE}",
    "-----BEGIN " + "PRIVATE KEY-----",
    "BRING_PASSWORD=" + "geheim123",
])
def test_findet_geheimnisse(line):
    assert guard.find_secrets(line)


@pytest.mark.parametrize("line", [
    "ANTHROPIC_API_KEY=",
    "BRING_PASSWORD=  # leer lassen",
    'bring_password=os.environ.get("BRING_PASSWORD", "") or None,',
    "Kein Geheimnis hier.",
])
def test_harmlose_zeilen(line):
    assert guard.find_secrets(line) == []


def test_verbotene_begriffe_und_ausnahme():
    text = "Wir wohnen in der Musterstraße 1\nMusterstraße 1  # guard: ok"
    assert guard.find_secrets(text, ["musterstraße"]) == [(1, "verbotener Begriff")]


@pytest.mark.parametrize("path,verboten", [
    (".env", True), (".env.example", False), ("data/familie.toml", True),
    ("google-dienstkonto.json", True), ("jarvis/core.py", False),
    (".claude/verbotene-begriffe.txt", True),
])
def test_verbotene_pfade(path, verboten):
    assert (guard.forbidden_path(path) is not None) is verboten


def test_added_lines_mit_zeilennummern():
    diff = ("+++ b/a.py\n@@ -1,0 +3,2 @@\n+eins\n+zwei\n"
            "+++ /dev/null\n@@ -1 +0,0 @@\n-weg\n")
    assert guard.added_lines(diff) == {"a.py": [(3, "eins"), (4, "zwei")]}


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.org")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("Hallo\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "start")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return tmp_path


def _run(mode, event):
    return subprocess.run([sys.executable, str(GUARD), mode], input=json.dumps(event),
                          capture_output=True, text=True)


def test_commit_mit_geheimnis_wird_blockiert(repo):
    (repo / "config.py").write_text(f"KEY = '{FAKE_ANTHROPIC}'\n")
    run = _run("bash", {"tool_input": {"command": "git add -A && git commit -m x"},
                        "cwd": str(repo)})
    assert run.returncode == 2
    assert "config.py:1: Anthropic-Schlüssel" in run.stderr


def test_commit_mit_eigenem_begriff_wird_blockiert(repo):
    (repo / ".claude").mkdir()
    (repo / ".claude" / "verbotene-begriffe.txt").write_text("# Adresse\nMusterweg\n")
    (repo / ".gitignore").write_text(".claude/verbotene-begriffe.txt\n")
    (repo / "README.md").write_text("Hallo\nWir wohnen am Musterweg.\n")
    run = _run("bash", {"tool_input": {"command": "git commit -am x"}, "cwd": str(repo)})
    assert run.returncode == 2
    assert "README.md:2: verbotener Begriff" in run.stderr


def test_harmloser_commit_und_fremdes_repo_laufen_durch(repo, tmp_path_factory):
    (repo / "README.md").write_text("Hallo Welt\n")
    assert _run("bash", {"tool_input": {"command": "git commit -am x"},
                         "cwd": str(repo)}).returncode == 0
    other = tmp_path_factory.mktemp("anderes")
    _git(other, "init", "-q")
    (other / "x.py").write_text(f"KEY = '{FAKE_ANTHROPIC}'\n")
    assert _run("bash", {"tool_input": {"command": "git commit -am x"},
                         "cwd": str(other)}).returncode == 0


def test_andere_befehle_unberuehrt(repo):
    (repo / "config.py").write_text(f"KEY = '{FAKE_ANTHROPIC}'\n")
    assert _run("bash", {"tool_input": {"command": "git status"},
                         "cwd": str(repo)}).returncode == 0


def test_push_blockiert_bei_roten_tests(repo):
    (repo / "test_rot.py").write_text("def test_rot():\n    assert False\n")
    run = _run("bash", {"tool_input": {"command": "git push -u origin main"},
                        "cwd": str(repo)})
    assert run.returncode == 2
    assert "Tests sind nicht grün" in run.stderr


def test_merge_erinnert_an_roadmap():
    run = _run("merged", {"tool_response": {"sha": "abc", "merged": True}})
    out = json.loads(run.stdout)
    assert "Tag vX.Y.Z" in out["hookSpecificOutput"]["additionalContext"]
    assert _run("merged", {"tool_response": "Merge fehlgeschlagen"}).stdout == ""
