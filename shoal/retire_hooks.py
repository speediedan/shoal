"""Trusted retirement hooks: preflight before removal, durable outcomes, and retry without rollback.

The document host holds the retirement lock from preflight through the hook. Routed operations keep a worker's
stdin open across the caller's metadata edit. Hook commands are argv (shlex), never implicit shell programs.
"""
from __future__ import annotations

import contextlib
import argparse
import dataclasses
import fcntl
import hashlib
import json
import os
import selectors
import shlex
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import lifecycle
import refs
from ledger import Ledger, SYSTEM_WRITER

STANDALONE = "_standalone"
PREFIX = "RETIRE_HOOK "


class Refused(RuntimeError):
    pass


@dataclasses.dataclass
class Target:
    sid: str
    role: str
    doc: str
    directory: str
    host: str
    session: str = ""
    kind: str = "role"
    repo: str = ""

    @property
    def key(self):
        data = dataclasses.asdict(self)
        if self.kind == "role":
            data.pop("session")  # a later incarnation retiring the same directory supersedes its earlier outcome
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _absolute(path, base):
    if not path:
        return ""
    if not isinstance(path, str):
        raise Refused("document and directory paths must be strings")
    path = os.path.expanduser(path)
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(base or os.getcwd(), path)) if path else ""


def role_target(spec, role):
    from roles import host_of
    doc = _absolute(role.doc, spec.dir)
    return Target(spec.sid, role.slug, doc, _absolute(getattr(role, "dir", ""), spec.dir) or os.path.dirname(doc),
                  host_of(role, spec) or config.server(), role.session)


def session_target(entry):
    doc = _absolute(entry.get("doc", ""), entry.get("cwd", ""))
    return Target("", entry.get("label") or entry["session"][:12], doc,
                  _absolute(entry.get("dir", ""), entry.get("cwd", "")) or os.path.dirname(doc),
                  entry.get("host") or config.server(), entry["session"], "standalone")


@contextlib.contextmanager
def lock(target):
    """Share the context-restarter's role lock and the handoff workflow's seat lock."""
    from roles import state_dir
    if target.kind == "role":
        import re
        if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", x) and x not in (".", "..") for x in (target.sid, target.role)):
            raise Refused("invalid role or shoal for a retirement lock")
        paths = [os.path.join(state_dir(target.sid), f"context-restart-{target.role}.lock")]
        key = target.sid + "/" + target.role
    else:
        paths = []
        key = "standalone/" + target.session
    runtime = config.env("RUNTIME_DIR") or os.path.join(
        os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/shoal-{os.getuid()}", "shoal-handoff")
    paths.append(os.path.join(runtime, hashlib.sha256(key.encode()).hexdigest() + ".lock"))
    with contextlib.ExitStack() as stack:
        for path in paths:
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            fh = stack.enter_context(open(path, "a"))
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise Refused(f"a restart of {target.role} is running, or a handoff/retirement holds its lock") from exc
        yield


def _within(path, directory):
    return bool(directory) and os.path.commonpath([os.path.abspath(path), os.path.abspath(directory)]) == os.path.abspath(directory)


def preflight(target, after=False):
    lc = lifecycle.load(target.repo or target.doc or target.directory)
    command = lc["values"]["hooks"]["on_retire"]
    out = {"repo": lc["repo"], "argv": [], "env": {}, "skip": ""}
    if not command or not lc["trusted"]:
        out["skip"] = "no trusted on_retire hook configured"
        return out
    if not target.doc or not target.directory or not _within(target.doc, target.directory):
        raise Refused("the role directory must contain its start doc")
    if not _within(os.path.realpath(target.directory), lc["repo"]):
        raise Refused("the role directory leaves its repository")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise Refused(f"invalid on_retire argv: {exc}") from exc
    if not argv:
        raise Refused("empty on_retire argv")
    executable = (os.path.join(lc["repo"], argv[0]) if "/" in argv[0] and not os.path.isabs(argv[0])
                  else argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0]))
    if not executable or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        raise Refused(f"on_retire executable is unavailable: {argv[0]}")
    argv[0] = os.path.abspath(executable)
    dirty = subprocess.run(["git", "-C", lc["repo"], "status", "--porcelain", "--untracked-files=all", "--",
                            target.directory], capture_output=True, text=True, timeout=30)
    if dirty.returncode or dirty.stdout:
        raise Refused(f"role directory is dirty or unreadable: {dirty.stdout.strip()[:500] or dirty.stderr.strip()[:500]}")
    known = refs.roots(all_hosts=True, refresh=True)
    if known["unreachable"]:
        raise Refused("cannot verify live roots on " + ", ".join(u["host"] for u in known["unreachable"]))
    for path, owners in known["roots"].items():
        for owner in owners:
            own = ((owner.get("kind") == "role" and target.kind == "role" and owner.get("shoal") == target.sid
                    and owner.get("slug") == target.role) or
                   (owner.get("kind") == "standalone" and target.kind == "standalone"
                    and owner.get("label") == target.role and owner.get("host") == target.host))
            if own and not after:
                continue
            if _within(path, target.directory) or _within(os.path.realpath(path), os.path.realpath(target.directory)):
                raise Refused(f"live root {path} ({refs.who_text(owner)}) still uses the role directory")
    out.update(argv=argv, env={"SHOAL_ID": target.sid, "SHOAL_ROLE": target.role, "SHOAL_DOC": target.doc,
                              "SHOAL_ROLE_DIR": target.directory, "SHOAL_HOST": target.host})
    return out


def execute(target):
    """Recheck immediately before execution; a hook must tolerate a previous partial run."""
    plan = preflight(target, after=True)
    if plan["skip"]:
        raise Refused("on_retire was disabled or is no longer trusted; hook not run")
    try:
        result = subprocess.run(plan["argv"], cwd=plan["repo"], env=dict(os.environ, **plan["env"]),
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"on_retire did not finish: {exc}") from exc
    if result.returncode:
        raise Refused(f"on_retire exited {result.returncode}: {(result.stderr or result.stdout)[-600:]}")
    return plan


class Remote:
    """A routed worker holds the document-host lock across the caller's removal."""
    def __init__(self, target, retry=False):
        import routes
        route = routes.route(target.host)
        if route is None or not routes.reachable(target.host, force=True):
            raise Refused(f"document host {target.host} has no reachable route")
        self.err = tempfile.TemporaryFile()
        args = ["lifecycle", "_retire-worker", "--target", json.dumps(dataclasses.asdict(target))]
        if retry:
            args.append("--removed")
        self.proc = subprocess.Popen(routes.ssh_argv(route) + [routes.remote_cmd(route, *args)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.err)
        self.pending = b""
        try:
            self.plan = self.receive(180)
        except Exception:
            self.close()
            raise

    def receive(self, timeout):
        import time
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while True:
                if b"\n" in self.pending:
                    line, self.pending = self.pending.split(b"\n", 1)
                    if line.startswith(PREFIX.encode()):
                        value = json.loads(line[len(PREFIX):])
                        if value.get("error"):
                            raise Refused(value["error"])
                        return value
                    continue
                if not selector.select(max(0, deadline - time.monotonic())):
                    raise Refused("document host did not answer the retirement protocol in time")
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    self.err.seek(0)
                    raise Refused("document host's retirement worker exited: " + self.err.read().decode(errors="replace")[-500:])
                self.pending += chunk

    def finish(self):
        self.proc.stdin.write(b"run\n")
        self.proc.stdin.flush()
        return self.receive(900)

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc.stdout.close()
        self.err.close()


class Operation:
    def __init__(self, target, ledger, stack, no_archive=False, retry=False):
        self.target, self.ledger = target, ledger
        self.remote = None
        if no_archive:
            stack.enter_context(lock(target))
            self.plan = {"skip": "explicit --no-archive", "argv": [], "env": {}, "repo": ""}
        elif target.host != config.server():
            lc = lifecycle.load(target.repo or target.doc or target.directory)
            # Repository configuration travels with the repository. A known no-hook configuration executes
            # nothing and preserves retirement's existing offline behavior. Remote-only directories need a route.
            if lc["repo"] and not lc["values"]["hooks"]["on_retire"]:
                stack.enter_context(lock(target))
                self.plan = {"skip": "no on_retire hook configured", "argv": [], "env": {}, "repo": lc["repo"]}
            else:
                self.remote = Remote(target, retry)
                stack.callback(self.remote.close)
                self.plan = self.remote.plan
        else:
            stack.enter_context(lock(target))
            self.plan = preflight(target, after=retry)
        target.repo = self.plan["repo"] or target.repo
        if retry and self.plan["skip"]:
            raise Refused("cannot retry: the hook is disabled or untrusted")
        if not self.plan["skip"] or no_archive:
            top = subprocess.run(["git", "-C", ledger.root, "rev-parse", "--show-toplevel"], capture_output=True,
                                 text=True, timeout=20)
            if top.returncode or os.path.realpath(top.stdout.strip()) != os.path.realpath(ledger.root):
                raise Refused("retirement hooks require a configured ledger repository for durable outcomes")

    @property
    def enabled(self):
        return not self.plan["skip"]

    def preview(self):
        t = self.target
        print(f"retire hook {t.role} on {t.host}: " + (self.plan["skip"] or
              json.dumps({k: self.plan[k] for k in ("repo", "argv", "env")}, sort_keys=True)))

    def record(self, status, error="", phase="finished"):
        return self.ledger.append(SYSTEM_WRITER, "system", event="retire-hook", status=status, phase=phase,
                                  role=self.target.role, doc_dir=self.target.directory, host=self.target.host,
                                  hook_argv=self.plan["argv"], hook_env=self.plan["env"], retire_target=dataclasses.asdict(self.target),
                                  target_key=self.target.key, error_tail=error[-600:])

    def prepare(self):
        if self.enabled:
            self.record("failed", "hook prepared; removal has not yet been confirmed", "prepared")

    def finish(self):
        t = self.target
        if self.plan["skip"]:
            if self.plan["skip"] == "explicit --no-archive":
                self.record("ok", self.plan["skip"], "skipped")
            print(f"retire hook {t.role}: skipped ({self.plan['skip']})")
            return 0
        try:
            self.record("failed", "removal complete; hook pending", "pending")
            if self.remote:
                self.remote.finish()
            else:
                execute(t)
            self.record("ok")
            print(f"retire hook {t.role}: ok")
            return 0
        except Exception as exc:
            message = str(exc)[-600:]
            try:
                self.record("failed", message)
            except Exception as recording:
                message += f"; ledger recording also failed: {recording}"
            selector = ["--session", t.session] if t.kind == "standalone" else ["--shoal", t.sid, "--role", t.role]
            print(f"retire hook {t.role}: failed ({message}); retry: shoal lifecycle retire-hook " + shlex.join(selector))
            return 1


def ledger_for(spec=None):
    return Ledger(spec.sid if spec else STANDALONE, root=spec.ledger_dir if spec else config.ledger_dir())


def latest(ledger):
    out = {}
    for event in ledger.events():
        if event.get("by") == SYSTEM_WRITER and event.get("_writer_file") == SYSTEM_WRITER and \
                event.get("type") == "system" and event.get("event") == "retire-hook" and event.get("target_key"):
            out[event["target_key"]] = event
    return list(out.values())


def failures(ledger):
    return [e for e in latest(ledger) if e.get("status") == "failed"]


def warning_lines(ledger):
    return [f"RETIRE HOOK {e['role']} on {e['host']}: {e.get('phase')} failed: {e.get('error_tail', '')} "
            f"(shoal lifecycle retire-hook --" +
            (f"session {shlex.quote(e['retire_target']['session'])})" if e["retire_target"]["kind"] == "standalone" else
             f"shoal {shlex.quote(e['retire_target']['sid'])} --role {shlex.quote(e['role'])})") for e in failures(ledger)]


def operator_failures(ledger):
    extra = Ledger(STANDALONE, root=ledger.root, sync=False)
    return failures(ledger) + (failures(extra) if ledger.sid != STANDALONE else [])


def print_warnings(ledger):
    for event in operator_failures(ledger):
        print(f"RETIRE HOOK {event['role']} on {event['host']}: {event.get('phase')} failed: "
              f"{event.get('error_tail', '')}; inspect/retry with shoal lifecycle retire-hook")


def cmd_retry(a):
    try:
        spec = None
        if a.sid:
            from roles import ShoalSpec, find_spec
            spec = ShoalSpec.load(find_spec(a.sid, a.spec))
        led = ledger_for(spec)
        led.pull()
        events = failures(led)
        if a.status:
            print(json.dumps(events, indent=1) if a.json else "\n".join(warning_lines(led)) or "no failed retirement hooks")
            return 0
        if not ((a.sid and a.role and not a.session) or (a.session and not a.sid and not a.role)):
            raise Refused("choose --shoal ID --role ROLE, or --session LABEL_OR_ID, or --status")
        events = [e for e in events if e["retire_target"]["kind"] == "role" and e["role"] == a.role] if a.sid else \
            [e for e in events if e["retire_target"]["kind"] == "standalone" and a.session in (e["role"], e["retire_target"]["session"])]
        if not events:
            raise Refused("no failed retirement hook matches that selector")
        results = []
        for event in events:
            with contextlib.ExitStack() as stack:
                op = Operation(Target(**event["retire_target"]), led, stack, retry=True)
                op.preview()
                if a.dry_run:
                    continue
                print(f"removal {op.target.role}: already complete (no rollback or repeated removal)")
                results.append(op.finish())
        return int(any(results))
    except (Refused, OSError, RuntimeError) as exc:
        print(f"REFUSED: {exc}")
        return 1


def cmd_worker(a):
    def answer(value):
        print(PREFIX + json.dumps(value), flush=True)
    try:
        target = Target(**json.loads(a.target))
        if target.host != config.server():
            raise Refused("retirement worker reached the wrong document host")
        with lock(target):
            answer(preflight(target, after=a.removed))
            with selectors.DefaultSelector() as selector:
                selector.register(sys.stdin, selectors.EVENT_READ)
                if not selector.select(1800):
                    raise Refused("retirement caller did not finish its metadata edit")
                if sys.stdin.readline().strip() == "run":
                    answer(execute(target))
        return 0
    except Exception as exc:
        answer({"error": str(exc)[-600:]})
        return 1


def add_parser(sub):
    p = sub.add_parser("retire-hook", help="retry a failed retirement hook from its durable ledger outcome")
    config.add_id_arg(p, default="")
    p.add_argument("--spec", default="")
    p.add_argument("--role", default="")
    p.add_argument("--session", default="")
    p.add_argument("--status", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_retry)
    p = sub.add_parser("_retire-worker", help=argparse.SUPPRESS)
    p.add_argument("--target", required=True)
    p.add_argument("--removed", action="store_true")
    p.set_defaults(func=cmd_worker)
