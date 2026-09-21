#!/usr/bin/env python3
"""HAG role/spec model.

Flattened by default (readme 5.k, 2026-09-20): a HAG is a set of roles that
all speak the same ledger primitive (ledger.py). Per role, `fill` says where
its work comes from — `self` (it pops the priority table in `sources` when
its queue is empty) or `assigned` (it works only what `user` or a filler
offers it) — and `can_fill_others` is the elevated permission a
coordinator-class role holds to offer work to other roles. The legacy
`coordinator` flag still exists as "the role that also receives the
watcher fires and holds the tasking authority"; it implies
`can_fill_others`. A spec with no coordinator flag is the default
flattened shape; setting the flag on one role is the coordinated shape.
Same machinery either way — one spec edit.

Two roots (both overridable by environment for tests and other hosts):
  HAG_DATA_ROOT  per-hag local home (unversioned bulk): <root>/<hag>/{briefs/,
                     reports/,telemetry/}. Briefs, report .md twins and events
                     live here, never in version control.
  HAG_LEDGER / ledger.dir (git repo speediedan/hag-queues):
                     hags/<hag>/{hag.json (THE canonical live spec, mutated by
                     hag.py with commit+push),ledger/}. Remote agents read this.
  HAG_ROOT       legacy versioned control plane (read-only since the ledger
                     incarnation): <root>/<hag>/{hag.json,queues/,briefs/,reports/}.
                     Resolves old specs by explicit path; new starts never write it.

A spec also records its generation history (`generations`): one entry per
generation with start/end timestamps, the live session ids, and the run pin
at start. That history is what makes per-incarnation report windows
computable instead of hand-typed.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import os
import re

ECOSYSTEMS = ("opencode", "claude", "codex")
FILL_MODES = ("self", "assigned", "none")
DEFAULT_LEDGER = {"repo": "git@github.com:speediedan/hag-queues.git",
                  "dir": os.path.expanduser("~/repos/hag-queues")}

HAG_ROOT = os.environ.get(
    "HAG_ROOT", "/home/speediedan/repos/distributed-insight/.agents/hags")
HAG_DATA_ROOT = os.environ.get(
    "HAG_DATA_ROOT", "/mnt/cache_extended/speediedan/hags")
DEFAULT_REPOS = ["speediedan/interpretune", "speediedan/it-interp-engine-adapter"]


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hag_dir(hag: str) -> str:
    return os.path.join(HAG_ROOT, hag)


def home_dir(hag: str) -> str:
    """Local per-hag home: briefs/, reports/, telemetry/ (unversioned)."""
    return os.path.join(HAG_DATA_ROOT, hag)


def data_dir(hag: str) -> str:
    return os.path.join(HAG_DATA_ROOT, hag)


def telemetry_dir(hag: str) -> str:
    return os.path.join(data_dir(hag), "telemetry")


def state_dir(hag: str) -> str:
    """Scratch state (/tmp): sidecars, heartbeats, wait files, watcher ticks."""
    return os.path.join("/tmp", hag)


@dataclasses.dataclass
class Role:
    slug: str                # stable identity (queue files, sidecars, heartbeats, telemetry)
    ecosystem: str = "opencode"  # opencode | claude | codex
    title: str = ""              # session title stem (sequence suffix appended)
    seq: int = 1                 # per-role session sequence; session tag is {slug}-r{seq}.
        # Bump ONLY this role's seq on a single-ecosystem restart; other roles keep
        # their sessions AND their seqs, so hag.json never conflates them.
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
    fill: str = "self"           # self | assigned | none. self: pops the priority
        # table when its queue is empty; assigned: works only offers addressed to
        # it (or `*`); none: never claims (a pure filler/observer role).
    can_fill_others: bool = False  # may `offer` to other roles (coordinator-class)
    scope: dict = dataclasses.field(default_factory=dict)  # self-fill filter:
        # {"repos": [...], "labels_any": [...], "labels_none": [...]}
    server: str = ""             # host the session runs on ("" = this host)

    def validate(self) -> list[str]:
        errs = []
        if not self.slug:
            errs.append("role missing slug")
        if self.seq < 1:
            errs.append(f"role {self.slug}: seq must be >= 1 (got {self.seq})")
        if self.ecosystem not in ECOSYSTEMS:
            errs.append(f"role {self.slug}: unknown ecosystem {self.ecosystem!r}")
        if not self.title:
            errs.append(f"role {self.slug}: missing title stem")
        if not self.doc:
            errs.append(f"role {self.slug}: missing start doc (plan_state.md)")
        if self.fill not in FILL_MODES:
            errs.append(f"role {self.slug}: fill must be one of {FILL_MODES} (got {self.fill!r})")
        return errs

    @property
    def fills_others(self) -> bool:
        return bool(self.can_fill_others or self.coordinator)

    @property
    def tag(self) -> str:
        """Session tag: {slug}-r{seq}. Stable slug, rolling sequence."""
        return f"{self.slug}-r{self.seq}"

    @staticmethod
    def from_dict(d: dict) -> "Role":
        known = {f.name for f in dataclasses.fields(Role)}
        d = dict(d)
        if "fill" not in d:
            # pre-2026-09-20 spec: consumers were coordinator-filled, so `assigned`
            d["fill"] = "assigned"
        if d.get("scope") is None:
            d["scope"] = {}
        return Role(**{k: v for k, v in d.items() if k in known})


@dataclasses.dataclass
class HagSpec:
    hag: str
    dir: str = ""   # repo dir sessions run in
    gen: str = ""   # legacy display string ("r1"); kept for old specs, else derived
    incarnation: int = 0  # 0-indexed HAG incarnation: THE telemetry counter.
        # Bumped on every restart that creates sessions (full or partial).
        # Display string is r{incarnation+1}, so --gen is unnecessary.
    roles: list[Role] = dataclasses.field(default_factory=list)
    repos: list[str] = dataclasses.field(default_factory=list)  # GitHub repos
        # the HAG works (reconcile ground truth); DEFAULT_REPOS when empty.
    generations: list[dict] = dataclasses.field(default_factory=list)
        # [{gen, started_utc, ended_utc, sessions{slug:id}, run{}}], oldest first
    retired_utc: str = ""  # set by `hag.py retire`; a retired HAG launches nothing
    ledger: dict = dataclasses.field(default_factory=dict)  # {"repo": url, "dir": clone}
        # empty = legacy QUEUE/STATUS queue files (pre-2026-09-20 HAGs)
    sources: list[dict] = dataclasses.field(default_factory=list)  # priority tables
        # for self-filling roles: [{"kind": "github-project", "owner", "number", ...}]

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
        if self.ledger and not self.ledger.get("dir"):
            errs.append("ledger set without a clone dir")
        for s in self.sources:
            if s.get("kind") != "github-project" or not s.get("owner") or not s.get("number"):
                errs.append(f"bad source {s} (want kind=github-project, owner, number)")
        if not self.sources and any(r.fill == "self" for r in self.roles) and self.ledger:
            errs.append("self-filling roles but no `sources` (priority table) in the spec")
        slugs = [r.slug for r in self.roles]
        if len(set(slugs)) != len(slugs):
            errs.append(f"duplicate role slugs: {slugs}")
        for r in self.roles:
            errs.extend(r.validate())
        return errs

    @property
    def display_gen(self) -> str:
        """Human generation string, derived: r{incarnation+1}. Old specs that
        stored gen explicitly keep working (migration in from_dict)."""
        if self.gen:
            return self.gen
        return f"r{self.incarnation + 1}"

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

    @property
    def effective_repos(self) -> list[str]:
        return list(self.repos) if self.repos else list(DEFAULT_REPOS)

    @property
    def ledger_mode(self) -> bool:
        """True when this HAG communicates over the ledger (5.j); False = legacy queue files."""
        return bool(self.ledger)

    @property
    def ledger_dir(self) -> str:
        return (self.ledger or {}).get("dir") or DEFAULT_LEDGER["dir"]

    @property
    def fillers(self) -> list["Role"]:
        return [r for r in self.roles if r.fills_others]

    def role(self, slug: str) -> Role | None:
        for r in self.roles:
            if r.slug == slug:
                return r
        return None

    # ---- generation history ------------------------------------------------

    def current_generation(self) -> dict | None:
        for g in reversed(self.generations):
            if g.get("gen") == self.display_gen:
                return g
        return None

    def record_generation(self, run: dict | None = None) -> dict:
        """Open (or refresh) the entry for the current incarnation with the live
        session ids AND per-role seqs (slug -> id is stable; seqs explain tags)."""
        g = self.current_generation()
        if g is None:
            g = {"gen": self.display_gen, "incarnation": self.incarnation,
                 "started_utc": utcnow_iso(), "ended_utc": "",
                 "sessions": {}, "seqs": {}, "run": run or {}}
            self.generations.append(g)
        g["incarnation"] = self.incarnation
        g["sessions"] = {r.slug: r.session for r in self.roles if r.session}
        g["seqs"] = {r.slug: r.seq for r in self.roles}
        if run and not g.get("run"):
            g["run"] = run
        return g

    def close_generation(self, gen: str = "") -> dict | None:
        gen = gen or self.display_gen
        for g in reversed(self.generations):
            if g.get("gen") == gen and not g.get("ended_utc"):
                g["ended_utc"] = utcnow_iso()
                return g
        return None

    def to_dict(self) -> dict:
        return {"hag": self.hag, "dir": self.dir, "gen": self.display_gen,
                "incarnation": self.incarnation,
                "repos": self.repos, "retired_utc": self.retired_utc,
                "ledger": self.ledger, "sources": self.sources,
                "roles": [dataclasses.asdict(r) for r in self.roles],
                "generations": self.generations}

    def snapshot(self) -> dict:
        """What gets copied into the ledger repo for remote agents: the spec
        minus generation history (which only the operator's copy maintains)."""
        d = self.to_dict()
        d.pop("generations", None)
        d["snapshot_utc"] = utcnow_iso()
        d["canonical"] = "distributed-insight:.agents/hags/<hag>/hag.json (this copy is a read-only snapshot)"
        return d

    @staticmethod
    def from_dict(d: dict) -> "HagSpec":
        gen = d.get("gen", "") or ""
        if "incarnation" in d:
            inc = int(d.get("incarnation") or 0)
        else:
            m = re.match(r"r(\d+)$", gen)
            inc = int(m.group(1)) - 1 if m else 0
        return HagSpec(hag=d.get("hag", ""), dir=d.get("dir", ""),
                       gen="" if "incarnation" in d else gen,
                       incarnation=inc,
                       roles=[Role.from_dict(r) for r in d.get("roles", [])],
                       repos=list(d.get("repos", []) or []),
                       generations=list(d.get("generations", []) or []),
                       retired_utc=d.get("retired_utc", "") or "",
                       ledger=dict(d.get("ledger", {}) or {}),
                       sources=list(d.get("sources", []) or []))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)

    @staticmethod
    def load(path: str) -> "HagSpec":
        with open(path) as fh:
            return HagSpec.from_dict(json.load(fh))


def default_spec_path(hag: str) -> str:
    return os.path.join(HAG_ROOT, hag, "hag.json")


def find_spec(hag: str, explicit: str = "") -> str:
    """Resolve a spec path: explicit wins, else <HAG_ROOT>/<hag>/hag.json."""
    if explicit:
        return explicit
    return default_spec_path(hag)
