#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

while IFS= read -r -d '' script; do
  bash -n "$script"
done < <(find . -type d -path './evals/results' -prune -o -type f -name '*.sh' -print0)

# Keep bytecode outside the source tree.
PYTHONPYCACHEPREFIX=$(mktemp -d)
export PYTHONPYCACHEPREFIX
trap 'rm -rf "$PYTHONPYCACHEPREFIX"' EXIT
while IFS= read -r -d '' source; do
  python3 -m py_compile "$source"
done < <(find . -type d -path './evals/results' -prune -o -type f -name '*.py' -print0)

python3 - <<'PY'
import json
from pathlib import Path

for path in Path('evals/scenarios').glob('*.json'):
    json.loads(path.read_text())
print('Scenario JSON valid')
PY
python3 tests/test-review-threads.py
python3 evals/run-evals.py --dry-run
python3 evals/run-evals.py --self-test
