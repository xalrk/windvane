"""The experiment mix (mix.py): experiment tickets name their kind of idea, a planning round's experiments follow
`[experiments] mix` moved toward the kinds the bench keeps but never under `floor`, a repeat of a rejected
mechanism needs a `Different because:` line, a rejected bold experiment gets one revision with the per-seed diff,
and a plateau runs one exploration round before planning stops. The planner, the worker and the bench are fakes."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, experiment, mix, planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_bench_periodic import rec  # noqa: E402
import test_experiments as te  # noqa: E402
from test_experiments import MAIN  # noqa: E402
from test_merge_draft import args  # noqa: E402

RECORD = ("\n### T-050: a stricter review (2026-10-20)\n- Idea: a stricter review\n- Kind: fix\n"
          "- Mechanism: worker prompt\n- Files: prompts/WORKER.md, tests/test_prompts.py\n"
          "- main aaaaaaa (1 run) → T-050 bbbbbbb (2 runs): hidden tests 4/10 → 3/10\n"
          "- Verdict: hidden tests down; branch `t-050-x` kept\n")
# the fake planner's own tickets: written by FAKE_SESSION_RUN in the plan's tree, from a JSON list of
# (id, idea, mechanism, touches, extra line); the fake then writes its usual non-experiment ticket as the first id
WRITE = ("import json, os, sys\n"
         "os.makedirs('.windvane/tickets', exist_ok=True)\n"
         "for tid, idea, mech, touches, extra in json.load(open(sys.argv[1])):\n"
         "    dep, extra = (extra, '') if extra.startswith('Depends on:') else ('Depends on: nothing.', extra)\n"
         "    open(f'.windvane/tickets/{tid}.md', 'w').write(f'# {tid}: idea {tid}\\n{dep}\\n'\n"
         "        f'Touches: {touches}.\\n' + (f'Kind: experiment · Idea: {idea} · Mechanism: {mech}\\n' if idea\n"
         "        else 'Kind: feature\\n') +\n"
         "        f'{extra}\\n\\n## Goal\\nx\\n')\n"
         "    open('.windvane/QUEUE.md', 'a').write(f'- {tid} idea {tid}\\n')\n")


class Base(unittest.TestCase):
    def make(self, record=RECORD):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.write("bench/tiny/seed/README.md", "tiny\n")
        self.s.write("bench/tiny/hidden/bench.toml", "")
        if record:
            self.s.write(".windvane/EXPERIMENTS.md", experiment.HEADER + record)
        self.s.publish()
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def verdicts(self, kind, kept, of):
        """`of` decided experiments of `kind`, the first `kept` kept, as the ledger holds them."""
        for i in range(of):
            row = {"event": "experiment", "verdict": "kept"} if i < kept else {"event": "outcome",
                                                                              "outcome": "rejected"}
            append_jsonl(self.p.state / "ledger.jsonl", {**row, "ticket": f"T-{kind[:2]}{i}", "idea": kind})

    def plan(self, tickets, max_new=10):
        """A plan whose fake planner writes `tickets`; the prompt it got, the tickets on main and the log."""
        spec = self.s.tmp / "tickets.json"
        spec.write_text(json.dumps(tickets))
        script = self.s.tmp / "write.py"
        script.write_text(WRITE)
        with mock.patch.dict("os.environ", {"FAKE_SESSION_RUN": f"{sys.executable} {script} {spec}"}):
            planner.plan(self.p, "make it better", merge=True, max_new=max_new)
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        prompt = [c["prompt"] for c in calls if c["system"].startswith("# windvane planner run")][-1]
        self.s.git("fetch", "-q", "origin")
        on_main = {Path(n).stem for n in self.s.git("ls-tree", "--name-only", "origin/main",
                                                    ".windvane/tickets/").split()}
        return prompt, on_main, (self.p.state / "autopilot.log").read_text()


class Mix(Base):
    def test_a_planning_rounds_experiments_follow_the_mix(self):
        self.make()
        fix = [(f"T-00{i}", "fix", f"thing {i}", f"windvane/f{i}.py", "") for i in range(2, 8)]
        prompt, on_main, log = self.plan(fix + [("T-008", "bold", "steps", "windvane/driver.py", ""),
                                                ("T-009", "scout", "outside", "windvane/x.py", "")])
        self.assertIn("## This round's experiment mix\nWrite this round's experiments in this mix: fix 4, bold 3, "
                      "ablation 2, revisit 1; `scout` ideas from outside come on top. Writing fewer, keep to the mix",
                      prompt)
        # seven experiments besides the scout: fix's share of them, rounded up, is three: the first three in order
        self.assertEqual(on_main & {f"T-00{i}" for i in range(1, 10)}, {"T-001", "T-002", "T-003", "T-004", "T-008",
                                                                        "T-009"})
        for tid in ("T-005", "T-006", "T-007"):
            self.assertIn(f"plan: dropped {tid} (fix): past fix's share of this round's experiments (fix 3, bold 2, "
                          "ablation 2, revisit 2)", log)
        queue = self.s.git("show", "origin/main:.windvane/QUEUE.md")
        self.assertNotIn("T-006", queue)
        self.assertIn("- T-004 idea T-004", queue)

    def test_a_plan_written_to_the_prompts_counts_keeps_them_all_after_a_run_of_fixes(self):
        self.make()
        for i in range(1, 6):
            self.s.write(f".windvane/tickets/T-00{i}.md", f"# T-00{i}: old fix {i}\nDepends on: nothing.\n"
                         f"Kind: experiment · Idea: fix · Mechanism: m{i}\n")
        self.s.publish()
        # the queue's recent experiments are all fixes, so the prompt asks for more of the other kinds than their
        # share of six, rounded up: the plan written to those counts loses none of them
        wrote = [("T-007", "bold", "a", "windvane/a.py", ""), ("T-008", "bold", "b", "windvane/b.py", ""),
                 ("T-009", "bold", "c", "windvane/c.py", ""), ("T-010", "ablation", "d", "windvane/d.py", ""),
                 ("T-011", "ablation", "e", "windvane/e.py", ""), ("T-012", "revisit", "f", "windvane/f.py", "")]
        prompt, on_main, log = self.plan(wrote, max_new=6)
        self.assertIn("in this mix: bold 3, ablation 2, revisit 1;", prompt)
        self.assertLessEqual({t for t, *_ in wrote}, on_main)
        self.assertNotIn("plan: dropped", log)

    def test_a_kind_rarely_kept_keeps_its_floor(self):
        self.make()
        self.verdicts("fix", 8, 10)
        self.verdicts("bold", 0, 10)
        cfg = self.p.cfg["experiments"]
        share = mix.shares(cfg["mix"], cfg["floor"], mix.rates(mix.decided(self.p, {})))
        self.assertGreaterEqual(share["bold"], 0.1)
        self.assertGreater(share["fix"], 0.4)  # moved toward the kind that's kept
        self.assertAlmostEqual(sum(share.values()), 1)
        share = mix.round_shares(self.p, self.p.root, {})
        self.assertGreaterEqual(mix.allot(share, 6)["bold"], 1)
        # small rounds: the recent experiments left bold out, so the next one-slot round is bold's
        q = mock.Mock(files=[f"T-1{i:02}" for i in range(12)], kinds={f"T-1{i:02}": "experiment" for i in range(12)},
                      ideas={f"T-1{i:02}": "fix" if i < 9 else "ablation" if i < 11 else "revisit" for i in range(12)})
        self.assertEqual(mix.planned(q), {"fix": 9, "ablation": 2, "revisit": 1})
        self.assertEqual(mix.plan_counts(self.p, self.p.root, 1, q), {"fix": 0, "bold": 1, "ablation": 0,
                                                                       "revisit": 0})
        ids = ["X2-10", "X2-11", "X2-9"]  # a prefix with a digit: sorted by the number after it
        q = mock.Mock(files=ids, kinds=dict.fromkeys(ids, "experiment"), ideas={"X2-9": "bold", "X2-10": "fix",
                                                                              "X2-11": "fix"})
        self.assertEqual(mix.planned(q, most=2), {"fix": 2})
        self.assertEqual(mix.shares({"fix": 1, "bold": 0}, 0.1, {}, ("fix", "bold")), {"fix": 0.9, "bold": 0.1})

    def test_windvane_experiments_shows_each_kinds_share_kept_rate_and_last_kept(self):
        self.make()
        self.verdicts("fix", 8, 10)
        self.verdicts("bold", 0, 10)
        out = mix.report(self.p, {}, {"T-fi7": "trim the critic's notes"})
        self.assertIn("  kind       share  kept   last kept", out)
        self.assertIn("  bold         10%  0/10   -", out)
        self.assertIn("  fix          57%  8/10   T-fi7 trim the critic's notes", out)
        self.assertNotIn("scout ", "\n".join(out[1:]))  # no row while no scout idea was decided
        (self.s.root / ".windvane/EXPERIMENTS.md").unlink()
        self.assertIn("  revisit       0%  -      - (no rejected experiment to revisit yet)",
                      mix.report(self.p, {}, {}))
        with mock.patch("sys.stdout") as stdout:
            cli.cmd_experiments(mock.Mock(what=[]), self.p)
        self.assertIn("experiment mix (kept: of each kind's last 10 the bench decided; scout ideas come on top)", "".join(c.args[0] for c in stdout.write.call_args_list))

    def test_the_config_refuses_an_unknown_kind_or_a_floor_too_high(self):
        from windvane import config
        self.make()
        for c in ({"mix": {"fix": 1, "wild": 1}, "floor": 0.1}, {"mix": {"fix": 0}, "floor": 0.1},
                  {"mix": {"fix": 1}, "floor": 0.25}):
            with self.subTest(c=c), self.assertRaises(SystemExit):
                config.mix(c)
        self.assertIn(".windvane/config.toml:mix", self.p.protected)


class Novelty(Base):
    def test_a_repeat_of_a_rejected_mechanism_needs_a_different_because_line(self):
        self.make()
        prompt, on_main, log = self.plan([
            ("T-002", "fix", "Worker prompt", "prompts/WORKER.md", ""),
            ("T-003", "revisit", "worker prompt", "prompts/WORKER.md", "Different because: a new model reads it"),
            ("T-004", "fix", "worker prompt", "windvane/other.py", "")])  # the same mechanism, other files: new
        self.assertIn("## Experiments the bench rejected (.windvane/EXPERIMENTS.md), by the mechanism each changed, "
                      "newest first\nworker prompt:\n- T-050 (fix, 2026-10-20): a stricter review · prompts/WORKER.md, "
                      "tests/test_prompts.py · hidden tests down", prompt)
        self.assertNotIn("T-002", on_main)
        self.assertIn("plan: dropped T-002: it changes the same mechanism (worker prompt) and files as T-050, which "
                      "the bench rejected, with no `Different because:` line", log)
        self.assertTrue({"T-003", "T-004"} <= on_main)  # a revisit and a fix of two: each within its share

    def test_a_new_ticket_depending_on_a_dropped_one_goes_with_it(self):
        self.make()
        _, on_main, log = self.plan([
            ("T-002", "fix", "worker prompt", "prompts/WORKER.md", ""),
            ("T-003", "revisit", "worker check", "windvane/driver.py", "Depends on: T-002."),
            ("T-004", "", "", "docs/x.md", "Depends on: T-003.")])
        self.assertFalse({"T-002", "T-003", "T-004"} & on_main)
        self.assertIn("plan: dropped T-003: it depends on T-002, dropped", log)
        self.assertIn("plan: dropped T-004: it depends on T-003, dropped", log)

    def test_only_the_last_20_rejected_count(self):
        old = "".join(RECORD.replace("T-050", f"T-{i:03}").replace("worker prompt", f"m{i}") for i in range(100, 120))
        recs = mix.records_of(experiment.HEADER + RECORD + old)
        head = "Touches: prompts/WORKER.md.\nKind: experiment · Idea: fix · Mechanism: worker prompt\n"
        self.assertIsNone(mix.repeats(head, head, recs))
        self.assertEqual(mix.repeats(head, head, recs[-20:] + recs[:1])["id"], "T-050")


class Bold(unittest.TestCase):
    """The experiment tests' sandbox and fake bench, borrowed without their tests."""
    make, run_pilot, on_main, log = (te.Experiment.make, te.Experiment.run_pilot, te.Experiment.on_main,
                                     te.Experiment.log)

    def test_a_rejected_bold_experiment_gets_one_revision_with_the_per_seed_diff(self):
        self.make(kind="Kind: experiment · Idea: bold\n")
        worse = {**MAIN, "passed": 2}
        pilot = self.run_pilot([worse] * 4)
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        workers = [c["prompt"] for c in calls if c["system"].startswith("# windvane worker run")]
        self.assertEqual(len(workers), 2)
        self.assertIn("A bold experiment gets one revision", workers[1])
        self.assertIn("- tiny S-0: hidden tests 4/10 → 2/10", workers[1])
        self.assertIn("T-001: bold experiment rejected by the bench (hidden tests down)", self.log())
        self.assertIn("back to its worker for its one revision", self.log())
        heads = list(dict.fromkeys(c for c in self.calls if c != self.main))
        self.assertEqual(len(heads), 2)  # two heads benched: the first and its revision
        self.assertEqual(pilot.merged, [])
        self.assertIn("- rejected: T-001 (hidden 4→2, judge 3→3, tokens/ticket +0%)",
                      self.on_main(".windvane/QUEUE.md").splitlines())  # the second rejection closes it
        record = self.on_main(".windvane/EXPERIMENTS.md")
        self.assertIn("### T-001: a shorter worker prompt", record)
        self.assertIn("- Kind: bold", record)
        self.assertIn(f"→ T-001 {heads[1][:7]} (2 runs)", record)

    def test_the_revision_counts_once_another_head_was_sent_back(self):
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        p.state.mkdir(parents=True, exist_ok=True)
        self.assertFalse(experiment.revised(p, "T-001", "a" * 40))
        row = {"event": "experiment", "ticket": "T-001", "verdict": "revise", "sha": "a" * 40}
        append_jsonl(p.state / "ledger.jsonl", row)
        # killed before its feedback was saved: the same head benched again is sent back again
        self.assertFalse(experiment.revised(p, "T-001", "a" * 40))
        self.assertTrue(experiment.revised(p, "T-001", "b" * 40))  # the revision's own head: closed
        append_jsonl(p.state / "ledger.jsonl", row)
        self.assertTrue(experiment.revised(p, "T-001", "a" * 40))  # a revision that pushed nothing new: no loop

    def test_the_diff_names_the_judge_checks_that_moved(self):
        a, b = rec("a" * 7, seed="tiny"), rec("b" * 7, seed="tiny", passed=6)
        a["judge"]["answers"] = [{"id": "c1", "answer": "yes", "reason": ""}, {"id": "c2", "answer": "yes",
                                                                              "reason": ""}]
        b["judge"]["answers"] = [{"id": "c1", "answer": "yes", "reason": ""}, {"id": "c2", "answer": "no",
                                                                              "reason": "the help text is gone"}]
        got = experiment.seed_diff([{"name": "main", "sha": "a" * 7, "runs": [[a]]},
                                    {"name": "T-1", "sha": "b" * 7, "runs": [[b]]}])
        self.assertEqual(got, ["- tiny S-0: hidden tests 8/10 → 6/10",
                               "- tiny judge c2: yes → no (the help text is gone)"])


class Plateau(Base):
    def test_eight_rejections_start_an_exploration_round_and_only_its_all_rejected_end_stops_planning(self):
        self.make()
        for i in range(8):
            append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": f"T-0{i}",
                                                         "outcome": "rejected"})
        pilot = driver.Pilot(self.p, args(goal="make it better"))
        with mock.patch.object(experiment, "push") as push:
            pilot.plateau_watch()
        self.assertEqual((push.call_count, experiment.stopped(self.p)), (0, ""))
        self.assertIn("one exploration round (bold, ablation and scout ideas only)",
                      (self.p.state / "autopilot.log").read_text())
        prompt, on_main, log = self.plan([("T-002", "fix", "a", "windvane/a.py", ""),
                                          ("T-003", "bold", "b", "windvane/b.py", ""),
                                          ("T-004", "ablation", "c", "windvane/c.py", "")])
        self.assertIn("## This round explores (a plateau: the last experiments were all rejected)\nOnly `bold`, "
                      "`ablation` and `scout` experiments this round in this mix: bold 6, ablation 4;", prompt)
        self.assertIn("plan: dropped T-002 (fix): this round explores, with no fix experiments", log)
        self.assertEqual(mix.exploring(self.p)["ids"], ["T-003", "T-004"])
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": "T-003", "outcome": "rejected"})
        with mock.patch.object(experiment, "push") as push:
            pilot.plateau_watch()
        self.assertEqual((push.call_count, experiment.stopped(self.p)), (0, ""))
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": "T-004", "outcome": "failed"})
        with mock.patch.object(experiment, "push") as push:  # failed before its bench: it closes the round too
            pilot.plateau_watch()
        self.assertEqual(push.call_count, 1)
        self.assertIn("exploration round after them kept none (1 rejected, 1 failed; a plateau)",
                      experiment.stopped(self.p))
        self.assertIn("kept none (1 rejected, 1 failed), so no more are planned", push.call_args.args[1])

    def test_a_plan_with_no_experiment_leaves_the_exploration_round_waiting(self):
        self.make()
        experiment.save_json(experiment.plateau_file(self.p), {"explore": {"at": 0, "ids": None}})
        _, on_main, _ = self.plan([("T-002", "", "", "windvane/a.py", "")])  # a desk plan of one feature
        self.assertIn("T-002", on_main)
        self.assertIsNone(mix.exploring(self.p)["ids"])
        with mock.patch.object(experiment, "push") as push:
            driver.Pilot(self.p, args(goal="make it better")).plateau_watch()
        self.assertEqual((push.call_count, experiment.stopped(self.p)), (0, ""))

    def test_a_round_experiment_parked_retried_or_gone_is_told_apart(self):
        self.make()
        self.s.write(".windvane/tickets/T-003.md", "# T-003: x\nDepends on: nothing.\n")
        self.s.write(".windvane/tickets/T-004.md", "# T-004: y\nDepends on: nothing.\n")
        self.s.write(".windvane/QUEUE.md", "- parked: T-004 (no room)\n")
        self.s.publish()
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": "T-003", "outcome": "failed"})
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": "T-003", "outcome": "retried"})
        self.assertEqual(experiment.fates(self.p, ["T-003", "T-004", "T-777"]),
                         {"T-003": None, "T-004": "parked", "T-777": "gone"})

    def test_a_round_still_on_an_unmerged_plan_branch_stays_open(self):
        self.make()
        self.s.git("checkout", "-q", "-b", "plan-20261025-000000")
        self.s.write(".windvane/tickets/T-095.md", "# T-095: explore\nDepends on: nothing.\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Plan: 1 ticket")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("checkout", "-q", "main")
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(experiment.fates(self.p, ["T-095", "T-096"]), {"T-095": None, "T-096": "gone"})
        experiment.save_json(experiment.plateau_file(self.p), {"explore": {"at": 0, "ids": ["T-095"]}})
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": "T-001", "outcome": "rejected"})
        with mock.patch.object(experiment, "push") as push:
            driver.Pilot(self.p, args(goal="make it better")).plateau_watch()
        self.assertEqual((push.call_count, experiment.stopped(self.p)), (0, ""))

    def test_a_kept_experiment_during_exploration_goes_back_to_the_mix(self):
        self.make()
        experiment.save_json(experiment.plateau_file(self.p), {"explore": {"at": 0, "ids": ["T-003"]}})
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "experiment", "ticket": "T-009", "verdict": "kept"})
        pilot = driver.Pilot(self.p, args(goal="make it better"))
        pilot.plateau_watch()
        self.assertIsNone(mix.exploring(self.p))
        self.assertEqual(experiment.stopped(self.p), "")


if __name__ == "__main__":
    unittest.main()
