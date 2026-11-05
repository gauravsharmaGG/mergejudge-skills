#!/usr/bin/env bash
# Print the local, not-yet-pushed change set as JSON: branch, upstream, merge-base,
# unpushed commits, and every changed file split into committed / staged / unstaged /
# untracked buckets. --patch prints the combined diff (merge-base..index) instead.
# Read-only: every git command here only observes the repository.
set -euo pipefail

repo=.
mode=json
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo=${2:?--repo needs a value}; shift 2 ;;
    --patch) mode='patch'; shift ;;
    -h|--help)
      echo 'usage: local-diff.sh [--repo DIR] [--patch]'
      exit 0
      ;;
    *)
      printf 'local-diff.sh: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

g() { command git -C "$repo" "$@"; }

top=$(g rev-parse --show-toplevel)
branch=$(g branch --show-current)
[ -n "$branch" ] || branch='(detached)'

# Upstream: @{u} if set, else origin/HEAD, else origin/main, else origin/master.
upstream=''
upstream_source='none'
track=''
if upstream=$(g rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) && [ -n "$upstream" ]; then
  track=$upstream
  upstream_source='upstream'
else
  upstream=''
  if head_ref=$(g symbolic-ref -q --short refs/remotes/origin/HEAD) && \
    g rev-parse -q --verify "$head_ref^{commit}" >/dev/null 2>&1; then
    track=$head_ref
    upstream_source='origin-head'
  elif g rev-parse -q --verify origin/main >/dev/null 2>&1; then
    track=origin/main
    upstream_source='origin-main'
  elif g rev-parse -q --verify origin/master >/dev/null 2>&1; then
    track=origin/master
    upstream_source='origin-master'
  fi
fi

if [ -n "$track" ]; then
  merge_base=$(g merge-base HEAD "$track")
else
  # No remote ref at all: review the staged and unstaged changes against HEAD.
  merge_base=$(g rev-parse HEAD)
fi
# Policy is read from the upstream tip when one is set (like MergeJudge's base branch),
# otherwise from the merge-base commit.
if [ "$upstream_source" = upstream ]; then
  base=$track
else
  base=$merge_base
fi

if [ "$mode" = patch ]; then
  # Combined diff of unpushed commits plus staged changes (merge-base..index).
  g diff --no-ext-diff --no-renames --cached "$merge_base"
  exit 0
fi

rows=$'\n'
add_row() { rows+="$1"$'\n'; }

while IFS=$'\t' read -r adds dels path; do
  [ -n "$path" ] || continue
  add_row "$(printf 'c\t%s\t%s\t%s' "$path" "$adds" "$dels")"
done < <(g diff --numstat --no-renames "$merge_base" HEAD)

while IFS=$'\t' read -r adds dels path; do
  [ -n "$path" ] || continue
  add_row "$(printf 's\t%s\t%s\t%s' "$path" "$adds" "$dels")"
done < <(g diff --numstat --no-renames --cached)

while IFS=$'\t' read -r adds dels path; do
  [ -n "$path" ] || continue
  add_row "$(printf 'u\t%s\t%s\t%s' "$path" "$adds" "$dels")"
done < <(g diff --numstat --no-renames)

while IFS=$'\t' read -r status path; do
  [ -n "$path" ] || continue
  add_row "$(printf 't\t%s\t%s' "$path" "$status")"
done < <(g diff --name-status --no-renames "$merge_base")

while IFS= read -r path; do
  [ -n "$path" ] || continue
  add_row "$(printf 'n\t%s' "$path")"
done < <(g ls-files --others --exclude-standard)

if [ -n "$track" ]; then
  while IFS=$'\t' read -r sha subject; do
    [ -n "$sha" ] || continue
    add_row "$(printf 'p\t%s\t%s' "$sha" "$subject")"
  done < <(g log --format='%H%x09%s' "$track..HEAD")
fi

up_json=$(printf '%s\n' "$upstream" | jq -R 'if . == "" then null else . end')
printf '%s' "$rows" | jq -Rn \
  --arg repo "$top" \
  --arg branch "$branch" \
  --argjson upstream "$up_json" \
  --arg upstream_source "$upstream_source" \
  --arg merge_base "$merge_base" \
  --arg base "$base" '
  def num: if . == null or . == "-" then null else tonumber end;
  [inputs | select(length > 0) | split("\t")] as $rows
  | reduce ($rows[] | select(.[0] == "c" or .[0] == "s" or .[0] == "u")) as $r ({};
      .[$r[1]] = ((.[$r[1]] // {path: $r[1], status: null, committed: null, staged: null, unstaged: null, untracked: false})
        | .[{c: "committed", s: "staged", u: "unstaged"}[$r[0]]] = {adds: ($r[2] | num), dels: ($r[3] | num)}))
  | reduce ($rows[] | select(.[0] == "t")) as $r (.;
      .[$r[1]] = ((.[$r[1]] // {path: $r[1], status: null, committed: null, staged: null, unstaged: null, untracked: false})
        | .status = $r[2]))
  | reduce ($rows[] | select(.[0] == "n")) as $r (.;
      .[$r[1]] = ((.[$r[1]] // {path: $r[1], status: null, committed: null, staged: null, unstaged: null, untracked: false})
        | .untracked = true
        | .status = (.status // "?")))
  | [.[]] | sort_by(.path) as $files
  | [$rows[] | select(.[0] == "p") | {sha: .[1], subject: .[2]}] as $unpushed
  | {repo: $repo, branch: $branch, upstream: $upstream, upstream_source: $upstream_source,
     merge_base: $merge_base, base: $base, unpushed: $unpushed, files: $files}
'
