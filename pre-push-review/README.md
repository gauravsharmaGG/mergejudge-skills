# pre-push-review

Works best with [MergeJudge](https://mergejudge.si).

A pre-push reviewer for any agent. Before you push, it reviews the local change set — unpushed commits plus staged edits, with unstaged and untracked files listed separately — against the repo's review policy (REVIEW.md and friends, read from the base ref), fixes small clear issues in the working tree, flags everything that needs a human, and reports a MergeJudge-style lane (`auto` / `review` / `block`). It never pushes and never rewrites history; fixes stay uncommitted so you review and commit them yourself. A secret inside an unpushed commit is flagged for you to rewrite before pushing.

Install: copy the `pre-push-review/` folder into `~/.claude/skills/`, `.cursor/skills/`, or your Codex skills directory. Requires Bash, git, `jq`, and Python 3.

Usage: ask your agent to "run the pre-push review before I push". The agent runs `scripts/local-diff.sh` and `scripts/policy-lane.py`, prints a summary, and writes a machine-readable report to `.git/pre-push-review.json` (or to `$PRE_PUSH_REVIEW_REPORT` when set).

Optional pre-push hook (install it yourself; the skill never installs hooks):

```bash
#!/bin/sh
# .git/hooks/pre-push — fail when the local change set lands in the block lane
base=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || git rev-parse HEAD)
lane=$(python3 ~/.claude/skills/pre-push-review/scripts/policy-lane.py --base "$base" | jq -r .lane)
if [ "$lane" = block ]; then
  echo "pre-push-review: lane is block; resolve findings first" >&2
  exit 1
fi
exit 0
```

Run the offline checks from the repo root:

```bash
bash pre-push-review/check.sh
```

This checks the shell and Python sources, runs the unit tests for both bundled tools, and validates plus self-tests the eval scenarios. It never calls an LLM, the network, or real GitHub. To evaluate an agent, see [evals/README.md](evals/README.md).

## License

Apache-2.0, see LICENSE at the repo root.
