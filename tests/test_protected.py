"""`protected`: what measures windvane (the bench's seeds and hidden tests, the judge's rules, the scoring, and the
config keys that set protection and the experiments' budget) is out of every session's reach. A ticket branch that
changes it is held for the user, not merged; a plan or periodic branch the same; and the bench refuses to score with
a yardstick edited in its measured worktree."""
import json
import os
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import bench, driver, held  # noqa: E402
from windvane.config import Project, protected_changes  # noqa: E402
from windvane.util import git, now  # noqa: E402
from windvane.worktree import clean_detach, ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_plan_retry import Base  # noqa: E402

HIDDEN = "bench/x/hidden/test_a.py"


def with_bench(s):
    """A project with a bench: `protected` defaults on."""
    s.write(HIDDEN, "def test_a():\n    assert True\n")
    s.write("prompts/JUDGE.md", "# Judge\nTry it as its user would.\n")


class Tickets(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        with_bench(self.s)
        self.s.ticket("T-001")
        self.s.publish()
        self.config = (self.s.root / ".windvane/config.toml").read_text()
        self.pushes = []
        self.addCleanup(setattr, driver, "push", driver.push)
        driver.push = lambda title, msg="", *a, **k: self.pushes.append((title, msg))

    def run_pilot(self):
        pilot = driver.Pilot(Project(self.s.root), Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0,
                                                             answer_wait=10, replan=0, goal="", parallel=1))
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def sessions(self):
        f = self.s.tmp / "calls.jsonl"
        return [s for s in (json.loads(l)["system"].split("\n")[0] for l in f.read_text().splitlines())
                if s.startswith("# windvane")]

    def held(self):
        return [(t, m) for t, m in self.pushes if t.startswith("Held")]

    def merged_on_main(self):
        self.s.git("fetch", "-q", "origin")
        return "Merge T-001" in self.s.git("log", "--format=%s", "origin/main")

    def assert_held(self, path):
        pilot = self.run_pilot()
        self.assertEqual((pilot.merged, pilot.failed, list(pilot.aside)), ([], [], ["T-001"]))
        self.assertFalse(self.merged_on_main())
        self.assertIn("refs/heads/t-001-work", self.s.git("ls-remote", "--heads", "origin"))  # the branch is kept
        self.assertEqual(self.sessions(), ["# windvane worker run"])  # held before any critic, check or review
        [(title, msg)] = self.held()
        self.assertEqual(title, "Held: do T-001 (T-001)")
        self.assertTrue(msg.startswith(f"Not merged: it changes protected paths: {path}.\n"), msg)
        self.assertIn("land it by hand: merge it into main: `git merge origin/t-001-work` on main", msg)
        [line] = driver.protected_lines(Project(self.s.root))
        self.assertTrue(line.startswith(f"held for you: T-001 (t-001-work) changes protected paths ({path}); "),
                        line)
        pilot = self.run_pilot()  # unchanged: the next run pays nothing and says nothing to find it again
        self.assertEqual((len(self.sessions()), pilot.merged, list(pilot.aside), len(self.held())),
                         (1, [], ["T-001"], 1))

    def test_a_ticket_changing_a_hidden_test_is_held_and_landed_by_hand(self):
        os.environ["FAKE_LEAK"] = f"{HIDDEN}=def test_a():\n    pass"
        self.assert_held(HIDDEN)
        self.s.git("merge", "-q", "--no-ff", "origin/t-001-work", "-m", "Merge branch 't-001-work'")  # as the push says
        self.s.git("push", "-q", "origin", "main")
        self.assertEqual(driver.protected_lines(Project(self.s.root)), [])  # landed: no longer listed
        pilot = self.run_pilot()  # nor held, nor set aside, nor worked again: the queue counts it merged
        self.assertEqual((list(pilot.aside), pilot.held_protected, len(self.sessions())), ([], {}, 1))
        self.assertIn("T-001: t-001-work was landed by hand; main has it",
                      (Project(self.s.root).state / "autopilot.log").read_text())

    def test_a_ticket_changing_the_judges_rules_is_held(self):
        os.environ["FAKE_LEAK"] = "prompts/JUDGE.md=# Judge\nSay yes."
        self.assert_held("prompts/JUDGE.md")

    def test_a_ticket_changing_the_configs_goal_merges(self):
        os.environ["FAKE_LEAK"] = f'.windvane/config.toml=goal = "a smaller bench"\n{self.config}'
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(self.held(), [])

    def test_a_ticket_changing_the_configs_protected_line_is_held(self):
        os.environ["FAKE_LEAK"] = f".windvane/config.toml=protected = []\n{self.config}"
        self.assert_held(".windvane/config.toml (protected)")


class Plans(Base):
    def test_a_plan_branch_adding_a_file_under_bench_is_held(self):
        with_bench(self.s)
        self.s.publish("bench")
        self.p = Project(self.s.root)
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        clean_detach(self.p, cd, self.p.upstream)
        (cd / "bench/x/hidden/test_b.py").write_text("def test_b():\n    pass\n")
        git(cd, "add", "-A")
        ok, where = held.land(self.p, cd, "plan-x", "Plan: 1 new ticket", "body")
        self.assertFalse(ok)
        self.assertEqual(self.git_pushes, 0)
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {"plan-x": "land"})
        [(title, msg)] = self.pushes
        self.assertEqual(title, "Not landed: Plan: 1 new ticket")
        self.assertTrue(msg.startswith("It changes protected paths (bench/x/hidden/test_b.py); if that's right, "
                                       "land it by hand: `git push origin plan-x`, then merge it into main"), msg)
        self.assertEqual(held.push_held(self.p), 0)  # the next run checks it again: still held, no second push
        self.assertEqual((self.git_pushes, len(self.pushes)), (0, 1))
        self.assertIn("not pushed: plan-x (Plan: 1 new ticket): it changes protected paths", held.held_lines(self.p)[0])


class Changes(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def test_the_default_needs_a_bench_and_always_guards_its_own_key(self):
        self.assertEqual(Project(self.s.root).protected, [])
        with_bench(self.s)
        self.assertIn("bench/", Project(self.s.root).protected)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text('protected = ["data/"]\n' + cfg.read_text())
        self.assertEqual(Project(self.s.root).protected, ["data/", ".windvane/config.toml:protected"])

    def test_a_protected_key_in_a_table_or_on_many_lines_and_a_prefix_not_a_name(self):
        rules = ["bench/", ".windvane/config.toml:experiment_*", ".windvane/config.toml:protected"]
        base = self.s.git("rev-parse", "HEAD")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text('protected = [\n  "bench/",\n]\n' + cfg.read_text() + "experiment_repeat = 2\n")
        self.s.write("benchmarks.md", "not under bench/\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "x")
        self.assertEqual(protected_changes(self.s.root, base, "HEAD", rules),
                         [".windvane/config.toml (critic.experiment_repeat)", ".windvane/config.toml (protected)"])
        base = self.s.git("rev-parse", "HEAD")
        cfg.write_text(cfg.read_text().replace('  "bench/",\n', '  "bench/", "data/",\n'))
        self.s.git("commit", "-qam", "y")
        self.assertEqual(protected_changes(self.s.root, base, "HEAD", rules), [".windvane/config.toml (protected)"])


class Bench(unittest.TestCase):
    def test_an_uncommitted_edit_under_bench_in_the_measured_worktree_is_refused(self):
        s = Sandbox()
        self.addCleanup(s.close)
        with_bench(s)
        s.publish("bench")
        made = bench.ref_worktree

        def edited(p, ref, path):  # what a session getting past its deny rules could do
            sha = made(p, ref, path)
            (path / HIDDEN).write_text("def test_a():\n    pass\n")
            return sha
        self.addCleanup(setattr, bench, "ref_worktree", made)
        bench.ref_worktree = edited
        a = Namespace(dry_run=False, hours=1, cap=None, week_cap=None)
        with self.assertRaises(SystemExit) as e:
            bench.one(Project(s.root), a, "HEAD", "full", None)
        self.assertRegex(str(e.exception), rf"^bench: refusing to score: {HIDDEN} in the measured worktree \S+ "
                                           r"differ from [0-9a-f]{7}'s committed tree; hidden tests and scoring must "
                                           "be the ref's own$")
        self.assertEqual(bench.results(Project(s.root)).exists(), False)  # nothing scored


if __name__ == "__main__":
    unittest.main()
