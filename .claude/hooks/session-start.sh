#!/bin/bash
# Start hook for Claude Code cloud sessions: install the package with its test dependencies.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"
python -m pip install --quiet --disable-pip-version-check --root-user-action=ignore -e ".[dev]"
echo "jarvis-core installed; run the tests with: python -m pytest -q"
