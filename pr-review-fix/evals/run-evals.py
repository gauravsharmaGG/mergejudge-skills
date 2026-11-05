#!/usr/bin/env python3
"""Fixture-backed, deterministic PR review skill evals; Python stdlib only."""
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
SKILL = EVALS.parent
REAL_GIT = shutil.which('git')


def run_git(repo, *args, **kwargs):
    return subprocess.run([REAL_GIT, '-C', str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def safe_path(path):
    p = PurePosixPath(path)
    return isinstance(path, str) and path == str(p) and not p.is_absolute() and '..' not in p.parts and bool(p.parts)


def fixture_items(name):
    fixture = EVALS / 'fixtures' / name / '.mj'
    data = json.loads((fixture / 'threads.json.fixture').read_text())
    items = []
    for t in data['data']['repository']['pullRequest']['reviewThreads']['nodes']:
        items.append(dict(t, kind='thread', key=f"{t['path']}:{t['line']}"))
    for file, kind, prefix in [('comments.json.fixture', 'conversation', 'comment:'),
                               ('reviews.json.fixture', 'review', 'review:')]:
        path = fixture / file
        for item in json.loads(path.read_text()) if path.exists() else []:
            body = item.get('body', '')
            if not body.strip() or item['user']['login'] == 'eval-user':
                continue
            if kind == 'review' and item.get('state') not in ('COMMENTED', 'CHANGES_REQUESTED'):
                continue
            if re.search(r'<!-- mergejudge:summary|(?i:walkthrough|pull request overview)', body):
                continue
            items.append(dict(item, kind=kind, key=prefix+str(item['id']), path=None))
    return items


def load_suite():
    scenarios = {}
    required = {'skills', 'query', 'files', 'expected_behavior', 'anti_behavior', 'source', 'expected'}
    for path in sorted((EVALS / 'scenarios').glob('*.json')):
        s = json.loads(path.read_text())
        if not required <= s.keys() or s['skills'] != ['pr-review-fix']:
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
        if not {'.mj/threads.json', '.mj/pr.json'} <= set(s['files']):
            raise ValueError(f'{path.name}: missing GitHub fixtures')
        data = json.loads((fixture / '.mj/threads.json.fixture').read_text())
        threads = data['data']['repository']['pullRequest']['reviewThreads']['nodes']
        keys = {t['key'] for t in fixture_items(path.stem)}
        if set(s['expected']) != keys or any(v not in ('fix', 'reply', 'none') for v in s['expected'].values()):
            raise ValueError(f'{path.name}: expected must cover every item with fix/reply/none')
        for t in threads:
            if t['path'] not in s['files']:
                raise ValueError(f'{path.name}: finding file has no fixture')
        for check in s.get('checks', []):
            if len(check) != 1 or next(iter(check)) not in ('file_unchanged', 'no_command', 'reply_contains', 'issue_comment_contains'):
                raise ValueError(f'{path.name}: unknown check {check}')
            if 'file_unchanged' in check and check['file_unchanged'] not in s['files']:
                raise ValueError(f'{path.name}: check references absent file')
        scenarios[path.stem] = s
    if any(p.is_file() for p in (EVALS / 'fixtures').iterdir()):
        raise ValueError('fixture files must live in a scenario directory')
    dirs = {p.name for p in (EVALS / 'fixtures').iterdir() if p.is_dir()}
    if dirs != scenarios.keys():
        raise ValueError(f'orphan/missing fixture directories: {dirs ^ scenarios.keys()}')
    return scenarios


def materialise(name, scenario, repo):
    repo.mkdir()
    for relative in scenario['files']:
        dest = repo / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EVALS / 'fixtures' / name / (relative + '.fixture'), dest)
    # Metadata stays untracked so API reply updates do not dirty the agent's tree.
    (repo / '.gitignore').write_text('.mj/\n')
    run_git(repo, 'init', '-b', 'eval-head')
    run_git(repo, 'config', 'user.name', 'Eval User')
    run_git(repo, 'config', 'user.email', 'eval@example.invalid')
    run_git(repo, 'config', 'commit.gpgsign', 'false')
    run_git(repo, 'add', '.')
    tree = run_git(repo, 'write-tree').strip().decode()
    # Plumbing builds the baseline without invoking git commit or hooks.
    sha = run_git(repo, 'commit-tree', tree, input=b'Eval fixture baseline\n').strip().decode()
    run_git(repo, 'update-ref', 'refs/heads/eval-head', sha)
    run_git(repo, 'branch', 'main', sha)
    remote = repo.parent / 'remote.git'
    run_git(repo, 'init', '--bare', str(remote))
    run_git(repo, 'remote', 'add', 'origin', str(remote))
    pr_path = repo / '.mj/pr.json'
    pr = json.loads(pr_path.read_text())
    old = pr['headRefOid']
    pr['headRefOid'] = sha
    pr_path.write_text(json.dumps(pr))
    summaries = repo / '.mj/comments.json'
    if summaries.exists():
        summaries.write_text(summaries.read_text().replace(old, sha))
    baseline = {p: (repo / p).read_bytes() for p in scenario['files'] if not p.startswith('.mj/')}
    return baseline


# git is real except pushes are confined to the disposable local bare remote.
GIT_WRAPPER = '''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['MJ_WORKSPACE'])
if 'push' in args:
    with (root/'.mj/gh-writes.jsonl').open('a') as out:
        out.write(json.dumps({'kind':'push','argv':args})+'\\n')
    subprocess.run([os.environ['MJ_REAL_GIT'], '-C', str(root), 'push', str(root.parent/'remote.git'), 'HEAD:refs/heads/eval-head'], check=True)
else:
    os.execv(os.environ['MJ_REAL_GIT'], [os.environ['MJ_REAL_GIT'], *args])
'''
BLOCK_COMMAND = '''#!/usr/bin/env python3
import json, os, pathlib, sys
with (pathlib.Path(os.environ['MJ_WORKSPACE'])/'.mj/commands.jsonl').open('a') as out:
    out.write(json.dumps({'command':pathlib.Path(sys.argv[0]).name,'argv':sys.argv[1:]})+'\\n')
sys.exit('Network command blocked in local eval')
'''


def environment(repo):
    bindir = repo.parent / 'bin'
    bindir.mkdir()
    shutil.copyfile(EVALS / 'fake-gh.py', bindir / 'gh')
    (bindir / 'git').write_text(GIT_WRAPPER)
    for command in ('curl', 'wget', 'ssh'):
        (bindir / command).write_text(BLOCK_COMMAND)
    for p in bindir.iterdir():
        p.chmod(0o755)
    env = dict(os.environ)
    for key in list(env):
        if key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN') or key.startswith('GIT_'):
            del env[key]
    env.update(PATH=str(bindir) + os.pathsep + env.get('PATH', ''), MJ_WORKSPACE=str(repo),
               MJ_REAL_GIT=REAL_GIT, GH_CONFIG_DIR=str(repo.parent / 'gh-config'),
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ALLOW_PROTOCOL='file', GIT_TERMINAL_PROMPT='0')
    return env


def reason(body):
    return '\n'.join(line for line in body.split('\n') if not re.match(r'^\s*>', line)).strip()


def events(repo, file='gh-writes.jsonl'):
    path = repo / '.mj' / file
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def grade(name, scenario, repo, baseline):
    errors = []
    nodes = fixture_items(name)
    writes = events(repo)
    def replies(t):
        if t['kind'] == 'thread':
            return [w for w in writes if w.get('operation') == 'reply' and w.get('threadId') == t['id']]
        return [w for w in writes if w.get('operation') == 'issue_comment' and
                (t['html_url'] in w.get('body', '') or re.search(r'(?<!\w)' + str(t['id']) + r'(?!\w)', w.get('body', '')))]
    def changed(path):
        current = repo / path
        if not current.exists() or current.read_bytes() != baseline[path]:
            return True
        # Also detect changes committed and then reverted in the working tree.
        for sha in run_git(repo, 'rev-list', '--reverse', 'HEAD').decode().splitlines()[1:]:
            snapshot = subprocess.run([REAL_GIT, '-C', str(repo), 'show', f'{sha}:{path}'], capture_output=True)
            if snapshot.returncode or snapshot.stdout != baseline[path]:
                return True
        return False
    for t in nodes:
        key = t['key']
        action = scenario['expected'][key]
        posted = replies(t)
        valid = any(len(reason(w.get('body', '')).encode('utf-16-le')) // 2 >= 8 for w in posted)
        if action == 'none' and posted:
            errors.append(f'{key}: already answered finding received a reply')
        if action in ('fix', 'reply') and not valid:
            errors.append(f'{key}: no reply with a concrete-length reason logged')
        if action == 'fix' and not any(changed(p) for p in ([t['path']] if t['path'] else scenario.get('item_files', {}).get(key, []))):
            errors.append(f'{key}: finding file did not change')
        if action == 'reply' and any(changed(p) for p in ([t['path']] if t['path'] else baseline)):
            errors.append(f'{key}: reply-only finding file changed')
    for check in scenario.get('checks', []):
        if 'file_unchanged' in check and changed(check['file_unchanged']):
            errors.append(f"check: {check['file_unchanged']} changed")
        if 'no_command' in check and any(e.get('command') == check['no_command'] for e in events(repo, 'commands.jsonl')):
            errors.append(f"check: forbidden command {check['no_command']} attempted")
        if 'reply_contains' in check:
            rule = check['reply_contains']
            t = next(t for t in nodes if t['key'] == rule['key'])
            if not any(rule['text'].lower() in w.get('body', '').lower() for w in replies(t)):
                errors.append(f"check: {rule['key']} reply missing {rule['text']}")
        if 'issue_comment_contains' in check:
            rule = check['issue_comment_contains']
            t = next(t for t in nodes if t['key'] == rule['key'])
            if not any(rule['text'].lower() in w.get('body', '').lower() for w in replies(t)):
                errors.append(f"check: {rule['key']} issue comment missing {rule['text']}")
    return errors


def negative_controls(name, scenario, repo, baseline):
    """A perfect run must fail after each deliberate observable regression."""
    log_path = repo / '.mj/gh-writes.jsonl'
    saved_log = log_path.read_text() if log_path.exists() else ''
    writes = events(repo)
    nodes = fixture_items(name)
    failures = []

    def rejected(label):
        if not grade(name, scenario, repo, baseline):
            failures.append('grader accepted negative control: ' + label)

    try:
        for t in nodes:
            key = t['key']
            action = scenario['expected'][key]
            if t['kind'] != 'thread':
                remaining = [w for w in writes if w.get('operation') != 'issue_comment']
                log_path.write_text(''.join(json.dumps(w) + '\n' for w in remaining))
                rejected('missing issue comment')
                for body, label in [('A sufficient answer without a target reference.', 'unlinked issue comment'),
                                    ('> ' + t['html_url'] + '\n1234567', 'quoted link with short reason')]:
                    log_path.write_text(''.join(json.dumps(w) + '\n' for w in remaining +
                                               [{'operation': 'issue_comment', 'body': body}]))
                    rejected(label)
                log_path.write_text(saved_log)
                continue
            if action == 'none':
                log_path.write_text(saved_log + json.dumps({'operation': 'reply', 'threadId': t['id'], 'body': 'Unnecessary extra answer.'}) + '\n')
                rejected('reply to answered thread')
            else:
                remaining = [w for w in writes if w.get('threadId') != t['id']]
                log_path.write_text(''.join(json.dumps(w) + '\n' for w in remaining))
                rejected('missing reply')
                remaining.append({'operation': 'reply', 'threadId': t['id'], 'body': '> quoted evidence only\n1234567'})
                log_path.write_text(''.join(json.dumps(w) + '\n' for w in remaining))
                rejected('short reason with a quote')
            log_path.write_text(saved_log)
            if action in ('fix', 'reply'):
                path = repo / t['path']
                content = path.read_bytes()
                path.write_bytes(baseline[t['path']] if action == 'fix' else content + b'\n// Unexpected edit\n')
                rejected('missing fix' if action == 'fix' else 'reply-only file changed')
                path.write_bytes(content)
        for check in scenario.get('checks', []):
            if 'no_command' in check:
                commands = repo / '.mj/commands.jsonl'
                saved = commands.read_bytes() if commands.exists() else None
                commands.write_text(json.dumps({'command': check['no_command']}) + '\n')
                rejected('forbidden command')
                if saved is None:
                    commands.unlink()
                else:
                    commands.write_bytes(saved)
    finally:
        log_path.write_text(saved_log)
    return failures


def command_argv(template, prompt):
    argv = shlex.split(template)
    if argv.count('{prompt}') != 1:
        raise ValueError('--runner-cmd must contain exactly one standalone {prompt} token')
    return [prompt if token == '{prompt}' else token for token in argv]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner-cmd', default='codex exec --skip-git-repo-check {prompt}')
    parser.add_argument('--list', action='store_true')
    parser.add_argument('--filter', default='*', help='scenario name, substring, or glob')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--self-test', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args()
    scenarios = load_suite()  # All drift checks run even with --filter or --list.
    selected = {n:s for n,s in scenarios.items() if fnmatch.fnmatch(n, args.filter) or args.filter in n}
    if not selected:
        parser.error('no scenarios matched')
    if args.list:
        print('\n'.join(selected))
        return 0
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    results = EVALS / 'results' / stamp
    results.mkdir(parents=True)
    skill = (SKILL / 'SKILL.md').read_text().split('---', 2)[2].strip()
    outcomes = {}
    for name, scenario in selected.items():
        dest = results / name
        dest.mkdir()
        with tempfile.TemporaryDirectory(prefix='mj-eval-') as scratch:
            repo = Path(scratch) / 'repo'
            baseline = materialise(name, scenario, repo)
            env = environment(repo)
            errors = []
            if not args.dry_run:
                if args.self_test:
                    env['MJ_SCENARIO'] = str(EVALS / 'scenarios' / (name + '.json'))
                    argv = [sys.executable, str(EVALS / 'perfect-agent.py')]
                else:
                    # Do not leak the answer patch or expected decisions into the agent prompt.
                    (repo / '.mj/golden.patch').unlink(missing_ok=True)
                    prompt = (skill + '\n\nSkill directory: ' + str(SKILL) +
                              '\nLocal eval: use only this disposable repo and fake gh; no network.\n\n' + scenario['query'])
                    argv = command_argv(args.runner_cmd, prompt)
                try:
                    completed = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, timeout=args.timeout)
                    (dest / 'stdout.txt').write_text(completed.stdout)
                    (dest / 'stderr.txt').write_text(completed.stderr)
                    if completed.returncode:
                        errors.append(f'agent exited {completed.returncode}')
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f'runner failed: {type(exc).__name__}')
                errors.extend(grade(name, scenario, repo, baseline))
                if args.self_test and not errors:
                    errors.extend(negative_controls(name, scenario, repo, baseline))
            shutil.copytree(repo, dest / 'workspace', ignore=shutil.ignore_patterns('.git'))
            (dest / 'diff.patch').write_bytes(run_git(repo, 'diff', 'main'))
            outcomes[name] = {'pass': not errors, 'errors': errors, 'mode': 'dry-run' if args.dry_run else 'self-test' if args.self_test else 'agent'}
            (dest / 'result.json').write_text(json.dumps(outcomes[name], indent=2) + '\n')
            print(f"{'PASS' if not errors else 'FAIL'} {name}" + (': ' + '; '.join(errors) if errors else ''))
    (results / 'summary.json').write_text(json.dumps(outcomes, indent=2) + '\n')
    passed = sum(o['pass'] for o in outcomes.values())
    print(f'{passed}/{len(outcomes)} passed; results: {results.relative_to(SKILL)}')
    return 0 if passed == len(outcomes) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError) as exc:
        sys.exit(f'Invalid eval suite: {exc}')
