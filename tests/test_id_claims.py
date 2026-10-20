"""A plan or periodic session writes its tickets uncommitted in its own worktree, where no branch shows them: the ids
it was handed are claimed in the state dir while it runs, so a `windvane new` meanwhile takes the id after them. And
`new`, which asks no forge, still frees the ids of a plan closed_plans.json already knows was closed."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import stamp  # noqa: E402
from helpers import Sandbox  # noqa: E402

PLAN = "plan-20261019-120000"


class IdClaims(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())  # `-C` changes into the sandbox, which close() removes
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.claims = self.p.state / "id_claims"

    def new(self):
        """`windvane new`'s ticket id."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), "new", "next"])
        return Path(out.getvalue().strip()).stem

    def claim(self, ids, pid, recorded):
        self.claims.mkdir(parents=True, exist_ok=True)
        f = self.claims / "plan.json"
        f.write_text(json.dumps({"ids": ids, "pid": pid, "stamp": recorded}))
        return f

    def test_new_during_a_plan_takes_the_id_after_its_claim(self):
        out = self.s.tmp / "during.txt"
        os.environ["FAKE_SESSION_RUN"] = f"windvane -C {self.s.root} new during > {out}"
        n = planner.plan(self.p, "more", use_followups=False, max_new=3)  # handed T-002..T-004, writes T-002
        self.assertEqual(n, 1)
        self.assertEqual(Path(out.read_text().strip()).name, "T-005.md")
        self.assertEqual(list(self.claims.glob("*")), [])  # released once its branch is pushed

    def test_a_plan_that_writes_nothing_releases_its_claim(self):
        with mock.patch.object(planner, "run_claude", return_value=("", None, "")):
            planner.plan(self.p, "more", use_followups=False, max_new=3)
        self.assertEqual(list(self.claims.glob("*")), [])
        self.assertEqual(self.new(), "T-002")

    def test_a_live_claim_counts(self):
        self.claim(["T-002", "T-003"], os.getpid(), stamp(os.getpid()))
        self.assertEqual(self.new(), "T-004")

    def test_a_dead_claim_is_ignored_and_removed(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        f = self.claim(["T-002", "T-003"], gone.pid, "boot:x:1")
        self.assertEqual(self.new(), "T-002")
        self.assertFalse(f.exists())

    def test_a_reused_pid_is_a_dead_claim(self):
        f = self.claim(["T-002"], os.getpid(), "boot:another boot:1")
        self.assertEqual(self.new(), "T-002")
        self.assertFalse(f.exists())

    def test_a_periodic_chain_skips_its_own_claim(self):
        """Its next session's ids follow the tickets the one before wrote (in its tree), not all it was handed."""
        planner.claim_ids(self.p, "periodic", ["T-002", "T-003"])
        self.assertEqual(planner.free_id(self.p, own="periodic"), "T-002")
        self.assertEqual(planner.free_id(self.p), "T-004")

    def unmerged_plan(self, tid):
        self.s.git("switch", "-q", "-c", PLAN)
        self.s.ticket(tid)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Plan: 1 new ticket")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")
        self.s.git("branch", "-q", "-D", PLAN)

    def test_new_frees_a_known_closed_plans_ids_without_reading_its_tree(self):
        self.unmerged_plan("T-002")
        self.assertEqual(self.new(), "T-003")  # unknown: it keeps its ids
        (self.s.root / ".windvane/tickets/T-003.md").unlink()
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "closed_plans.json").write_text(json.dumps([PLAN]))
        calls = []
        real = planner.gout

        def gout(cwd, *args):
            calls.append(args)
            return real(cwd, *args)

        with mock.patch.object(planner, "gout", gout):
            self.assertEqual(self.new(), "T-002")
        self.assertFalse([c for c in calls if c[0] == "ls-tree" and PLAN in " ".join(c)], calls)


class PeriodicClaims(unittest.TestCase):
    def test_new_during_a_periodic_session_takes_the_id_after_its_claim(self):
        import test_periodic  # the module, not its class: a class imported here would run all its tests again
        t = test_periodic.Periodic()
        t.make(test_periodic.PLAYTEST, tickets=2, rules="WRITE_TICKET")
        self.addCleanup(t.doCleanups)
        out = t.s.tmp / "during.txt"
        os.environ["FAKE_SESSION_RUN"] = f"windvane -C {t.s.root} new during > {out}"
        t.run_pilot()
        runs = [v for k, v in t.sessions() if k == "periodic"]
        self.assertIn("## Ids for new tickets, in order (at most 6)\nT-003, ", runs[0])
        self.assertEqual(Path(out.read_text().strip().splitlines()[0]).name, "T-009.md")
        self.assertEqual(list((t.p.state / "id_claims").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
