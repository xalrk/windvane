"""A night alone: a detached run's driver that crashes is restarted with its deadline and what it had done (at most
3 times an hour); a ticket counts as done only from a merge of its own branch; a stale wip branch waits its turn."""
import io
import os
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue, merged_branches  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import REPO, Sandbox  # noqa: E402


class Supervisor(unittest.TestCase):
    """`supervise` around the real driver, run in this process as `windvane run` (cli.main) would run it."""

    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes, self.calls = [], []
        self.orig = driver.push, driver.Pilot.pick
        driver.push = lambda title, msg="", *a, **k: self.pushes.append((title, msg))

    def tearDown(self):
        driver.push, driver.Pilot.pick = self.orig
        os.chdir(REPO)
        self.s.close()

    def crash_when(self, when):
        """The driver's loop raises when `when(pilot)` holds."""
        pick = self.orig[1]

        def crashing(pilot, free):
            if when(pilot):
                raise KeyError("slot 7")
            return pick(pilot, free)
        driver.Pilot.pick = crashing

    def start(self, args, env):
        """The driver as `supervise` starts it, here: its exit code, a traceback's 1 for an exception."""
        self.calls.append(list(args))
        os.environ.clear()
        os.environ.update(env)
        try:
            with redirect_stdout(io.StringIO()):
                cli.main(args)
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1
        except Exception:
            return 1
        return 0

    def supervise(self, *run_args):
        argv = ["-C", str(self.s.root), "run", *run_args, "--parallel", "1", "--no-preflight"]
        deadline = cli.parse_deadline(None, "2h")
        with redirect_stdout(io.StringIO()):
            return driver.supervise(self.p, argv, deadline, start=self.start, sleep=lambda s: None), deadline

    def titles(self):
        return [t for t, _ in self.pushes if t.startswith("windvane ")]

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_a_crash_mid_run_restarts_with_the_same_deadline_and_finishes_the_queue(self):
        crashed = []
        self.crash_when(lambda pilot: pilot.merged == ["T-001"] and not crashed and not crashed.append(1))
        rc, deadline = self.supervise("--for", "2h")
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][-3:], ["--until", f"{deadline:%Y-%m-%d %H:%M}", "--no-preflight"])
        self.assertEqual(self.titles(), ["windvane started", "windvane restarted", "windvane stopped"])
        self.assertIn("KeyError: 'slot 7'", dict(self.pushes)["windvane restarted"])
        self.assertIn("driver crashed (KeyError: 'slot 7'); restart 1 of 3 this hour", self.log())
        # the restarted driver knew T-001 was merged before the crash: the final summary has both
        self.assertIn("Merged: do T-001 (T-001); do T-002 (T-002)", self.pushes[-1][1])
        q = Queue(self.p)
        self.assertEqual((q.status("T-001"), q.status("T-002")), ("done", "done"))

    def test_a_crash_beside_a_running_ticket_lets_it_finish_and_never_reruns_it(self):
        """The crashing driver waits for its running tickets (they merge or end as usual) before it exits, so a
        restart starts no worker session again for them: a crash costs no ticket twice."""
        self.s.ticket("T-001", touches="a.txt")
        self.s.ticket("T-002", touches="b.txt")
        self.s.publish("touches")
        os.environ["FAKE_WORK_S"] = "1"
        crashed = []
        self.crash_when(lambda pilot: "T-001" in pilot.running and not crashed and not crashed.append(1))
        argv = ["-C", str(self.s.root), "run", "--for", "2h", "--parallel", "2", "--no-preflight"]
        with redirect_stdout(io.StringIO()):
            driver.supervise(self.p, argv, cli.parse_deadline(None, "2h"), start=self.start, sleep=lambda s: None)
        self.assertEqual(len(self.calls), 2)
        log = self.log()
        self.assertLess(log.index("T-001: merged"), log.index("driver crashed"))  # finished before the exit
        self.assertEqual((log.count("T-001: worker run"), log.count("T-002: worker run")), (1, 1))
        self.assertIn("Merged: do T-001 (T-001); do T-002 (T-002)", self.pushes[-1][1])

    def test_a_fourth_crash_in_an_hour_stops_with_one_push(self):
        self.crash_when(lambda pilot: True)
        rc, _ = self.supervise("--for", "2h")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.titles(), ["windvane started"] + ["windvane restarted"] * 3 + ["windvane stopped"])
        why = self.pushes[-1][1]
        self.assertIn("Why: the driver crashed (KeyError: 'slot 7'); not restarted: crashed 4 times in an hour.", why)
        self.assertIn("Merged: none.", why)  # the driver's own summary rides along: no second push from it

    def test_a_stop_and_the_deadline_are_not_restarted(self):
        def stop_asked(pilot):
            (self.p.state / "STOP").touch()
            return False
        self.crash_when(stop_asked)
        self.assertEqual(self.supervise("--for", "2h")[0], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Why: asked to stop.", self.pushes[-1][1])
        self.calls, self.pushes = [], []
        self.assertEqual(self.supervise("--for", "10m")[0], 0)  # inside min_slot (30 min): the deadline
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Why: deadline.", self.pushes[-1][1])

    def test_a_crash_after_a_stop_is_not_restarted(self):
        def crash_after_stop(pilot):
            (self.p.state / "STOP").write_text("")
            return True
        self.crash_when(crash_after_stop)
        self.supervise("--for", "2h")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.titles(), ["windvane started", "windvane stopped"])
        self.assertIn("not restarted: asked to stop", self.pushes[-1][1])

    def test_a_killed_driver_is_pushed_not_restarted(self):
        """Its sessions may still be running: a restart would resume their worktrees beside them. Someone's own
        stop (SIGTERM, Ctrl-C) says nothing."""
        for sig, pushes in ((signal.SIGKILL, 1), (signal.SIGTERM, 0), (signal.SIGINT, 0)):
            self.calls, self.pushes = [], []
            rc = driver.supervise(self.p, ["run", "--for", "2h"], now() + timedelta(hours=2),
                                  start=lambda args, env: self.calls.append(args) or -sig, sleep=lambda s: None)
            self.assertEqual((rc, len(self.calls), len(self.pushes)), (-sig, 1, pushes))
        self.p.state.mkdir(parents=True, exist_ok=True)
        driver.supervise(self.p, ["run"], now() + timedelta(hours=2), start=lambda a, e: -signal.SIGKILL)
        self.assertEqual(self.pushes, [("windvane stopped", "Why: the driver was killed (SIGKILL). Not "
                                        "restarted: its sessions may still be running in the worktrees.")])

    def test_a_moved_deadline_is_the_restarts(self):
        moved = (now() + timedelta(hours=5)).replace(second=0, microsecond=0)

        def crash(args, env):
            self.calls.append(args)
            if len(self.calls) > 1:
                return 0
            self.p.state.mkdir(parents=True, exist_ok=True)
            (self.p.state / "DEADLINE").write_text(moved.isoformat())
            (self.p.state / "crash.json").write_text('{"error": "OSError: disk", "summary": ""}')
            return 1
        driver.supervise(self.p, ["run", "--for", "2h"], now() + timedelta(hours=2), start=crash, sleep=lambda s: None)
        self.assertEqual(self.calls[1], ["run", "--until", f"{moved:%Y-%m-%d %H:%M}", "--no-preflight"])

    def test_the_command_detach_starts_restarts_a_crashed_driver_process(self):
        """`windvane run ... --supervise` (what --detach runs in tmux), with the driver in a process of its own that
        raises once after the first merge (a sitecustomize patches it in, as no product code may)."""
        (self.s.tmp / "inject").mkdir()
        (self.s.tmp / "inject/sitecustomize.py").write_text(SITE.format(repo=str(REPO), flag=str(self.s.tmp / "crashed")))
        r = subprocess.run(["windvane", "-C", str(self.s.root), "run", "--for", "2h", "--parallel", "1",
                            "--no-preflight", "--supervise"], capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONPATH": str(self.s.tmp / "inject")})
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("RuntimeError: injected", r.stderr)  # the traceback stays in the tmux pane
        log = self.log()
        self.assertIn("driver crashed (RuntimeError: injected); restart 1 of 3 this hour", log)
        self.assertIn("autopilot restarted, until", log)
        self.assertIn("Why: queue empty. Merged: do T-001 (T-001); do T-002 (T-002).", log)
        r = subprocess.run(["windvane", "run", "--help"], capture_output=True, text=True)
        self.assertNotIn("--supervise", r.stdout)  # internal: what --detach starts, not for the user


SITE = """import os, sys
sys.path.insert(0, {repo!r})
from windvane import driver
pick = driver.Pilot.pick
def crashing(self, free):
    if self.merged and not os.path.exists({flag!r}):
        open({flag!r}, "w").close()
        raise RuntimeError("injected")
    return pick(self, free)
driver.Pilot.pick, driver.RESTART_PAUSE = crashing, 0
sys.path.pop(0)
"""


class Done(unittest.TestCase):
    """Only a merge of the ticket's own branch marks it done."""

    def setUp(self):
        self.s = Sandbox()
        for t in ("T-001", "T-002"):
            self.s.ticket(t)
        self.s.publish()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def merge(self, branch):
        self.s.git("switch", "-q", "-c", branch)
        self.s.write(f"{branch}.txt", "x\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", branch)
        self.s.git("switch", "-q", "main")
        self.s.git("merge", "-q", "--no-ff", "--no-edit", branch)
        self.s.git("push", "-q", "origin", "main")
        self.s.git("fetch", "-q")

    def test_another_branch_with_the_id_does_not_count(self):
        self.merge("q-t-002-scope")
        self.assertEqual(Queue(self.p).status("T-002"), "todo")
        self.merge("t-002-build")
        self.assertEqual(Queue(self.p).status("T-002"), "done")

    def test_the_merge_subjects_git_and_github_write(self):
        subjects = "\n".join([
            "Merge T-012: retry (branch t-012-retry)",
            "Merge pull request #5 from owner/t-013-x",
            "Merge branch 't-014-x' into main",
            "Merge remote-tracking branch 'origin/t-015-x'",
            "Merge pull request #6 from owner/q-t-062d-scope",
            "Merge branch 'q-t-063-x'",
            "Merge T-024: Change T-021's answer, see t-021-old (branch t-024-auto)",
        ])
        self.assertEqual(merged_branches(subjects, "t"), ["t-012", "t-013", "t-014", "t-015", "t-024"])


class Stale(unittest.TestCase):
    """A wip branch nobody committed to for `stale_days` waits for its turn in QUEUE.md's order."""

    def setUp(self):
        self.s = Sandbox()
        for t in ("T-001", "T-002"):
            self.s.ticket(t)
        self.s.write(".windvane/QUEUE.md", "# Queue\n- T-001\n- T-002\n")
        self.s.publish()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def wip(self, days_ago):
        when = f"{(now() - timedelta(days=days_ago)):%Y-%m-%dT%H:%M:%S}"
        self.s.git("switch", "-q", "-c", "t-002-half")
        self.s.write("half.txt", "x\n")
        self.s.git("add", "-A")
        subprocess.run(["git", "commit", "-qm", "half"], cwd=self.s.root, check=True,
                       env={**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when})
        self.s.git("push", "-q", "origin", "t-002-half")
        self.s.git("switch", "-q", "main")
        self.s.git("fetch", "-q")

    def test_a_fresh_wip_branch_is_resumed_first(self):
        self.wip(1)
        self.assertEqual(Queue(self.p).pick(), ("T-002", "wip", ["t-002-half"]))

    def test_a_stale_wip_branch_waits_for_its_turn(self):
        self.wip(4)
        q = Queue(self.p)
        self.assertEqual(q.pick(), ("T-001", "todo", []))
        self.assertEqual(q.pick(skip={"T-001"}), ("T-002", "wip", ["t-002-half"]))  # resumed, in its turn
        self.assertIn("[t-002-half, stale]", q.listing())


if __name__ == "__main__":
    unittest.main()
