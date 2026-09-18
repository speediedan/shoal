#!/usr/bin/env bash
# watch-coordination.sh — ecosystem-neutral coordination monitors with a wakeup path.
#
# Two modes, one pattern (proven by agent_ci_watch.sh): poll state, hash it, and on change
# `session send` the owning session, because a monitor that only writes a log is an orphaned
# writer no idle session ever re-reads. No maintainer in the loop anywhere.
#
#   queue  — watch a cross-ecosystem queue file's STATUS section; notify on any change.
#   lanes  — watch lane sessions' (status + latest-report timestamp); notify on transitions.
#
# Usage:
#   watch-coordination.sh queue <queue-file> <notify-session-id> <repo-dir> [test]
#   watch-coordination.sh lanes <notify-session-id> <repo-dir> <lane-session-id>... [--test]
#     test/--test — wakeup proof: sends *-WATCH-TEST to the notify session and exits 0.
#             REQUIRED once per notify session before trusting the path (a wakeup never
#             demonstrated is assumed broken). Confirm arrival in-transcript.
#
# State files live in /tmp (never committed): a reboot causes at most one spurious
# notification on the next run, which the coordinator absorbs as a re-read.
#
# Cron (every 10 min is plenty for queues; lanes every 5):
#   */10 * * * * .../watch-coordination.sh queue <queue-file> <coord-id> <repo-dir>
#   */5 * * * *  .../watch-coordination.sh lanes <coord-id> <repo-dir> <laneA> <laneB>
#
# Address ALWAYS by session ID baked in at arm time; never bare lane names.
#
# HARDENING (2026-09-15 post-mortem): cron runs with PATH=/usr/bin:/bin, where `openchamber`
# (~/.bun/bin) does not exist. The script then hashed EMPTY snapshots, which compared equal
# run after run — silent forever, zero log output. So: explicit PATH below; a loud health gate
# before any snapshot (server reachable, queue file + STATUS section present, at least one lane
# resolving); never-hash-emptiness (an empty snapshot is ERROR, never baseline); one heartbeat
# line per run so a dead watcher is visible as a stale log instead of a clean one.
export PATH="/home/speediedan/.nvm/versions/node/v24.13.0/bin:/home/speediedan/.bun/bin:/usr/bin:/bin"
set -euo pipefail

HAG_EMIT=1
HAG_ID="it-mvp-2026-09"
STATE_DIR="/tmp/$HAG_ID"
mkdir -p "$STATE_DIR" 2>/dev/null || true
hag_emit() {
  # best-effort telemetry; never break the watcher. $1=type, rest k=v fields.
  [ "$HAG_EMIT" = "1" ] || return 0
  local t="$1"; shift
  local args=()
  for kv in "$@"; do args+=(--field "$kv"); done
  /home/speediedan/repos/distributed-insight/project_admin/shared_admin_scripts/hag/telemetry/hag_telemetry.py \
    emit --hag "$HAG_ID" --session "$HAG_SESSION" --ecosystem "$HAG_ECO" --type "$t" \
    "${args[@]}" >/dev/null 2>&1 || true
}
health() {
  if ! openchamber status >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) ERROR: openchamber server unreachable; refusing to snapshot." | tee -a $STATE_DIR/watch.log
    exit 2
  fi
}

MODE="${1:?mode required: queue|lanes}"; shift

fire() {
  # $1 = target session, $2 = dir, $3 = tag, $4 = summary.
  openchamber session send --session "$1" --dir "$2" --prompt "$3: $4" >/dev/null 2>&1
}

case "$MODE" in
  queue)
    QFILE="${1:?queue file required}"; TARGET="${2:?notify session id required}"
    DIR="${3:?repo dir required}"
    health
    [ -f "$QFILE" ] || { echo "$(date -u +%FT%TZ) ERROR: queue file $QFILE missing." | tee -a $STATE_DIR/watch.log; exit 2; }
    STATE="$STATE_DIR/queue-$(echo "$QFILE" | md5sum | cut -d' ' -f1).state"
    snap() {
      awk '/^## STATUS/{f=1} f && /^- (state|active-issue|base|waiting-on|updated-utc|last-done):/' "$QFILE" 2>/dev/null
    }
    if [ "${4:-}" = "test" ]; then
      fire "$TARGET" "$DIR" "QUEUE-WATCH-TEST" "dry-run wakeup proof for $(basename "$QFILE"); confirm arrival in-transcript"
      echo "test sent; confirm QUEUE-WATCH-TEST arrived in session $TARGET, then arm cron"
      exit 0
    fi
    cur="$(snap | md5sum | cut -d' ' -f1)"
    EMPTY="$(snap | grep -c . || true)"
    if [ "$EMPTY" -eq 0 ]; then
      echo "$(date -u +%FT%TZ) ERROR: STATUS section empty in $QFILE; refusing to snapshot." | tee -a $STATE_DIR/watch.log
      exit 2
    fi
    last="$(cat "$STATE" 2>/dev/null || echo none)"
    if [ "$cur" != "$last" ]; then
      printf '%s' "$cur" > "$STATE"
      [ "$last" = "none" ] && { echo "$(date -u +%FT%TZ) queue baseline established for $QFILE"; exit 0; }
      detail="$(snap | tr '\n' ';')"
      HAG_SESSION="$TARGET" HAG_ECO="opencode" hag_emit coord_comms direction=watch-fire peer=queue note="STATUS changed $(basename "$QFILE")" channel=within-ecosystem
      fire "$TARGET" "$DIR" "QUEUE-WATCH" "STATUS changed in $(basename "$QFILE"): $detail"
      echo "$(date -u +%FT%TZ) QUEUE-WATCH fired for $QFILE" | tee -a $STATE_DIR/watch.log
    else
      echo "$(date -u +%FT%TZ) queue ok no-change $(basename "$QFILE")"
    fi
    ;;
  lanes)
    TARGET="${1:?notify session id required}"; DIR="${2:?repo dir required}"; shift 2
    TEST=0; LANES=()
    for a in "$@"; do
      [ "$a" = "--test" ] && TEST=1 || LANES+=("$a")
    done
    [ "${#LANES[@]}" -gt 0 ] || { echo "at least one lane session id required." >&2; exit 1; }
    health
    if [ "$TEST" -eq 1 ]; then
      fire "$TARGET" "$DIR" "LANES-WATCH-TEST" "dry-run wakeup proof for ${#LANES[@]} lane(s); confirm arrival in-transcript"
      echo "test sent; confirm LANES-WATCH-TEST arrived in session $TARGET, then arm cron"
      exit 0
    fi
    STATE="$STATE_DIR/lanes-$(echo "$TARGET" | md5sum | cut -d' ' -f1).state"
    snap() {
      for lane in "${LANES[@]}"; do
        st="$(openchamber session status --session "$lane" --dir "$DIR" 2>/dev/null | head -n 1)"
        lastmsg="$(openchamber session messages --session "$lane" --dir "$DIR" --limit 1 --role assistant 2>/dev/null | grep -m1 -oE '\*20[0-9T:.-]+Z' || echo none)"
        printf '%s|%s|%s\n' "$lane" "$st" "$lastmsg"
      done
    }
    SNAP="$(snap)"
    SRV_PID="$(ls -t ~/.config/openchamber/managed-opencode/*.json 2>/dev/null | head -n 1 | xargs -r python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('pid','?'))" 2>/dev/null || echo ?)"
    SNAP="$SNAP
server_pid=$SRV_PID"
    cur="$(printf '%s' "$SNAP" | md5sum | cut -d' ' -f1)"
    if printf '%s' "$SNAP" | grep -qE 'status:(idle|busy)'; then :; else
      echo "$(date -u +%FT%TZ) ERROR: no lane resolved (all snapshots empty); refusing to snapshot." | tee -a $STATE_DIR/watch.log
      exit 2
    fi
    last="$(cat "$STATE" 2>/dev/null || echo none)"
    newpid="$(printf '%s' "$SNAP" | grep -oE 'server_pid=[0-9?]+' || echo server_pid=none)"
    oldpid="$(cat "$STATE.pid" 2>/dev/null || echo none)"
    printf '%s' "$newpid" > "$STATE.pid"
    if [ "$oldpid" != "none" ] && [ "$oldpid" != "$newpid" ]; then
      HAG_SESSION="$TARGET" HAG_ECO="opencode" hag_emit watch_event kind=server-respawned note="$oldpid->$newpid" channel=within-ecosystem
      HAG_SESSION="$TARGET" HAG_ECO="opencode" hag_emit coord_comms direction=watch-fire peer=lanes note="SERVER-RESPAWNED" channel=within-ecosystem
      fire "$TARGET" "$DIR" "LANES-WATCH" "SERVER-RESPAWNED ($oldpid->$newpid, re-send unacked INSTRUCTs): $(printf '%s' "$SNAP" | tr '\n' ';')"
      echo "$(date -u +%FT%TZ) LANES-WATCH SERVER-RESPAWNED fired: $oldpid->$newpid" | tee -a $STATE_DIR/watch.log
    fi
    if [ "$cur" != "$last" ]; then
      printf '%s' "$cur" > "$STATE"
      printf '%s' "$SNAP" > "$STATE.txt"
      [ "$last" = "none" ] && { echo "$(date -u +%FT%TZ) lanes baseline established for $TARGET"; exit 0; }
      detail="$(printf '%s' "$SNAP" | tr '\n' ';')"
      prefix="lane state transition"
      HAG_SESSION="$TARGET" HAG_ECO="opencode" hag_emit coord_comms direction=watch-fire peer=lanes note="$prefix" channel=within-ecosystem
      fire "$TARGET" "$DIR" "LANES-WATCH" "$prefix: $detail"
      echo "$(date -u +%FT%TZ) LANES-WATCH fired: $detail" | tee -a $STATE_DIR/watch.log
    else
      echo "$(date -u +%FT%TZ) lanes ok no-change -> $TARGET"
    fi
    ;;
  pipeline)
    TARGET="${1:?notify session id required}"; DIR="${2:?repo dir required}"
    ORG="${3:-https://dev.azure.com/speediedan}"; PROJ="${4:-interpretune}"
    health
    if [ -z "${AZURE_DEVOPS_EXT_PAT:-}" ]; then
      eval "$(grep -E '^export AZURE_DEVOPS_EXT_PAT=' ~/.bashrc 2>/dev/null || true)" || true
    fi
    export AZURE_DEVOPS_EXT_PAT
    STATE="$STATE_DIR/pipeline-$PROJ.state"
    running="$(az pipelines build list --organization "$ORG" --project "$PROJ" --status inProgress --top 5 --query "[].id" -o tsv 2>/dev/null | tr '\n' ',' || echo ERROR)"
    queued="$(az pipelines build list --organization "$ORG" --project "$PROJ" --status notStarted --top 10 --query "[].{id:id,branch:sourceBranch}" -o tsv 2>/dev/null | tr '\n' ';' || echo ERROR)"
    if [ "$running" = "ERROR" ] || [ "$queued" = "ERROR" ]; then
      echo "$(date -u +%FT%TZ) ERROR: az builds query failed; refusing to judge." | tee -a $STATE_DIR/watch.log
      exit 2
    fi
    cur="$(printf '%s|%s' "$running" "$queued" | md5sum | cut -d' ' -f1)"
    last="$(cat "$STATE" 2>/dev/null || echo none)"
    if [ "$cur" != "$last" ]; then
      printf '%s' "$cur" > "$STATE"
      [ "$last" = "none" ] && { echo "$(date -u +%FT%TZ) pipeline baseline established for $PROJ"; exit 0; }
      if [ -z "$running" ] && [ -n "$queued" ]; then
        HAG_SESSION="$TARGET" HAG_ECO="opencode" hag_emit coord_comms direction=watch-fire peer=pipeline note="depth-zero-with-queued" channel=within-ecosystem
        fire "$TARGET" "$DIR" "PIPELINE-WATCH" "STARVED: 0 running, queued: $queued — scan approvals + preconditions, release or hold-with-condition"
        echo "$(date -u +%FT%TZ) PIPELINE-WATCH STARVED fired: $queued" | tee -a $STATE_DIR/watch.log
      else
        echo "$(date -u +%FT%TZ) pipeline state changed (running/queued set moved)"
      fi
    else
      echo "$(date -u +%FT%TZ) pipeline ok no-change $PROJ (running=[${running:-none}])"
    fi
    ;;
  *) echo "mode must be queue|lanes|pipeline." >&2; exit 1 ;;
esac
