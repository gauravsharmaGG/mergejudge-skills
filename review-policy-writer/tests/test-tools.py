#!/usr/bin/env python3
"""Exercise repo-facts.sh and check-policy.py against small temporary repos."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FACTS = ROOT / 'scripts/repo-facts.sh'
CHECK = ROOT / 'scripts/check-policy.py'

GOOD = '''# Review policy

## What matters in this repo

- `npm test` must pass before merge.

## Raise to review even if small

- `src/auth/**` and `migrations/**`.

## Block until a human looks

- A removed or weakened permission check.

## Ignore

- Formatting the linter owns.

## Security expectations

- Credentials belong in the environment.

## Evidence and nits

- Findings name the file and line and quote the code.
'''


def facts(repo):
    result = subprocess.run(['bash', str(FACTS), str(repo)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def check(path, repo):
    result = subprocess.run([sys.executable, str(CHECK), str(path), '--repo', str(repo)],
                            capture_output=True, text=True)
    return result.returncode, json.loads(result.stdout)


def write(repo, relative, content):
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


with tempfile.TemporaryDirectory(prefix='rpw-facts-') as tmp:
    repo = Path(tmp)
    write(repo, 'src/auth/session.ts', 'export const x = 1;\n')
    write(repo, 'src/server.ts', 'export const y = 2;\n')
    write(repo, 'migrations/001.sql', 'CREATE TABLE t;\n')
    write(repo, '.github/workflows/ci.yml', 'name: CI\n')
    write(repo, 'package.json', json.dumps({'scripts': {'test': 'jest', 'lint': 'eslint .'}}))
    write(repo, '.prettierrc', '{}\n')
    write(repo, '.env', 'api_key = "abc123def456ghi789"\n')
    write(repo, 'node_modules/junk.js', 'module.exports = 1;\n')
    write(repo, 'dist/out.js', 'export {};\n')
    write(repo, '.git/config', '[core]\n')
    out = facts(repo)
    assert out['languages'].get('ts') == 2 and out['languages'].get('sql') == 1, out['languages']
    assert 'js' not in out['languages'], 'node_modules/dist must be skipped'
    assert 'npm test' in out['test_commands'] and 'npm' in out['package_managers']
    assert 'package.json' in out['manifests']
    assert 'prettier' in out['lint_tools'] and '.prettierrc' in out['lint_configs']
    assert '.github/workflows/ci.yml' in out['ci_files']
    for path in ('src/auth', 'migrations', '.github/workflows', '.env'):
        assert path in out['sensitive_paths'], (path, out['sensitive_paths'])
    raw = json.dumps(out)
    assert 'abc123def456ghi789' not in raw, '.env values must never be printed'
    assert '.env' in out['sensitive_paths'], 'paths of secret files are still listed'
    assert out['policy_files'] == [] and out['contributing'] is None
    print('PASS repo-facts: node repo facts, prunes, .env values never printed')

with tempfile.TemporaryDirectory(prefix='rpw-facts-') as tmp:
    repo = Path(tmp)
    write(repo, 'pyproject.toml', '[project]\nname = "x"\ndependencies = []\n\n'
            '[project.optional-dependencies]\ndev = ["pytest>=8"]\n\n[tool.ruff]\nline-length = 100\n')
    write(repo, 'src/mypkg/core.py', 'def f() -> int:\n    return 1\n')
    write(repo, 'tests/test_core.py', 'def test_f():\n    assert True\n')
    write(repo, 'CONTRIBUTING.md', '# Contributing\n\n- Public functions need type hints.\n')
    out = facts(repo)
    assert out['test_commands'] == ['pytest'], out['test_commands']
    assert out['test_dirs'] == ['tests'] and out['contributing'] == 'CONTRIBUTING.md'
    assert 'ruff' in out['lint_tools'] and 'pyproject.toml' in out['lint_configs']
    assert 'pip' in out['package_managers'] and 'npm' not in out['package_managers']
    print('PASS repo-facts: python repo, pytest/ruff, CONTRIBUTING.md')

with tempfile.TemporaryDirectory(prefix='rpw-facts-') as tmp:
    repo = Path(tmp)
    write(repo, '.mergejudge.yml', 'policy_files:\n  - REVIEW_POLICY.md\npolicy:\n  max_total_bytes: 4096\n')
    write(repo, 'REVIEW_POLICY.md', policy_text := '# Review policy\n')
    write(repo, 'AGENTS.md', 'x' * 100)
    out = facts(repo)
    assert out['mergejudge'] == {'config': '.mergejudge.yml', 'policy_files': ['REVIEW_POLICY.md'],
                                 'policy_exclude': [], 'max_each_bytes': 8192, 'max_total_bytes': 4096}
    assert out['policy_files'] == [{'path': 'REVIEW_POLICY.md', 'bytes': len(policy_text.encode())},
                                   {'path': 'AGENTS.md', 'bytes': 100}]
    print('PASS repo-facts: .mergejudge.yml policy settings and policy byte sizes')

with tempfile.TemporaryDirectory(prefix='rpw-check-') as tmp:
    repo = Path(tmp)
    write(repo, 'REVIEW.md', GOOD)
    code, out = check('REVIEW.md', repo)
    assert code == 0 and out['ok'] and out['errors'] == [], out
    assert out['bytes'] == len(GOOD.encode()) and out['total_policy_bytes'] == out['bytes']

    write(repo, 'REVIEW.md', GOOD.replace('## Ignore\n', ''))
    code, out = check('REVIEW.md', repo)
    assert code == 1 and any('Ignore' in e for e in out['errors']), out

    write(repo, 'REVIEW.md', GOOD + 'x' * 9000)
    code, out = check('REVIEW.md', repo)
    assert code == 1 and any('bytes' in e for e in out['errors']), out

    write(repo, 'REVIEW.md', GOOD + '\napi_key = "abcdefgh12345678"\n')
    code, out = check('REVIEW.md', repo)
    assert code == 1 and any('api key' in e for e in out['errors']), out

    write(repo, 'REVIEW.md', GOOD.replace('## Ignore', '## Ignore\n\n- auto-approve docs PRs.'))
    code, out = check('REVIEW.md', repo)
    assert code == 1 and any('lowering' in e for e in out['errors']), out

    write(repo, 'REVIEW.md', GOOD)
    write(repo, 'REVIEW_POLICY.md', '# Review policy\n')
    code, out = check('REVIEW.md', repo)
    assert code == 1 and any('both REVIEW.md and REVIEW_POLICY.md' in e for e in out['errors']), out
    print('PASS check-policy: headings, size, secrets, lowering, dual policy files')

with tempfile.TemporaryDirectory(prefix='rpw-check-') as tmp:
    repo = Path(tmp)
    write(repo, 'AGENTS.md', 'a' * 8000)
    write(repo, 'REVIEW.md', GOOD)
    code, out = check('REVIEW.md', repo)
    assert code == 1 and any('budget' in e for e in out['errors']), out
    assert out['total_policy_bytes'] == 8000 + len(GOOD.encode()), out

with tempfile.TemporaryDirectory(prefix='rpw-check-') as tmp:
    repo = Path(tmp)
    write(repo, '.mergejudge.yml', 'policy_files: [REVIEW_POLICY.md]\npolicy:\n  max_each_bytes: 2048\n')
    write(repo, 'REVIEW_POLICY.md', GOOD)
    code, out = check('REVIEW_POLICY.md', repo)
    assert code == 0 and out['total_policy_bytes'] == len(GOOD.encode()), out

    write(repo, 'REVIEW_POLICY.md', GOOD + 'y' * 3000)
    code, out = check('REVIEW_POLICY.md', repo)
    assert code == 1 and any('2048' in e for e in out['errors']), out
    print('PASS check-policy: shared budget, flow-style policy_files, custom caps')

# Every eval golden must satisfy the skill's own policy checker.
for scenario_path in sorted((ROOT / 'evals/scenarios').glob('*.json')):
    scenario = json.loads(scenario_path.read_text())
    with tempfile.TemporaryDirectory(prefix='rpw-golden-') as tmp:
        repo = Path(tmp)
        for relative in scenario['files']:
            if relative.startswith(('.mj/', '.gh/')):
                continue
            (repo / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / 'evals/fixtures' / scenario_path.stem / (relative + '.fixture'), repo / relative)
        shutil.copyfile(ROOT / 'evals/fixtures' / scenario_path.stem /
                        ('.mj/golden/' + scenario['expected']['target'] + '.fixture'),
                        repo / scenario['expected']['target'])
        code, out = check(scenario['expected']['target'], repo)
        assert code == 0 and out['ok'], (scenario_path.stem, out)
print('PASS check-policy: all eval golden policies pass their own checker')
