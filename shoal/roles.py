#!/usr/bin/env python3
"""HAG role/spec model.

Flatten-ready by construction: a HAG is a set of roles, and "coordinator" is
one boolean flag on a role — not a separate code path. Coordinator-only steps
(registry delivery, coordinator-executed loop test, coordinator-directed
watchers) are conditional on *some role carrying the flag*. Removing the flag
from every role yields the flattened 5.k shape: the same brief/watch/queue
machinery, with STALL/loop-test traffic addressed to the shared channel
instead of a coordinator session. No rewrite required at the pivot, only a
spec edit.
"""
from __future__ import annotations

import dataclasses
import json
import os

ECOSYSTEMS = ("opencode", "claude", "codex")

HAG_ROOT = "/home/speediedan/repos/distributed-insight/.agents/hags"


@dataclasses.dataclass
class Role:
    slug: str
    ecosystem: str = "opencode"  # opencode | claude | codex
    title: str = ""              # session title stem (generation suffix appended)
    doc: str = ""                # absolute path to THE start document (plan_state.md;
        # mission + current state + grants-needed + next). One doc per role;
        # handoffs/ dirs hold the immutable restart record, never the start doc.
    queue: str = ""              # absolute queue file (consumer roles)
    coordinator: bool = False    # exactly one role carries this pre-flattening
    session: str = ""            # live session id once started
    model: str = ""              # native model id ("" = harness default)
    effort: str = ""             # agent-neutral pace/depth knob ("" = default);
        # honored natively where the harness supports it, briefed otherwise.
    grants: list[str] = dataclasses.field(default_factory=list)  # extracted
        # from the doc's grants-needed section at brief time, never CLI.

    def validate(self) -> list[str]:
        errs = []
        if not self.slug:
            errs.append("role missing slug")
        if self.ecosystem not in ECOSYSTEMS:
            errs.append(f"role {self.slug}: unknown ecosystem {self.ecosystem!r}")
        if not self.title:
            errs.append(f"role {self.slug}: missing title stem")
        if not self.doc:
            errs.append(f"role {self.slug}: missing start doc (plan_state.md)")
        return errs


@dataclasses.dataclass
class HagSpec:
    hag: str
    dir: str = ""   # repo dir sessions run in
    gen: str = ""   # generation suffix, e.g. r3
    roles: list[Role] = dataclasses.field(default_factory=list)

    def validate(self) -> list[str]:
        errs = []
        if not self.hag:
            errs.append("spec missing hag id")
        if not self.dir:
            errs.append("spec missing dir")
        if not self.roles:
            errs.append("spec has no roles")
        coords = [r.slug for r in self.roles if r.coordinator]
        if len(coords) > 1:
            errs.append(f"multiple coordinator roles: {coords}")
        for r in self.roles:
            errs.extend(r.validate())
        return errs

    @property
    def coordinator(self) -> Role | None:
        for r in self.roles:
            if r.coordinator:
                return r
        return None

    @property
    def flattened(self) -> bool:
        """True when no role carries the coordinator flag (5.k shape)."""
        return self.coordinator is None

    def to_dict(self) -> dict:
        return {"hag": self.hag, "dir": self.dir, "gen": self.gen,
                "roles": [dataclasses.asdict(r) for r in self.roles]}

    @staticmethod
    def from_dict(d: dict) -> "HagSpec":
        return HagSpec(hag=d.get("hag", ""), dir=d.get("dir", ""),
                       gen=d.get("gen", ""),
                       roles=[Role(**r) for r in d.get("roles", [])])

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    @staticmethod
    def load(path: str) -> "HagSpec":
        with open(path) as fh:
            return HagSpec.from_dict(json.load(fh))


def default_spec_path(hag: str) -> str:
    return os.path.join(HAG_ROOT, hag, "hag.json")
