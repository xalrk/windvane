"""`windvane bench compare A B`: two windvane refs (or earlier runs) side by side, per seed and in all, running only
what isn't recorded yet, and saying when a difference can't be told from the runs' own noise."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO, Sandbox  # noqa: E402

from windvane import bench  # noqa: E402

FIXTURES = REPO / "tests/fixtures/bench"


def rec(run, sha, seed, merged, passed, judge, weighted, minutes, size="full"):
    """A seed's record as `windvane bench` writes it, with only what a compare reads filled in."""
    return {"run": run, "ref": "x", "sha": sha, "size": size, "seed": seed,
            "tickets": {"merged": ["T-001"][:merged], "failed": [], "parked": [], "open": []},
            "hidden": {}, "passed": passed, "total": 2, "weighted": weighted, "usd": 0.5, "per_merged": {},
            "judge": {"yes": judge, "total": 2, "answers": [], "weighted": 900, "usd": 0.01},
            "minutes": minutes, "contaminated": False}


def row(seed, label, a, b, delta, note=""):
    return f"{seed:<12} {label:<14} {a:>9} {b:>9} {delta:>7}" + note


class Compare(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        root = self.s.root
        (root / "bin").mkdir()
        (root / "bin/windvane").symlink_to(REPO / "bin/windvane")
        for name in ("tiny", "other"):
            shutil.copytree(FIXTURES / "tiny", root / "bench" / name)
        self.s.publish("bench")
        self.s.git("tag", "ref-a")
        self.s.git("commit", "-q", "--allow-empty", "-m", "a change to measure")
        self.s.git("tag", "ref-b")
        self.a, self.b = self.s.git("rev-parse", "ref-a"), self.s.git("rev-parse", "ref-b")
        self.results = self.s.tmp / "state/windvane/sandbox/bench.jsonl"
        self.results.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *recs):
        with self.results.open("a") as f:
            f.writelines(json.dumps(r) + "\n" for r in recs)

    def compare(self, *args):
        r = subprocess.run(["windvane", "-C", str(self.s.root), "bench", "compare", *args, "--full", "--hours", "1"],
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
        return r.stdout.splitlines()

    def test_recorded_results_print_each_seed_and_the_total_b_minus_a(self):
        self.record(rec("a1", self.a, "other", 0, 0, 0, 10000, 5.0), rec("a1", self.a, "tiny", 1, 1, 1, 40000, 3.0),
                    rec("b1", self.b, "other", 1, 1, 1, 20000, 4.0), rec("b1", self.b, "tiny", 1, 2, 2, 30000, 2.0),
                    rec("s1", self.b, "tiny", 1, 2, 2, 1, 1.0, size="small"))  # another size: never reused
        a, b = self.a[:7], self.b[:7]
        self.assertEqual(self.compare("ref-a", "ref-b"), [
            "bench compare: full (--full)",
            f"ref-a ({a}): 1 run recorded, 0 to run",
            f"ref-b ({b}): 1 run recorded, 0 to run",
            f"bench compare (full): A = ref-a ({a}), 1 run · B = ref-b ({b}), 1 run",
            row("seed", "measure", "A", "B", "B-A"),
            row("other", "merged", "0", "1", "+1"),
            row("", "hidden tests", "0/2", "1/2", "+1"),
            row("", "judge yes", "0/2", "1/2", "+1"),
            row("", "tokens/merged", "-", "20k", "-"),  # nothing merged on A: no cost per merged ticket
            row("", "wall min", "5", "4", "-1"),
            row("tiny", "merged", "1", "1", "0"),
            row("", "hidden tests", "1/2", "2/2", "+1"),
            row("", "judge yes", "1/2", "2/2", "+1"),
            row("", "tokens/merged", "40k", "30k", "-10k"),
            row("", "wall min", "3", "2", "-1"),
            row("all", "merged", "1", "2", "+1"),
            row("", "hidden tests", "1/4", "3/4", "+2"),
            row("", "judge yes", "1/4", "3/4", "+2"),
            row("", "tokens/merged", "50k", "25k", "-25k"),
            row("", "wall min", "5", "4", "-1"),  # the seeds run side by side: the slowest one's
            bench.NOISE])
        self.assertFalse((self.s.tmp / "state/windvane/_bench").exists())  # nothing ran
        by_id = self.compare("a1", "b1")
        self.assertEqual(by_id[0], "bench compare: full (the size of the earlier run compared)")
        self.assertEqual(by_id[3], f"bench compare (full): A = a1 ({a}), 1 run · B = b1 ({b}), 1 run")
        self.assertEqual(by_id[4:], self.compare("ref-a", "ref-b")[4:])

    def test_a_compare_with_one_side_recorded_runs_only_the_other(self):
        self.record(rec("a1", self.a, "other", 1, 1, 1, 10000, 5.0), rec("a1", self.a, "tiny", 1, 1, 1, 40000, 3.0))
        out = self.compare("ref-a", "ref-b")
        self.assertIn(f"ref-a ({self.a[:7]}): 1 run recorded, 0 to run", out)
        self.assertIn(f"ref-b ({self.b[:7]}): 0 runs recorded, 1 to run", out)
        recs = [json.loads(x) for x in self.results.read_text().splitlines()]
        new = [r for r in recs if r["run"] != "a1"]
        self.assertEqual(sorted((r["seed"], r["sha"], r["ref"]) for r in new),
                         [("other", self.b, "ref-b"), ("tiny", self.b, "ref-b")])
        self.assertEqual(len(list((self.s.tmp / "state/windvane/_bench").iterdir())), 1)  # one bench run, B's
        self.assertIn(row("", "hidden tests", "2/4", "2/4", "0"), out)  # the fake builds half of what's asked
        self.assertEqual(out[-1], bench.NOISE)
        again = self.compare("ref-a", "ref-b")  # now both are recorded
        self.assertIn(f"ref-b ({self.b[:7]}): 1 run recorded, 0 to run", again)

    def test_with_repeats_a_difference_inside_the_spread_is_not_called_out(self):
        for i, (ta, tb) in enumerate(((1, 2), (2, 2))):  # tiny's hidden tests: A 1 then 2, B 2 twice
            self.record(rec(f"a{i}", self.a, "other", 1, 1, 1, 10000, 5.0),
                        rec(f"a{i}", self.a, "tiny", 1, ta, 1, 40000 + 2000 * i, 3.0),
                        rec(f"b{i}", self.b, "other", 1, 1, 1, 10000, 5.0),
                        rec(f"b{i}", self.b, "tiny", 1, tb, 1, 30000 + 2000 * i, 3.0))
        out = self.compare("ref-a", "ref-b", "--repeat", "2")
        self.assertIn(f"ref-a ({self.a[:7]}): 2 runs recorded, 0 to run", out)
        tiny = out[out.index(row("tiny", "merged", "1", "1", "0")):]
        self.assertEqual(tiny[1], row("", "hidden tests", "1.5/2", "2/2", "+0.5"))  # spread 1 on A: noise
        self.assertEqual(tiny[3], row("", "tokens/merged", "41k", "31k", "-10k", "  past the noise"))  # spread 2k
        self.assertNotIn(bench.NOISE, out)
        self.assertNotIn("no difference past the spread of either side's own runs", out)
        one = self.compare("ref-a", "ref-b", "--repeat", "2", "--dry-run")  # a third side's run would be planned
        self.assertEqual(one[1:], [f"ref-a ({self.a[:7]}): 2 runs recorded, 0 to run",
                                   f"ref-b ({self.b[:7]}): 2 runs recorded, 0 to run"])
        three = self.compare("ref-a", "ref-b", "--repeat", "3", "--dry-run")
        self.assertEqual(three[1:], [f"ref-a ({self.a[:7]}): 2 runs recorded, 1 to run",
                                     f"ref-b ({self.b[:7]}): 2 runs recorded, 1 to run"])

    def test_a_run_whose_judge_failed_is_run_again_and_a_ref_against_itself_takes_two_runs(self):
        failed = {**rec("a0", self.a, "tiny", 1, 1, 0, 40000, 3.0), "judge": {
            "yes": 0, "total": 2, "answers": [], "weighted": 10, "usd": 0, "error": "API Error: 529 overloaded"}}
        self.record(rec("a0", self.a, "other", 1, 1, 1, 10000, 5.0), failed)
        self.assertEqual(self.compare("ref-a", "ref-b", "--dry-run")[1],
                         f"ref-a ({self.a[:7]}): 0 runs recorded, 1 to run (1 run not reused: a judge failed)")
        self.record(*(rec(f"a{i}", self.a, s, 1, 1, 1, 10000, 5.0) for i in (1, 2) for s in ("other", "tiny")))
        out = self.compare("ref-a", "ref-a")  # an A/A run: how far two runs of one windvane differ
        self.assertEqual(out[3], f"bench compare (full): A = ref-a ({self.a[:7]}), 1 run · "
                                 f"B = ref-a ({self.a[:7]}), 1 run")
        self.assertIn(f"ref-a ({self.a[:7]}): 1 run recorded, 0 to run (1 run not reused: a judge failed)", out)

    def test_repeat_goes_with_compare(self):
        r = subprocess.run(["windvane", "-C", str(self.s.root), "bench", "--repeat", "2", "--dry-run"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stderr.strip()),
                         (1, "bench: --repeat goes with `windvane bench compare <A> <B>`"))

    def test_stats_lists_every_recorded_run(self):
        self.record(rec("a1", self.a, "other", 0, 0, 0, 10000, 5.0), rec("a1", self.a, "tiny", 1, 1, 1, 40000, 3.0),
                    {**rec("b1", self.b, "tiny", 1, 2, 2, 30000, 2.0), "judge": None})
        out = subprocess.run(["windvane", "-C", str(self.s.root), "stats", "--bench"], capture_output=True,
                             text=True, timeout=60).stdout.splitlines()
        self.assertEqual(out[0].split(), ["run", "ref", "size", "seeds", "hidden", "judge", "tokens/merged"])
        self.assertEqual(out[1].split(), ["a1", "x", f"({self.a[:7]})", "full", "2", "1/4", "1/4", "50k"])
        self.assertEqual(out[2].split(), ["b1", "x", f"({self.b[:7]})", "full", "1", "2/2", "-", "30k"])


if __name__ == "__main__":
    unittest.main()
