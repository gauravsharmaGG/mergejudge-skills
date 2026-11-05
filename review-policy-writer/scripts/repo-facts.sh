#!/usr/bin/env bash
# Print review-relevant facts about a repository as JSON.
#
# Usage: repo-facts.sh [REPO_DIR]   (default: the current directory)
#
# Bash walks the tree (skipping .git, node_modules, vendor, dist, build and
# friends); python3 (stdlib only) classifies the findings and prints JSON.
# The output carries paths, sizes, counts, and command names only: contents of
# .env* files and secret-looking values are never read or printed.
set -euo pipefail

REPO="${1:-.}"
if [[ ! -d "$REPO" ]]; then
  echo "repo-facts.sh: not a directory: $REPO" >&2
  exit 1
fi

LIST="$(mktemp)"
trap 'rm -f "$LIST"' EXIT

# Paths with newlines would break the line-based list; repositories this skill
# targets do not use them, and LC_ALL=C sort keeps the output deterministic.
(
  cd "$REPO"
  find . \( -name .git -o -name node_modules -o -name vendor -o -name dist \
    -o -name build -o -name .next -o -name target -o -name out \
    -o -name __pycache__ -o -name .venv -o -name venv -o -name coverage \) -prune \
    -o -type f -print | sed 's|^\./||' | LC_ALL=C sort
) > "$LIST"

python3 - "$REPO" "$LIST" <<'PY'
import json
import re
import sys
import tomllib
from pathlib import Path

repo = Path(sys.argv[1]).resolve()
files = [line for line in Path(sys.argv[2]).read_text().splitlines() if line]
bases = [p.rsplit('/', 1)[-1] for p in files]


def base(p):
    return p.rsplit('/', 1)[-1]


def parent(p):
    return p.rsplit('/', 1)[0] if '/' in p else ''


def read(path):
    return (repo / path).read_text(encoding='utf-8', errors='replace')


# Languages: file counts per extension, most common first.
languages = {}
for p in files:
    b = base(p)
    if b.startswith('.'):
        continue
    i = b.rfind('.')
    if i > 0:
        e = b[i + 1:].lower()
        languages[e] = languages.get(e, 0) + 1
languages = dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0])))

# Package managers and manifests.
MANIFESTS = {'package.json', 'pyproject.toml', 'requirements.txt', 'go.mod',
             'Cargo.toml', 'Gemfile', 'composer.json', 'pom.xml', 'build.gradle', 'Pipfile'}
LOCKS = {'package-lock.json': 'npm', 'yarn.lock': 'yarn', 'pnpm-lock.yaml': 'pnpm',
         'bun.lockb': 'bun', 'bun.lock': 'bun', 'poetry.lock': 'poetry', 'uv.lock': 'uv',
         'Cargo.lock': 'cargo', 'Gemfile.lock': 'bundler', 'go.sum': 'go',
         'composer.lock': 'composer', 'Pipfile.lock': 'pipenv'}
manifests = sorted(p for p in files if base(p) in MANIFESTS)
managers = {LOCKS[base(p)] for p in files if base(p) in LOCKS}
if 'package.json' in bases and not managers & {'npm', 'yarn', 'pnpm', 'bun'}:
    managers.add('npm')
if ('pyproject.toml' in bases or any(b.startswith('requirements') and b.endswith('.txt') for b in bases)) \
        and not managers & {'poetry', 'uv', 'pipenv'}:
    managers.add('pip')
if 'go.mod' in bases:
    managers.add('go')
if 'Cargo.toml' in bases:
    managers.add('cargo')
package_managers = sorted(managers)

# Test directories and commands (real commands only; no file contents printed).
TEST_DIRS = {'test', 'tests', '__tests__', 'spec', 'e2e'}
test_dirs = sorted({parent(p) for p in files if parent(p) and base(parent(p)) in TEST_DIRS})
test_commands = []


def add_command(cmd):
    if cmd not in test_commands:
        test_commands.append(cmd)


if 'package.json' in files:
    try:
        scripts = json.loads(read('package.json')).get('scripts', {})
    except ValueError:
        scripts = {}
    pm = next((m for m in ('yarn', 'pnpm', 'bun') if m in package_managers), 'npm')
    if 'test' in scripts:
        add_command(f'{pm} test')
    if 'check' in scripts:
        add_command(f'{pm} run check')
if 'Makefile' in files:
    makefile = read('Makefile')
    for target in ('test', 'check'):
        if re.search(rf'^{target}:', makefile, re.M):
            add_command(f'make {target}')
python_project = 'pyproject.toml' in files or any(b.startswith('requirements') and b.endswith('.txt') for b in bases)
if python_project:
    deps = []
    if 'pyproject.toml' in files:
        try:
            data = tomllib.loads(read('pyproject.toml'))
            project = data.get('project', {})
            deps += project.get('dependencies', [])
            deps += [d for x in project.get('optional-dependencies', {}).values() for d in x]
            deps += data.get('tool', {}).get('poetry', {}).get('dev-dependencies', {}).keys()
        except (ValueError, KeyError):
            pass
    reqs = ''.join(read(p) for p in files if base(p).startswith('requirements') and base(p).endswith('.txt'))
    if 'pytest' in reqs or any('pytest' in d for d in deps) or test_dirs:
        add_command('pytest')
if 'go.mod' in files:
    add_command('go test ./...')
if 'Cargo.toml' in files:
    add_command('cargo test')

# Lint and format configs.
LINT_PREFIXES = [('.eslintrc', 'eslint'), ('eslint.config.', 'eslint'),
                 ('.prettierrc', 'prettier'), ('prettier.config.', 'prettier'),
                 ('ruff.toml', 'ruff'), ('.ruff.toml', 'ruff'),
                 ('biome.json', 'biome'), ('.stylelintrc', 'stylelint'),
                 ('.golangci', 'golangci-lint'), ('rustfmt.toml', 'rustfmt'),
                 ('.rustfmt.toml', 'rustfmt'), ('.flake8', 'flake8'),
                 ('.rubocop.yml', 'rubocop'), ('.editorconfig', 'editorconfig')]
lint_tools = set()
lint_configs = []
for p in files:
    for prefix, tool in LINT_PREFIXES:
        if base(p).startswith(prefix):
            lint_tools.add(tool)
            lint_configs.append(p)
if 'pyproject.toml' in files:
    text = read('pyproject.toml')
    for section, tool in (('[tool.ruff]', 'ruff'), ('[tool.black]', 'black')):
        if section in text:
            lint_tools.add(tool)
            if 'pyproject.toml' not in lint_configs:
                lint_configs.append('pyproject.toml')

# CI files.
CI_NAMES = {'.gitlab-ci.yml', 'Jenkinsfile', 'azure-pipelines.yml', '.travis.yml', 'cloudbuild.yaml'}
ci_files = sorted(p for p in files if p.startswith('.github/workflows/') or base(p) in CI_NAMES)

# Sensitive path candidates: the matched path prefix or file, never contents.
SENSITIVE_DIRS = {'auth', 'migrations', 'deploy', 'infra', 'terraform', 'k8s',
                  'kubernetes', 'payments', 'billing', 'secrets'}
SENSITIVE_BASES = {'wrangler.toml', 'Dockerfile', 'docker-compose.yml', '_headers'}
sensitive = set()
for p in files:
    parts = p.split('/')
    for i, part in enumerate(parts[:-1]):
        if part.lower() in SENSITIVE_DIRS:
            sensitive.add('/'.join(parts[:i + 1]))
    if parts[:2] == ['.github', 'workflows']:
        sensitive.add('.github/workflows')
    b = base(p).lower()
    if b in {x.lower() for x in SENSITIVE_BASES} or b.startswith('.env') or b.endswith('.env') \
            or b.endswith('.pem'):
        sensitive.add(p)
sensitive_paths = sorted(sensitive)

# Existing policy files and their byte sizes.
DEFAULT_POLICY = ['REVIEW_POLICY.md', 'REVIEW.md', 'APPROVAL_POLICY.md', 'AGENTS.md', 'CLAUDE.md']
policy_files = [{'path': p, 'bytes': (repo / p).stat().st_size}
                for p in DEFAULT_POLICY if (repo / p).is_file()]


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


mergejudge = {'config': None, 'policy_files': None, 'policy_exclude': [],
              'max_each_bytes': 8192, 'max_total_bytes': 8192}
config = next((p for p in files if base(p) in ('.mergejudge.yml', '.mergejudge.yaml')), None)
if config:
    settings = parse_mergejudge_policy(read(config))
    mergejudge = {'config': config, 'policy_files': settings['policy_files'],
                  'policy_exclude': settings['policy_exclude'],
                  'max_each_bytes': settings['max_each_bytes'] or 8192,
                  'max_total_bytes': settings['max_total_bytes'] or 8192}

contributing = next((p for p in ('CONTRIBUTING.md', 'contributing.md', 'docs/CONTRIBUTING.md',
                                 '.github/CONTRIBUTING.md') if p in files), None)

print(json.dumps({
    'root': str(repo),
    'languages': languages,
    'manifests': manifests,
    'package_managers': package_managers,
    'test_dirs': test_dirs,
    'test_commands': test_commands,
    'lint_configs': sorted(lint_configs),
    'lint_tools': sorted(lint_tools),
    'ci_files': ci_files,
    'sensitive_paths': sensitive_paths,
    'policy_files': policy_files,
    'contributing': contributing,
    'mergejudge': mergejudge,
}, indent=2))
PY
