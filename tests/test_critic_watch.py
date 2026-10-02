import argparse
import io
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

# (ticket, score, fixes, step), oldest first: the first self-runs' 15 tickets as critic.jsonl has them. 0 fixes
# always scored 8 and 1 fix 7 (T-004 aside); 10 of 15 first scores, 8 of the last 10, are 8.
COLLAPSED = [("T-001", 7, 1, "revise"), ("T-001", 8, 0, "pass"), ("T-002", 8, 0, "pass"), ("T-003", 7, 2, "revise"),
             ("T-003", 8, 0, "pass"), ("T-004", 8, 1, "pass"), ("T-005", 7, 1, "revise"), ("T-005", 8, 0, "pass"),
             ("T-006", 8, 0, "pass"), ("T-017", 8, 0, "pass"), ("T-017", 8, 0, "pass"), ("T-008", 8, 0, "pass"),
             ("T-020", 8, 0, "pass"), ("T-019", 8, 0, "pass"), ("T-018", None, 0, "skip"), ("T-013", 8, 0, "pass"),
             ("T-021", 8, 0, "pass"), ("T-014", 7, 1, "revise"), ("T-014", 8, 0, "pass"), ("T-021", 8, 0, "pass"),
             ("T-010", 8, 0, "pass"), ("T-011", 7, 1, "revise"), ("T-011", 8, 0, "pass")]
# A scale that measures: first scores 3-9, and the same fix count lands on different scores.
SPREAD = [("T-001", 4, 2, "revise"), ("T-001", 7, 0, "scope"), ("T-002", 9, 0, "pass"), ("T-003", 6, 1, "revise"),
          ("T-003", 8, 0, "pass"), ("T-004", 3, 3, "rethink"), ("T-004", 6, 1, "revise"), ("T-004", 9, 0, "pass"),
          ("T-005", 8, 0, "pass"), ("T-006", 5, 1, "revise"), ("T-006", 10, 0, "pass"), ("T-007", 7, 2, "revise"),
          ("T-007", 8, 0, "pass")]
# First scores spread out (6, 5, 4), but the score is only the fix count: 0 → 9, 1 → 6, 2 → 5, 3 → 4.
BY_FIXES = [(f"T-00{i}", s, f, st) for i, (s, f) in enumerate([(6, 1), (5, 2), (4, 3), (6, 1), (5, 2), (4, 3)], 1)
            for s, f, st in ((s, f, "revise"), (9, 0, "pass"))]

LINE = "Critic scale collapsed: 8 of 10 first scores are 8; recalibrate at the desk"


class CollapseCheck(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = false", "enabled = true"))
        self.s.publish("critic on")
        self.p = Project(self.s.root)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def record(self, rows):
        for t, score, fixes, step in rows:
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": t, "sha": "abc1234", "score": score,
                                                         "fixes": fixes, "step": step, "gist": "g"})

    def run_cli(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(argparse.Namespace(**kw), self.p)
        return out.getvalue()

    def gate(self, *tids):
        """Each ticket's verdict through the driver's critic gate, as after a worker's run."""
        pilot = driver.Pilot(self.p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                               replan=0, goal="", parallel=1))
        with redirect_stdout(io.StringIO()):
            for tid in tids:
                self.assertTrue(pilot.critic_gate(tid, "abc1234def"))
        return (self.p.state / "autopilot.log").read_text()

    def test_the_first_self_runs_verdicts_trip_it_once(self):
        self.record(COLLAPSED)
        log = self.gate("T-010", "T-011", "T-021")
        self.assertEqual(log.count("Critic scale collapsed"), 1)
        self.assertIn(LINE, log)
        self.assertEqual(self.pushes, [("sandbox: critic scale collapsed",
                                        "8 of 10 first scores are 8; recalibrate at the desk")])
        self.assertIn(LINE, self.run_cli(cli.cmd_overview).splitlines())
        self.assertIn(LINE, self.run_cli(cli.cmd_stats, critic=True, last=10).splitlines())

    def test_a_spread_scale_doesnt(self):
        self.record(SPREAD)
        self.assertNotIn("collapsed", self.gate("T-006", "T-007"))
        self.assertEqual(self.pushes, [])
        self.assertNotIn("collapsed", self.run_cli(cli.cmd_overview))
        self.assertNotIn("collapsed", self.run_cli(cli.cmd_stats, critic=True, last=10))

    def test_fewer_than_five_tickets_never_do(self):
        self.record([r for r in COLLAPSED if r[0] in ("T-001", "T-002", "T-003", "T-004")])
        self.assertNotIn("collapsed", self.gate("T-003", "T-004"))
        self.assertEqual(self.pushes, [])
        self.assertIsNone(critic.collapsed([{"ticket": f"T-00{i}", "score": 8, "fixes": 0} for i in range(4)]))

    def test_a_recalibration_starts_afresh(self):
        self.record(COLLAPSED)
        self.s.write(".windvane/CRITIC.md", "# Scale\nworks: 2 crashes, 5 ..., 8 ...\n")
        new = [{"ticket": f"T-03{i}", "score": 8, "fixes": 0, "step": "pass", "scale": critic.scale(self.p, self.s.root)}
               for i in range(4)]
        for r in new:
            append_jsonl(self.p.state / "critic.jsonl", r)
        self.assertIsNone(critic.collapsed(critic.read_jsonl(self.p.state / "critic.jsonl")))  # 4 on the new scale
        append_jsonl(self.p.state / "critic.jsonl", {**new[0], "ticket": "T-035"})
        self.assertIn("5 of 5 first scores are 8", critic.collapsed(critic.read_jsonl(self.p.state / "critic.jsonl")))

    def test_a_score_set_by_the_fix_count(self):
        recs = [{"ticket": t, "score": s, "fixes": f, "step": st} for t, s, f, st in BY_FIXES]
        self.assertEqual(critic.collapsed(recs), "Critic scale collapsed: the fix count sets the score (0 fixes: 9, "
                                                 "1 fix: 6, 2 fixes: 5, 3 fixes: 4) in 12 of 12 verdicts; "
                                                 "recalibrate at the desk")
        recs[0]["score"] = 7  # one miss in 12 still trips it; two don't
        self.assertIn("in 11 of 12", critic.collapsed(recs))
        recs[2]["score"] = 3
        self.assertIsNone(critic.collapsed(recs))


if __name__ == "__main__":
    unittest.main()
