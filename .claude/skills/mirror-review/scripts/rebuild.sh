#!/usr/bin/env bash
# Rebuild the unpushed mirror history: one commit per private commit (group),
# each made from sync_public.py run against that private commit.
# Usage: rebuild.sh <mirror-dir> <upstream-sha> <plan-dir>
# <plan-dir> holds plan.txt ("<msg-nr> <private-commit>..." per line, '#'
# comments) and msgs/<msg-nr>.txt with one commit message each.
# Precondition: mirror HEAD == <upstream-sha>, clean working tree.
set -euo pipefail

[ $# -eq 3 ] || { echo "usage: $0 <mirror-dir> <upstream-sha> <plan-dir>" >&2; exit 2; }
mirror=$1
upstream=$2
plan_dir=$(cd "$3" && pwd)
private=${MIRROR_PRIVATE_REPO:-$HOME/Projects/gitea/homelab-infra}
sync=$private/scripts/sync_public.py
ignore=$private/scripts/config/.publicignore

[ "$(git -C "$mirror" rev-parse HEAD)" = "$(git -C "$mirror" rev-parse "$upstream")" ] \
  || { echo "ABORT: mirror HEAD is not $upstream" >&2; exit 1; }
[ -z "$(git -C "$mirror" status --porcelain)" ] \
  || { echo "ABORT: mirror working tree is not clean" >&2; exit 1; }

src=$(mktemp -d)
trap 'rm -rf "$src"' EXIT
git clone -q "$private" "$src"

while read -r msg commits; do
  case $msg in ''|'#'*) continue ;; esac
  last=${commits##* }
  git -C "$src" checkout -q "$last"
  python3 "$sync" "$mirror" --source "$src" --ignore-file "$ignore" > "$src.log" \
    || { cat "$src.log" >&2; echo "ABORT: sync failed at $last" >&2; exit 1; }
  # stage the mirror changes among the files the listed commits touch;
  # blocklisted paths have no mirror counterpart and drop out here
  # shellcheck disable=SC2086  # $commits is a space-separated list
  comm -12 \
    <(for c in $commits; do
        git -C "$src" -c core.quotePath=false diff-tree --no-commit-id --name-only -r "$c"
      done | sort -u) \
    <(git -C "$mirror" -c core.quotePath=false status --porcelain --untracked-files=all \
        | cut -c4- | sort -u) \
    | while read -r f; do git -C "$mirror" add -A -- "$f"; done
  if git -C "$mirror" diff --cached --quiet; then
    echo "ABORT: nothing staged for $msg ($commits)" >&2; exit 1
  fi
  git -C "$mirror" commit -q -F "$plan_dir/msgs/$msg.txt"
  echo "$msg $commits -> $(git -C "$mirror" log -1 --format='%h %s')"
done < "$plan_dir/plan.txt"
rm -f "$src.log"

if [ -n "$(git -C "$mirror" status --porcelain)" ]; then
  git -C "$mirror" status --short >&2
  echo "ABORT: changes left over after the last commit" >&2; exit 1
fi
echo "tree: $(git -C "$mirror" rev-parse 'HEAD^{tree}')"
