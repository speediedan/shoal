#!/usr/bin/env python3
"""Brief builders: the exact first prompt each restarted/started session gets.

One function per addressee. The coordinator brief and the opencode-lane brief
preserve the hag-restart.sh language verbatim (proven loop-test path); the
consumer brief follows hag_readme §6. Flattened mode (no coordinator role):
lanes are briefed with the shared-channel variant — same queues, same ack
discipline, reports addressed to the channel instead of a coordinator id.
"""
from __future__ import annotations

from roles import HagSpec, Role


def coordinator_brief(role: Role, spec: HagSpec) -> str:
    mode = "direct-relay" if not spec.flattened else "shared-channel"
    return (
        f"You are {role.title} ({spec.gen}, {mode}). Read: (1) {role.handoff}, "
        f"(2) {role.plan}, (3) shared-orchestration-instructions.md incl. "
        f"Direct session relay + Telemetry mapping. You coordinate only. "
        f"Registry arrives next; address lanes ONLY by session ID; number "
        f"every INSTRUCT; send-confirmation is the only delivery signal.")


def lane_brief(role: Role, spec: HagSpec, coord_id: str = "") -> str:
    if spec.flattened or not coord_id:
        return (
            f"You are {role.slug} ({spec.gen}, shared-channel, flattened — no "
            f"coordinator). Read: (1) {role.handoff}, (2) {role.plan}, "
            f"(3) shared-orchestration-instructions.md incl. queue + decline + "
            f"telemetry sections. Your queue file is {role.queue or '(none)'}: "
            f"claim eligible items first-claim-wins, report completions to the "
            f"shared channel with the item id + base + links. Contested claims "
            f"go to agent vote per the proposal §5 mechanism. Authorizations "
            f"do not transfer: obtain your own first-person.")
    return (
        f"You are {role.slug} ({spec.gen}). Read: (1) {role.handoff}, "
        f"(2) {role.plan}, (3) shared-orchestration-instructions.md incl. "
        f"Direct session relay. Coordinator is {coord_id} (same dir). Report "
        f"DIRECTLY via: openchamber session send --session {coord_id} --dir "
        f"{spec.dir} --prompt 'LANE-REPORT <ack-n>: ...'. Ack every "
        f"LANE-INSTRUCT number. Authorizations do not transfer: obtain your "
        f"own first-person.")


def consumer_brief(role: Role, spec: HagSpec) -> str:
    """Cross-ecosystem consumer first prompt (claude/codex). Paste or launch with."""
    return (
        f"You are {role.slug} in HAG {spec.hag} ({spec.gen}). Read: "
        f"(1) {role.handoff}, (2) {role.queue}, "
        f"(3) the shared orchestration instructions (queue + decline + "
        f"telemetry sections). Rules: QUEUE is coordinator-owned, read-only "
        f"for you; STATUS is yours (state, active-issue+base, "
        f"waiting-on+holder, updated-utc, last-done with links, declined: "
        f"lines as needed). Stand up a background watch on the QUEUE section "
        f"keyed to sidecar /tmp/{spec.hag}/hag-watch-{spec.hag}-{role.slug}.state "
        f"(owning session ID inside); wake only on unseen eligible items. "
        f"Grants are first-person: ask the maintainer directly with "
        f"decline/hold options. Confirm with QUEUE item count, sidecar "
        f"echoed, loop alive.")


def grants_block(role: Role) -> str:
    if not role.grants:
        return ""
    items = "\n".join(f"- {g}" for g in role.grants)
    return (
        "\nGRANTS NEEDED — before starting any work, ask the maintainer "
        "directly (first-person), all items in ONE question with a "
        "grant / deny option on each (this ask is the only sanctioned "
        "interactive step of the restart):\n" + items)


def brief_for(role: Role, spec: HagSpec, coord_id: str = "") -> str:
    tick = (" Tick your heartbeat on every REPORT/STATUS update: "
            "`python3 project_admin/shared_admin_scripts/hag/hag.py heartbeat "
            f"--hag {spec.hag} --touch {role.slug}`.")
    if role.coordinator:
        return coordinator_brief(role, spec) + tick + grants_block(role)
    if role.ecosystem == "opencode":
        return lane_brief(role, spec, coord_id) + tick + grants_block(role)
    return consumer_brief(role, spec) + tick + grants_block(role)
