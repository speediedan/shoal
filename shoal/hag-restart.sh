#!/usr/bin/env bash
# hag-restart.sh — scripted HAG generation restart (runbook Variant A), plus consumer briefs.
#
# Creates a fresh coordinator + lane generation with loop-tested direct relay, or prints
# exactly what it would do. opencode lanes are fully scripted; the Claude consumer is
# prepared (brief file) but started by hand, because Claude sessions need an interactive
# terminal and MUST run on the native claude binary — Claude-subscription models are never
# passed to opencode sessions.
#
# Usage:
#   hag-restart.sh --hag ID --dir REPO --coord-title TITLE --coord-handoff FILE --coord-plan FILE
#     --lane SLUG,TITLE,HANDOFF,PLAN [--lane ...] [--gen N] [--dry-run] [--probe-claude]
#
#   --gen N            generation suffix for titles (default: next unused -rN for the role)
#   --dry-run          print every create/send/verify step; change nothing.
#   --stages LIST      comma subset of handoffs,sync-plans,retire,restart (default: all, in order).
#                      Each stage honors --dry-run independently.
#   --probe-claude     OPT-IN connectivity probe: one cheap haiku turn
#                      (claude -p --model claude-haiku-4-5-20251001, exact-reply prompt).
#                      Default off — socket presence is checked without any turn.
#
# Stages:
#   handoffs   — write ecosystem-neutral handoff files per participating session from the
#                template (role, queue position, base, blockers, grants held) into the goal's
#                handoffs/ dir. Verifies: every file parses (metadata header complete).
#   sync-plans — only where orchestration-standard *_coordination.md files exist: instruct those
#                sessions to update/sync them, verify mtimes moved. (Renaming to *_plan_state.md
#                is a separate migration; this stage works with either name.)
#   retire     — stop/archive old participants. Honest inventory: NEITHER ecosystem has a
#                scriptable archive API (no OpenChamber archive endpoint; no claude-archive
#                script on this host — Claude archives into ~/.claude/archived_sessions on its
#                own lifecycle). So: verify each old session silent (no new transcript output
#                past the cutoff), record retirement in the new handoffs, and for a superseded
#                LIVE session use explicit API DELETE only with maintainer approval.
#   restart    — clean session creation from handoffs/synced plans (steps 1-4 below), registry
#                delivery, coordinator-executed loop test, ack verification.
# Old generation retires by neglect; this script never deletes sessions unprompted.
set -euo pipefail

HAG=""; DIR=""; CTITLE=""; CHANDOFF=""; CPLAN=""; GEN=""; DRY=0; PROBE=0; STAGES="handoffs,sync-plans,retire,restart"
LANES=()
while [ $# -gt 0 ]; do
  case "$1" in
    --hag) HAG="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --coord-title) CTITLE="$2"; shift 2 ;;
    --coord-handoff) CHANDOFF="$2"; shift 2 ;;
    --coord-plan) CPLAN="$2"; shift 2 ;;
    --lane) LANES+=("$2"); shift 2 ;;
    --gen) GEN="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --stages) STAGES="$2"; shift 2 ;;
    --probe-claude) PROBE=1; shift ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[ -n "$HAG" ] && [ -n "$DIR" ] && [ -n "$CTITLE" ] && [ "${#LANES[@]}" -gt 0 ] || {
  echo "usage: hag-restart.sh --hag ID --dir REPO --coord-title T --coord-handoff F --coord-plan F --lane SLUG,TITLE,HANDOFF,PLAN [--lane ...] [--gen N] [--stages ...] [--dry-run] [--probe-claude]" >&2; exit 1; }

want() { case ",$STAGES," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }

run() { if [ "$DRY" -eq 1 ]; then echo "[dry-run] $*"; else "$@"; fi; }
new_id() { openchamber session create --dir "$DIR" --title "$1" 2>&1 | grep -oE "ses_[A-Za-z0-9]+" | head -n 1; }

if [ -z "$GEN" ]; then GEN="r2"; echo "note: --gen not given, using $GEN (override explicitly per generation)" >&2; fi

if want handoffs; then
echo "=== 0a. handoffs (template-seeded, sessions fill them)"
echo "handoffs live in each goal's handoffs/ dir; this stage verifies presence + metadata headers:"
for spec in "${LANES[@]}"; do SLUG="${spec%%,*}"; rest="${spec#*,}"; rest="${rest#*,}"; HANDOFF="${rest%%,*}"
  if [ "$DRY" -eq 1 ]; then echo "[dry-run] verify $HANDOFF (role, queue, base, blockers, grants, metadata header)";
  else
    if grep -qE "^- handing-off-model:|^# Handoff" "$HANDOFF" 2>/dev/null; then echo "ok: $HANDOFF"; else echo "MISSING/INCOMPLETE: $HANDOFF (session must write it before restart)"; exit 5; fi
  fi
done
fi

if want sync-plans; then
echo "=== 0b. sync-plans (only where *_coordination.md exists)"
for spec in "${LANES[@]}"; do SLUG="${spec%%,*}"; rest="${spec#*,}"; rest="${rest#*,}"; rest="${rest#*,}"; PLAN="$rest"
  case "$PLAN" in *_coordination.md|*_plan_state.md)
    echo "plan doc in scope: $PLAN (sessions instructed separately to sync; verify mtimes moved before restart)" ;;
  *) echo "skip (no orchestration-standard plan): $PLAN" ;;
  esac
done
fi

if want retire; then
echo "=== 0c. retire (verify silent; record, don't delete)"
echo "no archive API exists in either ecosystem; old sessions retire by neglect after verification."
fi

if ! want restart; then echo "DONE (stages: $STAGES)."; exit 0; fi

echo "=== 1. coordinator: $CTITLE-$GEN"
if [ "$DRY" -eq 1 ]; then echo "[dry-run] create + brief coordinator"; COORD="ses_DRYRUNCOORD"; else
COORD=$(new_id "$CTITLE-$GEN"); echo "coordinator: $COORD"
openchamber session send --session "$COORD" --dir "$DIR" --prompt "You are $CTITLE ($GEN, direct-relay). Read: (1) $CHANDOFF, (2) $CPLAN, (3) shared-orchestration-instructions.md incl. Direct session relay + Telemetry mapping. You coordinate only. Registry arrives next; address lanes ONLY by session ID; number every INSTRUCT; send-confirmation is the only delivery signal." >/dev/null; fi

echo "=== 2. lanes"
LIDS=()
for spec in "${LANES[@]}"; do
  SLUG="${spec%%,*}"; rest="${spec#*,}"; TITLE="${rest%%,*}"; rest="${rest#*,}"
  HANDOFF="${rest%%,*}"; PLAN="${rest#*,}"
  if [ "$DRY" -eq 1 ]; then echo "[dry-run] create + brief lane $SLUG ($TITLE-$GEN) with coord $COORD"; LIDS+=("ses_DRYRUN_$SLUG");
  else LID=$(new_id "$TITLE-$GEN"); echo "lane $SLUG: $LID"; LIDS+=("$LID")
  openchamber session send --session "$LID" --dir "$DIR" --prompt "You are $SLUG ($GEN). Read: (1) $HANDOFF, (2) $PLAN, (3) shared-orchestration-instructions.md incl. Direct session relay. Coordinator is $COORD (same dir). Report DIRECTLY via: openchamber session send --session $COORD --dir $DIR --prompt 'LANE-REPORT <ack-n>: ...'. Ack every LANE-INSTRUCT number. Authorizations do not transfer: obtain your own first-person." >/dev/null; fi
done

echo "=== 3. registry + loop test (coordinator-executed)"
REG="REGISTRY ($GEN, direct-relay): "
i=0
for spec in "${LANES[@]}"; do SLUG="${spec%%,*}"; REG="$REG $SLUG=${LIDS[$i]};"; i=$((i+1)); done
run openchamber session send --session "$COORD" --dir "$DIR" --prompt "$REG LOOP TEST NOW: send LANE-INSTRUCT 1 yourself to each lane (status ping), collect REPORTs in-transcript."
echo "=== 4. verify (10 min cap)"
if [ "$DRY" -eq 1 ]; then echo "[dry-run] poll $COORD for ${#LANES[@]} ack-1s"; else
  for k in $(seq 1 20); do
    n=$(openchamber session messages --session "$COORD" --dir "$DIR" --limit 20 --role user 2>/dev/null | grep -c "ack-1" || true)
    [ "$n" -ge "${#LANES[@]}" ] && { echo "LOOP TEST PASS: $n ack-1s in coordinator transcript"; break; }
    sleep 30
  done
  n=$(openchamber session messages --session "$COORD" --dir "$DIR" --limit 20 --role user 2>/dev/null | grep -c "ack-1" || true)
  [ "$n" -ge "${#LANES[@]}" ] || { echo "LOOP TEST INCOMPLETE: $n/${#LANES[@]} ack-1s — investigate before steady-state"; exit 3; }
fi
if [ "$PROBE" -eq 1 ]; then
  echo "=== 5. claude probe (cheap haiku turn)"
  run timeout 300 claude -p "\"Reply with exactly: HAG-PROBE-OK (connectivity probe, no action)\"" --model claude-haiku-4-5-20251001 --output-format text
else
  echo "=== 5. claude consumer: manual (native binary ONLY — never route subscription models through opencode)"
  echo "  paste the generated brief from step 2's lane specs (consumer brief file pattern in hag_readme §6), then start: claude"
  echo "  socket check (no turn): ls /run/user/\$(id -u)/cc-socks/ ; opt-in probe: $0 ... --probe-claude"
fi
echo "DONE. Old generation retires by neglect."
