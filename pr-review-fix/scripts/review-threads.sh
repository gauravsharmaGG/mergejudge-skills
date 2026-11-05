#!/usr/bin/env bash
set -euo pipefail
repo='' pr='' action_logins=github-actions me='' open=false
while (($#)); do
  case "$1" in
    --repo|--pr|--action-login|--me)
      (($# >= 2)) || { echo "Missing value for $1" >&2; exit 2; }
      case "$1" in
        --repo) repo=$2;; --pr) pr=$2;; --me) me=$2;;
        --action-login) action_logins+=",$2";;
      esac
      shift 2;;
    --open) open=true; shift;;
    -h|--help) echo 'Usage: review-threads.sh [--repo owner/name] [--pr N] [--action-login LOGIN,...] [--me LOGIN] [--open]'; exit 0;;
    *) echo "Unknown argument: $1" >&2; exit 2;;
  esac
done
command -v gh >/dev/null
command -v jq >/dev/null
[[ -n "$repo" ]] || repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
[[ "$repo" =~ ^[^/]+/[^/]+$ ]] || { echo 'Expected owner/name' >&2; exit 2; }
[[ -n "$pr" ]] || pr=$(gh pr view --repo "$repo" --json number --jq .number)
[[ "$pr" =~ ^[1-9][0-9]*$ ]] || { echo 'Expected numeric PR' >&2; exit 2; }
[[ -n "$me" ]] || me=$(gh api user --jq .login)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# shellcheck disable=SC2016
query='query($owner:String!,$name:String!,$pr:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$pr){author{login} reviewThreads(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{id isResolved path line comments(first:100){pageInfo{hasNextPage endCursor} nodes{databaseId id url body createdAt updatedAt author{login __typename}}}}}}}}'
gh api graphql --paginate --slurp -f query="$query" -f owner="${repo%/*}" -f name="${repo#*/}" -F pr="$pr" > "$tmp/threads.json"
jq -e 'length > 0 and all(.[]; (.errors | not) and .data.repository.pullRequest != null)' "$tmp/threads.json" >/dev/null
# Nested connections need their own pagination for threads exceeding 100 comments.
jq -r '.[] | .data.repository.pullRequest.reviewThreads.nodes[] | select(.comments.pageInfo.hasNextPage) | [.id,.comments.pageInfo.endCursor] | @tsv' "$tmp/threads.json" > "$tmp/overflow"
# shellcheck disable=SC2016
comments_query='query($id:ID!,$endCursor:String){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$endCursor){pageInfo{hasNextPage endCursor} nodes{databaseId id url body createdAt updatedAt author{login __typename}}}}}}'
while IFS=$'\t' read -r thread cursor; do
  gh api graphql --paginate --slurp -f query="$comments_query" -f id="$thread" -f endCursor="$cursor" > "$tmp/more.json"
  jq -e 'length > 0 and all(.[]; (.errors | not) and .data.node.comments != null)' "$tmp/more.json" >/dev/null
  jq --arg id "$thread" --slurpfile more "$tmp/more.json" 'map(.data.repository.pullRequest.reviewThreads.nodes |= map(if .id == $id then .comments.nodes += [$more[0][] | .data.node.comments.nodes[]] else . end))' "$tmp/threads.json" > "$tmp/updated.json"
  mv "$tmp/updated.json" "$tmp/threads.json"
done < "$tmp/overflow"
gh api --paginate "repos/$repo/issues/$pr/comments" | jq -s 'add // []' > "$tmp/comments.json"
gh api --paginate "repos/$repo/pulls/$pr/reviews" | jq -s 'add // []' > "$tmp/reviews.json"
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
jq -n --arg actions "$action_logins" --arg me "$me" --arg repo "$repo" --arg pr "$pr" --argjson open "$open" \
  --slurpfile threads "$tmp/threads.json" --slurpfile comments "$tmp/comments.json" --slurpfile reviews "$tmp/reviews.json" \
  -f "$script_dir/review-items.jq"
