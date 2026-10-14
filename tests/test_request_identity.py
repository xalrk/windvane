"""`windvane request`'s gaps (T-130): request files are written whole, requests sent back from a closed plan are held
on it again when its PR reopens, and a session is known by its process tree, not by an env it can change."""
import os
import subprocess
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import planner, requests  # noqa: E402
import test_request_reopen  # noqa: E402
from test_request_reopen import CLOSED  # noqa: E402
from test_requests import TRIAGE  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
# CLOSED's PR reads closed unmerged until the `reopened` file beside this script exists, then open again
GH = f"""#!/bin/sh
here="$(dirname "$0")"
echo "$*" >> "$here/gh.log"
case "$*" in
  *"--head {CLOSED} "*) if [ -f "$here/reopened" ]; then s=OPEN; else s=CLOSED; fi
                         echo '[{{"number": 1, "state": "'$s'"}}]';;
  *"--head "*) echo '[{{"number": 2, "state": "OPEN"}}]';;
  *"--state open"*) [ -f "$here/reopened" ] && echo '[{{"number": 1, "state": "OPEN", "headRefName": "{CLOSED}"}}]';;
esac
exit 0
"""


class Base(unittest.TestCase):
    R = test_request_reopen.RequestReopen  # its helpers, not its tests
    make, cli, file, inbox, github, plan_branch, accept = R.make, R.cli, R.file, R.inbox, R.github, R.plan_branch, R.accept


class WholeWrites(Base):
    def leftovers(self):
        return sorted(f.name for d in (requests.inbox(), requests.inbox() / "done") for f in d.glob(".*.tmp"))

    def test_a_write_cut_off_leaves_the_old_file_and_no_partial_one(self):
        self.make()
        f = requests.inbox() / "R-001.md"
        f.parent.mkdir(parents=True)
        f.write_text("old\n")
        for target, error in (("os.replace", OSError(28, "No space left on device")),
                              ("os.fsync", OSError(28, "No space left on device"))):
            with unittest.mock.patch(target, side_effect=error), self.assertRaises(OSError):
                requests.write_whole(f, "new and much longer\n")
            self.assertEqual(f.read_text(), "old\n")
            self.assertEqual(self.leftovers(), [])
        with unittest.mock.patch("os.replace", side_effect=OSError(28, "full")), self.assertRaises(OSError):
            requests.write_whole(requests.inbox() / "R-002.md", "new\n")
        self.assertEqual(sorted(p.name for p in requests.inbox().iterdir() if p.is_file()), ["R-001.md"])

    def test_a_verdict_or_a_reopen_cut_off_moves_nothing_and_is_done_whole_next_time(self):
        self.make(TRIAGE)
        self.file("Retry the check on a flaky network")
        before = self.inbox()["R-001"]
        full = unittest.mock.patch("os.replace", side_effect=OSError(28, "No space left on device"))
        with full, self.assertRaises(OSError):
            self.accept("REQUEST: R-001 accept T-009", {"T-009": CLOSED})
        self.assertEqual((self.inbox(), self.inbox(done=True), self.leftovers()), ({"R-001": before}, {}, []))
        self.accept("REQUEST: R-001 accept T-009", {"T-009": CLOSED})
        held = self.inbox(done=True)["R-001"]
        with full, self.assertRaises(OSError):
            requests.reopen({CLOSED})
        self.assertEqual((self.inbox(), self.inbox(done=True), self.leftovers()), ({}, {"R-001": held}, []))
        self.assertEqual(requests.reopen({CLOSED}), {"R-001": CLOSED})  # not skipped as already there
        self.assertIn(f"\n## Reopened (", self.inbox()["R-001"])
        self.assertEqual(self.inbox(done=True), {})

    def test_a_reopen_cut_between_its_write_and_its_unlink_is_finished(self):
        self.make(TRIAGE)
        self.file("Retry the check on a flaky network")
        self.accept("REQUEST: R-001 accept T-009", {"T-009": CLOSED})
        with unittest.mock.patch("pathlib.Path.unlink", side_effect=OSError("cut")), self.assertRaises(OSError):
            requests.reopen({CLOSED})
        self.assertEqual((list(self.inbox()), list(self.inbox(done=True))), (["R-001"], ["R-001"]))
        moved = self.inbox()["R-001"]
        self.assertEqual(requests.reopen({CLOSED}), {})
        self.assertEqual((self.inbox(), self.inbox(done=True)), ({"R-001": moved}, {}))


class Rehold(Base):
    def test_a_request_reopened_from_a_plan_is_held_again_when_its_pr_reopens(self):
        self.make(TRIAGE)
        self.github()
        (self.s.bin / "gh").write_text(GH)
        self.plan_branch("T-009", CLOSED)
        self.file("Retry the check on a flaky network")
        self.file("A stats column for cost")
        self.accept("REQUEST: R-001 accept T-009\nREQUEST: R-002 accept T-009", {"T-009": CLOSED})
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(planner.pending_branches(self.p), {})  # closed: both back in the inbox
        self.assertEqual(sorted(self.inbox()), ["R-001", "R-002"])
        self.accept("REQUEST: R-002 question which column?", {})  # triaged again since: not this fix's to move

        (self.s.bin / "reopened").touch()
        self.assertEqual(planner.pending_branches(self.p), {"T-009": CLOSED})
        self.assertEqual(list(self.inbox()), ["R-002"])
        text = self.inbox(done=True)["R-001"]
        self.assertIn(f"- Verdict: accepted → T-009 (held on {CLOSED})\n", text)
        self.assertIn(f"): {CLOSED} was reopened\n", text)
        self.assertEqual([f.stem for f in requests.pending()], [])  # no triage tickets it twice
        self.assertIn(f"R-001: held on {CLOSED} again", (self.p.state / "autopilot.log").read_text())
        self.assertEqual(requests.rehold({CLOSED}), {})  # a second pass changes nothing
        self.assertEqual(planner.pending_branches(self.p), {"T-009": CLOSED})
        self.assertEqual(self.inbox(done=True)["R-001"], text)
        self.assertEqual(list(self.inbox()), ["R-002"])

    def test_a_rehold_that_fails_is_tried_again(self):
        self.make(TRIAGE)
        self.github()
        (self.s.bin / "gh").write_text(GH)
        self.plan_branch("T-009", CLOSED)
        self.file("Retry the check on a flaky network")
        self.accept("REQUEST: R-001 accept T-009", {"T-009": CLOSED})
        self.s.git("fetch", "-q", "origin")
        planner.pending_branches(self.p)
        (self.s.bin / "reopened").touch()
        with unittest.mock.patch.object(requests, "write_whole", side_effect=OSError(28, "full")):
            self.assertEqual(planner.pending_branches(self.p), {"T-009": CLOSED})  # its ids are claimed anyway
        self.assertEqual(list(self.inbox()), ["R-001"])
        self.assertIn("not held again yet", (self.p.state / "autopilot.log").read_text())
        planner.pending_branches(self.p)
        self.assertEqual((list(self.inbox()), list(self.inbox(done=True))), ([], ["R-001"]))


class ProcessIdentity(Base):
    def role_in_child(self, **env):
        """`role_here()` in a grandchild of this process (sh, then python3), started from outside the state dir with
        the env a worker could leave: no session name, and a desk claim."""
        env = {**{k: v for k, v in os.environ.items() if k != requests.NEST}, requests.ROLE: "desk",
               "PYTHONPATH": str(REPO), **env}
        r = subprocess.run(["sh", "-c", 'python3 -c "from windvane import requests; print(requests.role_here())"; :'],
                           cwd=self.s.tmp, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def session(self, stem, pid, role="worker"):
        d = self.p.state / "nested" / stem
        d.mkdir(parents=True)
        (d / "role").write_text(role)
        (d / "session").write_text(f"{pid} 1 -")
        return d / "session"

    def test_a_session_is_known_by_its_process_whatever_its_env_says(self):
        self.make()
        self.assertEqual(self.role_in_child(), "desk")  # no session above it: the user's shell
        self.session("20261009-050000-other", 999999999)  # someone else's session, not an ancestor
        self.assertEqual(self.role_in_child(), "desk")
        own = self.session("20261009-050001-T-009", os.getpid())  # this test stands in for the session's claude
        self.assertEqual(self.role_in_child(), "worker")
        self.assertEqual(self.role_in_child(**{requests.ROLE: "planner"}), "worker")
        self.assertEqual(self.role_in_child(**{requests.NEST: "20261009-050000-other"}), "worker")
        (own.parent / "role").write_text("desk")  # no session is the desk
        self.assertEqual(self.role_in_child(), "session")
        old = time.time() - 86400  # a record older than this process: its pid was a session's that ended
        os.utime(own, (old, old))
        self.assertEqual(self.role_in_child(), "desk")

    def test_a_worker_that_hides_its_env_still_files_under_its_cap(self):
        self.make()
        self.session("20261009-050001-T-009", os.getpid())
        env = {k: v for k, v in os.environ.items() if k != requests.NEST}
        outs = [subprocess.run(["sh", "-c", f'windvane request "{t}"'], cwd=self.s.tmp, capture_output=True,
                               text=True, timeout=60, env={**env, requests.ROLE: "desk"})
                for t in ("Retry the check on a flaky network", "A stats column for cost", "Quiet hours on Sunday")]
        self.assertEqual([o.returncode for o in outs], [0, 0, 1], [o.stdout + o.stderr for o in outs])
        self.assertIn("refused: a worker run files at most 2", outs[2].stdout + outs[2].stderr)
        self.assertIn("- Role: worker", self.inbox()["R-001"])


if __name__ == "__main__":
    unittest.main()
