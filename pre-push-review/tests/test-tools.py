#!/usr/bin/env python3
"""Offline unit tests for the bundled tools: scripts/local-diff.sh and scripts/policy-lane.py."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL_DIFF = ROOT / 'scripts' / 'local-diff.sh'
POLICY_LANE = ROOT / 'scripts' / 'policy-lane.py'

REVIEW_MD = '''# Review policy

## What matters in this repo
- No token logging.

## Raise to review even if small
- `**/migrations/**` needs a DBA sign-off.

## Block until a human looks
- `deploy/**` touches production.

## Ignore
- Pure formatting churn.
'''


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, check=True, **kwargs).stdout


def git(repo, *args):
    return run('git', '-C', repo, *args)


def make_repo(parent, review=REVIEW_MD, upstream=True):
    """Base commit pushed to a local bare origin; returns the repo path."""
    repo = parent / 'repo'
    repo.mkdir()
    if review is not None:
        (repo / 'REVIEW.md').write_text(review)
    (repo / 'package.json').write_text('{"name": "demo"}\n')
    (repo / 'src').mkdir()
    (repo / 'src' / 'app.js').write_text('export const app = 1;\n')
    git(repo, 'init', '-q', '-b', 'feature')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'base')
    git(repo, 'init', '-q', '--bare', str(parent / 'origin.git'))
    git(repo, 'remote', 'add', 'origin', str(parent / 'origin.git'))
    if upstream:
        git(repo, 'push', '-q', '-u', 'origin', 'feature')
    else:  # only origin/main exists; no upstream configured
        git(repo, 'push', '-q', 'origin', 'feature:main')
        git(repo, 'branch', '-q', '-m', 'feature2')
    return repo


def entry(diff, path):
    return next(f for f in diff['files'] if f['path'] == path)


def test_local_diff_upstream(parent):
    repo = make_repo(parent)
    (repo / 'src' / 'app.js').write_text('export const app = 2;\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'bump app')
    (repo / 'src' / 'new.js').write_text('export const n = 1;\n')
    git(repo, 'add', 'src/new.js')  # staged only
    (repo / 'src' / 'app.js').write_text('export const app = 3;\n')  # unstaged on top
    (repo / 'notes.txt').write_text('scratch\n')  # untracked
    diff = json.loads(run('bash', LOCAL_DIFF, '--repo', repo))
    assert diff['branch'] == 'feature'
    assert diff['upstream'] == 'origin/feature' and diff['upstream_source'] == 'upstream'
    assert diff['base'] == 'origin/feature'
    assert [c['subject'] for c in diff['unpushed']] == ['bump app']
    assert entry(diff, 'src/app.js')['committed'] == {'adds': 1, 'dels': 1}
    assert entry(diff, 'src/app.js')['unstaged'] == {'adds': 1, 'dels': 1}
    assert entry(diff, 'src/app.js')['staged'] is None
    assert entry(diff, 'src/new.js')['staged'] == {'adds': 1, 'dels': 0}
    assert entry(diff, 'src/new.js')['committed'] is None
    assert entry(diff, 'notes.txt')['untracked'] is True
    assert entry(diff, 'notes.txt')['status'] == '?'
    assert entry(diff, 'src/app.js')['status'] == 'M'
    patch = run('bash', LOCAL_DIFF, '--repo', repo, '--patch')
    assert '+export const app = 2;' in patch and '+export const n = 1;' in patch
    assert 'app = 3' not in patch, 'unstaged edits must stay out of the combined patch'
    print('PASS local-diff: upstream, bucket split, patch')


def test_local_diff_no_upstream(parent):
    repo = make_repo(parent, upstream=False)
    (repo / 'src' / 'app.js').write_text('export const app = 2;\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'bump app')
    (repo / 'src' / 'fix.js').write_text('export const f = 1;\n')
    git(repo, 'add', 'src/fix.js')
    diff = json.loads(run('bash', LOCAL_DIFF, '--repo', repo))
    assert diff['upstream'] is None and diff['upstream_source'] == 'origin-main'
    assert diff['merge_base'] == diff['base'] and len(diff['base']) == 40
    assert [c['subject'] for c in diff['unpushed']] == ['bump app']
    assert {f['path'] for f in diff['files']} == {'src/app.js', 'src/fix.js'}
    lane = json.loads(run('python3', POLICY_LANE, '--repo', repo, '--base', diff['base']))
    assert lane['lane'] == 'review' and any(r['path'] == 'src/fix.js' for r in lane['reasons'])
    print('PASS local-diff: no-upstream origin/main fallback feeds policy-lane')


def test_policy_lane_floors(parent):
    repo = make_repo(parent)
    (repo / 'migrations').mkdir()
    (repo / 'migrations' / '0003.sql').write_text('ALTER TABLE t DROP COLUMN c;\n')
    (repo / 'README.md').write_text('docs change\n')
    (repo / 'package.json').write_text('{"name": "demo", "version": "2.0.0"}\n')
    git(repo, 'add', '-A')
    out = json.loads(run('python3', POLICY_LANE, '--repo', repo, '--base', 'origin/feature'))
    by_rule = {(r['rule'], r['path']): r for r in out['reasons']}
    assert by_rule[('policy-review', 'migrations/0003.sql')]['detail'].startswith('**/migrations/**')
    assert ('code-file', 'migrations/0003.sql') in by_rule
    assert ('dependency-file', 'package.json') in by_rule
    assert not [r for r in out['reasons'] if r['path'] == 'README.md'], 'docs-only must stay auto'
    assert out['lane'] == 'review'
    print('PASS policy-lane: globs, dependency, code, docs-only')


def test_policy_lane_secret_and_base_wins(parent):
    repo = make_repo(parent)
    (repo / 'deploy').mkdir()
    (repo / 'deploy' / 'prod.yml').write_text('replicas: 4\n')
    (repo / 'src' / 'config.js').write_text('export const config = {};\nconfig.api_key = "abc123def456ghi789";\n')
    git(repo, 'add', '-A')
    out = json.loads(run('python3', POLICY_LANE, '--repo', repo, '--base', 'origin/feature'))
    assert out['lane'] == 'block'
    secret = next(r for r in out['reasons'] if r['rule'] == 'secret')
    assert secret['path'] == 'src/config.js' and secret['line'] == 2
    assert next(r for r in out['reasons'] if r['path'] == 'deploy/prod.yml')['lane'] == 'block'
    # A working-tree or committed edit that weakens REVIEW.md must not lower the lane.
    (repo / 'REVIEW.md').write_text('# Review policy\n\n## Block until a human looks\n- nothing\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'weaken policy')
    out = json.loads(run('python3', POLICY_LANE, '--repo', repo, '--base', 'origin/feature'))
    assert out['lane'] == 'block' and any(r['path'] == 'deploy/prod.yml' and r['lane'] == 'block' for r in out['reasons'])
    print('PASS policy-lane: secret floor blocks, policy read from base only')


def test_policy_config(parent):
    repo = make_repo(parent, review=None)
    (repo / '.mergejudge.yml').write_text('policy_files: [CUSTOM.md, OTHER.md]\npolicy_exclude:\n  - "OTHER.md"\n')
    (repo / 'CUSTOM.md').write_text('## Block until a human looks\n- `**/ops/**`\n')
    (repo / 'OTHER.md').write_text('## Block until a human looks\n- `**/*`\n')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', 'add config')
    git(repo, 'push', '-q', 'origin', 'feature')  # policy config must be on the base tip
    (repo / 'ops').mkdir()
    (repo / 'ops' / 'run.sh').write_text('echo hi\n')
    (repo / 'README.md').write_text('docs\n')
    git(repo, 'add', '-A')
    out = json.loads(run('python3', POLICY_LANE, '--repo', repo, '--base', 'origin/feature'))
    assert any(r['path'] == 'ops/run.sh' and r['lane'] == 'block' for r in out['reasons']), 'CUSTOM.md globs apply'
    assert not any(r['path'] == 'README.md' and r['lane'] == 'block' for r in out['reasons']), 'OTHER.md is excluded'
    used = {p['path']: p for p in out['policy_files']}
    assert used['CUSTOM.md']['found']
    assert not used['OTHER.md']['found'] and used['OTHER.md'].get('excluded') is True
    print('PASS policy-lane: .mergejudge.yml policy_files/policy_exclude')


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for test in tests:
        with tempfile.TemporaryDirectory(prefix='ppr-tools-') as tmp:
            test(Path(tmp))
    print(f'{len(tests)} tool tests passed')


if __name__ == '__main__':
    main()
