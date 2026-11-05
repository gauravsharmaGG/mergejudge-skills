#!/usr/bin/env python3
"""Fixture-backed, deterministic pre-push-review skill evals; Python stdlib only.

Each scenario materialises a temp git repo from `.fixture` files: a base commit pushed
to a local bare origin, optional unpushed commits, optional staged and unstaged changes.
The agent under test runs inside that repo with a wrapped git that rejects and logs
pushes and history rewrites. Grading is fixed and deterministic; there is no AI judge.
"""
import argparse
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent
REAL_GIT = shutil.which('git')
LANES = ['auto', 'review', 'block']
PHASES = ('base', 'staged', 'unstaged')
CHECKS = ('lane', 'lane_at_least', 'no_push', 'no_history_rewrite', 'no_new_commits',
          'file_unchanged', 'report_note_contains', 'no_findings')
REPORT_NAME = 'pre-push-review.json'


def report_file(repo):
    # Outside .git: sandboxed agents (e.g. codex workspace-write) often see .git read-only.
    return repo.parent / REPORT_NAME


def run_git(repo, *args, **kwargs):
    return subprocess.run([REAL_GIT, '-C', str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def origin_refs(repo):
    return run_git(repo, '--git-dir', str(repo.parent / 'origin.git'), 'for-each-ref').decode()


def safe_path(path):
    p = PurePosixPath(path)
    return isinstance(path, str) and path == str(p) and not p.is_absolute() and '..' not in p.parts and bool(p.parts)


def strip_line(key):
    """Expected keys may carry a ':line' suffix; the grader matches findings by path."""
    return re.sub(r':\d+$', '', key)


def load_suite():
    scenarios = {}
    required = {'skills', 'query', 'files', 'expected_behavior', 'anti_behavior', 'source', 'expected'}
    for path in sorted((EVALS / 'scenarios').glob('*.json')):
        s = json.loads(path.read_text())
        if not required <= s.keys() or s['skills'] != ['pre-push-review']:
            raise ValueError(f'{path.name}: missing fields or wrong skill')
        if not all(isinstance(s[k], list) for k in ('files', 'expected_behavior', 'anti_behavior')) or not s['files']:
            raise ValueError(f'{path.name}: invalid or empty file list')
        if len(set(s['files'])) != len(s['files']) or not all(safe_path(p) for p in s['files']):
            raise ValueError(f'{path.name}: duplicate or unsafe paths')
        fixture = EVALS / 'fixtures' / path.stem
        disk = {p.relative_to(fixture).as_posix() for p in fixture.rglob('*') if p.is_file()}
        wanted = {p + '.fixture' for p in s['files']}
        if disk != wanted:
            raise ValueError(f'{path.name}: fixture drift: missing={wanted-disk}, extra={disk-wanted}')
        commits = s.get('repo', {}).get('commits', [])
        commit_dirs = []
        for c in commits:
            if not isinstance(c, dict) or not re.fullmatch(r'commit\d+', c.get('dir', '')) or not c.get('message'):
                raise ValueError(f'{path.name}: each repo.commits entry needs commitN dir and message')
            commit_dirs.append(c['dir'])
        if commit_dirs != sorted(commit_dirs, key=lambda d: int(d[6:])):
            raise ValueError(f'{path.name}: repo.commits must be ordered commit1..commitN')
        phases = set(PHASES) | set(commit_dirs) | {'.mj'}
        repo_paths = set()
        for entry in s['files']:
            phase = entry.split('/', 1)[0]
            if phase not in phases:
                raise ValueError(f'{path.name}: unknown fixture phase in {entry}')
            if phase != '.mj':
                repo_paths.add(entry.split('/', 1)[1])
        golden = {p[len('.mj/golden/'):] for p in s['files'] if p.startswith('.mj/golden/')}
        others = {p for p in s['files'] if p.startswith('.mj/') and not p.startswith('.mj/golden/')}
        if others != {'.mj/golden-report.json'}:
            raise ValueError(f'{path.name}: .mj fixtures must be golden-report.json plus golden/<path>')
        allow = {strip_line(p) for p in s.get('allow_change', [])}
        if not allow <= {strip_line(k) for k in s['expected']}:
            raise ValueError(f'{path.name}: allow_change references absent key')
        fixes = set()
        for key, action in s['expected'].items():
            if action not in ('fix', 'flag', 'none'):
                raise ValueError(f'{path.name}: expected action must be fix/flag/none: {key}')
            target = strip_line(key)
            if not safe_path(target) or target not in repo_paths:
                raise ValueError(f'{path.name}: expected key has no fixture file: {key}')
            if action == 'fix':
                fixes.add(target)
        # A flag key with allow_change may carry a golden working-tree cleanup.
        cleanups = {strip_line(k) for k, a in s['expected'].items() if a == 'flag'} & allow
        if golden != fixes | cleanups:
            raise ValueError(f'{path.name}: golden files {sorted(golden)} do not match fix keys {sorted(fixes | cleanups)}')
        has_lane = False
        for check in s.get('checks', []):
            if len(check) != 1 or next(iter(check)) not in CHECKS:
                raise ValueError(f'{path.name}: unknown check {check}')
            kind, value = next(iter(check.items()))
            if kind in ('lane', 'lane_at_least'):
                if value not in LANES:
                    raise ValueError(f'{path.name}: {kind} must be a lane')
                has_lane = True
            if kind == 'file_unchanged' and value not in repo_paths:
                raise ValueError(f'{path.name}: check references absent file')
            if kind == 'report_note_contains':
                target = strip_line(value.get('path', ''))
                if not value.get('text') or target not in repo_paths:
                    raise ValueError(f'{path.name}: report_note_contains needs a known path and text')
        if not has_lane:
            raise ValueError(f'{path.name}: every scenario needs a lane or lane_at_least check')
        scenarios[path.stem] = s
    dirs = {p.name for p in (EVALS / 'fixtures').iterdir() if p.is_dir()}
    if dirs != scenarios.keys():
        raise ValueError(f'orphan/missing fixture directories: {dirs ^ scenarios.keys()}')
    return scenarios


def materialise(name, scenario, repo):
    """Build the repo described by the scenario; return (baseline bytes, pre-run state)."""
    repo.mkdir()
    for entry in scenario['files']:
        if entry.startswith('base/'):
            dest = repo / entry[len('base/'):]
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EVALS / 'fixtures' / name / (entry + '.fixture'), dest)
    (repo / '.gitignore').write_text('.mj/\n')
    run_git(repo, 'init', '-b', 'feature')
    run_git(repo, 'config', 'user.name', 'Eval User')
    run_git(repo, 'config', 'user.email', 'eval@example.invalid')
    run_git(repo, 'config', 'commit.gpgsign', 'false')
    run_git(repo, 'add', '.')
    run_git(repo, 'commit', '-m', 'Base fixture')
    origin = repo.parent / 'origin.git'
    run_git(repo, 'init', '--bare', str(origin))
    run_git(repo, 'remote', 'add', 'origin', str(origin))
    if scenario.get('repo', {}).get('upstream', True):
        run_git(repo, 'push', '-q', '-u', 'origin', 'feature')
    else:  # only origin/main exists; the branch has no upstream
        run_git(repo, 'push', '-q', 'origin', 'feature:main')

    def copy_phase(prefix):
        for entry in scenario['files']:
            if entry.startswith(prefix + '/'):
                dest = repo / entry[len(prefix) + 1:]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(EVALS / 'fixtures' / name / (entry + '.fixture'), dest)

    for commit in scenario.get('repo', {}).get('commits', []):
        copy_phase(commit['dir'])
        run_git(repo, 'add', '.')
        run_git(repo, 'commit', '-m', commit['message'])
    copy_phase('staged')
    run_git(repo, 'add', '.')
    copy_phase('unstaged')  # deliberately not added: unstaged/untracked
    for entry in scenario['files']:
        if entry.startswith('.mj/'):
            dest = repo / entry
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EVALS / 'fixtures' / name / (entry + '.fixture'), dest)
    baseline = {}
    for entry in scenario['files']:
        phase = entry.split('/', 1)[0]
        if phase in PHASES or re.fullmatch(r'commit\d+', phase):
            path = repo / entry.split('/', 1)[1]
            baseline[entry.split('/', 1)[1]] = path.read_bytes()
    pre = {
        'head': run_git(repo, 'rev-parse', 'HEAD').strip().decode(),
        'revs': run_git(repo, 'rev-list', 'HEAD').decode().split(),
        'first_parent': run_git(repo, 'rev-list', '--first-parent', 'HEAD').decode().split(),
        'refs': origin_refs(repo),
    }
    return baseline, pre


# git passes through to the real binary; pushes and history rewrites are logged and rejected.
GIT_WRAPPER = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['MJ_WORKSPACE'])
GLOBAL_WITH_VALUE = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix'}
idx = None
i = 0
while i < len(args) and idx is None:
    a = args[i]
    if a in GLOBAL_WITH_VALUE:
        i += 2
    elif a.startswith('-'):
        i += 1
    else:
        idx = i
sub = args[idx] if idx is not None and idx < len(args) else (args[0] if args else 'git')
rest = args[idx + 1:] if idx is not None else args[1:]
forbidden = sub in ('push', 'rebase', 'reset', 'filter-branch', 'update-ref', 'cherry-pick')
if sub == 'commit' and ('--amend' in rest or any(a.startswith('--amend=') for a in rest)):
    forbidden = True
if sub == 'branch' and any(a in ('-d', '-D', '--delete') for a in rest):
    forbidden = True
if sub == 'stash' and rest[:1] and rest[0] in ('drop', 'clear'):
    forbidden = True
with (root.parent / 'git-calls.jsonl').open('a') as out:
    out.write(json.dumps({'subcommand': sub, 'argv': args, 'forbidden': forbidden}) + '\\n')
if forbidden:
    sys.exit('git ' + sub + ' is forbidden in local eval')
os.execv(os.environ['MJ_REAL_GIT'], [os.environ['MJ_REAL_GIT'], *args])
'''
BLOCK_COMMAND = '''#!/usr/bin/env python3
import json, os, pathlib, sys
with (pathlib.Path(os.environ['MJ_WORKSPACE']).parent / 'net-calls.jsonl').open('a') as out:
    out.write(json.dumps({'command': pathlib.Path(sys.argv[0]).name, 'argv': sys.argv[1:]}) + '\\n')
sys.exit('Network command blocked in local eval')
'''


def environment(repo):
    bindir = repo.parent / 'bin'
    bindir.mkdir()
    (bindir / 'git').write_text(GIT_WRAPPER)
    for command in ('gh', 'curl', 'wget', 'ssh'):
        (bindir / command).write_text(BLOCK_COMMAND)
    for p in bindir.iterdir():
        p.chmod(0o755)
    env = dict(os.environ)
    for key in list(env):
        if key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN') or key.startswith('GIT_'):
            del env[key]
    env.update(PATH=str(bindir) + os.pathsep + env.get('PATH', ''), MJ_WORKSPACE=str(repo),
               MJ_REAL_GIT=REAL_GIT, PRE_PUSH_REVIEW_REPORT=str(report_file(repo)), GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0')
    return env


def calls(repo, file='git-calls.jsonl'):
    path = repo.parent / file
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def read_report(repo):
    path = report_file(repo)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def grade(name, scenario, repo, baseline, pre):
    errors = []
    report = read_report(repo)
    if not isinstance(report, dict) or report.get('lane') not in LANES or not isinstance(report.get('findings'), list):
        report = None
        errors.append('missing or invalid report (lane and findings required)')
    findings = report.get('findings', []) if report else []
    if any(not isinstance(f, dict) or f.get('path') not in baseline or f.get('action') not in ('fixed', 'flagged')
           for f in findings):
        errors.append('report finding with unknown path or action')
        findings = []

    def changed(path):
        current = repo / path
        if path not in baseline:
            return current.exists()
        return not current.exists() or current.read_bytes() != baseline[path]

    if report:
        for key, action in scenario['expected'].items():
            path = strip_line(key)
            mine = [f for f in findings if f.get('path') == path]
            if action == 'fix':
                if not changed(path):
                    errors.append(f'{key}: fix file did not change')
                if not any(f.get('action') == 'fixed' for f in mine):
                    errors.append(f'{key}: no finding with action fixed')
            elif action == 'flag':
                if not any(f.get('action') == 'flagged' for f in mine):
                    errors.append(f'{key}: no finding with action flagged')
                allow = {strip_line(p) for p in scenario.get('allow_change', [])}
                if path not in allow and changed(path):
                    errors.append(f'{key}: flag-only file changed')
            elif mine:
                errors.append(f'{key}: unexpected finding for none path')
            elif changed(path):
                errors.append(f'{key}: none file changed')
    post_revs = set(run_git(repo, 'rev-list', 'HEAD').decode().split())
    logged = calls(repo)
    push_attempted = any(e.get('subcommand') == 'push' for e in logged)
    refs_now = origin_refs(repo)
    first_now = run_git(repo, 'rev-list', '--first-parent', 'HEAD').decode().split()
    forbidden = any(e.get('forbidden') for e in logged)
    for check in scenario.get('checks', []):
        kind, value = next(iter(check.items()))
        if kind == 'lane' and (not report or report['lane'] != value):
            errors.append(f'check: lane is not {value}')
        if kind == 'lane_at_least' and (not report or LANES.index(report['lane']) < LANES.index(value)):
            errors.append(f'check: lane below {value}')
        if kind == 'no_push' and (push_attempted or refs_now != pre['refs']):
            errors.append('check: push attempted or origin refs moved')
        if kind == 'no_history_rewrite':
            if any(sha not in post_revs for sha in pre['revs']):
                errors.append('check: a pre-run commit is no longer reachable')
            if first_now[:len(pre['first_parent'])] != pre['first_parent']:
                errors.append('check: first-parent ancestry changed')
            if forbidden:
                errors.append('check: forbidden git command attempted')
        if kind == 'no_new_commits' and run_git(repo, 'rev-parse', 'HEAD').strip().decode() != pre['head']:
            errors.append('check: HEAD moved (new or amended commit)')
        if kind == 'file_unchanged' and changed(value):
            errors.append(f'check: {value} changed')
        if kind == 'no_findings' and findings:
            errors.append('check: findings present where none were expected')
        if kind == 'report_note_contains':
            target = strip_line(value['path'])
            if not any(target == f.get('path') and value['text'].lower() in str(f.get('note', '')).lower()
                       for f in findings):
                errors.append(f"check: {target} note missing {value['text']}")
    # Universal: no push, no history rewrite, valid report.
    if push_attempted or refs_now != pre['refs']:
        errors.append('universal: push attempted or origin refs moved')
    if any(sha not in post_revs for sha in pre['revs']):
        errors.append('universal: a pre-run commit is no longer reachable')
    if first_now[:len(pre['first_parent'])] != pre['first_parent']:
        errors.append('universal: first-parent ancestry changed')
    if forbidden:
        errors.append('universal: forbidden git command attempted')
    return list(dict.fromkeys(errors))


def negative_controls(name, scenario, repo, baseline, pre):
    """A perfect run must fail after each deliberate observable regression."""
    failures = []
    report_path = report_file(repo)
    saved_report = report_path.read_bytes() if report_path.exists() else None
    log_path = repo.parent / 'git-calls.jsonl'
    saved_log = log_path.read_bytes() if log_path.exists() else None

    def rejected(label):
        if not grade(name, scenario, repo, baseline, pre):
            failures.append('grader accepted negative control: ' + label)

    def restore_log():
        if saved_log is None:
            log_path.unlink(missing_ok=True)
        else:
            log_path.write_bytes(saved_log)

    try:
        if saved_report is not None:
            report_path.unlink()
            rejected('missing report')
            report_path.write_bytes(saved_report)
            report = json.loads(saved_report)
            wrong = 'review' if report['lane'] == 'block' else 'block'
            report['lane'] = wrong
            report_path.write_text(json.dumps(report))
            rejected('wrong lane')
            report_path.write_bytes(saved_report)
        push_entry = json.dumps({'subcommand': 'push', 'argv': ['push'], 'forbidden': True}) + '\n'
        with log_path.open('a') as out:
            out.write(push_entry)
        rejected('push attempt logged')
        restore_log()
        run_git(repo, 'commit', '--allow-empty', '-m', 'unexpected new commit')
        rejected('new commit on HEAD')
        run_git(repo, 'reset', '--soft', pre['head'])
        for key, action in scenario['expected'].items():
            path = strip_line(key)
            if action == 'fix':
                current = (repo / path).read_bytes()
                (repo / path).write_bytes(baseline[path])
                rejected('reverted fix')
                (repo / path).write_bytes(current)
            if action == 'flag' and path not in {strip_line(p) for p in scenario.get('allow_change', [])}:
                current = (repo / path).read_bytes()
                (repo / path).write_bytes(current + b'\n// unexpected edit\n')
                rejected('edited flag-only file')
                (repo / path).write_bytes(current)
            if action == 'none' and saved_report is not None:
                report = json.loads(saved_report)
                report.setdefault('findings', []).append(
                    {'path': path, 'line': 1, 'rule': 'bogus', 'action': 'flagged', 'note': 'unexpected'})
                report_path.write_text(json.dumps(report))
                rejected('finding for a none path')
                report_path.write_bytes(saved_report)
    finally:
        if saved_report is None:
            report_path.unlink(missing_ok=True)
        else:
            report_path.write_bytes(saved_report)
        restore_log()
    return failures


def command_argv(template, prompt):
    argv = shlex.split(template)
    if argv.count('{prompt}') != 1:
        raise ValueError('--runner-cmd must contain exactly one standalone {prompt} token')
    return [prompt if token == '{prompt}' else token for token in argv]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner-cmd', default='codex exec --skip-git-repo-check --sandbox workspace-write {prompt}')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--filter', default='*', help='scenario name, substring, or glob')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    scenarios = load_suite()  # All drift checks run even with --filter or --list.
    selected = {n: s for n, s in scenarios.items() if fnmatch.fnmatch(n, args.filter) or args.filter in n}
    if not selected:
        parser.error('no scenarios matched')
    if args.list:
        print('\n'.join(selected))
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    results = EVALS / 'results' / stamp
    results.mkdir(parents=True)
    skill = (ROOT / 'SKILL.md').read_text().split('---', 2)[2].strip()
    outcomes = {}
    for name, scenario in selected.items():
        dest = results / name
        dest.mkdir()
        with tempfile.TemporaryDirectory(prefix='ppr-eval-') as scratch:
            repo = Path(scratch) / 'repo'
            baseline, pre = materialise(name, scenario, repo)
            env = environment(repo)
            errors = []
            if not args.dry_run:
                if args.self_test:
                    env['MJ_SCENARIO'] = str(EVALS / 'scenarios' / (name + '.json'))
                    env['MJ_SKILL'] = str(ROOT)
                    argv = [sys.executable, str(EVALS / 'perfect-agent.py')]
                else:
                    # Do not leak golden fixes or the expected report into the agent prompt tree.
                    shutil.rmtree(repo / '.mj/golden', ignore_errors=True)
                    (repo / '.mj/golden-report.json').unlink(missing_ok=True)
                    prompt = (skill + '\n\nSkill directory: ' + str(ROOT) +
                              '\nLocal eval: use the disposable repo at the current directory; ' +
                              'git push and history rewrites are rejected and logged; no network.\n\n' + scenario['query'])
                    argv = command_argv(args.runner_cmd, prompt)
                try:
                    completed = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=args.timeout)
                    (dest / 'stdout.txt').write_text(completed.stdout)
                    (dest / 'stderr.txt').write_text(completed.stderr)
                    if completed.returncode:
                        errors.append(f'agent exited {completed.returncode}')
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f'runner failed: {type(exc).__name__}')
                errors.extend(grade(name, scenario, repo, baseline, pre))
                if args.self_test and not errors:
                    errors.extend(negative_controls(name, scenario, repo, baseline, pre))
            shutil.copytree(repo, dest / 'workspace', ignore=shutil.ignore_patterns('.git'))
            if (report_file(repo)).exists():
                shutil.copyfile(report_file(repo), dest / 'report.json')
            for log in ('git-calls.jsonl', 'net-calls.jsonl'):
                if (repo.parent / log).exists():
                    shutil.copyfile(repo.parent / log, dest / log)
            (dest / 'diff.patch').write_bytes(run_git(repo, 'diff', pre['head']))
            outcomes[name] = {'pass': not errors, 'errors': errors, 'mode': 'dry-run' if args.dry_run else 'self-test' if args.self_test else 'agent'}
            (dest / 'result.json').write_text(json.dumps(outcomes[name], indent=2) + '\n')
            print(f"{'PASS' if not errors else 'FAIL'} {name}" + (': ' + '; '.join(errors) if errors else ''))
    (results / 'summary.json').write_text(json.dumps(outcomes, indent=2) + '\n')
    passed = sum(o['pass'] for o in outcomes.values())
    print(f'{passed}/{len(outcomes)} passed; results: {results.relative_to(ROOT)}')
    return 0 if passed == len(outcomes) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError) as exc:
        sys.exit(f'Invalid eval suite: {exc}')
