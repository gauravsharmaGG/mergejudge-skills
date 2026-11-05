#!/usr/bin/env python3
"""Local GitHub fixture server. Never delegates to a real gh binary."""
import json
import os
import re
import sys
from pathlib import Path

root = Path(os.environ.get('MJ_WORKSPACE', '.')).resolve()
meta = root / '.mj'
args = sys.argv[1:]


def read(name, default=None):
    path = meta / name
    return json.loads(path.read_text()) if path.exists() else default


def emit(value):
    print(json.dumps(value))


def log(value):
    with (meta / 'gh-writes.jsonl').open('a') as out:
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
    # Support the simple projections used by the skill and bundled script.
    for flag in ('--jq', '-q'):
        if flag in args:
            expression = args[args.index(flag) + 1]
            if re.fullmatch(r'\.[A-Za-z]+', expression):
                projected = value.get(expression[1:])
                print(projected if isinstance(projected, str) else json.dumps(projected))
                return
            fail('unsupported jq projection (pipe to jq instead)')
    emit(value)


def reply(thread_id, comment_id, body, kind, extra):
    data = read('threads.json')
    nodes = data['data']['repository']['pullRequest']['reviewThreads']['nodes']
    target = next((t for t in nodes if t['id'] == thread_id or
                   str(t['comments']['nodes'][0]['databaseId']) == str(comment_id)), None)
    if not target:
        fail('unknown reply target')
    event = dict(kind=kind, threadId=target['id'], rootCommentId=target['comments']['nodes'][0]['databaseId'], body=body, **extra)
    log(event)
    comment = {'id': 'REPLY_' + target['id'], 'databaseId': 9999, 'body': body,
               'createdAt': '2026-11-04T12:00:00Z',
               'author': {'login': read('user.json', {'login': 'eval-user'})['login'], '__typename': read('user.json', {'type': 'User'})['type']}}
    target['comments']['nodes'].append(comment)
    (meta / 'threads.json').write_text(json.dumps(data))
    emit({'data': {'addPullRequestReviewThreadReply': {'comment': comment}}, **comment})


if args[:2] == ['pr', 'view']:
    output(read('pr.json'))
elif args[:2] == ['pr', 'checkout']:
    print('Already on eval-head')
elif args[:2] == ['repo', 'view']:
    output({'nameWithOwner': 'eval/repo'})
elif args[:2] == ['pr', 'diff']:
    print('\n'.join(read('changed-files.json', ['src/first.js', 'test/first.test.js'])))
elif args and args[0] == 'api':
    endpoint = next((a for a in args[1:] if a == 'graphql' or a == 'user' or a.startswith('repos/')), '')
    f = fields()
    if endpoint == 'user':
        output(read('user.json', {'login': 'eval-user', 'type': 'User'}))
    elif endpoint == 'graphql':
        query = f.get('query', '')
        if re.search(r'\bmutation\b', query):
            # Supports variable names chosen by the agent and literal input fields.
            def value(name):
                match = re.search(r'\b' + name + r'\s*:\s*(\$\w+|"(?:[^"\\]|\\.)*")', query)
                if not match:
                    fail('missing mutation input ' + name)
                token = match[1]
                variables = f.get('variables', {})
                if isinstance(variables, str):
                    variables = json.loads(variables)
                return f.get(token[1:], variables.get(token[1:])) if token.startswith('$') else json.loads(token)
            if 'addPullRequestReviewThreadReply' in query:
                reply(value('pullRequestReviewThreadId'), None, value('body'), 'graphql', {'operation': 'reply'})
            else:
                log({'kind': 'graphql', 'operation': 'mutation', 'query': query, 'fields': f})
                emit({'data': {}})
        else:
            if 'reviewThreads' in query:
                data = read('thread-pages.json', [read('threads.json')])
            elif 'PullRequestReviewThread' in query:
                data = read('comment-pages.json', {}).get(f.get('id'), [])
            else:
                fail('unsupported GraphQL read')
            if '--slurp' in args:
                output(data)
            else:
                for page in data:
                    output(page)
    elif re.search(r'/comments/\d+/replies$', endpoint):
        reply(None, re.search(r'/comments/(\d+)/replies$', endpoint)[1], f.get('body', ''), 'rest', {'operation': 'reply'})
    elif '/contents/.mergejudge.yml' in endpoint:
        import base64
        output({'content': base64.b64encode((root / '.mergejudge.yml').read_bytes()).decode(), 'encoding': 'base64'})
    elif re.search(r'/issues/\d+/comments$', endpoint) and ('POST' in args or f):
        comments = read('comments.json', [])
        comment = {'id': 10000 + len(comments), 'body': f.get('body', ''),
                   'html_url': 'https://github.com/eval/repo/pull/42#issuecomment-' + str(10000 + len(comments)),
                   'created_at': '2026-11-04T12:00:00Z',
                   'user': read('user.json', {'login': 'eval-user', 'type': 'User'})}
        comments.append(comment)
        (meta / 'comments.json').write_text(json.dumps(comments))
        log({'kind': 'rest', 'operation': 'issue_comment', **comment})
        output(comment)
    elif endpoint.endswith('/comments'):
        pages = read('issue-pages.json', [read('comments.json', [])])
        for page in pages:
            output(page)
    elif endpoint.endswith('/reviews'):
        pages = read('review-pages.json', [read('reviews.json', [])])
        for page in pages:
            output(page)
    elif endpoint.endswith('/files'):
        output([{'filename': p} for p in read('changed-files.json', ['src/first.js', 'test/first.test.js'])])
    elif any(a in args for a in ('POST', 'PATCH', 'PUT', 'DELETE')) or f:
        log({'kind': 'rest', 'operation': 'write', 'endpoint': endpoint, 'fields': f})
        emit({'id': 9999})
    else:
        fail('unsupported REST read')
else:
    fail('unsupported command')
