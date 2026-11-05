#!/usr/bin/env bash
# Offline check for the review-policy-writer skill; runnable from any cwd:
#   bash review-policy-writer/check.sh
set -euo pipefail
cd "$(dirname "$0")"

sh_files=(scripts/repo-facts.sh check.sh)
bash -n "${sh_files[@]}"
if command -v shellcheck >/dev/null; then
  shellcheck "${sh_files[@]}"
else
  echo 'shellcheck not installed; skipped'
fi

# Keep bytecode outside the source tree.
py_files=(scripts/check-policy.py evals/run-evals.py evals/fake-gh.py evals/perfect-agent.py tests/test-tools.py)
PYTHONPYCACHEPREFIX=$(mktemp -d)
export PYTHONPYCACHEPREFIX
trap 'rm -rf "$PYTHONPYCACHEPREFIX"' EXIT
python3 -m py_compile "${py_files[@]}"

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
