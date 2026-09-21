#!/usr/bin/env python3
"""Brief builders: the exact first prompt each started session gets.

Ledger mode (2026-09-20) uses ONE brief for every role (`ledger_brief`);
the coordinator/lane/consumer briefs below are the legacy shapes kept for
specs without a ledger.

One function per addressee. Every brief points at the role's single start
document (plan_state.md) plus the shared instructions; grants-needed items
are extracted from that document at brief time, so the doc is the only place
grants are configured. Model/effort ride along as launch config. Every brief
names ONE state primitive (`hag.py transition`) so heartbeat, wait proof and
telemetry cannot be done separately and forgotten separately.
"""
from __future__ import annotations

import os
import re

from roles import HagSpec, Role

HAGPY = "python3 /home/speediedan/repos/distributed-insight/project_admin/shared_admin_scripts/hag/hag.py"


def extract_grants(doc: str) -> list[str]:
    """Bullets under the doc's grants-needed heading (excludes Nones)."""
    try:
        text = open(doc).read()
    except OSError:
        return []
    m = re.search(r"^#+\s*grants-needed.*$", text,
                  flags=re.M | re.I)
    if not m:
        return []
    grants = []
    for line in text[m.end():].splitlines():
        if re.match(r"^#+\s", line):
            break
        b = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if not b:
            continue
        item = b.group(1).strip()
        low = item.lower()
        if re.match(r"none\b", low) or low.startswith("upcoming:") \
                or low.startswith("pre-authorized") or low.startswith("standing:"):
            continue  # recorded, never asked: Nones, UPCOMING, PRE-AUTHORIZED, STANDING
        grants.append(item)
    return grants


def launch_config(role: Role) -> str:
    if not role.model and not role.effort:
        return ""
    return (f"\nLaunch config — model: {role.model or '(harness default)'}, "
            f"effort: {role.effort or '(default)'} (pace/depth knob; honored "
            f"natively where the harness supports it).")


def grants_block(role: Role) -> str:
    grants = extract_grants(role.doc) if role.doc else []
    if not grants:
        return ""
    items = "\n".join(f"- {g}" for g in grants)
    return (
        "\nGRANTS NEEDED — before starting any work, ask the maintainer "
        "directly (first-person), all items in ONE question with a "
        "grant / deny option on each (this ask is the only sanctioned "
        "interactive step of the restart):\n" + items)


def state_block(role: Role, spec: HagSpec) -> str:
    return (
        f"\nSTATE PRIMITIVE (mandatory, one command per row-state change): "
        f"`{HAGPY} transition --hag {spec.hag} --slug {role.slug} --to active|idle|paused|offline` "
        f"or `--to waiting --sub <local-ci|remote-ci|user-auth|user-design|session-blocked|queue-empty> "
        f"--holder '<who/what clears it>' --rescan-min <N> [--monitor '<armed watch>']`. "
        f"It ticks your heartbeat, proves the wait (stale past rescan = STALL-SUSPECT), and "
        f"emits telemetry in one step; re-run it when you re-verify a wait. Going quiet "
        f"without a transition is the stall.")


def coordinator_brief(role: Role, spec: HagSpec) -> str:
    mode = "direct-relay" if not spec.flattened else "shared-channel"
    return (
        f"You are {role.title} ({spec.display_gen}, {mode}) in HAG {spec.hag}. Read: (1) {role.doc} "
        f"(your plan_state start doc: mission, current state, grants-needed, "
        f"next), (2) shared-orchestration-instructions.md incl. Direct "
        f"session relay + Telemetry mapping. You coordinate only. "
        f"Registry arrives next; address lanes ONLY by session ID; number "
        f"every INSTRUCT; send-confirmation is the only delivery signal. "
        f"Watcher fires (QUEUE-WATCH, LANES-WATCH, PIPELINE-WATCH, STALL-SUSPECT) arrive "
        f"as user turns from systemd timers; transcribe lane transitions you learn of via "
        f"`transition --slug <lane> --session <id>` when a lane forgets.")


def lane_brief(role: Role, spec: HagSpec, coord_id: str = "") -> str:
    if spec.flattened or not coord_id:
        return (
            f"You are {role.slug} ({spec.display_gen}, shared-channel, flattened — no "
            f"coordinator) in HAG {spec.hag}. Read: (1) {role.doc} (your plan_state start doc), "
            f"(2) shared-orchestration-instructions.md incl. queue + decline + "
            f"telemetry sections. Your queue file is {role.queue or '(none)'}: "
            f"claim eligible items first-claim-wins, report completions to the "
            f"shared channel with the item id + base + links. Contested claims "
            f"go to agent vote per the proposal §5 mechanism. Authorizations "
            f"do not transfer: obtain your own first-person.")
    return (
        f"You are {role.slug} ({spec.display_gen}) in HAG {spec.hag}. Read: (1) {role.doc} "
        f"(your plan_state start doc), "
        f"(2) shared-orchestration-instructions.md incl. "
        f"Direct session relay. Coordinator is {coord_id} (same dir). Report "
        f"DIRECTLY via: openchamber session send --session {coord_id} --dir "
        f"{spec.dir} --prompt 'LANE-REPORT <ack-n>: ...'. Ack every "
        f"LANE-INSTRUCT number. CI/lease waits arm agent_ci_watch.sh with a self-send "
        f"(dry-run MONITOR-TEST first). Authorizations do not transfer: obtain your "
        f"own first-person.")


def consumer_brief(role: Role, spec: HagSpec) -> str:
    sidecar = f"/tmp/{spec.hag}/hag-watch-{spec.hag}-{role.slug}.state"
    return (
        f"You are {role.slug} in HAG {spec.hag} ({spec.display_gen}). Read: "
        f"(1) {role.doc} (your plan_state start doc — convert to the "
        f"plan_state cadence from here on: keep mission/current/decisions/"
        f"next current, grants-needed maintained), (2) {role.queue}, "
        f"(3) the shared orchestration instructions (queue + decline + "
        f"telemetry sections). Rules: QUEUE is coordinator-owned, read-only "
        f"for you; STATUS is yours (state, active-issue+base, "
        f"waiting-on+holder, updated-utc, last-done with links (max 3), declined: "
        f"lines as needed); commit STATUS edits alone (never with QUEUE lines). "
        f"Self-watch: write {sidecar} with lines "
        f"`session_id=<your full session id>`, `queue_file={role.queue}`, `queue_hash=`, "
        f"`consumed=`, `declined=`; then run in the background "
        f"`{HAGPY} queue-watch --sidecar {sidecar} --interval 600` — it exits (waking you) "
        f"only on unseen eligible items; re-arm after each wake; add consumed ids to the "
        f"sidecar. Grants are first-person: ask the maintainer directly with "
        f"decline/hold options. Confirm with QUEUE item count, sidecar echoed, loop alive.")


def ledger_brief(role: Role, spec: HagSpec) -> str:
    """ONE brief shape for every role in ledger mode (5.j/5.k): the same
    verbs, the same watcher, the same wake discipline, whatever the
    ecosystem. Differences are data: fill mode, permissions, wake adapter."""
    hag = spec.hag
    wake = "exit" if role.ecosystem != "opencode" else "send:<your-session-id>"
    arm = (f"{HAGPY} ledger-watch --hag {hag} --slug {role.slug} --wake {wake} "
           f"--session <your-full-session-id> --interval 120")
    how_arm = ("as a background command that re-invokes you when it exits (Claude: persistent "
               "Monitor / run_in_background; a killed background command is a lost wake path — re-arm)"
               if role.ecosystem != "opencode" else
               "setsid-detached (`setsid nohup <cmd> >>/tmp/" + hag + "/ledger-watch-" + role.slug
               + ".log 2>&1 < /dev/null &`); it self-sends LEDGER-WAKE to your session id and exits")
    if role.fill == "self":
        fill = (f"FILL: self. When your queue is empty and you hold nothing, the watcher offers the top "
                f"board candidates (priority table in board order, minus what the ledger shows taken or "
                f"declined by you). Claim the top one that fits your lane (`claim`), or decline it with a "
                f"reason (`release --kind decline --reason .. --retry ..`) so it never re-wakes you; "
                f"user/filler offers ALWAYS outrank board candidates.")
    elif role.fill == "assigned":
        fill = ("FILL: assigned. You work only offers addressed to you or to `*`; you never pop the board. "
                "An empty queue is `transition --to waiting --sub queue-empty --holder user` plus one "
                "`msg --to user` naming the next item you would take, then wait for the watcher.")
    else:
        fill = "FILL: none. You never claim; you fill and observe."
    fills = ("" if not role.fills_others else
             " You hold can_fill_others: you may `offer` items to other roles and `withdraw` them; a "
             "seeded queue at start is your duty (an empty queue is a verdict, never an omission).")
    coord = ("" if not spec.flattened else
             "There is NO coordinator: nobody relays for you. Status is the ledger; the maintainer reads "
             f"`hag.py show --hag {hag}`. Stall-suspect msgs come to YOU: answer by re-verifying the wait "
             "(transition) or acting.")
    return (
        f"You are {role.slug} ({role.tag}, incarnation {spec.display_gen}, ledger mode"
        f"{', flattened' if spec.flattened else ''}) in HAG {hag}. Read: (1) {role.doc} (your plan_state "
        f"start doc: mission, current state, decisions, grants-needed, next; keep it current), (2) the shared "
        f"orchestration instructions section 'Unified ledger primitive'. ALL traffic is the ledger "
        f"(git repo {spec.ledger.get('repo', '')}, clone {spec.ledger_dir}, hags/{hag}/ledger/<writer>.jsonl, "
        f"one file per writer, append-only) through `{HAGPY}` verbs: `show --hag {hag} --slug {role.slug} "
        f"[--board]` (your view), `claim --slug {role.slug} --item repo#n:verb --base repo@sha`, "
        f"`done --slug {role.slug} --item .. --url ..`, `release --slug {role.slug} --item .. --kind "
        f"decline|blocked|handoff --reason ..`, `msg --by {role.slug} --to <slug|*|user> --text .. [--ack]` "
        f"(answer an ack request with `--re <id> --kind ack`). No direct session sends to peers, ever. "
        f"{fill}{fills} {coord} "
        f"WATCH (mandatory, first act after reading): arm `{arm}` {how_arm}. It wakes you ONLY for offers "
        f"to you, msgs to you or `*`, a lost/contested claim, a withdrawal, or board candidates when "
        f"self-filling; each event wakes at most once. Run it once with `--test` first and confirm "
        f"LEDGER-WAKE-TEST arrived. On EVERY wake (this watcher or any CI/lease watch): run `show`, act, "
        f"`transition`, re-arm — the ledger is read only on wakes, never polled by you. CI/lease waits arm "
        f"agent_ci_watch.sh (or a harness Monitor) exactly as before, with your own session id. Keep a "
        f"`## Running monitors` section in your plan_state current (exact re-arm lines). Grants are "
        f"first-person: ask the maintainer directly with decline/hold options; authorizations never transfer. "
        f"Confirm arming with: ledger view echoed (item counts), watcher pid, test wake seen.")


def brief_for(role: Role, spec: HagSpec, coord_id: str = "") -> str:
    if spec.ledger_mode:
        body = ledger_brief(role, spec)
    elif role.coordinator:
        body = coordinator_brief(role, spec)
    elif role.ecosystem == "opencode":
        body = lane_brief(role, spec, coord_id)
    else:
        body = consumer_brief(role, spec)
    return body + launch_config(role) + state_block(role, spec) + grants_block(role)


if __name__ == "__main__":
    import sys
    print("\n".join(f"- {g}" for g in extract_grants(sys.argv[1])))
