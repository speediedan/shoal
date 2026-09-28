#!/bin/bash
# agent_ci_watch.sh: head-pinned hosted-matrix watch with a self-send wakeup path.
#
# Conclusion-level polling (leg failures visible, unlike check-rollup watches, which logged nothing
# for 45 minutes across two failed legs) plus a wakeup self-send, because a monitor that only writes
# a log is an orphaned writer no idle session ever re-reads.
#
# The wakeup half is what makes this a monitor rather than a log writer.
#
# Usage:
#   agent_ci_watch.sh <pr-number> <wanted-head-sha> <own-session-id> <repo-dir> [test]
#     test  — dry-run proof: sends MONITOR-TEST to the owning session and exits 0.
#             REQUIRED once per session before trusting the path (a wakeup never demonstrated is assumed broken).
#
# Launch detached in its own session so a tool-call timeout cannot take it with the launcher:
#   setsid nohup agent_ci_watch.sh <pr> <head> <own-id> <dir> >>/tmp/watch_<pr>.launch.log 2>&1 < /dev/null &
# Verify alive from a SEPARATE shell: ps -p <pid>; tail the log.
#
# Semantics (one-shot per arming; re-arm explicitly for the next wait):
#   HEAD-MOVED (a push landed) -> log + exit 3 (never inherit a watch across heads).
#   ALERT (any fail/cancel/timed_out/action_required conclusion) -> log + self-send + exit 4.
#   TERMINAL (nothing pending/queued/waiting/in_progress/expected) -> log + self-send + exit 0.
# Address ALWAYS by session ID baked in at arm time; never bare lane names.
PR=${1:?"pr number required"}
HEAD_WANTED=${2:?"head sha required"}
SELF=${3:?"own session id required"}
DIR=${4:?"repo dir required"}
LOG=/tmp/mon_${PR}_hosted.log

fire() {
  openchamber session send --session "$SELF" --dir "$DIR" --prompt "MONITOR-FIRED PR-$PR $1: $2" >> "$LOG" 2>&1
  # telemetry is optional: set SHOAL_ID to the group id to record the fire
  local id="${SHOAL_ID:-}"
  [ -n "$id" ] || return 0
  python3 "$(dirname "$(readlink -f "$0")")/../telemetry/shoal_telemetry.py" \
    emit --shoal "$id" --session "$SELF" --ecosystem opencode --type coord_comms \
    --field direction=watch-fire --field peer="PR-$PR" --field note="$1" --field channel=within-ecosystem >/dev/null 2>&1 || true
}

if [ "${5:-}" = "test" ]; then
  echo "$(date -u +%FT%TZ) dry-run MONITOR-TEST send to $SELF" >> "$LOG"
  fire "MONITOR-TEST" "dry-run wakeup proof; confirm arrival in-transcript"
  echo "dry-run sent; confirm MONITOR-TEST arrived in session $SELF, then arm for real"
  exit 0
fi

echo "$(date -u +%FT%TZ) monitor armed for PR $PR head $HEAD_WANTED wakeup->$SELF" >> "$LOG"
last=""
alerted=""
matrix_green_fired=""
while true; do
  head_now=$(gh pr view "$PR" --json headRefOid --jq .headRefOid 2>/dev/null)
  if [ "$head_now" != "$HEAD_WANTED" ]; then
    echo "$(date -u +%FT%TZ) HEAD MOVED: $head_now (wanted $HEAD_WANTED); monitor exiting" >> "$LOG"
    exit 3
  fi
  snapshot=$(gh pr checks "$PR" 2>/dev/null | grep -viE 'skipping' | awk -F'\t' '{print $1" :: "$2}' | sort)
  state=$(printf '%s' "$snapshot" | md5sum | cut -d' ' -f1)
  if [ "$state" != "$last" ]; then
    echo "$(date -u +%FT%TZ) $snapshot" | tr '\n' ';' >> "$LOG"
    echo "" >> "$LOG"
    last="$state"
  fi
  failures=$(printf '%s' "$snapshot" | grep -iE ':: (fail|cancel|timed_out|action_required)')
  if [ -n "$failures" ] && [ "$failures" != "$alerted" ]; then
    echo "$(date -u +%FT%TZ) ALERT (conclusion changed to failure): $failures" | tr '\n' ';' >> "$LOG"
    echo "" >> "$LOG"
    alerted="$failures"
    fire "ALERT" "$failures"
    exit 4
  fi
  if ! printf '%s' "$snapshot" | grep -qiE ':: (pending|queued|waiting|in_progress|expected)'; then
    echo "$(date -u +%FT%TZ) TERMINAL: no pending checks." >> "$LOG"
    fire "TERMINAL" "no pending checks on $HEAD_WANTED"
    exit 0
  fi
  # MATRIX-GREEN (measured 2026-09-18: the hosted matrix can go fully green while the Azure
  # gate still pends approval, a state TERMINAL above can never observe — the watch would loop
  # silently forever on a decided wait). Fire once, keep watching for the gate verdict.
  if [ -z "$matrix_green_fired" ] && printf '%s' "$snapshot" | grep -qiE 'cpu \(.*\) :: pass' && ! printf '%s' "$snapshot" | grep -viE 'speediedan\.interpretune|skipping' | grep -qiE ':: (pending|queued|waiting|in_progress|expected|fail|cancel|timed_out|action_required)'; then
    echo "$(date -u +%FT%TZ) MATRIX-GREEN: hosted fully green, only Azure pending." >> "$LOG"
    fire "MATRIX-GREEN" "hosted green on $HEAD_WANTED, gate pending approval — release or hold-with-condition now"
    matrix_green_fired=1
  fi
  sleep 300
done
