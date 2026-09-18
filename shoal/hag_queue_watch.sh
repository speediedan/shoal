#!/usr/bin/env bash
# Consumer QUEUE self-watch (promoted 2026-09-18 from the it-architect pair; generalized).
# Every INTERVAL s: rehash the QUEUE section; exit (which wakes the owning session) ONLY
# when an item whose bold heading is in neither `consumed` nor `declined` of the sidecar
# appears. The sidecar is re-read every tick, so consumed/declined edits take effect without
# a restart. A QUEUE edit that only confirms a decline updates queue_hash and does not wake.
# Five consecutive read failures exit 2 (QUERY-FAILED) so a broken instrument is reported
# rather than silently sleeping.
#
# Usage: hag_queue_watch.sh --sidecar PATH --interval SECONDS [--section-bin DIR]
#   --section-bin defaults to this script's directory (queue_section.py lives alongside).
S=""
INTERVAL=""
BIN="$(dirname "$0")"
while [ $# -gt 0 ]; do
  case "$1" in
    --sidecar) S="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --section-bin) BIN="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[ -n "$S" ] && [ -n "$INTERVAL" ] || { echo "usage: hag_queue_watch.sh --sidecar PATH --interval SECONDS [--section-bin DIR]" >&2; exit 1; }
P="$BIN/queue_section.py"
fails=0
while true; do
  sleep "$INTERVAL"
  Q=$(grep -oP '^queue_file=\K.*' "$S" 2>/dev/null) || { echo "QUEUE-WATCH ERROR: sidecar unreadable at $S"; exit 3; }
  if ! out=$(python3 "$P" "$Q"); then
    fails=$((fails + 1)); echo "QUEUE-WATCH QUERY-FAILED ($fails/5): $out"
    [ "$fails" -ge 5 ] && exit 2
    continue
  fi
  fails=0
  read -r h n <<<"$(head -1 <<<"$out")"
  prev=$(grep -oP '^queue_hash=\K.*' "$S")
  [ "$h" = "$prev" ] && continue
  sed -i "s/^queue_hash=.*/queue_hash=$h/" "$S"
  cons=$(grep -oP '^consumed=\K.*' "$S"); decl=$(grep -oP '^declined=\K.*' "$S")
  new=""
  while IFS= read -r line; do
    id=${line#ITEM }
    [ "$line" = "$id" ] && continue
    case ",$cons,$decl," in *",$id,"*) ;; *) new="$new | $id" ;; esac
  done <<<"$out"
  if [ -n "$new" ]; then
    echo "QUEUE-CHANGED NEW-ITEMS at $(date -u +%H:%M:%SZ): [$h $n items]$new"
    exit 0
  fi
  echo "QUEUE-CHANGED no unseen items at $(date -u +%H:%M:%SZ): [$prev -> $h, $n items] (decline confirmation or reorder)"
done
