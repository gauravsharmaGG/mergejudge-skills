Works best with [MergeJudge](https://mergejudge.si).

# review-policy-writer

Reads a repository and writes its review-rules file — `REVIEW.md` at the repo root by default — for AI and human reviewers. The file follows [MergeJudge's](https://mergejudge.si/guide/policy-files) template, but the wording is generic: Claude Code, Cursor, Copilot, or a human can use it too.

Copy `review-policy-writer/` into `~/.claude/skills/`, `.cursor/skills/`, or your Codex skills directory. Then ask your agent: "Write a REVIEW.md for this repo" or "Update our review policy". The skill gathers facts with `scripts/repo-facts.sh`, optionally mines recurring themes from past PR review comments via `gh`, writes the policy, and validates it with `scripts/check-policy.py` (headings, 8 KB shared budget, no secrets, no rule that lowers review strictness). When a policy file already exists it is updated in place: team rules are kept verbatim.

Requires Bash, Git, and Python 3 (stdlib only). `gh` is optional and only used, when authenticated, to read past review comments.

Run the offline checks:

```bash
bash review-policy-writer/check.sh
```

This runs shell and Python checks, unit tests, and a scripted grader self-test over the eval fixtures. It does not call an LLM or real GitHub. To evaluate an agent, see [evals/README.md](evals/README.md).

## License

Apache-2.0, see LICENSE at the repository root
