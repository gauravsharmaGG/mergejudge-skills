# review-policy-writer skill evals

Seven fixture scenarios cover writing a policy from repo facts (Node, Python), a custom `policy_files` target, updating an existing REVIEW.md without losing team rules, recurring themes from past review comments (including an injected instruction), refusing to copy secrets, and the shared 8 KB budget. The shape follows the fixture-based evals in `bjgreenberg/senior-engineering-partner`: `skills`, `query`, `files`, `expected_behavior`, and `anti_behavior`. Local `expected` names the target file (`{"target": "REVIEW.md"}`); its golden copy lives at `.mj/golden/<target>` and is removed before LLM runs.

Every fixture ends in `.fixture`. The runner strips that suffix into a temporary Git repository. Every listed file must exist, every fixture must be listed, and every fixture directory must belong to a scenario. These checks run across the whole suite before filtering. Optional `.gh/` files (`pull-comments.json`, `closed-prs.json`, `reviews.json`, `repo.json`, `user.json`) are served by the fake `gh`; both `.mj/` and `.gh/` are gitignored inside the eval repo so they never dirty the agent's tree.

```bash
python3 evals/run-evals.py --list
python3 evals/run-evals.py --dry-run
python3 evals/run-evals.py --self-test
python3 evals/run-evals.py --filter node-api-basic
python3 evals/run-evals.py --runner-cmd 'codex exec --skip-git-repo-check --sandbox workspace-write {prompt}'
```

`--dry-run` validates and materialises fixtures without an agent. `--self-test` runs a scripted perfect agent that copies the golden policy into place; it tests the harness, not skill quality. Default runs invoke Codex. The command template is split into argv without a shell; `{prompt}` must be one standalone token and is replaced by the skill body and scenario query. `--timeout` defaults to 600 seconds per scenario. `--filter` accepts a name, substring, or glob.

Fake `gh` serves repo/user identity, past PR review comments, closed PRs, reviews, and base-branch file contents. It logs every call to `.mj/gh-calls.jsonl`, logs writes (any POST/PATCH/PUT/DELETE, `pr create`/`edit`/`merge`, `issue`, GraphQL mutations) to `.mj/gh-writes.jsonl`, and fails them. `curl`, `wget`, and `ssh` on the injected PATH log and reject calls. Fake API operations never call real GitHub; there is no remote to push to.

The runner is a test harness, **not an OS sandbox**. Use an isolated environment for an LLM run: an agent can bypass PATH wrappers or use other network clients. The runner removes GitHub tokens from the child environment; use an environment without other secrets. Invoking an LLM CLI can itself use its configured provider network. Offline check modes do not invoke it.

Grading is deterministic over scenario `checks`:

- `file_exists` / `file_absent` {path}: the path exists or does not (a bare string is shorthand for `{path}`; same for `no_secret_patterns`).
- `max_bytes` {path, bytes}: file size at or under the cap.
- `headings` {path, required[]}: every named heading appears (exact text, case-insensitive).
- `contains` {path, any_of[]}: at least one substring present (case-insensitive; repeatable).
- `not_contains` {path, text[]}: none of the substrings present.
- `preserved_lines` {path, lines[]}: each line present verbatim.
- `policy_budget` {bytes}: existing MergeJudge policy files (defaults, or `.mergejudge.yml` `policy_files`) total at most this many bytes.
- `no_secret_patterns` {path}: none of MergeJudge's four secret regexes match.
- `no_gh_writes`: `.mj/gh-writes.jsonl` stayed empty.
- `file_unchanged` {path}: bytes match the fixture baseline, committed snapshots included.

The self-test adds negative controls after each perfect run: deleting the target, appending a forbidden string, removing a required heading, removing a preserved line (where the scenario has one), exceeding the shared budget or the per-file cap, and a logged `gh` write. Each must make the grader fail. The grader checks structure and content markers, not semantic quality — whether the rules are the *right* rules for a repo is judged by `expected_behavior`/`anti_behavior` and a human. It does not score final report prose or the agent's console output.

Results, logs, final workspaces, and diffs go to gitignored `evals/results/<timestamp>/`. Temporary Git repos are deleted after each scenario. Compare an agent's failures with the expected behaviour before changing the skill; the scripted self-test passing is not an LLM evaluation result.
