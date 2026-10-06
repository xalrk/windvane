"""T-105: `windvane critic --project` judges the whole project on main against its CRITIC.md, one session per
dimension (in parallel when asked), joins them into one report, and feeds the planner its new follow-ups; the points
an open ticket covers become that ticket's notes. A `project-critic` [[periodic]] preset runs it every N merges."""
import contextlib
import io
import json
import os
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, critic, driver  # noqa: E402
from windvane import periodic as periodic_mod  # noqa: E402
from windvane.config import Project, periodic  # noqa: E402
from windvane.runner import run_claude  # noqa: E402
from windvane.util import now, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

SCALE = """# Scale
## works
- 2 runs: Does it run?
- 5 tested: Is it tested?
- 8 safe: Is it safe?
## clear
- 2 readable: Can a user read the output?
- 8 documented: Is every command documented?
## lean
- 5 small: Is it small?
- 8 fast: Is it fast?
"""
AUDIT = ('[[periodic]]\nname = "audit"\nbuiltin = "project-critic"\nevery = 2\nminutes = 30\nthen = "triage"\n'
         '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n')


class ProjectCritic(unittest.TestCase):
    def make(self, config="", tickets=2):
        self.s = Sandbox(config)
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.s.write(".windvane/CRITIC.md", SCALE)
        for i in range(1, tickets + 1):
            self.s.ticket(f"T-00{i}", f"T-00{i - 1}" if i > 1 else "")
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\n")
        self.s.publish()
        self.p = Project(self.s.root)
        os.environ["FAKE_PROJECT_BEYOND_works"] = "- add a retry (covered by T-001)|- a faster start (new)"
        os.environ["FAKE_PROJECT_BEYOND_clear"] = "- A faster start. (new)|- the old thing (covered by T-009)"

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def calls(self):
        f = self.s.tmp / "calls.jsonl"
        return [c for c in map(json.loads, f.read_text().splitlines() if f.exists() else [])]

    def project_calls(self):
        return [c for c in self.calls() if c["prompt"].startswith("Critique the project")]

    def records(self):
        return [r for r in read_jsonl(self.p.state / "critic.jsonl") if r.get("ticket") == critic.PROJECT]

    def test_one_session_per_dimension_in_parallel_joined_into_one_report(self):
        self.make()
        os.environ["FAKE_PROJECT_BARRIER"] = "3"  # each answers no unless all three run at once
        os.environ["FAKE_PROJECT_NO"] = "fast"
        out = self.cli("critic", "--project", "--parallel", "3")
        calls = self.project_calls()
        self.assertEqual(sorted(re.search(r"the (\S+) dimension", c["prompt"]).group(1) for c in calls),
                         ["clear", "lean", "works"])
        for c in calls:  # read-only: no edits, no commits, no pushes
            self.assertTrue({"Edit", "Write", "Bash(git commit *)", "Bash(git push *)"} <= set(c["deny"]))
            self.assertIn("Bash", c["flags"][c["flags"].index("--tools") + 1])
        report = out[out.index("Project critic on"):]  # after the log lines
        self.assertIn("works=8 clear=8 lean=5", report.splitlines()[0])
        self.assertIn("## works: 8\n- runs (2): yes, seen", out)
        self.assertIn("- fast (8): no, README:1 says so", out)  # every no with its evidence
        self.assertEqual(out.count("a faster start"), 1)  # joined, the same point once
        self.assertIn("Beyond (covered by open tickets):\n- T-001: add a retry", out)
        self.assertIn("For the user:\n- decide works", out)
        rec = self.records()[-1]
        self.assertEqual(rec["dims"], {"works": 8, "clear": 8, "lean": 5})
        self.assertEqual(rec["new"], ["a faster start", "the old thing", "improve lean"])  # T-009 isn't open
        self.assertEqual(rec["covered"], {"T-001": ["add a retry"]})
        ledger = read_jsonl(self.p.state / "ledger.jsonl")
        self.assertEqual(sum(r.get("role") == "critic:project" and not r.get("event") for r in ledger), 3)
        score = [r for r in ledger if r.get("role") == "critic:project" and r.get("event") == "score"][-1]
        self.assertEqual((score["score"], score["dims"]), (5, rec["dims"]))
        # the covered point is T-001's note on main
        t1 = self.s.git("show", "origin/main:.windvane/tickets/T-001.md")
        self.assertTrue(t1.startswith("# T-001: do T-001\n"))
        self.assertIn("## From the project critic", t1)
        self.assertIn("- add a retry", t1)
        # a following plan gets the new points, not the covered one
        self.cli("plan", "--merge")
        plan = [c for c in self.calls() if c["system"].startswith("# windvane planner")][-1]["prompt"]
        self.assertIn("- (PROJECT) a faster start", plan)
        self.assertIn("- (PROJECT) improve lean", plan)
        self.assertNotIn("add a retry", plan)
        self.assertEqual(critic.followups(self.p), [])  # used by that plan
        table = self.cli("stats").splitlines()
        self.assertTrue(any(line.startswith("critic:project    3 ") for line in table))  # named whole
        self.assertLessEqual(max(map(len, table[:4])), 100)

    def test_dims_picks_a_subset_and_last_shows_the_scores(self):
        self.make()
        os.environ["FAKE_PROJECT_NO"] = "tested"
        self.cli("critic", "--project", "--dims", "works, lean")
        self.assertEqual(len(self.project_calls()), 2)
        self.assertEqual(self.records()[-1]["dims"], {"works": 2, "lean": 8})
        last = self.cli("critic", "--project", "--last")
        self.assertIn("works=2 lean=8  (2 new, 1 covered)", last)
        with self.assertRaises(SystemExit) as e:
            self.cli("critic", "--project", "--dims", "nope")
        self.assertIn("no dimension nope", str(e.exception.code))
        self.assertEqual(len(self.project_calls()), 2)  # nothing ran

    def test_its_verdicts_stay_out_of_the_ticket_calibration(self):
        self.make()
        self.cli("critic", "--project", "--dims", "works")
        recs = read_jsonl(self.p.state / "critic.jsonl")
        self.assertIsNone(critic.calibration(recs, 8))
        self.assertEqual(critic.rates(recs), {})

    def test_it_never_changes_the_tree_or_the_repo(self):
        self.make()
        os.environ["FAKE_PROJECT_EDIT"] = "1"
        self.s.write("earlier.txt", "an earlier session's file\n")  # left by an earlier session in a chain: kept
        head = self.s.git("rev-parse", "HEAD")

        def run(role, label, prompt, system, allow, deny):
            return run_claude(self.p, role, label, prompt, system, self.s.root, allow, deny)
        rec, report = critic.project(self.p, self.s.root, run, ["works", "clear"], 2)
        self.assertEqual(rec["dims"], {"works": 8, "clear": 8})
        self.assertEqual(self.s.git("rev-parse", "HEAD"), head)  # its commit undone
        self.assertEqual((self.s.root / "README").read_text(), "hi\n")
        self.assertFalse((self.s.root / "stray-critic.txt").exists())
        self.assertTrue((self.s.root / "earlier.txt").exists())
        self.assertEqual(self.s.git("status", "--porcelain"), "?? earlier.txt")
        # and through the desk command, nothing of its edits reaches main
        self.cli("critic", "--project", "--dims", "works")
        self.assertEqual(self.s.git("show", "origin/main:README"), "hi")

    def test_the_preset_runs_after_n_merges_and_chains_to_then(self):
        self.make(AUDIT, tickets=3)
        os.environ["FAKE_PROJECT_BEYOND_works"] = "- add a retry (covered by T-003)|- a faster start (new)"
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged[:3], ["T-001", "T-002", "T-003"])
        order = []
        for c in self.calls():
            kind = ("critic" if c["prompt"].startswith("Critique the project") else "triage"
                    if c["system"].startswith("# windvane periodic") else
                    c["prompt"].split()[1].rstrip(".") if c["system"].startswith("# windvane worker") else "")
            if kind and (not order or order[-1] != kind):
                order.append(kind)
        self.assertEqual(order[:5], ["T-001", "T-002", "critic", "triage", "T-003"])
        self.assertEqual(len(self.project_calls()) % 3, 0)  # every round: one session per dimension
        triage = [c for c in self.calls() if c["system"].startswith("# windvane periodic")][0]["prompt"]
        self.assertIn("## The previous session's output\nProject critic on origin/main", triage)
        self.assertIn("- (PROJECT) a faster start", triage)  # its new points, as follow-ups
        self.assertIn("audit: Project critic on origin/main", (self.p.state / "autopilot.log").read_text())
        t3 = self.s.git("show", "origin/main:.windvane/tickets/T-003.md")  # open when the audit ran
        self.assertIn("## From the project critic", t3)
        self.assertIn("- add a retry", t3)

    def test_a_failed_dimension_is_named_the_rest_still_scored(self):
        self.make()
        os.environ["FAKE_ERROR_FOR"] = "the lean dimension"  # its session dies with an API error
        out = self.cli("critic", "--project")
        self.assertIn("works=8 clear=8 lean=-", out)
        self.assertIn("## lean: no verdict (", out)
        rec = self.records()[-1]
        self.assertEqual((rec["dims"], rec["failed"]), ({"works": 8, "clear": 8}, ["lean"]))
        self.assertIn("no verdict on lean", self.cli("critic", "--project", "--last"))
        self.assertEqual(critic.calibration(read_jsonl(self.p.state / "critic.jsonl"), 8), None)

    def test_no_verdict_at_all_fails_and_pushes(self):
        self.make()
        pushes = []
        orig = periodic_mod.push
        periodic_mod.push = lambda title, msg="", *a, **k: pushes.append((title, msg))
        self.addCleanup(setattr, periodic_mod, "push", orig)
        os.environ["FAKE_ERROR_FOR"] = "Critique the project"  # every dimension's session dies
        with self.assertRaises(SystemExit) as e:
            self.cli("critic", "--project")
        self.assertIn("gave no verdict", str(e.exception.code))
        self.assertIn(("project-critic round failed", "The project-critic round didn't finish: the critic gave no "
                       "verdict on any dimension."), pushes)
        self.assertEqual(self.records()[-1]["step"], "unavailable")
        self.assertEqual(critic.followups(self.p), [])
        self.assertEqual(self.s.git("log", "-1", "--format=%s", "origin/main"), "tickets")  # nothing landed

    def test_config(self):
        e = periodic([{"name": "audit", "builtin": "project-critic", "dims": "works, Lean", "every": 5}])["audit"]
        self.assertEqual((e["builtin"], e["dims"], e["parallel"], e["prompt"]), ("project-critic", ["works", "lean"], 4, ""))
        for bad in ({"name": "a", "builtin": "nope"}, {"name": "a"}, {"name": "a", "builtin": "project-critic",
                                                                       "parallel": "x"}):
            with self.assertRaises(SystemExit):
                periodic([bad])


if __name__ == "__main__":
    unittest.main()
