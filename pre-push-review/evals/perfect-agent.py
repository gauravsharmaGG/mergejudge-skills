#!/usr/bin/env python3
"""Scripted grader positive control; not an LLM evaluation.

Applies the golden working-tree files for every 'fix' key, then writes the golden
report (with @@BASE@@ replaced by the base ref from the bundled local-diff.sh) to
the location the skill reports to.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

scenario = json.loads(Path(os.environ['MJ_SCENARIO']).read_text())
skill = Path(os.environ['MJ_SKILL'])

for key, action in scenario['expected'].items():
    path = re.sub(r':\d+$', '', key)
    golden = Path('.mj/golden') / path
    if action in ('fix', 'flag') and golden.exists():  # flag: an allowed working-tree cleanup
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(golden, dest)

base = 'unknown'
try:
    diff = subprocess.run(['bash', str(skill / 'scripts' / 'local-diff.sh')],
                          capture_output=True, text=True, check=True).stdout
    base = json.loads(diff)['base']
except (OSError, ValueError, subprocess.SubprocessError):
    pass

gitdir = Path(subprocess.run(['git', 'rev-parse', '--git-dir'], capture_output=True, text=True,
                             check=True).stdout.strip())
report = Path('.mj/golden-report.json').read_text().replace('@@BASE@@', base)
target = os.environ.get('PRE_PUSH_REVIEW_REPORT') or str(gitdir / 'pre-push-review.json')
Path(target).write_text(report)
print(report)
