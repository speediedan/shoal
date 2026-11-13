"""Role time adapters on a synthetic ledger clone: no network, no live harness, no provider call.
Ledger events are written as raw JSONL exactly as the writers append them; blocker ages are
relative to the real clock because blockers.check reads it."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
os.environ.pop("SHOAL_HEALTH_SHIFT_MIN", None)
import muse as muse_h  # noqa: E402
import resource_usage as ru  # noqa: E402
import role_time as rt  # noqa: E402
import role_time_adapters as ad  # noqa: E402
from ledger import Ledger, View  # noqa: E402
from roles import Role, ShoalSpec  # noqa: E402

SID = "testgrp"
NOW = time.time()


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_world(td, now=NOW):
    """(spec path, ledger root) with a worker waiting on remote-ci, an offered peer, a background
    fill-none role and a parked role; FETCH_HEAD fetched 10 s ago."""
    root = Path(td) / "clone"
    ldir = root / "shoals" / SID / "ledger"
    ldir.mkdir(parents=True)
    (root / ".git").mkdir()
    fh = root / ".git" / "FETCH_HEAD"
    fh.write_text("deadbeef\t\tbranch 'main' of example\n")
    os.utime(fh, (now - 10, now - 10))
    roles = [Role(slug="worker", ecosystem="muse", title="worker", doc="/x/w.md", fill="self",
                  session="11111111-2222-3333-4444-555555555555"),
             Role(slug="peer", ecosystem="claude", title="peer", doc="/x/p.md", fill="assigned",
                  session="aaaa1111"),
             Role(slug="observer", ecosystem="opencode", title="obs", doc="/x/o.md", fill="none",
                  session="obs1"),
             Role(slug="parked-one", ecosystem="muse", title="park", doc="/x/k.md", fill="assigned",
                  session="", parked={"at": iso(now - 100), "by": "user", "note": "x"})]
    gens = [{"gen": "r1", "incarnation": 0, "started_utc": iso(now - 86400),
             "ended_utc": iso(now - 43200), "sessions": {"worker": "w0", "peer": "p0"}, "run": {}},
            {"gen": "r2", "incarnation": 1, "started_utc": iso(now - 43200), "ended_utc": "",
             "sessions": {"worker": "w1", "peer": "p1", "parked-one": "k1"}, "run": {}}]
    spec = ShoalSpec(sid=SID, dir=str(Path(td)), incarnation=1, roles=roles,
                     generations=gens, ledger={"repo": "example.invalid/x.git", "dir": str(root)},
                     features={"blockers": True})
    spec_path = root / "shoals" / SID / "shoal.json"
    spec.save(str(spec_path))
    ev = json.dumps
    (ldir / "user.jsonl").write_text(
        ev({"ts": iso(now - 300), "by": "user", "type": "offer", "id": "o1", "to": "peer",
            "item": "o/r#9"}) + "\n")
    (ldir / "worker.jsonl").write_text(
        ev({"ts": iso(now - 200), "by": "worker", "type": "state", "id": "s1", "state": "waiting",
            "sub": "remote-ci", "target": "job:ci", "session": "w1"}) + "\n" +
        ev({"ts": iso(now - 200), "by": "worker", "type": "queue", "id": "q1", "version": 3,
            "next": ["o/r#7"], "asks": ["more work when ready?"], "status": "waiting",
            "next_blocked": {"o/r#7": "peer:o/r#9"},
            "next_blocked_meta": {"o/r#7": {"since": iso(now - 60), "first": iso(now - 60),
                                            "n": 0}}}) + "\n")
    (ldir / "peer.jsonl").write_text(
        ev({"ts": iso(now - 150), "by": "peer", "type": "state", "id": "s2", "state": "idle",
            "session": "p1"}) + "\n" +
        ev({"ts": iso(now - 150), "by": "peer", "type": "queue", "id": "q2", "version": 1,
            "next": ["o/r#8"], "status": "idle",
            "next_blocked": {"o/r#8": "hold:release-window"},
            "next_blocked_meta": {"o/r#8": {"since": iso(now - 13 * 3600),
                                            "first": iso(now - 13 * 3600), "n": 0}}}) + "\n")
    return str(spec_path), str(root)


def load(spec_path):
    spec = ShoalSpec.load(spec_path)
    return spec, View(Ledger(SID, root=spec.ledger_dir, sync=False), spec)


class LedgerAdapter(unittest.TestCase):
    def test_fetch_time_is_fetch_head_not_newest_event(self):
        with tempfile.TemporaryDirectory() as td:
            _sp, root = make_world(td)
            self.assertAlmostEqual(ad.ledger_fetched_at(root), NOW - 10, delta=2)
            self.assertIsNone(ad.ledger_fetched_at(str(Path(td) / "nofetch")))

    def test_foreground_and_membership(self):
        with tempfile.TemporaryDirectory() as td:
            sp, _root = make_world(td)
            spec, _view = load(sp)
            fg, bg = ad.foreground_roles(spec)
            self.assertEqual({r.slug for r in fg}, {"worker", "peer", "parked-one"})
            self.assertEqual(bg, {"observer"})
            spans = ad.membership_spans(spec)
            self.assertEqual(len(spans["worker"]), 1)  # touching generations merge
            self.assertAlmostEqual(spans["worker"][0][0], NOW - 86400, delta=2)
            self.assertIsNone(spans["worker"][0][1])
            self.assertAlmostEqual(spans["parked-one"][0][0], NOW - 43200, delta=2)
            spec.features = {"role_time": {"foreground": ["observer"], "background": ["peer"]}}
            fg, bg = ad.foreground_roles(spec)
            self.assertIn("observer", {r.slug for r in fg})
            self.assertIn("peer", bg)
            spec.retired_utc = iso(NOW - 50)
            spec.features = {}
            self.assertAlmostEqual(ad.membership_spans(spec)["worker"][0][1], NOW - 50, delta=2)

    def test_ledger_part_status_blockers_and_refill(self):
        with tempfile.TemporaryDirectory() as td:
            sp, root = make_world(td)
            spec, view = load(sp)
            led = ad.ledger_part(spec, view, "worker", ad.ledger_fetched_at(root))
            self.assertEqual((led["state"], led["sub"], led["target"]), ("waiting", "remote-ci", "job:ci"))
            self.assertTrue(led["refill_requested"])  # the recorded ask is the refill request
            b = led["blockers"]["o/r#7"]
            self.assertEqual((b["status"], b["evidence"]), ("live", "ledger"))
            self.assertAlmostEqual(b["expires_at"], NOW - 60 + 720 * 60, delta=2)
            led_p = ad.ledger_part(spec, view, "peer", NOW - 10)
            self.assertFalse(led_p["refill_requested"])
            # a hold recorded 13 h ago is past its 4 h horizon: expired, age-only evidence
            self.assertEqual(led_p["blockers"]["o/r#8"]["status"], "expired")
            self.assertEqual(led_p["blockers"]["o/r#8"]["evidence"], "age-only")
            self.assertIsNone(led_p["blockers"]["o/r#8"]["expires_at"])
            # peer therefore has runnable work it is not doing (v5 wait:self), not a hold wait
            snap = {"t": NOW, "native": ad._native("between", NOW), "ledger": led_p, "lifecycle": {}}
            self.assertEqual(rt.classify(snap, NOW)[:2], ("wait", "self"))

    def test_next_items_are_item_keys_so_blockers_match(self):
        # live queues carry `<owner>/<repo>#<n>:<verb>`; blockers are keyed by item key. Without
        # normalizing, every blocked item read as runnable (measured on a live ledger, 2026-10-30).
        with tempfile.TemporaryDirectory() as td:
            sp, root = make_world(td)
            ldir = Path(root) / "shoals" / SID / "ledger"
            ev = json.dumps
            from ledger import item_key
            key = item_key("someone/r#8:fix")  # what the queue writer stores as the blocker key
            (ldir / "peer.jsonl").write_text(
                ev({"ts": iso(NOW - 150), "by": "peer", "type": "state", "id": "s2", "state": "idle",
                    "session": "p1"}) + "\n" +
                ev({"ts": iso(NOW - 150), "by": "peer", "type": "queue", "id": "q2", "version": 1,
                    "next": ["someone/r#8:fix"], "status": "idle",
                    "next_blocked": {key: "hold:release-window"},
                    "next_blocked_meta": {key: {"since": iso(NOW - 60), "first": iso(NOW - 60),
                                                "n": 0}}}) + "\n")
            spec, view = load(sp)
            led = ad.ledger_part(spec, view, "peer", NOW - 10)
            self.assertEqual(led["next"], [key])
            snap = {"t": NOW, "native": ad._native("between", NOW), "ledger": led, "lifecycle": {}}
            self.assertEqual(rt.classify(snap, NOW)[:2], ("wait", "hold"))

    def test_blocker_resolves_when_the_named_item_is_done(self):
        with tempfile.TemporaryDirectory() as td:
            sp, root = make_world(td)
            ldir = Path(root) / "shoals" / SID / "ledger"
            with open(ldir / "peer.jsonl", "a") as fh:
                fh.write(json.dumps({"ts": iso(NOW - 5), "by": "peer", "type": "done", "id": "d1",
                                     "item": "o/r#9"}) + "\n")
            spec, view = load(sp)
            b = ad.ledger_part(spec, view, "worker", NOW - 10)["blockers"]["o/r#7"]
            self.assertEqual(b["status"], "resolved")

    def test_snapshots_classify_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            sp, root = make_world(td)
            spec, view = load(sp)
            natives = {"worker": ad._native("between", NOW), "peer": ad._native("turn", NOW)}
            snaps = ad.build_snapshots(spec, view, NOW - 10, natives, NOW)
            self.assertEqual(rt.classify(snaps["worker"], NOW)[:2], ("wait", "remote-ci"))
            self.assertEqual(rt.classify(snaps["peer"], NOW)[:2], ("work", "turn"))
            # parked needs no native lookup; the background role is not in the population
            self.assertEqual(rt.classify(snaps["parked-one"], NOW)[:2], ("excluded", "parked"))
            self.assertNotIn("observer", snaps)
            # a stale clone reads unknown between turns, even with a fresh native source
            snaps = ad.build_snapshots(spec, view, NOW - 10000, natives, NOW)
            self.assertEqual(rt.classify(snaps["worker"], NOW)[:2], ("unknown", "stale-source"))
            self.assertEqual(rt.classify(snaps["peer"], NOW)[:2], ("work", "turn"))


class NativeProbes(unittest.TestCase):
    def test_muse_activity_age_reads_the_durable_session_file(self):
        with tempfile.TemporaryDirectory() as td:
            sess = "11111111-2222-3333-4444-555555555555"
            p = Path(td) / "2026" / "01" / "02" / sess / "session.jsonl"
            p.parent.mkdir(parents=True)
            p.write_text("{}\n")
            os.utime(p, (NOW - 5, NOW - 5))
            old = "99999999-8888-7777-6666-555555555555"
            j = Path(td) / ".msp-view-v1" / old / "journal-00000000.bin"
            j.parent.mkdir(parents=True)
            j.write_text("x")
            os.utime(j, (NOW - 7, NOW - 7))
            with mock.patch.dict(os.environ, {"SHOAL_MUSE_DATA": td}):
                self.assertAlmostEqual(muse_h.activity_age(sess), time.time() - NOW + 5, delta=3)
                self.assertAlmostEqual(muse_h.activity_age(old), time.time() - NOW + 7, delta=3)
                self.assertIsNone(muse_h.activity_age("00000000-0000-0000-0000-000000000000"))

    def _fake_mod(self, td, attached=0, age=None, last=None):
        d = Path(td) / "state"
        d.mkdir(exist_ok=True)
        (d / "lock").write_text("")
        return types.SimpleNamespace(
            ACTIVE_WINDOW_S=120, sdir=lambda s, create=False: str(d),
            attached_client=lambda s: attached, activity_age=lambda s: age,
            last_run=lambda s: last if last is not None else {"started": "t0"})

    def test_lock_harness_attached_is_never_work(self):
        role = Role(slug="r", ecosystem="muse", title="r", doc="/x", session="s1")
        with tempfile.TemporaryDirectory() as td:
            mod = self._fake_mod(td)
            fh = open(Path(td) / "state" / "lock", "a+")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertEqual(ad._probe_lock_harness(mod, role, NOW, "muse")["state"], "turn")
            finally:
                fh.close()
            # attached: quiet is between turns; moving or unknown is raw `attached` (unknown time)
            for age, want in ((600.0, "between"), (5.0, "attached"), (None, "attached")):
                got = ad._probe_lock_harness(self._fake_mod(td, attached=4242, age=age), role, NOW, "muse")
                self.assertEqual(got["state"], want)
            snap = {"t": NOW, "native": ad._native("attached", NOW), "ledger": None, "lifecycle": {}}
            self.assertEqual(rt.classify(snap, NOW)[:2], ("unknown", "unmapped-state"))
            self.assertEqual(ad._probe_lock_harness(self._fake_mod(td), role, NOW, "muse")["state"],
                             "between")


class ObserverEndToEnd(unittest.TestCase):
    def test_two_ticks_journal_prometheus_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as td:
            now = time.time()
            sp, _root = make_world(td, now=now)
            cache = Path(td) / "native.json"
            cache.write_text(json.dumps({
                "v": 1, "shoal": SID, "host": "h", "written_at": now,
                "roles": {"worker": {"native": ad._native("between", now)},
                          "peer": {"native": ad._native("between", now)}}, "errors": {}}))
            obs_path = str(Path(td) / "obs.jsonl")
            prom_path = str(Path(td) / "out" / "role_time.prom")
            ob = ad.Observer(sp, obs_path=obs_path, checkpoint_path=str(Path(td) / "acc.json"),
                             prom_path=prom_path, cache_paths=[str(cache)], probe=False)
            ob.tick(now)
            ob.tick(now + 30)
            lines = [json.loads(x) for x in Path(obs_path).read_text().splitlines()]
            # membership is written once per role; one snapshot per role per tick
            self.assertEqual(sum(1 for x in lines if x.get("type") == "membership"), 3)
            self.assertEqual(sum(1 for x in lines if x.get("type") == "snapshot"), 6)
            prom = Path(prom_path).read_text()
            self.assertIn('role="worker",class="wait",event="remote-ci"} 30.000', prom)
            self.assertIn('role="worker",class="work",event="turn"} 0.000', prom)  # zero-initialized
            self.assertIn("shoal_role_source_timestamp_seconds", prom)
            self.assertIn("shoal_role_time_exporter_last_success_timestamp_seconds", prom)
            self.assertNotIn("host=", prom.split("shoal_role_time_seconds_total")[1].split("\n")[0])
            # the reconciled event-time view of the same window agrees with the live accumulator
            obs, mem = rt.load_observations(obs_path)
            per, _segs = rt.account(obs, mem, now, now + 30)
            self.assertAlmostEqual(per["worker"][("wait", "remote-ci")], 30.0)
            self.assertAlmostEqual(ob.acc.counters["worker"][("wait", "remote-ci")], 30.0)
            self.assertAlmostEqual(per["parked-one"][("excluded", "parked")], 30.0)
            # a restarted observer resumes the checkpoint instead of double counting
            ob2 = ad.Observer(sp, obs_path=obs_path, checkpoint_path=str(Path(td) / "acc.json"),
                              prom_path=prom_path, cache_paths=[str(cache)], probe=False)
            ob2.tick(now + 30)
            self.assertAlmostEqual(ob2.acc.counters["worker"][("wait", "remote-ci")], 30.0)


class V5Inputs(unittest.TestCase):
    """The inputs v5 needs: a fill=self role's recorded table read, and dormant Claude jobs."""

    def test_sources_cache_round_trip_into_the_ledger_part(self):
        from unittest import mock
        import worker
        with tempfile.TemporaryDirectory() as td:
            sp, root = make_world(td)
            spec, view = load(sp)
            with mock.patch("roles.state_dir", lambda sid: td), mock.patch.object(worker, "state_dir", lambda sid: td):
                self.assertIsNone(ad.sources_part(SID, "worker"))
                worker.record_sources(SID, "worker", [], "board", now=NOW)
                part = ad.sources_part(SID, "worker")
                self.assertEqual((part["candidates"], part["observed_at"]), (0, NOW))
                self.assertEqual(part["budget_s"], worker.SOURCES_CACHE_BUDGET_S)
                led = ad.ledger_part(spec, view, "worker", ad.ledger_fetched_at(root))
                self.assertEqual(led["fill"], "self")
                self.assertEqual(led["sources"]["candidates"], 0)
                # only fill=self roles carry a table; an assigned role's empty queue needs none
                led_p = ad.ledger_part(spec, view, "peer", NOW)
                self.assertEqual((led_p["fill"], led_p["sources"]), ("assigned", None))
                snap = {"t": NOW, "native": ad._native("between", NOW), "lifecycle": {},
                        "ledger": dict(led, next=[], active="", refill_requested=False, state="idle",
                                       observed_at=NOW)}
                self.assertEqual(rt.classify(snap, NOW)[:2], ("wait", "sources"))

    def test_dormant_claude_job_is_between_turns_not_missing(self):
        import types
        from unittest import mock
        import health
        role = types.SimpleNamespace(slug="peer", session="0123456789abcdef")
        with mock.patch.object(health, "claude_busy", lambda: {"another-session-id": "idle"}), \
                mock.patch.object(health, "_CLAUDE_ROWS", [], create=True), \
                mock.patch.object(health, "claude_worker_gone", lambda r, c: True):
            with mock.patch.object(health, "watcher_alive", lambda sid, slug: (True, {"session_id": role.session})):
                self.assertEqual(ad._probe_claude([role], NOW, SID)["peer"]["state"], "between")
            with mock.patch.object(health, "watcher_alive", lambda sid, slug: (True, {"session_id": "ffffffffffffffff"})):
                self.assertEqual(ad._probe_claude([role], NOW, SID)["peer"]["state"], "absent",
                                 "a watcher armed for another session does not vouch for this one")
            with mock.patch.object(health, "watcher_alive", lambda sid, slug: (False, {})):
                self.assertEqual(ad._probe_claude([role], NOW, SID)["peer"]["state"], "absent")


class LiveLocalSpecs(unittest.TestCase):
    def test_caches_follow_the_live_specs_with_a_role_here(self):
        """The sampler refreshes every live group with a foreground role on this host: not one only on another host,
        not a retired one, not a spec that does not load, and a group moved here is followed at the next read."""
        from unittest import mock
        import config
        with tempfile.TemporaryDirectory() as td:
            here, root = make_world(td)
            def group(sid, server="", retired=""):
                path = Path(root) / "shoals" / sid / "shoal.json"
                ShoalSpec(sid=sid, dir=td, roles=[Role(slug="w", ecosystem="claude", title="w", doc="/x/w.md",
                                                       fill="self", session="s1", server=server)],
                          retired_utc=retired).save(str(path))
                return str(path)
            away = group("away", server="otherhost")
            group("retired", retired=iso(NOW))
            group("background-only")
            spec = ShoalSpec.load(str(Path(root) / "shoals" / "background-only" / "shoal.json"))
            spec.roles[0].fill = "none"
            spec.save(str(Path(root) / "shoals" / "background-only" / "shoal.json"))
            (Path(root) / "shoals" / "broken").mkdir()
            (Path(root) / "shoals" / "broken" / "shoal.json").write_text("{not json")
            with mock.patch.object(config, "server", lambda: "thishost"):
                self.assertEqual(ad.live_local_specs(str(root)), [str(here)])
                spec = ShoalSpec.load(away)
                spec.roles[0].server = "thishost"
                spec.save(away)
                self.assertEqual(ad.live_local_specs(str(root)), [away, str(here)])
                a = types.SimpleNamespace(role_time_spec=[str(here)], no_role_time_live=False)
                with mock.patch.object(config, "ledger_dir", lambda: str(root)):
                    self.assertEqual(ru.role_time_specs(a), [str(here), away], "named first, each spec once")
                    a.no_role_time_live = True
                    self.assertEqual(ru.role_time_specs(a), [str(here)])


class RemoteNativeCaches(unittest.TestCase):
    def test_other_hosts_cache_is_read_over_its_route(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            sp, _root = make_world(td)
            spec = ShoalSpec.load(sp)
            for r in spec.roles:
                if r.slug == "peer":
                    r.server = "otherhost"  # this role's session runs on another host
            spec.save(sp)
            remote = Path(td) / "remote.json"
            stub = Path(td) / "ssh-stub"
            stub.write_text(f"#!/bin/sh\ncat {remote}\n")
            stub.chmod(0o755)
            remote.write_text(json.dumps({"v": ad.CACHE_V, "shoal": SID, "host": "otherhost", "written_at": NOW,
                                          "roles": {"peer": {"native": ad._native("turn", NOW)}}, "errors": {}}))
            prom = Path(td) / "rt.prom"
            route = {"ssh": "user@otherhost"}
            with mock.patch.dict(os.environ, {"SHOAL_SSH": str(stub)}), \
                    mock.patch("routes.route", lambda server: route if server == "otherhost" else None):
                ob = ad.Observer(sp, obs_path=str(Path(td) / "obs.jsonl"), checkpoint_path=str(Path(td) / "acc.json"),
                                 prom_path=str(prom), probe=False)
                snaps, _f = ob.tick(NOW)
                self.assertEqual(snaps["peer"]["native"]["state"], "turn", "the other host's cache arrived")
                self.assertTrue((Path(td) / "role-time-native-otherhost.json").is_file())
                self.assertIn('shoal_role_remote_cache_ok{shoal="testgrp",server="otherhost"} 1', prom.read_text())
                # a failed read keeps the last copy (it ages into unknown on its own) and says so
                remote.write_text("not json")
                snaps, _f = ob.tick(NOW + 5)
                self.assertEqual(snaps["peer"]["native"]["state"], "turn")
                self.assertIn('server="otherhost"} 0', prom.read_text())
            # opted out: no route is read
            ob2 = ad.Observer(sp, obs_path=str(Path(td) / "obs2.jsonl"), checkpoint_path=str(Path(td) / "acc2.json"),
                              probe=False, remote=False)
            ob2.tick(NOW)
            self.assertEqual(ob2.remote_ok, {})


class JobPart(unittest.TestCase):
    def test_placed_job_grant_and_finish_from_its_records(self):
        from unittest import mock
        import jobs
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(jobs, "state_dir", lambda sid: td), mock.patch.object(jobs, "data_dir", lambda sid: td):
                p = jobs.paths(SID, "u1")
                os.makedirs(p["sdir"], exist_ok=True)
                meta = {"unit": "u1", "name": "ci", "slug": "worker", "placement": {"vendor": "nvidia"},
                        "rc_file": p["rc"]}
                Path(p["meta"]).write_text(json.dumps(meta))
                self.assertIsNone(ad.job_part(SID, "worker", "remote-ci"))
                self.assertEqual(ad.job_part(SID, "worker", "job:ci"), {"placed": True, "granted": False, "done": False})
                Path(p["granted"]).write_text("2026-11-01T00:00:00Z\n")
                self.assertTrue(ad.job_part(SID, "worker", "job:ci")["granted"])
                Path(p["rc"]).write_text("0\n")
                self.assertTrue(ad.job_part(SID, "worker", "job:ci")["done"])
                # a remote placed job: its supervisor copies the grant into the home meta, so the
                # local marker is not consulted and the meta field decides
                Path(p["meta"]).write_text(json.dumps(dict(meta, host="otherhost")))
                self.assertIs(ad.job_part(SID, "worker", "job:ci")["granted"], False)
                Path(p["meta"]).write_text(json.dumps(dict(meta, host="otherhost", lease_granted="2026-11-01T22:31:26Z")))
                self.assertIs(ad.job_part(SID, "worker", "job:ci")["granted"], True)


class PopulationExit(unittest.TestCase):
    def test_removed_and_departed_roles_leave_the_population(self):
        with tempfile.TemporaryDirectory() as td:
            sp, root = make_world(td)
            ldir = Path(root) / "shoals" / SID / "ledger"
            (ldir / "shoal.jsonl").write_text(json.dumps(
                {"ts": iso(NOW - 60), "by": "shoal", "type": "system", "event": "role-removed",
                 "id": "x1", "role": "peer", "removed_by": "user", "note": "lane closed"}) + "\n")
            obs = Path(td) / "obs.jsonl"
            ob = ad.Observer(sp, obs_path=str(obs), checkpoint_path=str(Path(td) / "acc.json"), probe=False)
            ob.tick(NOW)
            mem = {x["role"]: x for x in map(json.loads, obs.read_text().splitlines()) if x.get("type") == "membership"}
            self.assertAlmostEqual(mem["peer"]["spans"][-1][1], NOW - 60, delta=1)
            self.assertFalse(ob.acc.member["peer"], "the removed role accrues no more time")
            self.assertIsNone(mem["worker"]["spans"][-1][1])
            # a role that leaves the spec without an event closes at the first tick that misses it
            spec = ShoalSpec.load(sp)
            spec.roles = [r for r in spec.roles if r.slug != "worker"]
            spec.save(sp)
            ob.tick(NOW + 30)
            rows = [x for x in map(json.loads, obs.read_text().splitlines())
                    if x.get("type") == "membership" and x["role"] == "worker"]
            self.assertEqual(rows[-1]["exit"], "left-spec")
            self.assertAlmostEqual(rows[-1]["spans"][-1][1], NOW + 30, delta=1)
            _o, m = rt.load_observations(str(obs))
            self.assertAlmostEqual(m["worker"][-1][1], NOW + 30, delta=1, msg="the last record wins")


class JournalSegments(unittest.TestCase):
    """Daily rotation, compression, retention and the merged read (data-volume budget)."""

    def _observer(self, td, sp, t):
        cache = Path(td) / "native.json"
        cache.write_text(json.dumps({
            "v": 1, "shoal": SID, "host": "h", "written_at": t,
            "roles": {"worker": {"native": ad._native("between", t)}}, "errors": {}}))
        return ad.Observer(sp, obs_path=str(Path(td) / "obs.jsonl"),
                           checkpoint_path=str(Path(td) / "acc.json"), cache_paths=[str(cache)],
                           probe=False)

    def test_rotation_compression_merged_read_and_retention(self):
        midnight = 1793577600.0  # a UTC midnight
        before, after = ad._utc_day(midnight - 1), ad._utc_day(midnight)
        with tempfile.TemporaryDirectory() as td:
            sp, _root = make_world(td, now=midnight - 20)
            ob = self._observer(td, sp, midnight - 20)
            ob.tick(midnight - 20)
            ob.tick(midnight + 10)  # crosses the boundary: the first day closes
            seg = Path(td) / f"obs-{before}.jsonl.gz"
            self.assertTrue(seg.is_file(), "the closed day is compressed")
            self.assertFalse((Path(td) / f"obs-{before}.jsonl").exists())
            live = [json.loads(x) for x in (Path(td) / "obs.jsonl").read_text().splitlines()]
            self.assertEqual(live[0]["type"], "segment")
            self.assertEqual(live[0]["policy"], rt.POLICY_VERSION)
            self.assertEqual(sum(1 for x in live if x.get("type") == "membership"), 3,
                             "membership is rewritten in full at the head of each segment")
            # the live path reads the closed segment with it; both segment headers are counted
            policies = []
            obs, mem = rt.load_observations(str(Path(td) / "obs.jsonl"), policies)
            self.assertEqual(len(obs["worker"]), 2)
            self.assertEqual(policies, [rt.POLICY_VERSION, rt.POLICY_VERSION])
            self.assertEqual(set(mem), {"worker", "peer", "parked-one"})
            # retention and recovery: an expired compressed day goes, a plain closed day left by
            # a crash is compressed, at the next day's rotation
            old = Path(td) / "obs-20200101.jsonl.gz"
            old.write_bytes(seg.read_bytes())
            crash_day = ad._utc_day(midnight - 2 * 86400)
            (Path(td) / f"obs-{crash_day}.jsonl").write_text("")
            ob.tick(midnight + 86400 + 10)
            self.assertFalse(old.exists())
            self.assertTrue((Path(td) / f"obs-{crash_day}.jsonl.gz").is_file())
            self.assertTrue((Path(td) / f"obs-{after}.jsonl.gz").is_file())

    def test_journal_written_before_rotation_is_named_for_its_first_day(self):
        midnight = 1793577600.0
        with tempfile.TemporaryDirectory() as td:
            sp, _root = make_world(td, now=midnight + 10)
            first = midnight - 3 * 86400 + 5
            (Path(td) / "obs.jsonl").write_text(json.dumps({"type": "snapshot", "role": "worker", "t": first}) + "\n")
            ob = self._observer(td, sp, midnight + 10)
            ob.tick(midnight + 10)
            self.assertTrue((Path(td) / f"obs-{ad._utc_day(first)}.jsonl.gz").is_file())
            obs, _mem = rt.load_observations(str(Path(td)))  # a directory reads the same set
            self.assertEqual([s["t"] for s in obs["worker"]], [first, midnight + 10])


class PolicyChange(unittest.TestCase):
    def test_a_segment_from_an_older_policy_closes_before_new_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            sp, _root = make_world(td)
            obs = Path(td) / "obs.jsonl"
            obs.write_text(json.dumps({"type": "segment", "policy": rt.LEGACY_POLICY, "opened": NOW - 20}) + "\n"
                           + json.dumps({"type": "snapshot", "role": "worker", "t": NOW - 20}) + "\n")
            ob = ad.Observer(sp, obs_path=str(obs), checkpoint_path=str(Path(td) / "acc.json"), probe=False)
            ob.tick(NOW)
            self.assertTrue((Path(td) / f"obs-{ad._utc_day(NOW)}.jsonl.gz").is_file())
            got, _m = rt.load_observations(str(obs))
            self.assertEqual([x["policy"] for x in got["worker"]], [rt.LEGACY_POLICY, rt.POLICY_VERSION])
            ob.tick(NOW + 30)  # the same policy keeps appending to the same segment
            self.assertEqual(len([x for x in got["worker"]]), 2)
            self.assertEqual(sum(1 for x in map(json.loads, obs.read_text().splitlines())
                                 if x.get("type") == "segment"), 1)


class StateRoot(unittest.TestCase):
    def test_state_root_holds_observations_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            sp, _root = make_world(td)
            ob = ad.Observer(sp, probe=False, state_root=str(Path(td) / "state"))
            ob.tick(NOW)
            base = Path(td) / "state" / SID
            self.assertTrue((base / "role-time-observations.jsonl").is_file())
            self.assertTrue((base / "role-time-accumulator.json").is_file())


class ResourceProjection(unittest.TestCase):
    def test_render_keeps_missing_measurements_missing(self):
        s = {"host": "h1", "ts": 100.0,
             "gpus": [{"uuid": "GPU-1", "vendor": "amd", "utilization_pct": None,
                       "vram_used_mib": 2048.0, "vram_total_mib": 16384.0}],
             "leases": [{"key": "gpu", "scope": "host_gpu"}],
             "queue": [{"key": "gpu"}, {"key": "gpu"}]}
        text = ru.render_prometheus(s)
        self.assertNotIn("shoal_gpu_utilization_percent{", text)
        self.assertIn("shoal_gpu_vram_used_bytes{" + 'host="h1"', text)
        self.assertIn("2147483648", text)
        self.assertIn('shoal_lease_reservations{host="h1",key="gpu",scope="host_gpu"} 1', text)
        self.assertIn('shoal_lease_queue_length{host="h1",key="gpu"} 2', text)

    def test_render_suspended_gpu_and_idle_lease_keys_are_represented(self):
        # a runtime-suspended GPU has no measurable utilization (its counter reads EBUSY): it is
        # drawn as suspended, never as 0 percent. A known lease key with nothing held or queued
        # is a measured zero, so an idle host still appears in the lease panels.
        s = {"host": "h2", "ts": 100.0,
             "gpus": [{"uuid": "GPU-2", "vendor": "amd", "runtime_status": "suspended",
                       "utilization_pct": None, "vram_used_mib": 57.0, "vram_total_mib": 32624.0},
                      {"uuid": "GPU-3", "vendor": "amd", "runtime_status": "active",
                       "utilization_pct": 25.0, "vram_used_mib": 57.0, "vram_total_mib": 32624.0}],
             "lease_provider": {"ok": True, "known_keys": ["gpu", "cpu-heavy", "gpu@GPU-2"]},
             "leases": [], "queue": []}
        text = ru.render_prometheus(s)
        self.assertIn('shoal_gpu_runtime_suspended{host="h2",uuid="GPU-2",vendor="amd"} 1', text)
        self.assertIn('shoal_gpu_runtime_suspended{host="h2",uuid="GPU-3",vendor="amd"} 0', text)
        self.assertNotIn('shoal_gpu_utilization_percent{host="h2",uuid="GPU-2"', text)
        self.assertIn('shoal_gpu_utilization_percent{host="h2",uuid="GPU-3"', text)
        self.assertIn('shoal_lease_reservations{host="h2",key="gpu",scope="host_gpu"} 0', text)
        self.assertIn('shoal_lease_reservations{host="h2",key="gpu@GPU-2",scope="device_gpu"} 0', text)
        self.assertIn('shoal_lease_reservations{host="h2",key="cpu-heavy",scope="host_cpu_memory"} 0', text)
        self.assertIn('shoal_lease_queue_length{host="h2",key="gpu"} 0', text)


if __name__ == "__main__":
    unittest.main()
