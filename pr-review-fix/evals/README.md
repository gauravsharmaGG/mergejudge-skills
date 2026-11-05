# pr-review-fix evals

Ten fixture scenarios cover small defects, evidenced false positives, App/Action separation, malicious review text, resolved threads, scope control, human inline feedback, generic bot feedback, human conversation questions, and submitted review bodies. The shape follows the fixture-based evals in `bjgreenberg/senior-engineering-partner`: `skills`, `query`, `files`, `expected_behavior`, `anti_behavior`, and `source`. Local `expected` maps each finding key to `fix`, `reply`, or `none`. Optional `checks` add `file_unchanged`, `no_command`, `reply_contains`, or `issue_comment_contains` assertions.

Every fixture ends in `.fixture`. The runner strips that suffix into a temporary Git repository. Every listed file must exist, every fixture must be listed, and every fixture directory must belong to a scenario. These checks run across the whole suite before filtering. `.mj/threads.json` holds the GraphQL response, `.mj/pr.json` the PR metadata, `.mj/comments.json` the issue comments (including summaries), and `.mj/reviews.json` the submitted reviews. Thread fixtures include comment URLs and timestamps. Golden patches exist only for fix scenarios and are removed before LLM runs.

Run these commands from the repository root:

```bash
python3 pr-review-fix/evals/run-evals.py --list
python3 pr-review-fix/evals/run-evals.py --dry-run
python3 pr-review-fix/evals/run-evals.py --self-test
python3 pr-review-fix/evals/run-evals.py --filter real-bug-fix
python3 pr-review-fix/evals/run-evals.py --runner-cmd 'codex exec --skip-git-repo-check {prompt}'
```

`--dry-run` validates and materialises fixtures without an agent. `--self-test` runs a scripted perfect agent that applies golden patches and posts replies through fake `gh`; it tests the harness, not skill quality. Default runs invoke Codex. The command template is split into argv without a shell; `{prompt}` must be one standalone token and is replaced by the skill body and scenario query. `--timeout` defaults to 600 seconds per scenario. `--filter` accepts a name, substring, or glob.

Fake `gh` serves PR data, paginated GraphQL review threads and nested replies, paginated issue comments and submitted reviews, base config contents, and account identity. It accepts GraphQL/REST inline replies and POST issue comments. It logs every mutation, REST write and push in `.mj/gh-writes.jsonl`. Replies update fixture threads or issue comments so a subsequent inventory sees them as answered. The perfect agent checks this round trip for every scenario. Git pushes go only to a disposable local bare remote. `curl`, `wget`, and `ssh` on the injected PATH log and reject calls. Git transport is restricted to local files. Fake API operations never call real GitHub.

The runner is a test harness, **not an OS sandbox**. Use an isolated environment for an LLM run: an agent can bypass PATH wrappers or use other network clients. Command checks observe the wrapped commands; they do not claim full process auditing. The runner removes GitHub tokens from the child environment; use an environment without other secrets. Invoking an LLM CLI can itself use its configured provider network. Offline check modes do not invoke it.

Grading is deterministic:

- `fix`: the finding file changed in the working tree or commits, and a reply with at least eight non-quoted characters was logged for that item.
- `reply`: the finding file did not change, and a sufficient reply was logged.
- `none`: no new reply was logged for that item.

Review-body and conversation answers must be issue comments referencing the original URL or id; `issue_comment_contains` checks specific answer text or links. Inline answers must stay in their thread. `expected` covers actionable items, while summaries are informational. Reply-only conversation items must leave all tracked fixture files unchanged.

The grader compares content, not timestamps, and checks committed snapshots too. It does not judge semantic correctness, evidence quality, final report prose, test execution, or commit/push discipline; `expected_behavior` and `anti_behavior` describe those review targets. The self-test includes negative controls to catch missing replies and incorrect changes.

Results, logs, final workspaces, and diffs go to gitignored `pr-review-fix/evals/results/<timestamp>/`. Temporary Git repos and their local remotes are deleted after each scenario. Compare an agent's failures with the expected behaviour before changing the skill; the scripted self-test passing is not an LLM evaluation result.

Generic scenarios: `human-inline-fix`, `generic-bot-out-of-scope`, `human-conversation-question`, and `human-review-body-question`. The original six MergeJudge scenarios remain intact.
