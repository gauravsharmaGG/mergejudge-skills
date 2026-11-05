#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if command -v shellcheck >/dev/null 2>&1; then
  shell_scripts=()
  while IFS= read -r -d '' script; do
    if git ls-files --error-unmatch -- "$script" >/dev/null 2>&1; then
      shell_scripts+=("$script")
    fi
  done < <(find . -type d \( -name .git -o -path '*/evals/results' \) -prune -o -type f -name '*.sh' -print0)
  if (( ${#shell_scripts[@]} )); then
    shellcheck "${shell_scripts[@]}"
  fi
else
  echo 'shellcheck not installed; skipped'
fi

skill_count=0
while IFS= read -r -d '' skill_dir; do
  [[ -f "$skill_dir/SKILL.md" ]] || continue
  skill_count=$((skill_count + 1))
  skill_name=${skill_dir#./}
  if ! python3 - "$skill_dir/SKILL.md" "$skill_name" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
lines = path.read_text().splitlines()
if not lines or lines[0] != '---' or '---' not in lines[1:]:
    sys.exit(f'{path}: missing frontmatter')
frontmatter = lines[1:lines.index('---', 1)]
fields = {}
for line in frontmatter:
    match = re.fullmatch(r'(name|description):\s*(.*?)\s*', line)
    if match:
        fields[match[1]] = match[2].strip('\"\'')
if fields.get('name') != sys.argv[2]:
    sys.exit(f'{path}: name must equal {sys.argv[2]}')
if not fields.get('description'):
    sys.exit(f'{path}: missing description')
PY
  then
    printf '%s: FAIL (frontmatter)\n' "$skill_name"
    exit 1
  fi
  if [[ -f "$skill_dir/check.sh" ]]; then
    if bash "$skill_dir/check.sh"; then
      printf '%s: PASS (check.sh)\n' "$skill_name"
    else
      printf '%s: FAIL (check.sh)\n' "$skill_name"
      exit 1
    fi
  elif [[ -f "$skill_dir/evals/run-evals.py" ]]; then
    if python3 "$skill_dir/evals/run-evals.py" --dry-run &&
       python3 "$skill_dir/evals/run-evals.py" --self-test; then
      printf '%s: PASS (evals)\n' "$skill_name"
    else
      printf '%s: FAIL (evals)\n' "$skill_name"
      exit 1
    fi
  else
    printf '%s: no checks\n' "$skill_name"
  fi
done < <(find . -mindepth 1 -maxdepth 1 -type d ! -name .git -print0)

if (( skill_count == 0 )); then
  echo 'No skill folders found' >&2
  exit 1
fi
