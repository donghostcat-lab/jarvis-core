#!/bin/bash
# Start-Hook für Claude-Code-Cloud-Sitzungen: Paket mit Testabhängigkeiten installieren.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"
python -m pip install --quiet --disable-pip-version-check --root-user-action=ignore -e ".[dev]"
echo "jarvis-core installiert; Tests mit: python -m pytest -q"
