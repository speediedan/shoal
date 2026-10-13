#!/usr/bin/env python3
"""Brief builders: the exact first prompt each started session gets.

ONE brief shape for every role (`ledger_brief`, 2026-09-20; flattened only
since 2026-09-24), with the work-loop variant (`workwake_brief`) under
features.workwake and the wake relay's own brief (the `wake-relay` role). Every brief points at the role's single start
document (plan_state.md) plus the shared instructions; grants-needed items
are extracted from that document at brief time, so the doc is the only place
grants are configured. Model/effort ride along as launch config. Every brief
names ONE state primitive (`shoal transition`) so heartbeat, wait proof and
telemetry cannot be done separately and forgotten separately.
"""
from __future__ import annotations

import os
import re

import config
from layout import group_dir
from ledger import queue_next_max
from roles import ShoalSpec, Role, is_wake_relay, state_dir

SHOAL_CLI = config.cli()  # the command line every brief tells a session to run


def pid_proof(pidfile: str, sidecar: bool = False) -> str:
    """How a session proves a watcher pid: two separate commands, never `$(...)`. A relay in
    manual permission mode combined them as `ps -p $(cat <pidfile>)`, and command substitution
    always needs the user's approval there (measured 2026-09-28)."""
    read = (f"`cat {pidfile}` and take the number on its `pid=` line" if sidecar
            else f"`cat {pidfile}` to read the pid")
    return (f"prove the pid with TWO separate commands: first {read}, then `ps -p <that number>`; never use "
            f"$(...) command substitution (it always needs a permission prompt)")


def watch_sidecar(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), f"ledger-watch-{slug}.state")


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


def state_block(role: Role, spec: ShoalSpec) -> str:
    return (
        f"\nSTATE PRIMITIVE (mandatory, one command per row-state change): "
        f"`{SHOAL_CLI} transition --shoal {spec.sid} --slug {role.slug} --to active|idle|paused|offline` "
        f"or `--to waiting --sub <local-ci|remote-ci|user-auth|user-design|session-blocked|queue-empty> "
        f"--holder '<who/what clears it>' --rescan-min <N> [--monitor '<armed watch>']`. "
        f"It ticks your heartbeat, proves the wait (stale past rescan = STALL-SUSPECT), and "
        f"emits telemetry in one step; re-run it when you re-verify a wait. Going quiet "
        f"without a transition is the stall.")


def relay_brief(role: Role, spec: ShoalSpec) -> str:
    """The Claude wake relay (architecture doc §6): not a worker; one waiter, one tool."""
    sid = spec.sid
    return (
        f"You are {role.slug} ({role.tag}) in shoal {sid}: the wake relay for Claude roles whose own watcher "
        f"is dead. You do no project work, claim nothing, and message nobody on your own initiative. "
        f"LOOP: run `{SHOAL_CLI} relay-watch --shoal {sid}` with the Bash tool, run_in_background=true and "
        f"timeout=7200000, on exactly that command: no trailing `&`, no nohup, and NEVER the Monitor tool (a Monitor "
        f"expires every 5-30 min and each expiry costs a turn; a background Bash command is killed at its timeout, 30 "
        f"min by default and 2 h at most, so always pass the maximum: a kill is a wake, re-arm). "
        f"When it exits it prints "
        f"`RELAY-WAKE to=<session name> session=<id> :: <text>` lines: for EACH line call SendMessage with "
        f"to=<session name> and message=<text> verbatim (use ListAgents to add the [ref] only when two sessions "
        f"share the name; prefer the one whose session id starts with <id>), then re-arm relay-watch, then end "
        f"the turn with one line: `relayed N`. Never transition yourself offline and never kill relay-watch; only "
        f"the operator stops the relay. Never add commentary to a relayed message. Keep your context "
        f"small: no other reading. Confirm arming: {pid_proof(os.path.join(state_dir(sid), 'relay-watch.pid'))}.")


def arming(role: Role, spec: ShoalSpec) -> tuple[str, str]:
    """(--wake value, how to arm it) for a role's ledger-watch. opencode: a detached watcher that sends
    each wake. Claude in a shoal with a wake relay: a detached watcher whose wakes the relay delivers as
    messages, so no lane holds a background command (the harness kills those at 2 h at most, a turn per
    kill; maintainer-approved 2026-10-13). Claude without a relay: a background Bash command (exit)."""
    sid = spec.sid
    detached = ("setsid-detached (`setsid nohup <cmd> >>/tmp/" + sid + "/ledger-watch-" + role.slug
                + ".log 2>&1 < /dev/null &`, run once with the Bash tool, NOT as a background command and never "
                "the Monitor tool)")
    if role.ecosystem == "opencode":
        return "send:<your-session-id>", (detached + "; it sends each wake to your session id between your turns "
                                          "and keeps running, so it needs no re-arm after a wake (re-arming while it "
                                          "runs prints ALREADY ARMED); re-arm only if `next` or health says it is dead")
    if (getattr(spec, "features", {}) or {}).get("relay"):
        return "relay", (detached + "; between your turns the wake relay delivers each wake to you as a message, and "
                         "the watcher keeps running, so it needs no re-arm after a wake and is never killed at a "
                         "time limit (re-arming while it runs prints ALREADY ARMED); re-arm only if `next` or health "
                         "says it is dead")
    return "exit", ("with the Bash tool, run_in_background=true and timeout=7200000 (the 2 h maximum: the default "
                    "30 min kill costs you a turn every 30 min; no trailing `&`, never the Monitor tool, which expires "
                    "and costs a turn per expiry): it re-invokes you when it exits or is killed at that timeout, and a "
                    "kill is a wake: re-arm")


def workwake_brief(role: Role, spec: ShoalSpec) -> str:
    """The work-loop brief (architecture doc §3): wake on work only, two verbs per boundary."""
    sid = spec.sid
    feats = getattr(spec, "features", {}) or {}
    wake, how_arm = arming(role, spec)
    arm = (f"{SHOAL_CLI} ledger-watch --shoal {sid} --slug {role.slug} --wake {wake} "
           f"--session <your-full-session-id> --interval 120")
    fill = {"self": "You fill your own queue: from offers first (by prio), then your digest/board candidates.",
            "assigned": "You work only offers addressed to you (or `*`); you never pop the board.",
            "none": "You never claim."}.get(role.fill, "")
    if getattr(role, "aux", False):
        fill += (" You are the RESIDUAL lane (aux): your candidates are the free workable items no other active "
                 "lane's brief covers, unblockers of their work first; `next` prints an AUX line naming the lanes "
                 "you stay out of, and it changes as the roster changes. Pass an item that belongs to a lane with "
                 "`--pass <item>:out-of-lane:suggest=<lane>`.")
    return (
        f"FIRST, load the `shoal-protocol` skill and follow it: it is the protocol this brief summarizes. "
        f"You are {role.label} in shoal {sid} (incarnation {spec.incarnation}). Your Lane row label in every "
        f"posted block is exactly `{role.label}`: the base name, then your session tag; never an incarnation "
        f"number. Read: (1) {role.doc} "
        f"(your plan_state start doc: mission, current state, decisions, grants-needed, next; keep it current), "
        f"(2) the shoal-protocol skill's references `ledger.md`, `work-loop.md` and "
        f"`watchers-and-waits.md`. Your job is to work through a QUEUE: one active item and up to {queue_next_max(spec)} next items, "
        f"declared by you in the ledger. {fill} "
        f"AT EVERY BOUNDARY (a wake, or finishing/blocking an item) do exactly two things: READ `{SHOAL_CLI} next "
        f"--shoal {sid} --slug {role.slug}` (queue, offers, candidates with scores and excerpts, mail, waits, watcher) "
        f"and WRITE ONE `{SHOAL_CLI} queue --shoal {sid} --slug {role.slug} --active <repo#n:verb> --next <a,b,c> "
        f"[--done <item> --url <evidence>] [--pass <item>:<class>[:suggest=<role>]] [--wait <sub> --holder <who> "
        f"--target <lease:..|job:..|gh-run:..|gh-pr:..|az-build:..>] | --idle`. The write claims the active item, "
        f"records done/passes/posture and your new queue version in one commit. Every pop BACKFILLS next to {queue_next_max(spec)} "
        f"(or give --reason why fewer). Then keep working the active item in the SAME turn. EVERY TURN ENDS IN A "
        f"SLEEP POSTURE: the last write of a turn is `queue --wait ..` (waiter armed first) or `queue --idle`, and the "
        f"Lane row you post says that posture, never `active`. If you stop while your posture is still active, the "
        f"health quorum wakes you to continue about 5 minutes later, which is a wasted turn. Sleep only when "
        f"WAITING (arm the waiter first, give --target so the health quorum can verify it) or when your queue is "
        f"empty (--idle). EVERY WAIT EXPIRES: a user-auth/user-design wait exists only together with a "
        f"first-person prompted question you raised THIS turn (question tool / AskUserQuestion, options plus "
        f"decline/hold) and recorded with `queue --ask \"<question>\"`; a session-blocked wait names its blocker "
        f"with `--blocked-on <role>:<repo#n>`; when `next` prints RESCAN EXPIRED, re-verify and re-raise or pivot "
        f"to the next unblocked item. When `next` prints BLOCKING, another role waits on you: schedule that item or "
        f"pass it with a reason. HELD-UNSCHEDULED claims are scheduled or released (`release --kind handoff`), "
        f"never hoarded. WAKES ARE WORK-ONLY: new work while you are idle, a change to your active item, a wait "
        f"you armed, or a health wake. Mail from other roles never wakes you; read it in `next` and act at the "
        f"boundary. To get another role to DO something, offer it as work; do not message for it. "
        f"WATCH (mandatory, first act after reading): arm `{arm}` {how_arm}; run it once with `--test` first and "
        f"confirm the test wake arrived. Re-arm after every wake and {pid_proof(watch_sidecar(sid, role.slug), sidecar=True)}. "
        f"USER-OFFLINE: when `next` prints "
        f"USER OFFLINE, never wait on the maintainer: record each blocking ask with `queue --ask`, stack PRs "
        f"(gh-stack) or take the next unblocked item; you are woken when they return. When they RETURN (or "
        f"whenever `next` prints ASKS TO RAISE): re-verify every recorded ask, drop the ones that no longer apply "
        f"(`queue --drop-ask <n> --drop-reason <why>`), build the sets with `{SHOAL_CLI} asks --shoal {sid} --slug "
        f"{role.slug} --question-json` (live PR labels: never describe a PR from memory), and raise EVERY set, "
        f"one question-tool call per set of at most four; remove answered asks with `queue --answered <n>`. "
        f"BEFORE ANY PROMPTED QUESTION (merge asks included): run `next` unpiped (never through head/grep: MAIL "
        f"and banners print first); if it says USER OFFLINE, record the ask with `queue --ask` instead. The "
        f"question tool blocks your turn, so record the ask and a user-auth wait first. "
        f"PR SIZING: every PR attempts to close at least two issues (find the second before the first push; a "
        f"single-issue PR says why in its body); slices of one issue ('Advances #n') count as single-issue, and "
        f"slices that share a change or files belong in ONE PR; separable work goes in a gh-stack with every "
        f"gated-path change in the bottom layer, because only a PR based on the default branch queues a gated "
        f"build. "
        f"{h4_disciplines(role, spec)}Grants are first-person: ask the maintainer directly with decline/hold "
        f"options; authorizations never transfer. Confirm arming with: `next` output echoed, watcher pid, test "
        f"wake seen, first `queue` version written.")


def ledger_brief(role: Role, spec: ShoalSpec) -> str:
    """ONE brief shape for every role in ledger mode (5.j/5.k): the same
    verbs, the same watcher, the same wake discipline, whatever the
    ecosystem. Differences are data: fill mode, permissions, wake adapter."""
    sid = spec.sid
    feats0 = getattr(spec, "features", {}) or {}
    if is_wake_relay(role):
        return relay_brief(role, spec)
    if feats0.get("workwake"):
        return workwake_brief(role, spec)
    wake, how_arm = arming(role, spec)
    arm = (f"{SHOAL_CLI} ledger-watch --shoal {sid} --slug {role.slug} --wake {wake} "
           f"--session <your-full-session-id> --interval 120")
    feats = getattr(spec, "features", {}) or {}
    if role.fill == "self" and feats.get("digest"):
        fill = (f"FILL: self, via the shared board digest. When your queue is empty and you hold nothing, the "
                f"watcher delivers your top digest candidates ranked against your working context (`{SHOAL_CLI} "
                f"context show --shoal {sid} --slug {role.slug}`), each with score, why-tokens, blockers and a body "
                f"excerpt: decide from those, open an issue only when the excerpt is not enough. Record ALL "
                f"decisions of one wake in one call: `{SHOAL_CLI} triage --shoal {sid} --slug {role.slug} "
                f"[--claim repo#n:verb] [--pass repo#n:CLASS[:suggest=<role>][:note=..]]...` with CLASS one of "
                f"out-of-lane|blocked|needs-maintainer|needs-hardware|duplicate-or-stale; name `suggest=` "
                f"whenever you know whose lane it is. Full ranked list: `{SHOAL_CLI} digest --shoal {sid} --slug "
                f"{role.slug}`. user offers ALWAYS outrank board candidates.")
    elif role.fill == "self":
        fill = (f"FILL: self. When your queue is empty and you hold nothing, the watcher offers the top "
                f"board candidates (priority table in board order, minus what the ledger shows taken or "
                f"declined by you). Claim the top one that fits your lane (`claim`), or decline it with a "
                f"reason (`release --kind decline --reason .. --retry ..`) so it never re-wakes you; "
                f"user offers ALWAYS outrank board candidates.")
    elif role.fill == "assigned":
        fill = ("FILL: assigned. You work only offers addressed to you or to `*`; you never pop the board. "
                "An empty queue is `transition --to waiting --sub queue-empty --holder user` plus one "
                "`msg --to user` naming the next item you would take, then wait for the watcher.")
    else:
        fill = "FILL: none. You never claim; you observe."
    relay = ("Nobody relays for you: status is the ledger; the maintainer reads "
             f"`shoal show --shoal {sid}`. Stall-suspect msgs come to YOU: answer by re-verifying the wait "
             "(transition) or acting.")
    return (
        f"You are {role.slug} ({role.tag}, incarnation {spec.display_gen}, ledger mode, flattened) in shoal "
        f"{sid}. Read: (1) {role.doc} (your plan_state "
        f"start doc: mission, current state, decisions, grants-needed, next; keep it current), (2) the "
        f"shoal-protocol skill's reference `ledger.md`. ALL traffic is the ledger "
        f"(git repo {spec.ledger.get('repo', '')}, clone {spec.ledger_dir}, {os.path.relpath(group_dir(spec.ledger_dir, sid), spec.ledger_dir)}/ledger/<writer>.jsonl, "
        f"one file per writer, append-only) through `{SHOAL_CLI}` verbs: `show --shoal {sid} --slug {role.slug} "
        f"[--board]` (your view), `claim --slug {role.slug} --item repo#n:verb --base repo@sha`, "
        f"`done --slug {role.slug} --item .. --url ..`, `release --slug {role.slug} --item .. --kind "
        f"decline|blocked|handoff --reason ..`, `msg --by {role.slug} --to <slug|*|user> --text .. [--ack]` "
        f"(answer an ack request with `--re <id> --kind ack`). No direct session sends to peers, ever. "
        f"{fill} {relay} "
        f"WATCH (mandatory, first act after reading): arm `{arm}` {how_arm}. It wakes you ONLY for offers "
        f"to you, msgs to you or `*`, a lost/contested claim, a withdrawal, or board candidates when "
        f"self-filling; each event wakes at most once. Run it once with `--test` first and confirm "
        f"LEDGER-WAKE-TEST arrived. On EVERY wake (this watcher or any CI/lease watch): run `show`, act, "
        f"`transition`, re-arm — the ledger is read only on wakes, never polled by you. CI/lease waits arm "
        f"agent_ci_watch.sh (or a harness Monitor) exactly as before, with your own session id. Register EVERY "
        f"armed watch for daemon validation: `{SHOAL_CLI} watch-register --shoal {sid} --slug {role.slug} "
        f"--kind ci|ledger|lease --target <what> --pid <pid> --session <your-full-session-id> "
        f"[--rescan-min N]` (the daemon pages you if the pid dies or the rescan lapses +50%). Keep a "
        f"`## Running monitors` section in your plan_state current (exact re-arm lines). Grants are "
        f"first-person: ask the maintainer directly with decline/hold options; authorizations never transfer. "
        f"{h4_disciplines(role, spec)}"
        f"Confirm arming with: ledger view echoed (item counts), watcher pid, test wake seen; "
        f"{pid_proof(watch_sidecar(sid, role.slug), sidecar=True)}.")


def h4_disciplines(role: Role, spec: ShoalSpec) -> str:
    """Watcher placement and recovery (architecture doc §5.3, §6); empty unless opted in."""
    feats = getattr(spec, "features", {}) or {}
    if not (feats.get("jobs") or feats.get("workwake")):
        return ""
    sid = spec.sid
    out = []
    if feats.get("jobs"):
        out.append(
            f"JOBS: any local work that would hurt to lose (test suites, GPU renders, A/B runs, anything "
            f"over a few minutes) runs as a job unit, never as a harness background command: `{SHOAL_CLI} job run "
            f"--shoal {sid} --slug {role.slug} --name <short> [--memory-high 24G] -- <cmd>` (wrap GPU work in "
            f"gpu_lease.sh inside <cmd>), then follow it with `{SHOAL_CLI} job wait --shoal {sid} --slug {role.slug} "
            f"--name <short>` as your background waiter. The job survives your session and any reap; the "
            f"waiter is safe to re-arm and prints one JOB-VERDICT line.")
    out.append(
        "A KILL IS A WAKE: a watcher notification with status killed (any reason) is handled exactly like a "
        f"wake: show, act, re-arm, then {pid_proof(watch_sidecar(sid, role.slug), sidecar=True)}, in the same "
        "turn. Tally kills from notification status only, never from exit counts.")
    return " ".join(out) + " "


def brief_for(role: Role, spec: ShoalSpec) -> str:
    body = ledger_brief(role, spec)
    return body + launch_config(role) + state_block(role, spec) + grants_block(role)


if __name__ == "__main__":
    import sys
    print("\n".join(f"- {g}" for g in extract_grants(sys.argv[1])))
