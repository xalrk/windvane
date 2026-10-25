"""`windvane bench`'s judge: a fresh session per seed that tries the finished project as its user would, knowing its
goal and tickets but nothing of how it was built, and whose tokens are the bench's own, not the seed's."""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO, Sandbox  # noqa: E402

from windvane import bench  # noqa: E402
from windvane.stats import weighted  # noqa: E402

FIXTURES = REPO / "tests/fixtures/bench"
SCALE = "Players who like a greeting that never changes"  # the seed's critic scale: never the judge's to read
REF = "judge-must-not-know-this-ref"


class Judge(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        root = self.s.root
        (root / "bin").mkdir()
        (root / "bin/windvane").symlink_to(REPO / "bin/windvane")
        seed = root / "bench/tiny"
        shutil.copytree(FIXTURES / "tiny", seed)
        # the seed has a critic scale and a pass mark: what the judge must never be told
        (seed / "seed/.windvane/CRITIC.md").write_text(f"# What a 10 means for this project\n\n{SCALE}.\n\n"
                                                       "## works\n- 2 greets: `hello.txt` says hello.\n")
        cfg = seed / "seed/.windvane/config.toml"
        cfg.write_text(cfg.read_text().replace("enabled = false", "enabled = false\npass = 7"))
        self.s.publish("bench")
        self.s.git("tag", REF)
        self.state = self.s.tmp / "state/windvane/sandbox"

    def bench(self, *args):
        r = subprocess.run(["windvane", "-C", str(self.s.root), "bench", *args], capture_output=True, text=True,
                           timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
        return r.stdout

    def test_the_judge_sees_the_goal_tickets_and_tree_and_nothing_of_the_run(self):
        os.environ["FAKE_JUDGE_NO"] = "J2"
        out = self.bench("--hours", "1", "--ref", REF)
        r = json.loads((self.state / "bench.jsonl").read_text().splitlines()[0])
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        judge = [c for c in calls if c["system"].startswith("# windvane judge")]
        self.assertEqual(len(judge), 1)
        prompt = judge[0]["prompt"]
        # what it gets: the goal, the tickets, the checks, a copy of the final tree to work in
        self.assertIn("A seed for the bench tests.", prompt)
        self.assertIn("# T-001: Write the greeting", prompt)
        self.assertIn("- J1: `cat hello.txt` prints `hello` and nothing else.", prompt)
        run = self.s.tmp / "state/windvane/_bench" / r["run"]
        tree = Path(judge[0]["cwd"])
        self.assertEqual(tree, run / "judge/tiny/tree")
        self.assertTrue((tree / "T-001.txt").is_file())  # the merged work
        self.assertFalse((tree / ".windvane").exists())  # no tickets folder, critic scale or answered questions
        self.assertFalse((tree / ".git").exists())  # no history: no branch names, no worker commit messages
        # what it never gets: the scale, a verdict, the pass mark, the ref
        for word in (SCALE, "CRITIC", "verdict", "pass mark", "pass = 7", "7/10", REF, r["sha"], r["sha"][:7]):
            self.assertNotIn(word, prompt)
        self.assertNotIn(REF, judge[0]["cwd"])
        self.assertIn(f"Read(/{run / 'tiny'}/**)", judge[0]["deny"])  # the seed's home: its streams and verdicts
        self.assertIn(f"Read(/{run / 'windvane'}/**)", judge[0]["deny"])  # the ref's worktree
        self.assertIn(f"Read(/{self.s.root}/bench/**)", judge[0]["deny"])  # the hidden tests
        self.assertIn("--tools", judge[0]["flags"])
        self.assertEqual(judge[0]["flags"][judge[0]["flags"].index("--tools") + 1], "Bash,Read,Glob,Grep")
        # its answers land in the seed's record
        self.assertEqual((r["judge"]["yes"], r["judge"]["total"]), (1, 2))
        self.assertEqual(r["judge"]["answers"], [{"id": "J1", "answer": "yes", "reason": "ran it"},
                                                 {"id": "J2", "answer": "no", "reason": "it fails"}])
        self.assertRegex(out, r"\ntiny\s+1\s+0\s+0\s+1/2\s+1/2 ")
        self.assertIn("  judge no: J2 it fails", out)
        # its tokens are the bench's own: in its record, never in the seed's ledger or cost per merged ticket
        seed_ledger = next((run / "tiny/state/windvane").glob("*/ledger.jsonl"))
        ledger = [json.loads(x) for x in seed_ledger.read_text().splitlines()]
        sessions = [x for x in ledger if not x.get("event")]
        self.assertNotIn("judge", {x["role"] for x in sessions})
        self.assertEqual(r["weighted"], round(sum(map(weighted, sessions))))
        self.assertEqual(r["per_merged"]["T-001"]["weighted"],
                         round(sum(weighted(x) for x in sessions if x.get("ticket") == "T-001")))
        self.assertGreater(r["judge"]["weighted"], 0)
        self.assertRegex(out, r"the judge's own cost, not in the above: \S+ weighted tokens")
        self.assertIn(r["run"], self.stats())

    def stats(self):
        return subprocess.run(["windvane", "-C", str(self.s.root), "stats", "--bench"], capture_output=True,
                              text=True, timeout=60).stdout

    def test_a_judge_that_fails_is_tried_again_then_shown_as_no_verdict(self):
        os.environ["FAKE_ERROR_FOR"] = "^Judge the project"  # every judge session dies of an API error
        out = self.bench("--hours", "1")
        r = json.loads((self.state / "bench.jsonl").read_text().splitlines()[0])
        calls = (self.s.tmp / "calls.jsonl").read_text()
        self.assertEqual(calls.count('"system": "# windvane judge run'), 2)  # once more, then given up
        self.assertIn("overloaded", r["judge"]["error"].lower())
        self.assertRegex(out, r"\ntiny\s+1\s+0\s+0\s+1/2\s+- ")  # no verdict, never 0/2
        self.assertRegex(out, r"\n  judge failed: .*overloaded.* \(no verdict: a compare won't reuse this run\)\n")
        self.assertNotIn("judge no:", out)
        line = self.stats().splitlines()[1].split()
        self.assertEqual(line[6:], ["-", "<1k", "(judge", "failed:", "tiny)"])

    def test_a_seed_without_checks_has_no_judge(self):
        (self.s.root / "bench/tiny/hidden/JUDGE.md").unlink()
        self.s.git("commit", "-qam", "no judge")
        out = self.bench("--hours", "1")
        r = json.loads((self.state / "bench.jsonl").read_text().splitlines()[0])
        self.assertIsNone(r["judge"])
        self.assertRegex(out, r"\ntiny\s+1\s+0\s+0\s+1/2\s+- ")
        calls = (self.s.tmp / "calls.jsonl").read_text()
        self.assertNotIn("# windvane judge", calls)


class Parts(unittest.TestCase):
    def test_a_skipped_check_is_a_no_and_answers_are_read_loosely(self):
        checks = [("J1", "a"), ("J2", "b"), ("J3", "c")]
        said = bench.answers("J1: Yes - ran `x`, saw y\n**J3** no: it crashed\nJUDGE: 1/3", checks)
        self.assertEqual(said, [{"id": "J1", "answer": "yes", "reason": "ran `x`, saw y"},
                                {"id": "J2", "answer": "no", "reason": "not answered"},
                                {"id": "J3", "answer": "no", "reason": "it crashed"}])
        self.assertEqual([x["answer"] for x in bench.answers("", checks)], ["no"] * 3)

    def test_every_real_seed_has_about_eight_checks_answered_by_running_it(self):
        for d in sorted((REPO / "bench").iterdir()):
            with self.subTest(seed=d.name):
                checks = bench.judge_checks({"dir": d, "name": d.name}, REPO)
                self.assertTrue(6 <= len(checks) <= 10, len(checks))
                self.assertEqual([i for i, _ in checks], [f"J{n}" for n in range(1, len(checks) + 1)])
                text = (d / "hidden/JUDGE.md").read_text()
                self.assertNotRegex(text.lower(), r"\b(clear|good|nice|clean|intuitive|helpful|well)\b")

    def test_an_older_ref_borrows_the_checkouts_checks(self):
        seed = {"dir": Path("/nonexistent/tiny"), "name": "tiny"}
        home = REPO / "tests/fixtures"  # its bench/tiny is the fixture seed
        self.assertEqual([i for i, _ in bench.judge_checks(seed, home)], ["J1", "J2"])

    def test_the_rule_is_short_and_says_the_reply_format(self):
        text = (REPO / "prompts/JUDGE.md").read_text()
        self.assertTrue(text.startswith("# windvane judge run"))
        self.assertTrue(re.search(r"^JUDGE: <yeses>/<checks>$", text, re.M))


if __name__ == "__main__":
    unittest.main()
