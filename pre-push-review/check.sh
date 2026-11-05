#!/usr/bin/env bash
# Offline check for the pre-push-review skill. Runnable from any cwd:
#   bash pre-push-review/check.sh
set -euo pipefail
cd "$(dirname "$0")"

bash -n scripts/local-diff.sh check.sh
if command -v shellcheck >/dev/null; then
  shellcheck scripts/local-diff.sh check.sh
else
  echo 'shellcheck not installed; skipped'
fi
# Keep bytecode outside the source tree.
PYTHONPYCACHEPREFIX=$(mktemp -d)
export PYTHONPYCACHEPREFIX
trap 'rm -rf "$PYTHONPYCACHEPREFIX"' EXIT
python3 -m py_compile scripts/policy-lane.py tests/test-tools.py evals/run-evals.py evals/perfect-agent.py
python3 - <<'PY'
import json
from pathlib import Path
for path in sorted(Path('evals/scenarios').glob('*.json')):
    json.loads(path.read_text())
print('Scenario JSON valid')
PY
python3 tests/test-tools.py
python3 evals/run-evals.py --dry-run
python3 evals/run-evals.py --self-test
