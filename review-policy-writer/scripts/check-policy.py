#!/usr/bin/env python3
"""Validate a review-policy file against MergeJudge's policy rules. Stdlib only.

Usage: check-policy.py PATH [--repo DIR]

Exits non-zero and explains when the file is missing required headings, over
the per-file byte cap, pushing the shared policy budget over its cap, carrying
a secret pattern, using lowering language (policy may only make review
stricter), or when REVIEW.md and REVIEW_POLICY.md both exist. Prints a JSON
summary {ok, errors, warnings, bytes, total_policy_bytes}.
"""
import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_POLICY_FILES = ['REVIEW_POLICY.md', 'REVIEW.md', 'APPROVAL_POLICY.md', 'AGENTS.md', 'CLAUDE.md']
POLICY_MAX_EACH = 8192
POLICY_MAX_TOTAL = 8192
REQUIRED_HEADINGS = [
    'Review policy',
    'What matters in this repo',
    'Raise to review even if small',
    'Block until a human looks',
    'Ignore',
    'Security expectations',
    'Evidence and nits',
]
# The same patterns MergeJudge blocks in added diff lines (rules.ts SECRET_PATTERNS).
SECRET_PATTERNS = [
    ('AWS access key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('private key block', re.compile(r'-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----')),
    ('GitHub token', re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b')),
    ('hard-coded api key', re.compile(r'\bapi[_-]?key\s*[:=]\s*["\'][^"\'\s]{8,}["\']', re.I)),
]
LOWERING = re.compile(r'auto-?approve|always approve|skip (the )?review|lane:? *auto|ignore security|lgtm everything', re.I)


def parse_mergejudge_policy(text):
    """Subset parser for the policy keys this skill needs from .mergejudge.yml."""
    out = {'policy_files': None, 'policy_exclude': [], 'max_each_bytes': None, 'max_total_bytes': None}
    current, flow_pending = None, False

    def flow(value):
        value = value.strip()
        if value.startswith('[') and value.endswith(']'):
            inner = value[1:-1].strip()
            return [v.strip().strip('\'"') for v in inner.split(',')] if inner else []
        return None

    for raw in text.splitlines():
        line = re.sub(r'\s+#.*$', '', raw.rstrip())
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(' '))
        body = line.strip()
        if current in ('policy_files', 'policy_exclude') and body.startswith('- ') and flow_pending:
            out[current].append(body[2:].strip().strip('\'"'))
            continue
        if current == 'policy' and indent > 0 and ':' in body and not body.startswith('- '):
            key, _, value = body.partition(':')
            if key.strip() in ('max_each_bytes', 'max_total_bytes') and value.strip().isdigit():
                out[key.strip()] = int(value)
            continue
        current = None
        key, _, value = body.partition(':')
        key = key.strip()
        if key in ('policy_files', 'policy_exclude'):
            parsed = flow(value)
            out[key] = parsed if parsed is not None else []
            current, flow_pending = key, parsed is None
        elif key == 'policy':
            current, flow_pending = 'policy', False
    return out


def policy_paths(repo):
    """(policy file list MergeJudge would read from this repo, its byte caps)."""
    caps = {'each': POLICY_MAX_EACH, 'total': POLICY_MAX_TOTAL}
    listed = DEFAULT_POLICY_FILES
    exclude = []
    config = next((p for p in ('.mergejudge.yml', '.mergejudge.yaml') if (repo / p).is_file()), None)
    if config:
        settings = parse_mergejudge_policy((repo / config).read_text(encoding='utf-8', errors='replace'))
        if settings['policy_files'] is not None:
            listed = settings['policy_files'][:10]
        exclude = settings['policy_exclude']
        for key, cap in (('max_each_bytes', 'each'), ('max_total_bytes', 'total')):
            if settings[key]:
                caps[cap] = settings[key]
    import fnmatch
    return [p for p in listed if not any(fnmatch.fnmatch(p, g) for g in exclude)], caps


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('path', help='policy file to check, e.g. REVIEW.md')
    parser.add_argument('--repo', default='.', help='repository root (default: current directory)')
    args = parser.parse_args()

    repo = Path(args.repo)
    path = Path(args.path)
    if not path.is_file() and (repo / args.path).is_file():
        path = repo / args.path

    errors, warnings = [], []
    data = path.read_bytes() if path.is_file() else b''
    text = data.decode('utf-8', errors='replace')
    if not path.is_file():
        errors.append(f'{args.path}: file does not exist')

    listed, caps = policy_paths(repo)
    headings = [m.group(1).strip() for m in re.finditer(r'^#{1,6}[ \t]+(.*?)\s*$', text, re.M)]
    lowered = [h.lower() for h in headings]
    for heading in REQUIRED_HEADINGS:
        if heading.lower() not in lowered:
            errors.append(f'missing required heading "{heading}"')

    if len(data) > caps['each']:
        errors.append(f'{path.name if str(path) else args.path} is {len(data)} bytes; '
                      f'MergeJudge reads at most {caps["each"]} per policy file')

    sizes = {p: (repo / p).stat().st_size for p in listed if (repo / p).is_file()}
    total = sum(sizes.values())
    if total > caps['total']:
        errors.append(f'policy files total {total} bytes over the {caps["total"]}-byte budget '
                      f'({", ".join(f"{p}={n}" for p, n in sorted(sizes.items()))}); MergeJudge truncates the '
                      f'listed files fairly, so content is lost — trim {path.name if str(path) else args.path} '
                      f'(or the other policy files) until the total fits')
    elif total > caps['total'] * 3 // 4:
        warnings.append(f'policy files total {total} of {caps["total"]} bytes; keep '
                        f'{path.name if str(path) else args.path} short')

    for name, pattern in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            errors.append(f'possible {name} in {path.name if str(path) else args.path}: "{m.group(0)}"')

    for m in LOWERING.finditer(text):
        errors.append(f'lowering language "{m.group(0)}" — a policy can only make review stricter, '
                      f'never approve or skip anything')

    if (repo / 'REVIEW.md').is_file() and (repo / 'REVIEW_POLICY.md').is_file():
        errors.append('both REVIEW.md and REVIEW_POLICY.md exist; keep exactly one')

    print(json.dumps({'ok': not errors, 'errors': errors, 'warnings': warnings,
                      'bytes': len(data), 'total_policy_bytes': total}, indent=2))
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
