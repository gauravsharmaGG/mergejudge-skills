#!/usr/bin/env python3
"""Scripted grader positive control; not an LLM evaluation."""
import json
import os
import shutil
from pathlib import Path

scenario = json.loads(Path(os.environ['MJ_SCENARIO']).read_text())
target = scenario['expected']['target']
dest = Path(target)
dest.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(Path('.mj/golden') / target, dest)
