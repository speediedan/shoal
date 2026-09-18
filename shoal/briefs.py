#!/usr/bin/env python3
"""Brief builders: the exact first prompt each started session gets.

One function per addressee. Every brief points at the role's single start
document (plan_state.md) plus the shared instructions; grants-needed items
are extracted from that document at brief time, so the doc is the only place
grants are configured. Model/effort ride along as launch config.
"""
from __future__ import annotations

import os
import re

from roles import HagSpec, Role


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
        if low == "none" or low.startswith("none pending") \
                or low.startswith("upcoming:"):
            continue
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


def coordinator_brief(role: Role, spec: HagSpec) -> str:
    mode = "direct-relay" if not spec.flattened else "shared-channel"
    return (
        f"You are {role.title} ({spec.gen}, {mode}). Read: (1) {role.doc} "
        f"(your plan_state start doc: mission, current state, grants-needed, "
        f"next), (2) shared-orchestration-instructions.md incl. Direct "
        f"session relay + Telemetry mapping. You coordinate only. "
        f"Registry arrives next; address lanes ONLY by session ID; number "
        f"every INSTRUCT; send-confirmation is the only delivery signal.")


def lane_brief(role: Role, spec: HagSpec, coord_id: str = "") -> str:
    if spec.flattened or not coord_id:
        return (
            f"You are {role.slug} ({spec.gen}, shared-channel, flattened — no "
            f"coordinator). Read: (1) {role.doc} (your plan_state start doc), "
            f"(2) shared-orchestration-instructions.md incl. queue + decline + "
            f"telemetry sections. Your queue file is {role.queue or '(none)'}: "
            f"claim eligible items first-claim-wins, report completions to the "
            f"shared channel with the item id + base + links. Contested claims "
            f"go to agent vote per the proposal §5 mechanism. Authorizations "
            f"do not transfer: obtain your own first-person.")
    return (
        f"You are {role.slug} ({spec.gen}). Read: (1) {role.doc} "
        f"(your plan_state start doc), "
        f"(2) shared-orchestration-instructions.md incl. "
        f"Direct session relay. Coordinator is {coord_id} (same dir). Report "
        f"DIRECTLY via: openchamber session send --session {coord_id} --dir "
        f"{spec.dir} --prompt 'LANE-REPORT <ack-n>: ...'. Ack every "
        f"LANE-INSTRUCT number. Authorizations do not transfer: obtain your "
        f"own first-person.")


def consumer_brief(role: Role, spec: HagSpec) -> str:
    return (
        f"You are {role.slug} in HAG {spec.hag} ({spec.gen}). Read: "
        f"(1) {role.doc} (your plan_state start doc — convert to the "
        f"plan_state cadence from here on: keep mission/current/decisions/"
        f"next current, grants-needed maintained), (2) {role.queue}, "
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


def brief_for(role: Role, spec: HagSpec, coord_id: str = "") -> str:
    tick = (" Tick your heartbeat on every REPORT/STATUS update: "
            "`python3 project_admin/shared_admin_scripts/hag/hag.py heartbeat "
            f"--hag {spec.hag} --touch {role.slug}`.")
    if role.coordinator:
        return (coordinator_brief(role, spec) + launch_config(role)
                + tick + grants_block(role))
    if role.ecosystem == "opencode":
        return (lane_brief(role, spec, coord_id) + launch_config(role)
                + tick + grants_block(role))
    return (consumer_brief(role, spec) + launch_config(role)
            + tick + grants_block(role))


if __name__ == "__main__":
    import sys
    print("\n".join(f"- {g}" for g in extract_grants(sys.argv[1])))
