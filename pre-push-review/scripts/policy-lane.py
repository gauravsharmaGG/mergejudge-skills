#!/usr/bin/env python3
"""Lane for the local not-yet-pushed change set, using the repo's review policy.

Reads the policy files from the BASE ref (upstream tip or merge-base, like MergeJudge),
never from the working tree. Policy sections can only raise a lane: auto < review < block.
Built-in floors mirror MergeJudge's rules: secrets in added lines block, dependency
manifests/lockfiles and code files need review. Python 3.12 stdlib only.
"""
import argparse
import json
import re
import subprocess
import sys

DEFAULT_POLICY_FILES = ['REVIEW_POLICY.md', 'REVIEW.md', 'APPROVAL_POLICY.md', 'AGENTS.md', 'CLAUDE.md']
MAX_EACH = 8192
MAX_POLICY_FILES = 10

# MergeJudge's four secret patterns (rules.ts SECRET_PATTERNS).
SECRET_PATTERNS = [
    ('AWS access key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('private key block', re.compile(r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----')),
    ('GitHub token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b')),
    ('hard-coded api key', re.compile(r"\bapi[_-]?key\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']", re.IGNORECASE)),
]

LOCKFILES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'bun.lockb', 'bun.lock', 'Cargo.lock',
    'Gemfile.lock', 'poetry.lock', 'Pipfile.lock', 'composer.lock', 'go.sum', 'uv.lock',
}
MANIFESTS = {
    'package.json', 'Cargo.toml', 'Gemfile', 'pyproject.toml', 'requirements.txt', 'Pipfile',
    'composer.json', 'go.mod', 'build.gradle', 'pom.xml',
}
CODE_EXTENSIONS = {
    'js', 'mjs', 'cjs', 'jsx', 'ts', 'mts', 'cts', 'tsx', 'vue', 'svelte', 'astro',
    'py', 'go', 'rs', 'java', 'kt', 'kts', 'scala', 'rb', 'php', 'sh', 'bash', 'zsh', 'ps1',
    'sql', 'c', 'h', 'cc', 'cpp', 'hpp', 'cs', 'swift', 'm', 'mm', 'dart', 'lua', 'pl',
    'ex', 'exs', 'erl', 'clj', 'hs', 'r', 'wasm',
}


def git(repo, *args):
    """Run git, returning stdout or None when the command fails (missing path/ref)."""
    done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True)
    return done.stdout.decode('utf-8', 'replace') if done.returncode == 0 else None


def basename(path):
    return path.rsplit('/', 1)[-1] if path else path


def is_dependency_file(path):
    base = basename(path)
    return base in LOCKFILES or base in MANIFESTS or re.fullmatch(r'requirements.*\.txt', base) is not None


def is_code_file(path):
    base = basename(path)
    i = base.rfind('.')
    return i > 0 and base[i + 1:].lower() in CODE_EXTENSIONS


def glob_regex(glob):
    """fnmatch-style glob with ** support: '**/' matches any directory depth (including none)."""
    out, i = '', 0
    while i < len(glob):
        if glob.startswith('**/', i):
            out += '(?:[^/]+/)*'
            i += 3
        elif glob.startswith('**', i):
            out += '.*'
            i += 2
        elif glob[i] == '*':
            out += '[^/]*'
            i += 1
        elif glob[i] == '?':
            out += '[^/]'
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r'\Z')


def parse_policy_config(text):
    """Read `policy_files` and `policy_exclude` from a tiny YAML subset (top-level keys,
    inline [a, b] lists or `- item` blocks). Anything else is ignored."""
    values = {'policy_files': None, 'policy_exclude': []}
    current = None
    for line in text.splitlines():
        top = re.match(r'^(policy_files|policy_exclude):\s*(.*)$', line)
        item = re.match(r'^\s+-\s*(.+?)\s*$', line)
        if top:
            current = top.group(1)
            rest = top.group(2)
            if rest.startswith('[') and rest.endswith(']'):
                entries = [e.strip().strip('\'"') for e in rest[1:-1].split(',')]
                values[current] = [e for e in entries if e]
        elif item and current:
            values[current].append(item.group(1).strip().strip('\'"'))
        elif line.strip() and not line[0].isspace():
            current = None
    return values['policy_files'], values['policy_exclude']


def headings(text):
    """(level, title, body) for every markdown heading; a section ends at the next heading."""
    out, current = [], None
    for line in text.splitlines():
        m = re.match(r'^(#{1,6})\s+(.*?)\s*#*\s*$', line)
        if m:
            if current:
                out.append(current)
            current = [len(m.group(1)), m.group(2).strip(), []]
        elif current:
            current[2].append(line)
    if current:
        out.append(current)
    return [(lvl, title, '\n'.join(body)) for lvl, title, body in out]


def policy_globs(text):
    """Backticked globs/paths from raise-to-review and block sections (heading contains
    'review'/'block', case-insensitive, level >= 2)."""
    review, block = [], []
    for lvl, title, body in headings(text):
        low = title.lower()
        target = block if 'block' in low else review if 'review' in low else None
        if target is None or lvl < 2:
            continue
        target.extend(g.strip() for g in re.findall(r'`([^`\n]+)`', body) if g.strip())
    return review, block


def load_policy(repo, base):
    """Policy files and their raise/block globs, from the BASE ref only."""
    files = DEFAULT_POLICY_FILES
    exclude = []
    for name in ('.mergejudge.yml', '.mergejudge.yaml'):
        text = git(repo, 'show', f'{base}:{name}')
        if text is not None:
            cfg_files, exclude = parse_policy_config(text)
            if cfg_files is not None:
                files = cfg_files[:MAX_POLICY_FILES]
            break
    used, review, block = [], [], []
    for path in files:
        if exclude and any(glob_regex(g).match(path) for g in exclude):
            used.append({'path': path, 'found': False, 'excluded': True})
            continue
        text = git(repo, 'show', f'{base}:{path}')
        found = bool(text and text.strip())
        used.append({'path': path, 'found': found})
        if not found:
            continue
        r, b = policy_globs(text[:MAX_EACH])
        review.extend((path, g) for g in r)
        block.extend((path, g) for g in b)
    return used, review, block


def changed_files(repo, base):
    """[(path, adds, dels)] for the combined diff base..index; adds/dels are None for binary."""
    numstat = git(repo, 'diff', '--numstat', '--no-renames', '--cached', str(base)) or ''

    def num(x):
        return int(x) if x.isdigit() else None

    out = []
    for line in numstat.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[2]:
            out.append((parts[2], num(parts[0]), num(parts[1])))
    return out


def added_lines(repo, base):
    """{path: [(new-file line number, text)]} for every added line in base..index."""
    diff = git(repo, 'diff', '--no-ext-diff', '--no-renames', '--cached', str(base)) or ''
    out, path, lineno = {}, None, 0
    for line in diff.split('\n'):
        if line.startswith('diff --git '):
            m = re.search(r' b/(.+)$', line)
            path = m.group(1) if m else None
        elif line.startswith('@@'):
            m = re.search(r'\+(\d+)', line)
            lineno = int(m.group(1)) if m else 0
        elif path and line.startswith('+') and not line.startswith('+++'):
            out.setdefault(path, []).append((lineno, line[1:]))
            lineno += 1
        elif path and line.startswith(' '):
            lineno += 1
    return out


def classify(repo, base):
    used, review_globs, block_globs = load_policy(repo, base)
    files = changed_files(repo, base)
    added = added_lines(repo, base)
    reasons, seen = [], set()

    def raise_(rule, path, lane, detail=None, line=None):
        reason = {'rule': rule, 'path': path, 'lane': lane}
        if line is not None:
            reason['line'] = line
        if detail:
            reason['detail'] = detail
        reasons.append(reason)

    for path, _adds, _dels in files:
        for source, glob in block_globs:
            if (path, 'policy-block') not in seen and glob_regex(glob).match(path):
                seen.add((path, 'policy-block'))
                raise_('policy-block', path, 'block', f'{glob} ({source})')
        for source, glob in review_globs:
            if (path, 'policy-review') not in seen and glob_regex(glob).match(path):
                seen.add((path, 'policy-review'))
                raise_('policy-review', path, 'review', f'{glob} ({source})')
        for name, pattern in SECRET_PATTERNS:
            if (path, name) in seen:
                continue
            hit = next((ln for ln, text in added.get(path, []) if pattern.search(text)), None)
            if hit is not None:
                seen.add((path, name))
                raise_('secret', path, 'block', name, hit)
        if is_dependency_file(path):
            raise_('dependency-file', path, 'review')
        elif is_code_file(path):
            raise_('code-file', path, 'review')
    order = {'auto': 0, 'review': 1, 'block': 2}
    lane = max((r['lane'] for r in reasons), key=order.get, default='auto')
    return {'lane': lane, 'base': str(base), 'policy_files': used, 'reasons': reasons}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', required=True, help='base ref (upstream tip or merge-base) for policy and diff')
    parser.add_argument('--repo', default='.', help='repository (default: current directory)')
    args = parser.parse_args()
    if git(args.repo, 'rev-parse', '--verify', f'{args.base}^{{commit}}') is None:
        sys.exit(f'policy-lane.py: cannot resolve base ref: {args.base}')
    print(json.dumps(classify(args.repo, args.base), indent=2))


if __name__ == '__main__':
    main()
