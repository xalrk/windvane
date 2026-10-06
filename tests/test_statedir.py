"""The state dir stays lean (on 2026-10-06 it held 2.5G of a full 22G disk) without losing work: idle slot worktrees
with nothing unmerged go, a WIP commit or uncommitted change or running session keeps one; old transcripts and merged
tickets' critic packs go; slot 0's build output goes only on opt-in. Real git worktrees, in a Sandbox's state dir."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import Sandbox
from test_install import REPO
from test_pipeline import args
from test_temp_clean import age

sys.path.insert(0, str(REPO))
from windvane import doctor, driver, phone, scratch, statedir  # noqa: E402
from windvane.config import Project  # noqa: E402

LATER = time.time() + 2 * scratch.AGE  # `now` an hour past every file the test made: the age rule lets them go
OLD = time.time() - 30 * statedir.DAY


def ledger_row(stream, tokens):
    return {"at": time.strftime("%Y-%m-%d %H:%M"), "role": "worker", "label": "T-002", "ticket": "T-002",
            "model": "opus", "effort": "high", "minutes": 3.0, "turns": 9, "estimated": False, "input": tokens,
            "output": 100, "cache_read": 1000, "cache_write": 50, "base": 0, "usd": 0.5, "killed": None,
            "end": "ok", "error": None, "exit": 0, "stream": stream}


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.write(".gitignore", "build/\n")
        self.s.publish("ignore build output")
        self.merge_ticket("t-001-done")
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def merge_ticket(self, branch):
        """A ticket branch merged on main the driver's way, so the queue counts it done."""
        s = self.s
        s.git("switch", "-q", "-c", branch)
        s.write(f"{branch}.txt", "done\n")
        s.git("add", "-A")
        s.git("commit", "-qm", f"{branch}: work")
        s.git("push", "-q", "origin", branch)
        s.git("switch", "-q", "main")
        tid = branch[:5].upper()
        s.git("merge", "-q", "--no-ff", "-m", f"Merge {tid}: done (branch {branch})", branch)
        s.git("push", "-q", "origin", "main")

    def tree(self, name, rev="origin/main"):
        d = self.p.state / name
        self.s.git("worktree", "add", "-q", "--detach", str(d), rev)
        return d

    def listed(self):
        return self.s.git("worktree", "list")


class StateDir(Base):
    def test_idle_merged_slots_go_and_work_in_progress_stays(self):
        gone = self.tree("work-2")
        wip = self.tree("work-1")  # the case of 2026-10-06: a WIP commit the next run resumes
        self.s.git("switch", "-q", "-c", "t-002-wip", cwd=wip)
        (wip / "a.py").write_text("half\n")
        self.s.git("add", "-A", cwd=wip)
        self.s.git("commit", "-qm", "WIP: 7 tests fail", cwd=wip)
        dirty = self.tree("work-3")
        (dirty / "notes.txt").write_text("not committed\n")
        held = self.tree("work-4")  # a running session's
        (self.p.state / "state.json").write_text(json.dumps({"running": {"T-009": 4}}))
        trial = self.tree("ci-1", "origin/main~1")  # the driver's own trial merge of a merged ticket
        self.s.git("merge", "-q", "--no-ff", "-m", "trial", "origin/t-001-done", cwd=trial)
        items, kept = statedir.survey(self.p, procs=set(), now=LATER)
        self.assertEqual(sorted(i.paths[0].name for i in items if i.kind == "worktree"), ["ci-1", "work-2"])
        self.assertEqual(dict(kept), {"work-1": "commits not on origin/main (t-002-wip)",
                                      "work-3": "uncommitted changes", "work-4": "its ticket is running"})
        self.assertEqual(statedir.clean(self.p, procs=set(), now=LATER)[0], 2)
        self.assertFalse(gone.exists() or trial.exists())
        self.assertNotIn(str(gone), self.listed())
        for d in (wip, dirty, held):
            self.assertIn(str(d), self.listed())
        self.assertEqual(self.s.git("log", "-1", "--format=%s", cwd=wip), "WIP: 7 tests fail")
        self.assertTrue((dirty / "notes.txt").exists())

    def test_a_tree_in_use_or_touched_lately_stays(self):
        d = self.tree("work-1")
        self.assertEqual(statedir.survey(self.p, procs=set())[1], [("work-1", "used in the last hour")])
        self.assertEqual(statedir.survey(self.p, procs={str(d / "sub")}, now=LATER)[1],
                         [("work-1", "a process is working in it")])
        self.assertEqual(statedir.survey(self.p, busy={1}, procs=set(), now=LATER)[1],
                         [("work-1", "its ticket is running")])
        self.assertEqual(statedir.survey(self.p, keep_slots=2, procs=set(), now=LATER)[1],
                         [("work-1", "this run may use it")])

    def test_old_transcripts_go_but_an_unmerged_tickets_newest_stay_and_stats_holds(self):
        runs = self.p.state / "runs"
        runs.mkdir()
        names = {"T-001": [f"2026090{i}-100000-T-001{x}.jsonl" for i, x in ((1, ""), (2, "-review"))],
                 "T-002": [f"2026090{i}-100000-T-002.jsonl" for i in range(1, 6)],
                 "other": ["20260901-090000-preflight.jsonl"]}
        for i, f in enumerate(sorted(sum(names.values(), []))):
            (runs / f).write_text('{"type": "result"}\n' * 100)
            os.utime(runs / f, (OLD + i, OLD + i))
        (runs / "20261006-100000-T-001.jsonl").write_text("{}\n")  # this week's
        ledger = self.p.state / "ledger.jsonl"
        ledger.write_text("".join(json.dumps(ledger_row(f, 1000 * i)) + "\n" for i, f in enumerate(names["T-002"])))
        stats = lambda: subprocess.run(["windvane", "stats"], cwd=self.s.root, capture_output=True, text=True).stdout
        before = stats()
        self.assertIn("worker", before)
        item, = statedir.reclaimable(self.p, procs=set())
        self.assertEqual(item.what, "runs/ (5 transcripts over 14 days old)")
        statedir.clean(self.p, procs=set())
        self.assertEqual(sorted(f.name for f in runs.iterdir()),
                         ["20260903-100000-T-002.jsonl", "20260904-100000-T-002.jsonl",
                          "20260905-100000-T-002.jsonl", "20261006-100000-T-001.jsonl"])
        self.assertEqual(stats(), before)  # stats reads the ledger, not the transcripts

    def test_a_merged_tickets_old_critic_pack_goes_an_unmerged_ones_stays(self):
        packs = {t: self.p.state / "critic" / t for t in ("T-001", "T-002")}
        for d in packs.values():
            d.mkdir(parents=True)
            (d / "notes.md").write_text("User-facing: yes\n")
            (d / "shot.png").write_bytes(b"x" * 5000)
        self.assertEqual(statedir.reclaimable(self.p, procs=set()), [])  # merged, but this week's
        for d in packs.values():
            age(d, OLD)
        item, = statedir.reclaimable(self.p, procs=set())
        self.assertEqual((item.what, item.size), ("critic/ (1 merged ticket's packs: T-001)", 5000 + 17))
        statedir.clean(self.p, procs=set())
        self.assertEqual((packs["T-001"].exists(), packs["T-002"].exists()), (False, True))

    def build_output(self):
        work = self.tree("work")
        (work / "build").mkdir()
        (work / "build" / "app.o").write_bytes(b"x" * 4096)
        return work

    def test_slot_0s_build_output_goes_only_with_trim_build_and_only_when_idle(self):
        work = self.build_output()
        statedir.clean(self.p, procs=set(), now=LATER)
        self.assertTrue((work / "build" / "app.o").exists())  # off by default
        machine = Path(os.environ["XDG_CONFIG_HOME"]) / "windvane" / "machine.toml"
        machine.parent.mkdir(parents=True, exist_ok=True)
        machine.write_text("[state]\ntrim_build = true\n")
        statedir.clean(self.p, busy={0}, procs=set(), now=LATER)
        self.assertTrue((work / "build" / "app.o").exists())  # its ticket is running
        statedir.clean(self.p, procs=set())
        self.assertTrue((work / "build" / "app.o").exists())  # built in the last hour
        item, = statedir.reclaimable(self.p, procs=set(), now=LATER)
        self.assertEqual((item.what, item.size), ("work/ build output (ignored files)", 4096))
        statedir.clean(self.p, procs=set(), now=LATER)
        self.assertFalse((work / "build").exists())
        self.assertTrue((work / "README").exists() and (work / ".git").exists())  # the tree itself stays
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text()
                     + "[state]\ntrim_build = false\n")
        self.assertFalse(statedir.settings(Project(self.s.root))["trim_build"])  # the project's word wins

    def test_bad_settings_stop_rather_than_delete_more(self):
        for bad in ("[state]\nkeep_days = -1\n", "[state]\nkeep_runs = \"3\"\n", "[state]\nkeepdays = 1\n"):
            self.s.write(".windvane/config.toml", "name = \"sandbox\"\n" + bad)
            with self.assertRaises(SystemExit):
                statedir.settings(Project(self.s.root))

    def test_the_driver_keeps_its_slots_between_tickets_and_drops_them_at_the_runs_end(self):
        d = self.tree("work-1")
        age(d, OLD)
        pilot = driver.Pilot(self.p, args(parallel=2))
        pilot.clean_state(between=True)
        self.assertTrue(d.exists())  # the loop may prepare slot 1 at any moment
        pilot.clean_state(between=False)
        self.assertFalse(d.exists())
        self.assertIn("state dir: freed", (self.p.state / "autopilot.log").read_text())


class DoctorClean(Base):
    def other(self):
        """A second project on the machine, with an idle merged slot."""
        root = self.s.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.s.remote), str(root)], check=True)
        (root / ".windvane/config.toml").write_text('name = "other"\nforge = "git"\n')
        q = Project(root)
        q.state.mkdir(parents=True)
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(q.state / "work-1"), "origin/main"],
                       cwd=root, check=True, capture_output=True)
        age(q.state / "work-1", OLD)
        phone.register(q)
        return q

    def doctor(self, *a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.main(self.p, *a)
        return out.getvalue()

    def test_dry_run_lists_sizes_and_removes_nothing_and_a_running_project_is_skipped(self):
        q = self.other()
        mine = self.tree("work-2")
        age(mine, OLD)
        out = self.doctor(True, True)
        self.assertRegex(out, r"other \(.*\): would free \d+K\n +\d+K  work-1 \(idle worktree, nothing unmerged\)")
        self.assertRegex(out, r"sandbox \(.*\): would free \d+K\n +\d+K  work-2 ")
        self.assertTrue((q.state / "work-1").exists() and mine.exists())
        with open(self.p.state / "lock", "w") as lock:  # the sandbox's autopilot is running
            fcntl.flock(lock, fcntl.LOCK_EX)
            out = self.doctor(True)
        self.assertIn("sandbox: skipped, its autopilot is running", out)
        self.assertRegex(out, r"other \(.*\): freed \d+K")
        self.assertFalse((q.state / "work-1").exists())
        self.assertTrue(mine.exists())

    def test_doctor_holds_the_run_lock_while_it_removes_and_a_run_starting_then_waits(self):
        q = self.other()
        seen, remove = [], statedir.remove

        def watched(p, item):
            seen.append((driver.locked(p), (p.state / statedir.CLEANING).exists()))
            if not seen[1:]:  # a run starts in the middle of the clean: it waits, then takes the lock
                pilot = driver.Pilot(p, args())
                self.addCleanup(lambda: pilot.lockfile.close())
                threading.Thread(target=lambda: started.append(pilot.take_lock() or time.time())).start()
                time.sleep(0.5)
            try:
                return remove(p, item)
            finally:
                removed.append(time.time())  # the clean lets go of this project's lock after this

        started, removed = [], []
        with mock.patch.object(statedir, "remove", watched):
            self.doctor(True)
        for _ in range(50):
            if started:
                break
            time.sleep(0.1)
        self.assertEqual(seen, [(True, True)])
        self.assertTrue(started and started[0] >= removed[0], (started, removed))  # only once the clean let go
        self.assertFalse((q.state / statedir.CLEANING).exists())

    def test_a_run_that_finds_another_run_still_refuses_at_once(self):
        with open(self.p.state / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            t = time.time()
            with self.assertRaises(SystemExit):
                driver.Pilot(self.p, args()).take_lock()
        self.assertLess(time.time() - t, 1)

    def test_doctor_names_the_biggest_reclaimable_item(self):
        self.assertIn(("ok", f"state dir {self.p.state}: nothing to reclaim", ""), doctor.state_warnings(self.p))
        big = self.p.state / "critic" / "T-001"
        big.mkdir(parents=True)
        (big / "shot.png").write_bytes(b"\0" * (doctor.BIG + 1))
        age(big, OLD)
        (level, what, fix), = doctor.state_warnings(self.p)
        self.assertEqual((level, fix), ("warn", "windvane doctor --clean"))
        self.assertIn("can go (biggest: critic/ (1 merged ticket's packs: T-001), 100M)", what)



if __name__ == "__main__":
    unittest.main()
