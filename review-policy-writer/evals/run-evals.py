#!/usr/bin/env python3
"""Fixture-backed, deterministic review-policy-writer skill evals; stdlib only."""
import argparse
from datetime import datetime, timezone
import fnmatch
import importlib.util
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
SKILL_DIR = EVALS.parent
REAL_GIT = shutil.which('git')

spec = importlib.util.spec_from_file_location('mj_check_policy', SKILL_DIR / 'scripts' / 'check-policy.py')
CP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CP)

CHECK_PARAMS = {
    'file_exists': ('path',),
    'file_absent': ('path',),
    'max_bytes': ('path', 'bytes'),
    'headings': ('path', 'required'),
    'contains': ('path', 'any_of'),
    'not_contains': ('path', 'text'),
    'preserved_lines': ('path', 'lines'),
    'policy_budget': ('bytes',),
    'no_gh_writes': (),
    'no_secret_patterns': ('path',),
    'file_unchanged': ('path',),
}


def run_git(repo, *args, **kwargs):
    return subprocess.run([REAL_GIT, '-C', str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout


def safe_path(path):
    p = PurePosixPath(path)
    return isinstance(path, str) and path == str(p) and not p.is_absolute() and '..' not in p.parts and bool(p.parts)


def load_suite():
    scenarios = {}
    required = {'skills', 'query', 'files', 'expected_behavior', 'anti_behavior', 'source', 'expected'}
    for path in sorted((EVALS / 'scenarios').glob('*.json')):
        s = json.loads(path.read_text())
        if not required <= s.keys() or s['skills'] != ['review-policy-writer']:
            raise ValueError(f'{path.name}: missing fields or wrong skill')
        if not all(isinstance(s[k], list) for k in ('files', 'expected_behavior', 'anti_behavior')) or not s['files']:
            raise ValueError(f'{path.name}: invalid or empty file list')
        if len(set(s['files'])) != len(s['files']) or not all(safe_path(p) for p in s['files']):
            raise ValueError(f'{path.name}: duplicate or unsafe paths')
        if s['expected'].keys() != {'target'} or not safe_path(s['expected']['target']):
            raise ValueError(f'{path.name}: expected must be {{"target": PATH}}')
        target = s['expected']['target']
        if f'.mj/golden/{target}' not in s['files']:
            raise ValueError(f'{path.name}: missing .mj/golden/{target} fixture')
        fixture = EVALS / 'fixtures' / path.stem
        disk = {p.relative_to(fixture).as_posix() for p in fixture.rglob('*') if p.is_file()}
        wanted = {p + '.fixture' for p in s['files']}
        if disk != wanted:
            raise ValueError(f'{path.name}: fixture drift: missing={wanted-disk}, extra={disk-wanted}')
        for check in s.get('checks', []):
            if len(check) != 1 or next(iter(check)) not in CHECK_PARAMS:
                raise ValueError(f'{path.name}: unknown check {check}')
            kind, rule = next(iter(check.items()))
            params = CHECK_PARAMS[kind]
            # String shorthand: {"file_exists": "REVIEW.md"} == {"file_exists": {"path": "REVIEW.md"}}.
            if isinstance(rule, str) and params == ('path',):
                check[kind] = rule = {'path': rule}
            if params and (not isinstance(rule, dict) or any(k not in rule for k in params)):
                raise ValueError(f'{path.name}: check {kind} missing params {params}')
            for key in ('path',):
                if key in params and not safe_path(rule[key]):
                    raise ValueError(f'{path.name}: unsafe path in {kind} check')
            for key in ('required', 'any_of', 'text', 'lines'):
                if key in params and (not isinstance(rule[key], list) or not rule[key]
                                      or not all(isinstance(v, str) and v for v in rule[key])):
                    raise ValueError(f'{path.name}: {kind} needs a non-empty list of strings')
            if 'bytes' in params and not isinstance(rule['bytes'], int):
                raise ValueError(f'{path.name}: {kind} needs integer bytes')
            if 'path' in params and rule['path'] not in s['files'] and not (
                    rule['path'] == target or kind == 'file_absent'):
                raise ValueError(f'{path.name}: check references file with no fixture')
            if kind == 'file_unchanged' and rule['path'] not in s['files']:
                raise ValueError(f'{path.name}: file_unchanged needs a fixture file')
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
    # Metadata stays untracked so fixture data never dirties the agent's tree.
    (repo / '.gitignore').write_text('.mj/\n.gh/\n')
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
    return {p: (repo / p).read_bytes() for p in scenario['files']
            if not p.startswith(('.mj/', '.gh/'))}


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
    for command in ('curl', 'wget', 'ssh'):
        (bindir / command).write_text(BLOCK_COMMAND)
    for p in bindir.iterdir():
        p.chmod(0o755)
    env = dict(os.environ)
    for key in list(env):
        if key in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN') or key.startswith('GIT_'):
            del env[key]
    env.update(PATH=str(bindir) + os.pathsep + env.get('PATH', ''), MJ_WORKSPACE=str(repo),
               GH_CONFIG_DIR=str(repo.parent / 'gh-config'), GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    return env


def events(repo, filename='gh-writes.jsonl'):
    path = repo / '.mj' / filename
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def changed(repo, path, baseline):
    current = repo / path
    if not current.exists() or current.read_bytes() != baseline[path]:
        return True
    # Also detect changes committed and then reverted in the working tree.
    for sha in run_git(repo, 'rev-list', '--reverse', 'HEAD').decode().splitlines()[1:]:
        snapshot = subprocess.run([REAL_GIT, '-C', str(repo), 'show', f'{sha}:{path}'], capture_output=True)
        if snapshot.returncode or snapshot.stdout != baseline[path]:
            return True
    return False


def grade(scenario, repo, baseline):
    errors = []
    reported_missing = set()

    def text_of(path):
        current = repo / path
        if not current.is_file():
            if path not in reported_missing:
                errors.append(f'{path}: file missing')
                reported_missing.add(path)
            return None
        return current.read_text(encoding='utf-8', errors='replace')

    for check in scenario['checks']:
        kind, rule = next(iter(check.items()))
        if kind == 'file_exists':
            if not (repo / rule['path']).is_file():
                errors.append(f"check: {rule['path']} does not exist")
        elif kind == 'file_absent':
            if (repo / rule['path']).exists():
                errors.append(f"check: {rule['path']} exists but must not")
        elif kind == 'max_bytes':
            size = (repo / rule['path']).stat().st_size if (repo / rule['path']).is_file() else -1
            if size < 0:
                errors.append(f"check: {rule['path']} file missing")
            elif size > rule['bytes']:
                errors.append(f"check: {rule['path']} is {size} bytes (cap {rule['bytes']})")
        elif kind == 'headings':
            text = text_of(rule['path'])
            if text is not None:
                found = {m.group(1).strip().lower()
                         for m in re.finditer(r'^#{1,6}[ \t]+(.*?)\s*$', text, re.M)}
                for heading in rule['required']:
                    if heading.lower() not in found:
                        errors.append(f'check: {rule["path"]} missing heading "{heading}"')
        elif kind == 'contains':
            text = text_of(rule['path'])
            if text is not None and not any(s.lower() in text.lower() for s in rule['any_of']):
                errors.append(f'check: {rule["path"]} mentions none of {rule["any_of"]}')
        elif kind == 'not_contains':
            text = text_of(rule['path'])
            if text is not None:
                present = [s for s in rule['text'] if s.lower() in text.lower()]
                if present:
                    errors.append(f'check: {rule["path"]} contains forbidden {present}')
        elif kind == 'preserved_lines':
            text = text_of(rule['path'])
            if text is not None:
                lines = text.split('\n')
                for line in rule['lines']:
                    if line not in lines:
                        errors.append(f'check: {rule["path"]} lost the line "{line}"')
        elif kind == 'policy_budget':
            listed, _caps = CP.policy_paths(repo)
            total = sum((repo / p).stat().st_size for p in listed if (repo / p).is_file())
            if total > rule['bytes']:
                errors.append(f'check: policy files total {total} bytes over the {rule["bytes"]}-byte budget')
        elif kind == 'no_gh_writes':
            writes = events(repo)
            if writes:
                errors.append(f'check: {len(writes)} gh write(s) logged: {writes[0].get("argv")}')
        elif kind == 'no_secret_patterns':
            text = text_of(rule['path'])
            if text is not None:
                hits = [n for n, pattern in CP.SECRET_PATTERNS if pattern.search(text)]
                if hits:
                    errors.append(f'check: secret patterns in {rule["path"]}: {hits}')
        elif kind == 'file_unchanged':
            if changed(repo, rule['path'], baseline):
                errors.append(f"check: {rule['path']} changed")
    return errors


def negative_controls(scenario, repo, baseline):
    """A perfect run must fail after each deliberate observable regression."""
    target = scenario['expected']['target']
    path = repo / target
    saved = path.read_bytes() if path.exists() else None
    writes_log = repo / '.mj/gh-writes.jsonl'
    saved_writes = writes_log.read_text() if writes_log.exists() else ''
    by_kind = {}
    for c in scenario['checks']:
        kind = next(iter(c))
        by_kind.setdefault(kind, []).append(c[kind])
    not_contains = next((r for r in by_kind.get('not_contains', []) if r['path'] == target), None)
    headings = next((r for r in by_kind.get('headings', []) if r['path'] == target), None)
    max_bytes = next((r for r in by_kind.get('max_bytes', []) if r['path'] == target), None)
    budget = next((r for r in by_kind.get('policy_budget', [])), None)
    preserved = next((r for r in by_kind.get('preserved_lines', []) if r['path'] == target), None)
    failures = []

    def rejected(label):
        if not grade(scenario, repo, baseline):
            failures.append('grader accepted negative control: ' + label)

    def restore():
        if saved is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(saved)

    try:
        path.unlink(missing_ok=True)
        rejected('deleted target')
        restore()
        if not_contains:
            path.write_bytes((saved or b'') + f"\n{not_contains['text'][0]}\n".encode())
            rejected(f"forbidden string appended ({not_contains['text'][0]})")
            restore()
        if headings:
            heading = headings['required'][0].lower()
            text = (saved or b'').decode('utf-8', errors='replace')
            path.write_text('\n'.join(line for line in text.split('\n')
                                      if line.strip().lstrip('#').strip().lower() != heading))
            rejected(f'required heading removed ({headings["required"][0]})')
            restore()
        if preserved:
            line = preserved['lines'][0]
            text = (saved or b'').decode('utf-8', errors='replace')
            path.write_text('\n'.join(l for l in text.split('\n') if l != line))
            rejected(f'preserved line removed ({line})')
            restore()
        if budget:
            listed, _caps = CP.policy_paths(repo)
            total = sum((repo / p).stat().st_size for p in listed if (repo / p).is_file())
            padding = budget['bytes'] + 1 - total
            if padding > 0 and (not max_bytes or len(saved or b'') + padding <= max_bytes['bytes']):
                path.write_bytes((saved or b'') + b'\n' + b'x' * padding)
                rejected(f'shared policy budget exceeded ({budget["bytes"]} bytes)')
                restore()
        if max_bytes:
            path.write_bytes((saved or b'') + b'\n' + b'x' * (max_bytes['bytes'] + 1) + b'\n')
            rejected(f'file inflated past its {max_bytes["bytes"]}-byte cap')
            restore()
        repo.joinpath('.mj').mkdir(exist_ok=True)
        writes_log.write_text(saved_writes + json.dumps(
            {'kind': 'write', 'argv': ['pr', 'create', '--title', 'sneaky']}) + '\n')
        rejected('logged gh write')
        if saved_writes:
            writes_log.write_text(saved_writes)
        else:
            writes_log.unlink()
    finally:
        restore()
        if saved_writes:
            writes_log.write_text(saved_writes)
    return failures


def command_argv(template, prompt):
    argv = shlex.split(template)
    if argv.count('{prompt}') != 1:
        raise ValueError('--runner-cmd must contain exactly one standalone {prompt} token')
    return [prompt if token == '{prompt}' else token for token in argv]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    skill = (SKILL_DIR / 'SKILL.md').read_text().split('---', 2)[2].strip()
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
                    # Do not leak the golden file into the agent's workspace.
                    shutil.rmtree(repo / '.mj/golden')
                    prompt = (skill + '\n\nSkill directory: ' + str(SKILL_DIR) +
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
                errors.extend(grade(scenario, repo, baseline))
                if args.self_test and not errors:
                    errors.extend(negative_controls(scenario, repo, baseline))
            shutil.copytree(repo, dest / 'workspace', ignore=shutil.ignore_patterns('.git'))
            # Stage everything (metadata is gitignored) so new files appear in the diff.
            run_git(repo, 'add', '-A')
            (dest / 'diff.patch').write_bytes(run_git(repo, 'diff', 'main'))
            outcomes[name] = {'pass': not errors, 'errors': errors,
                              'mode': 'dry-run' if args.dry_run else 'self-test' if args.self_test else 'agent'}
            (dest / 'result.json').write_text(json.dumps(outcomes[name], indent=2) + '\n')
            print(f"{'PASS' if not errors else 'FAIL'} {name}" + (': ' + '; '.join(errors) if errors else ''))
    (results / 'summary.json').write_text(json.dumps(outcomes, indent=2) + '\n')
    passed = sum(o['pass'] for o in outcomes.values())
    print(f'{passed}/{len(outcomes)} passed; results: {results.relative_to(SKILL_DIR)}')
    return 0 if passed == len(outcomes) else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, KeyError, TypeError) as exc:
        sys.exit(f'Invalid eval suite: {exc}')
