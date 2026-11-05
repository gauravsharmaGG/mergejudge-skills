#!/usr/bin/env python3
"""Exercise the actual Bash/jq inventory through the offline gh interface."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/review-threads.sh'
REQUIRED = {'kind', 'id', 'threadId', 'rootCommentId', 'url', 'author', 'authorType',
            'source', 'path', 'line', 'key', 'mjState', 'isResolved', 'countsForGate',
            'answered', 'needsAnswer', 'message', 'replies'}

with tempfile.TemporaryDirectory(prefix='review-threads-test-') as scratch:
    tmp = Path(scratch)
    meta = tmp / '.mj'
    meta.mkdir()
    (tmp / 'bin').mkdir()
    fake = tmp / 'bin/gh'
    shutil.copyfile(ROOT / 'evals/fake-gh.py', fake)
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp / 'bin') + os.pathsep + os.environ['PATH'], MJ_WORKSPACE=str(tmp))

    def save(name, value):
        (meta / name).write_text(json.dumps(value))

    def execute(*args):
        return json.loads(subprocess.check_output([str(SCRIPT), *args], env=env, text=True))

    for scenario in sorted((ROOT / 'evals/scenarios').glob('*.json')):
        data = json.loads(scenario.read_text())
        for p in meta.iterdir():
            p.unlink()
        for p in (ROOT / 'evals/fixtures' / scenario.stem / '.mj').glob('*.json.fixture'):
            shutil.copyfile(p, meta / p.name.removesuffix('.fixture'))
        items = execute('--repo', 'eval/repo', '--pr', '42')
        assert execute() == items, 'default repo/PR/me discovery'
        by_key = {i['key']: i for i in items}
        for key, action in data['expected'].items():
            assert by_key[key]['needsAnswer'] == (action != 'none'), (scenario.stem, key)
        for item in items:
            assert REQUIRED <= item.keys(), item
            assert len(item['message']) <= 500
            assert item['countsForGate'] == (item['kind'] == 'thread' and item['source'] == 'mergejudge-action')
            if item['kind'] == 'summary':
                assert not item['needsAnswer']
        assert execute('--open') == [i for i in items if i['needsAnswer']]
        print('PASS inventory:', scenario.stem)

    template = json.loads((ROOT / 'evals/fixtures/real-bug-fix/.mj/threads.json.fixture').read_text())
    root = copy.deepcopy(template['data']['repository']['pullRequest']['reviewThreads']['nodes'][0])
    root['comments']['nodes'] = root['comments']['nodes'][:1]
    cases = [('quote', '  > This quote is more than eight chars', False, False),
             ('seven', '1234567', False, False), ('bot', 'A substantial bot reason', True, False),
             ('eight', '12345678', False, True), ('trim', '   1234567  \n', False, False),
             ('mixed', '> ignore this quote\nSpecific evidence supports this.', False, True),
             ('utf16', '😀😀😀😀', False, True)]
    nodes = []
    for label, body, bot, answered in cases:
        t = copy.deepcopy(root)
        t['id'] = label
        t['comments']['nodes'].append({'body': body, 'author': {'login': 'writer', '__typename': 'Bot' if bot else 'User'}})
        nodes.append(t)
    custom = copy.deepcopy(root)
    custom['id'] = 'custom'
    c = custom['comments']['nodes'][0]
    c['author']['login'] = 'Custom-Reviewer[bot]'
    c['body'] = '<!-- mergejudge:inline key=src%2Fcaf%C3%A9.js%3A2 state=active sha=abcdef0 -->\n⚖️ **MergeJudge (AI):** Check the \\*bound\\*.\n\n```suggestion\nx\n```'
    nodes.append(custom)
    custom_user = copy.deepcopy(custom)
    custom_user['id'] = 'custom-user'
    custom_user['comments']['nodes'][0]['author'] = {'login':'action-service', '__typename':'User'}
    nodes.append(custom_user)
    for login, typ, source in [('alice','User','human'), ('Copilot','User','copilot'),
                               ('copilot-pull-request-reviewer[bot]','Bot','copilot'),
                               ('coderabbitai[bot]','Bot','coderabbit'), ('cursor[bot]','Bot','bugbot'),
                               ('other[bot]','Bot','other-bot')]:
        t = copy.deepcopy(root)
        t['id'] = login
        t['comments']['nodes'][0].update(body='Please fix the bound.', author={'login': login, '__typename': typ})
        nodes.append(t)
    for login in ('eval-user', 'pr-author'):
        t = copy.deepcopy(nodes[-1])
        t['id'] = 'short-'+login
        t['comments']['nodes'].append({'body': 'ok', 'author': {'login': login, '__typename': 'User'}})
        nodes.append(t)
    fresh = copy.deepcopy(root)
    fresh['id'] = 'reflag'
    fresh['comments']['nodes'][0]['updatedAt'] = '2026-11-04T10:00:00Z'
    fresh['comments']['nodes'].append({'body': 'An old sufficient answer', 'createdAt': '2026-11-03T12:00:00Z', 'author': {'login': 'eval-user', '__typename': 'User'}})
    nodes.append(fresh)
    template['data']['repository']['pullRequest']['reviewThreads']['nodes'] = nodes
    save('threads.json', template)
    save('comments.json', [])
    save('reviews.json', [])
    found = {i['threadId']: i for i in execute()}
    for label, _, _, answered in cases:
        assert found[label]['answered'] == answered, label
    assert found['custom']['key'] == 'src/café.js:2'
    assert found['custom']['message'] == 'Check the *bound*.'
    assert not found['custom']['countsForGate']
    configured = {i['id']: i for i in execute('--action-login', 'one,CUSTOM-REVIEWER', '--action-login', 'action-service')}
    assert configured['custom']['countsForGate']
    assert configured['custom-user']['countsForGate'], 'custom Action human-token login owns its findings'
    assert not found['custom-user']['countsForGate']
    for login, source in [('alice','human'), ('Copilot','copilot'), ('copilot-pull-request-reviewer[bot]','copilot'), ('coderabbitai[bot]','coderabbit'), ('cursor[bot]','bugbot'), ('other[bot]','other-bot')]:
        assert found[login]['source'] == source and found[login]['needsAnswer']
    assert all(found['short-'+login]['answered'] for login in ('eval-user', 'pr-author'))
    assert found['reflag']['needsAnswer']
    assert next(i for i in execute('--me', 'writer') if i['id'] == 'seven')['needsAnswer'], 'MergeJudge still needs 8 chars'
    print('PASS inventory: reply boundaries, source attribution, custom logins, Unicode, re-flags')

    # Thread, nested reply, issue-comment, and review pagination all use real gh flags.
    pages = []
    for i, subset in enumerate((nodes[:3], nodes[3:])):
        page = copy.deepcopy(template)
        conn = page['data']['repository']['pullRequest']['reviewThreads']
        conn['nodes'] = subset
        conn['pageInfo'] = {'hasNextPage': i == 0, 'endCursor': 'next' if i == 0 else None}
        pages.append(page)
    pages[0]['data']['repository']['pullRequest']['reviewThreads']['nodes'][0]['comments']['pageInfo'] = {'hasNextPage': True, 'endCursor': 'replies-next'}
    save('thread-pages.json', pages)
    save('comment-pages.json', {'quote': [{'data': {'node': {'comments': {'nodes': [{'body':'A later sufficient answer.', 'author':{'login':'writer','__typename':'User'}}], 'pageInfo':{'hasNextPage':False,'endCursor':None}}}}}]})
    def comment(id, body, login='alice', time='2026-11-03T10:00:00Z'):
        return dict(id=id, body=body, user={'login':login,'type':'User'}, html_url=f'https://github.com/eval/repo/pull/42#issuecomment-{id}', created_at=time)
    c1 = comment(310, 'Could you document this?')
    c2 = comment(311, 'Why is this anonymous?')
    c3 = comment(312, 'Please explain this contract?')
    replies = [comment(401, 'Answer '+c1['html_url'], 'eval-user', '2026-11-04T10:00:00Z'),
               comment(402, 'Answer 3110', 'eval-user', '2026-11-04T10:00:00Z'),
               comment(403, 'Answer 312', 'eval-user', '2026-11-02T10:00:00Z'),
               comment(404, 'Answer 311', 'someone-else', '2026-11-04T10:00:00Z')]
    save('issue-pages.json', [[c1,c2],[c3,*replies]])
    r1 = dict(comment(510,'Please fix the API.'), state='CHANGES_REQUESTED', submitted_at='2026-11-03T10:00:00Z')
    r2 = dict(comment(511,'## Pull request overview\nReviewed the files.', 'Copilot'), state='COMMENTED')
    r3 = dict(comment(512,'', 'reviewer'), state='COMMENTED')
    save('review-pages.json', [[r1], [r2, r3]])
    found = {i['key'] if i['kind'] != 'thread' else i['id']: i for i in execute()}
    assert found['quote']['answered'], 'nested reply pagination'
    assert found['comment:310']['answered']
    assert found['comment:311']['needsAnswer'], 'id substring and other authors do not answer'
    assert found['comment:312']['needsAnswer'], 'earlier comment does not answer'
    assert found['review:510']['needsAnswer']
    assert found['review:511']['kind'] == 'summary' and not found['review:511']['needsAnswer']
    assert 'review:512' not in found
    assert all(i['author'] != 'eval-user' for i in found.values())
    print('PASS inventory: all four pagination paths, review bodies, summaries, linked answers, timestamps')

    save('thread-pages.json', [{'errors':[{'message':'Denied'}]}])
    result = subprocess.run([str(SCRIPT)], env=env, capture_output=True)
    assert result.returncode != 0, 'GraphQL errors must not look like an empty inventory'
    for args in [('--pr', 'zero'), ('--repo', 'bad'), ('--me',), ('--unknown',)]:
        assert subprocess.run([str(SCRIPT), *args], env=env, capture_output=True).returncode != 0
    print('PASS inventory: fail closed on API errors and invalid arguments')
