Works best with [MergeJudge](https://mergejudge.si).

# mergejudge-skills

Agent skills for pull request reviews, with built-in support for MergeJudge and its [GitHub Action runner](https://github.com/gauravsharmaGG/mergejudge-runner).

## Skills

| Skill | Purpose |
| --- | --- |
| [pr-review-fix](pr-review-fix/) | Fixes or answers every open PR review comment, from people or review bots, with built-in MergeJudge answer-gate support. |
| [review-policy-writer](review-policy-writer/) | Writes a repo's review policy file (REVIEW.md) for MergeJudge and other reviewers. |
| [pre-push-review](pre-push-review/) | Reviews local changes against the repo's review policy before you push. |

## Install and usage

Copy the skill folder into your agent's skills directory, such as `~/.claude/skills/`, `.cursor/skills/`, or `.agents/skills/`. For example, copy `pr-review-fix/` and ask your agent: “Use pr-review-fix to fix or answer every open review comment on PR 42.”

`pr-review-fix` requires Bash, `gh`, `jq`, Git, and authenticated GitHub access. MergeJudge gate answers require a human account; bot replies never count. Repository tests/lint need their usual tools. See [the eval guide](pr-review-fix/evals/README.md) for agent evaluation.

## Checks

Run `bash scripts/check.sh`. It checks tracked shell scripts with ShellCheck when available and runs per-skill checks for every top-level folder with a `SKILL.md`. CI runs it on every PR.

To add a skill, create `<skill>/SKILL.md` with frontmatter containing `name:` equal to its folder name and a `description:`. Add `<skill>/check.sh` for its checks, or `<skill>/evals/run-evals.py` supporting `--dry-run` and `--self-test`. The root script discovers it automatically; skills with neither print “no checks”.

The `pr-review-fix` checks validate scripts, scenario JSON, inventory behavior, fixtures, and the grader. They run offline with Python 3, Bash, `jq`, and Git, without an LLM or real GitHub calls.

License: Apache-2.0, see [LICENSE](LICENSE).
