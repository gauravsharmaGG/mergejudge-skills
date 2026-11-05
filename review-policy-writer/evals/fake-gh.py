#!/usr/bin/env python3
"""Local GitHub fixture server. Never delegates to a real gh binary; writes fail."""
import base64
import json
import os
import re
import sys
from pathlib import Path

root = Path(os.environ.get('MJ_WORKSPACE', '.')).resolve()
meta = root / '.mj'
data = root / '.gh'
args = sys.argv[1:]


def read(name, default=None):
    path = data / name
    return json.loads(path.read_text()) if path.exists() else default


def emit(value):
    print(json.dumps(value))


def log(filename, value):
    meta.mkdir(exist_ok=True)
    with (meta / filename).open('a') as out:
        out.write(json.dumps(value) + '\n')


def fail(message):
    sys.exit('fake gh: ' + message)


def fields():
    result = {}
    for i, arg in enumerate(args[:-1]):
        if arg in ('-f', '-F', '--field', '--raw-field'):
            key, value = args[i + 1].split('=', 1)
            if value.startswith('@'):
                value = Path(value[1:]).read_text()
            result[key] = value
        if arg == '--input':
            raw = sys.stdin.read() if args[i + 1] == '-' else Path(args[i + 1]).read_text()
            result.update(json.loads(raw))
    return result


def output(value):
    # Support the simple projections used by the skill.
    for flag in ('--jq', '-q'):
        if flag in args:
            expression = args[args.index(flag) + 1]
            if re.fullmatch(r'\.[A-Za-z]+', expression):
                projected = value.get(expression[1:])
                print(projected if isinstance(projected, str) else json.dumps(projected))
                return
            fail('unsupported jq projection (pipe to jq instead)')
    emit(value)


def block_write():
    log('gh-writes.jsonl', {'kind': 'write', 'argv': args})
    fail('writes are not allowed in this local eval: ' + ' '.join(args))


log('gh-calls.jsonl', {'argv': args})
WRITE_METHODS = ('POST', 'PATCH', 'PUT', 'DELETE')

if args[:2] == ['auth', 'status']:
    print('Logged in to github.com account eval-user')
elif args[:1] == ['issue'] or args[:2] == ['pr', 'create'] or \
        (args[:1] == ['pr'] and len(args) > 1 and args[1] in ('edit', 'merge', 'ready', 'close', 'reopen', 'review')):
    block_write()
elif args[:2] == ['repo', 'view']:
    output(read('repo.json', {'nameWithOwner': 'eval/repo'}))
elif args[:2] == ['pr', 'view']:
    output(read('pr.json', {'number': 42, 'state': 'open', 'baseRefName': 'main', 'headRefName': 'eval-head'}))
elif args[:2] == ['pr', 'list']:
    output(read('closed-prs.json', []))
elif args and args[0] == 'api':
    endpoint = next((a for a in args[1:] if a == 'user' or a == 'graphql' or a.startswith('repos/')), '')
    base = endpoint.split('?')[0]
    f = fields()
    if endpoint == 'user':
        output(read('user.json', {'login': 'eval-user', 'type': 'User'}))
    elif endpoint == 'graphql':
        if 'mutation' in f.get('query', ''):
            block_write()
        fail('unsupported GraphQL read')
    elif '/pulls/comments' in base:
        output(read('pull-comments.json', []))
    elif re.search(r'/pulls/\d+/reviews$', base):
        output(read('reviews.json', []))
    elif base.endswith('/pulls'):
        output(read('closed-prs.json', []))
    elif '/contents/' in base:
        name = re.sub(r'^.*?/contents/', '', base).strip('/')
        target = root / name
        if not target.is_file():
            fail(f'no fixture file for contents/{name}')
        output({'name': name, 'content': base64.b64encode(target.read_bytes()).decode(), 'encoding': 'base64'})
    elif any(m in args for m in WRITE_METHODS) or f:
        block_write()
    else:
        fail('unsupported REST read')
else:
    fail('unsupported command')
