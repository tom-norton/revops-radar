#!/usr/bin/env bash
# Commit the radar's data files and push them to main, surviving a race with the other
# writer.
#
#   tools/push_main.sh "<commit message>" scan|apply
#
# Two workflows write to main: the scan, and apply-scores.yml, which puts the subscription
# session's answers on the board the moment they are pushed. They are in different
# concurrency groups on purpose -- in one group a queued apply would cancel a queued scan --
# so either can land while the other is running, and the loser's push is rejected.
#
# On a rejected push this fetches the new main and redoes the work on top of it, rather
# than merging two copies of docs/jobs.json line by line:
#   apply  throws its commit away and re-applies. Applying is idempotent: an answer is
#          applied exactly when its role is still on the queue.
#   scan   keeps its own data files (copied aside, laid over the new main), then
#          re-applies, which puts back whatever the apply that beat it had added. Code
#          and anything else that changed on main is taken from main.
set -u
msg="$1"; mode="$2"
DATA="seen.json deferred.json dealt.json kills.json unkill.json docs/jobs.json docs/status.json
docs/excluded.json board-cache.json entities.json nl_sponsors_extra.txt scoring/queue.json
shadow/queue.json"

stage() {
  for f in $DATA; do [ -e "$f" ] && git add -- "$f"; done
  git diff --cached --quiet || git commit -qm "$msg"
}

stage
for i in 1 2 3 4 5; do
  git push origin HEAD:main && exit 0
  echo "push rejected (attempt $i); redoing on top of the new main"
  sleep $((i * 4))
  git fetch -q origin +refs/heads/main:refs/remotes/origin/main
  git fetch -q origin +refs/heads/claude/scores:refs/remotes/origin/claude/scores 2>/dev/null || true
  if [ "$mode" = "scan" ]; then
    tmp=$(mktemp -d)
    for f in $DATA; do [ -e "$f" ] && mkdir -p "$tmp/$(dirname "$f")" && cp "$f" "$tmp/$f"; done
    git reset -q --hard origin/main
    (cd "$tmp" && find . -type f) | while read -r f; do cp "$tmp/$f" "$f"; done
    rm -rf "$tmp"
  else
    git reset -q --hard origin/main
  fi
  # Quiet: whichever run applied these first already sent their phone notification.
  APPLY_NO_NOTIFY=1 python scan.py --apply-scores
  stage
done
echo "could not push after 5 attempts"
exit 1
