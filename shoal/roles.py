#!/usr/bin/env python3
"""shoal role/spec model.

Flattened only (2026-09-24, extraction plan D7): a shoal is a set of roles that
all speak the same ledger primitive (ledger.py). There is no coordinator role.
Per role, `fill` says where its work comes from: `self` (it pops the priority
table in `sources` when its queue is empty), `assigned` (it works only what
`user` offers it) or `none` (it never claims). Only `user` offers work to
other roles; a role hands its own claim to a peer with `release --kind
handoff --to <role>`. Specs written before the change may still carry
`coordinator`, `can_fill_others` or `queue` keys: they load and are ignored.

Two roots (resolved by config.py: environment, then the host config file,
then a generic default):
  data_root      per-shoal local home (unversioned bulk): <root>/<id>/{briefs/,
                     reports/,telemetry/}. Briefs, report .md twins and events
                     live here, never in version control.
  ledger.dir     the ledger repo's local clone (config `ledger_dir`):
                     shoals/<id>/{shoal.json (THE canonical live spec, mutated by
                     shoal with commit+push),ledger/}. Remote agents read this.
  SHOAL_SPEC       optional pin: the path of one shoal's spec (<dir named for the shoal>/shoal.json).
                     Tests and operators use it to resolve a spec outside the ledger clone.

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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

ECOSYSTEMS = ("opencode", "claude", "codex")
FILL_MODES = ("self", "assigned", "none")

# The helper session behind the Claude implementation of direct session wake (backends.py
# `wake`).
WAKE_RELAY_SLUG = "wake-relay"
# resolved once per process: environment, then the host config file, then a generic default
DEFAULT_LEDGER = {"repo": config.ledger_repo(), "dir": config.ledger_dir()}

SHOAL_DATA_ROOT = config.data_root()
DEFAULT_REPOS = config.default_repos()


def utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def home_dir(sid: str) -> str:
    """Local per-shoal home: briefs/, reports/, telemetry/ (unversioned)."""
    return os.path.join(SHOAL_DATA_ROOT, sid)


def data_dir(sid: str) -> str:
    return os.path.join(SHOAL_DATA_ROOT, sid)


def telemetry_dir(sid: str) -> str:
    return os.path.join(data_dir(sid), "telemetry")


def contexts_dir(sid: str) -> str:
    """Per-shoal orchestration state: one editable working-context file per role,
    `<slug>.json`, loaded into the spec by `shoal context set|load` (architecture §4)."""
    return os.path.join(data_dir(sid), "contexts")


def roles_dir(sid: str) -> str:
    """Per-shoal orchestration state: generated start docs for infrastructure roles
    (e.g. `roles/wake-relay/plan_state.md`); worker roles keep their plan_state in the
    design repo, where their mission lives."""
    return os.path.join(data_dir(sid), "roles")


def state_dir(sid: str) -> str:
    """Scratch state (/tmp): sidecars, heartbeats, wait files, watcher ticks."""
    return os.path.join("/tmp", sid)


@dataclasses.dataclass
class Role:
    slug: str                # stable identity (ledger file, sidecars, heartbeats, telemetry)
    ecosystem: str = "opencode"  # opencode | claude | codex
    title: str = ""              # session title stem (sequence suffix appended)
    seq: int = 1                 # per-role session sequence; session tag is {slug}-r{seq}.
        # Bump ONLY this role's seq on a single-ecosystem restart; other roles keep
        # their sessions AND their seqs, so shoal.json never conflates them.
    doc: str = ""                # absolute path to THE start document (plan_state.md;
        # mission + current state + grants-needed + next). One doc per role;
        # handoffs/ dirs hold the immutable restart record, never the start doc.
    session: str = ""            # live session id once started
    model: str = ""              # native model id ("" = harness default)
    effort: str = ""             # agent-neutral pace/depth knob ("" = default);
        # honored natively where the harness supports it, briefed otherwise.
    grants: list[str] = dataclasses.field(default_factory=list)  # extracted
        # from the doc's grants-needed section at brief time, never CLI.
    fill: str = "self"           # self | assigned | none. self: pops the priority
        # table when its queue is empty; assigned: works only offers addressed to
        # it (or `*`); none: never claims (an observer role, e.g. the relay).
    scope: dict = dataclasses.field(default_factory=dict)  # DEPRECATED hard self-fill
        # filter {"repos", "labels_any", "labels_none"}; read only to derive `context`
        # when a spec predates it (`shoal context migrate` converts and removes it).
    context: dict = dataclasses.field(default_factory=dict)  # working context
        # (architecture doc §4.2): {"brief": str, "affinity": {"repos": {r: w},
        # "labels": {l: w}, "keywords": {k: w}, "paths": {glob: w}},
        # "never": {"repos": [...], "labels": [...]}}. Soft ranking; `never` is the
        # only hard exclusion and is reserved for safety.
    server: str = ""             # host the session runs on ("" = this host)
    aux: bool = False            # residual lane (architecture doc §4.1): no brief of its own;
        # the digest ranks it the work no other ACTIVE role's brief covers (minmax over the
        # semantic matcher's fits, unblockers first). Needs features.matcher.

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
        if self.aux and self.fill != "self":
            errs.append(f"role {self.slug}: an aux role must be fill=self (got {self.fill!r})")
        return errs

    @property
    def tag(self) -> str:
        """Session tag: {slug}-r{seq}. Stable slug, rolling sequence."""
        return f"{self.slug}-r{self.seq}"

    @property
    def label(self) -> str:
        """The row label everywhere a role is shown (show, posted blocks, reports):
        BASE name first, session tag in parentheses, e.g. `jlens-expert (jlens-expert-r2)`.
        The base name is the stable identity; the tag says which generation holds it.
        A tag alone drifts: a lane once labelled itself with the incarnation number."""
        return f"{self.slug} ({self.tag})"

    @staticmethod
    def from_dict(d: dict) -> "Role":
        # unknown keys are dropped, so a pre-flattening spec (`coordinator`,
        # `can_fill_others`, `queue`) still loads
        known = {f.name for f in dataclasses.fields(Role)}
        d = dict(d)
        if "fill" not in d:
            # pre-2026-09-20 spec: consumers were assigned their work, so `assigned`
            d["fill"] = "assigned"
        if d.get("scope") is None:
            d["scope"] = {}
        if d.get("context") is None:
            d["context"] = {}
        return Role(**{k: v for k, v in d.items() if k in known})

    def working_context(self) -> dict:
        """The role's working context, derived from a legacy `scope` when unset."""
        return self.context or scope_to_context(self.scope)


def scope_to_context(scope: dict | None) -> dict:
    """Mechanical conversion of a legacy hard `scope` into a soft working context:
    repos become affinity weights, labels_any strong label weights, labels_none
    negative weights (a safety exclusion must be moved to `never` by hand)."""
    scope = scope or {}
    if not scope:
        return {}
    aff: dict = {}
    if scope.get("repos"):
        aff["repos"] = {r: 1.0 for r in scope["repos"]}
    labels = {l: 3.0 for l in scope.get("labels_any") or []}
    labels.update({l: -3.0 for l in scope.get("labels_none") or []})
    if labels:
        aff["labels"] = labels
    return {"brief": "", "affinity": aff, "never": {}}


@dataclasses.dataclass
class ShoalSpec:
    sid: str
    dir: str = ""   # repo dir sessions run in
    gen: str = ""   # legacy display string ("r1"); kept for old specs, else derived
    incarnation: int = 0  # 0-indexed shoal incarnation: THE telemetry counter.
        # Bumped on every restart that creates sessions (full or partial).
        # Display string is r{incarnation+1}, so --gen is unnecessary.
    roles: list[Role] = dataclasses.field(default_factory=list)
    repos: list[str] = dataclasses.field(default_factory=list)  # GitHub repos
        # the shoal works (reconcile ground truth); DEFAULT_REPOS when empty.
    generations: list[dict] = dataclasses.field(default_factory=list)
        # [{gen, started_utc, ended_utc, sessions{slug:id}, run{}}], oldest first
    retired_utc: str = ""  # set by `shoal retire`; a retired shoal launches nothing
    ledger: dict = dataclasses.field(default_factory=dict)  # {"repo": url, "dir": clone}
        # required: the ledger is the only transport
    sources: list[dict] = dataclasses.field(default_factory=list)  # priority tables
        # for self-filling roles: [{"kind": "github-project", "owner", "number", ...}]
    features: dict = dataclasses.field(default_factory=dict)  # per-shoal opt-ins so new
        # behaviour lands at a restart, never under running sessions (architecture doc):
        # "digest" board digest; "jobs" job-unit brief discipline; "workwake" work-only
        # wakes + the next/queue loop; "health" (true or {"validity_wake_min": 90}) the
        # health quorum timers replacing heartbeat; "relay" the Claude wake relay role;
        # "matcher" the semantic board matcher (architecture §4.1).
    tooling: dict = dataclasses.field(default_factory=dict)  # {"sha", "repo", "prev", "upgraded_utc",
        # "changed"}: the shoal/ tooling commit this shoal was last upgraded to (`shoal upgrade`);
        # `repo` names the repository that sha belongs to (origin URL, else toplevel dir name)

    def validate(self) -> list[str]:
        errs = []
        if not self.sid:
            errs.append("spec missing shoal id")
        if not self.dir:
            errs.append("spec missing dir")
        if not self.roles:
            errs.append("spec has no roles")
        if not self.ledger:
            errs.append("spec has no `ledger` (the pre-ledger queue-file transport is removed)")
        elif not self.ledger.get("dir"):
            errs.append("ledger set without a clone dir")
        for s in self.sources:
            if s.get("kind") != "github-project" or not s.get("owner") or not s.get("number"):
                errs.append(f"bad source {s} (want kind=github-project, owner, number)")
        if not self.sources and any(r.fill == "self" for r in self.roles):
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
    def effective_repos(self) -> list[str]:
        return list(self.repos) if self.repos else list(DEFAULT_REPOS)

    @property
    def ledger_dir(self) -> str:
        return (self.ledger or {}).get("dir") or DEFAULT_LEDGER["dir"]

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
        return {"shoal": self.sid, "dir": self.dir, "gen": self.display_gen,
                "incarnation": self.incarnation,
                "repos": self.repos, "retired_utc": self.retired_utc,
                "ledger": self.ledger, "sources": self.sources, "features": self.features,
                "tooling": self.tooling,
                "roles": [dataclasses.asdict(r) for r in self.roles],
                "generations": self.generations}

    def snapshot(self) -> dict:
        """What gets copied into the ledger repo for remote agents: the spec
        minus generation history (which only the operator's copy maintains)."""
        d = self.to_dict()
        d.pop("generations", None)
        d["snapshot_utc"] = utcnow_iso()
        d["canonical"] = "the operator's spec (this copy is a read-only snapshot for remote agents)"
        return d

    @staticmethod
    def from_dict(d: dict) -> "ShoalSpec":
        gen = d.get("gen", "") or ""
        if "incarnation" in d:
            inc = int(d.get("incarnation") or 0)
        else:
            m = re.match(r"r(\d+)$", gen)
            inc = int(m.group(1)) - 1 if m else 0
        return ShoalSpec(sid=d.get("shoal", ""), dir=d.get("dir", ""),
                       gen="" if "incarnation" in d else gen,
                       incarnation=inc,
                       roles=[Role.from_dict(r) for r in d.get("roles", [])],
                       repos=list(d.get("repos", []) or []),
                       generations=list(d.get("generations", []) or []),
                       retired_utc=d.get("retired_utc", "") or "",
                       ledger=dict(d.get("ledger", {}) or {}),
                       sources=list(d.get("sources", []) or []),
                       features=dict(d.get("features", {}) or {}),
                       tooling=dict(d.get("tooling", {}) or {}))

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)

    @staticmethod
    def load(path: str) -> "ShoalSpec":
        with open(path) as fh:
            return ShoalSpec.from_dict(json.load(fh))


def is_wake_relay(role) -> bool:
    """True for the wake relay role (slug `wake-relay`; always `fill=none`)."""
    return (role is not None and getattr(role, "fill", "") == "none"
            and role.slug == WAKE_RELAY_SLUG)


def wake_relay_role(spec) -> "Role | None":
    return next((r for r in spec.roles if is_wake_relay(r)), None)


def default_spec_path(sid: str) -> str:
    """The canonical live spec: `$SHOAL_SPEC` when it pins this shoal (its directory is named
    for the group), else the ledger clone's `shoals/<id>/shoal.json` (see layout.py). A missing
    spec fails by
    name when loaded; there is no fallback location (the pre-ledger control plane is gone)."""
    pin = config.env("SPEC")
    if pin and os.path.basename(os.path.dirname(os.path.abspath(pin))) == sid:
        return pin
    ledger_root = config.ledger_dir()
    from layout import spec_path
    return spec_path(ledger_root, sid)


def find_spec(sid: str, explicit: str = "") -> str:
    """Resolve a spec path: explicit wins, else default_spec_path."""
    if explicit:
        return explicit
    return default_spec_path(sid)
