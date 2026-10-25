"""`windvane bench`: seed projects built by the pipeline (the fake `claude` here), scored on hidden tests no session
sees; and the real seeds' hidden tests, which must pass on a reference solution and fail on the bare seed."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO, Sandbox, tmpdir  # noqa: E402

from windvane import bench  # noqa: E402

FIXTURES = REPO / "tests/fixtures/bench"
LOGGER = "#!/bin/sh\necho \"$(basename \"$0\") $*\" >> {log}\n"


class Bench(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        root = self.s.root
        (root / "bin").mkdir()
        (root / "bin/windvane").symlink_to(REPO / "bin/windvane")  # this checkout's windvane, at the bench's ref
        shutil.copytree(FIXTURES / "tiny", root / "bench/tiny")
        self.s.publish("bench")
        self.tools = self.s.tmp / "tools.log"
        for name in bench.BLOCKED:  # on PATH from here on: a seed run must not reach them
            f = self.s.bin / name
            f.write_text(LOGGER.format(log=self.tools))
            f.chmod(0o755)
        self.state = self.s.tmp / "state/windvane/sandbox"

    def bench(self, *args, timeout=240):
        r = subprocess.run(["windvane", "-C", str(self.s.root), "bench", *args], capture_output=True, text=True,
                           timeout=timeout)
        self.assertEqual(r.returncode, 0, r.stdout[-1500:] + r.stderr[-1500:])
        return r.stdout

    def reading(self, week):
        f = self.s.tmp / "state/windvane/other/usage.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"five": 10, "week": week, "at": time.time()}))

    def checkout(self):
        g = lambda *a: self.s.git(*a)
        return g("branch", "--show-current"), g("rev-parse", "HEAD"), g("status", "--porcelain", "--ignored"), \
            g("worktree", "list", "--porcelain"), g("ls-files", "-s")

    def test_a_run_scores_the_hidden_tests_and_leaves_the_checkout_alone(self):
        before = self.checkout()
        out = self.bench("--hours", "1")
        self.assertEqual(out.splitlines()[0], "bench: full (no weekly usage reading yet)")
        recs = [json.loads(x) for x in (self.state / "bench.jsonl").read_text().splitlines()]
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["seed"], r["size"], r["ref"]), ("tiny", "full", "HEAD"))
        self.assertEqual(r["sha"], self.s.git("rev-parse", "HEAD"))
        self.assertEqual(r["hidden"], {"T-001": {"passed": 1, "total": 2}})
        self.assertEqual(r["tickets"]["merged"], ["T-001"])
        self.assertFalse(r["contaminated"])
        self.assertGreater(r["weighted"], 0)  # the fake sessions' tokens, from the seed run's own ledger
        self.assertIn("T-001", r["per_merged"])
        self.assertEqual(r["week_after"], 20)  # the fake's weekly reading, from the seed's usage.json
        self.assertIn("T-001 1/2", out)
        self.assertRegex(out, r"\ntiny\s+1\s+0\s+0\s+1/2 ")
        # the sessions ran in the seed's own repo and state dir, denied the hidden tests
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        worker = next(c for c in calls if c["system"].startswith("# windvane worker run"))
        home = self.s.tmp / "state/windvane/_bench" / r["run"] / "tiny"
        self.assertTrue(worker["cwd"].startswith(str(home)), worker["cwd"])
        self.assertIn(f"Read(/{self.s.root}/bench/**)", worker["deny"])
        self.assertIn(f"Bash(*{home.parent / 'windvane'}/bench*)", worker["deny"])
        self.assertFalse((home / "repo/accept_tiny_greeting.py").exists())  # copied into the scratch clone only
        self.assertTrue((home / "score/accept_tiny_greeting.py").exists())
        # nothing reached the phone or GitHub, and the user's checkout is as it was
        self.assertFalse(self.tools.exists(), self.tools.read_text() if self.tools.exists() else "")
        self.assertEqual(self.checkout(), before)
        self.assertFalse((home.parent / "windvane").exists())  # the ref's worktree is gone
        again = self.bench("--last")
        self.assertIn("T-001 1/2", again)
        self.assertIn(r["run"], again.splitlines()[0])
        plan = self.bench("--dry-run")
        self.assertIn("earlier full runs: 1, each about", plan)

    def test_weekly_usage_picks_the_size(self):
        self.reading(80)
        out = self.bench("--dry-run").splitlines()
        self.assertEqual(out[0], "bench: small (weekly usage 80% > 75%)")
        self.assertIn("  tiny: T-001", out)
        self.assertIn("earlier small runs: none yet, so no cost to go by", out)
        self.reading(50)
        self.assertEqual(self.bench("--dry-run").splitlines()[0], "bench: full (weekly usage 50% ≤ 75%)")
        self.assertEqual(self.bench("--dry-run", "--small").splitlines()[0], "bench: small (--small)")
        (self.s.tmp / "config/windvane").mkdir(parents=True)
        (self.s.tmp / "config/windvane/machine.toml").write_text("bench_week_small = 40\n")
        self.assertEqual(self.bench("--dry-run").splitlines()[0], "bench: small (weekly usage 50% > 40%)")
        self.assertFalse(list((self.s.tmp / "state/windvane/_bench").glob("*/*")))  # a dry run leaves nothing behind
        self.assertFalse(self.tools.exists())

    def test_a_stream_naming_a_hidden_file_marks_the_seed_contaminated(self):
        os.environ["FAKE_SAY"] = "I peeked at accept_tiny_greeting.py"  # the reviewer's reply says so
        out = self.bench("--hours", "1")
        r = json.loads((self.state / "bench.jsonl").read_text())
        self.assertTrue(r["contaminated"])
        self.assertTrue(any("accept_tiny_greeting.py" in h for h in r["hits"]), r["hits"])
        self.assertIn("contaminated: not counted", out)


class Small(unittest.TestCase):
    def test_a_small_run_takes_two_seeds_and_parks_the_fourth_ticket(self):
        src = tmpdir(self)
        for name in ("a", "b", "c"):
            d = src / name
            shutil.copytree(FIXTURES / "tiny", d)
            for n in (2, 3, 4):
                shutil.copy(d / "seed/.windvane/tickets/T-001.md", d / f"seed/.windvane/tickets/T-00{n}.md")
        found = bench.seeds(src)
        chosen = bench.pick(found, "small")
        self.assertEqual([(s["name"], ts) for s, ts in chosen], [("a", ["T-001", "T-002", "T-003"]),
                                                                  ("b", ["T-001", "T-002", "T-003"])])
        self.assertEqual(len(bench.pick(found, "full")), 3)
        home = tmpdir(self)
        sp = bench.make_repo(found["a"], ["T-001", "T-002", "T-003"], home, ["Read(//x/**)"])
        queue = (sp.root / ".windvane/QUEUE.md").read_text()
        self.assertIn("- parked: T-004 (left out of a small bench run)", queue)
        self.assertEqual(sp.perms["worker"]["deny"], ["Read(//x/**)"])
        self.assertEqual(sp.state, home / "state/windvane/tiny")
        self.assertIn("daily 00:00-00:00", (sp.state / "quiet.txt").read_text())

    def test_unittest_output_is_read_per_test(self):
        text = ("test_a (m.C.test_a) ... ok\ntest_b (m.C.test_b)\nT-001: says so ... FAIL\n"
                "m (unittest.loader._FailedTest.m) ... ERROR\n")
        self.assertEqual(bench.outcomes(text), {"m.C.test_a": "ok", "m.C.test_b": "FAIL",
                                                "unittest.loader._FailedTest.m": "ERROR"})


class RealSeeds(unittest.TestCase):
    """Each seed's hidden tests are reachable and fair: all pass on a reference solution (kept in the tests, never in
    bench/), none on the bare seed, whose own check passes."""

    def run_seed(self, seed, reference=None):
        d = tmpdir(self) / "repo"
        shutil.copytree(seed["dir"] / "seed", d)
        if reference:
            shutil.copytree(reference, d, dirs_exist_ok=True)
        check = bench.tomllib.loads((d / ".windvane/config.toml").read_text())["check"]
        r = subprocess.run(shlex.split(check), cwd=d, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, f"{seed['name']}: {check}\n{r.stdout[-1500:]}{r.stderr[-1500:]}")
        return bench.tally(seed, seed["tickets"], d), (d.parent / "hidden.log").read_text()

    def test_each_seed_fails_bare_and_passes_on_the_reference(self):
        found = bench.seeds(REPO / "bench")
        self.assertEqual(len(found), 3)
        for name, seed in found.items():
            with self.subTest(seed=name):
                self.assertEqual(len(seed["tickets"]), 4)
                self.assertEqual(sorted(seed["bench"]["tickets"]), seed["tickets"])
                bare, log = self.run_seed(seed)
                self.assertTrue(all(h["total"] >= 2 for h in bare.values()), bare)
                self.assertEqual({t: h["passed"] for t, h in bare.items()}, dict.fromkeys(seed["tickets"], 0), log)
                good, log = self.run_seed(seed, FIXTURES / "reference" / name)
                self.assertEqual({t: h["passed"] for t, h in good.items()},
                                 {t: h["total"] for t, h in good.items()}, log)

    def test_every_hidden_test_names_the_ticket_line_it_checks(self):
        for name, seed in bench.seeds(REPO / "bench").items():
            for tid, mods in seed["bench"]["tickets"].items():
                ticket = (seed["dir"] / f"seed/.windvane/tickets/{tid}.md").read_text()
                for m in mods:
                    tree = bench.ast.parse((seed["dir"] / f"hidden/{m}.py").read_text())
                    for c in (n for n in tree.body if isinstance(n, bench.ast.ClassDef)):
                        for f in (n for n in c.body if isinstance(n, bench.ast.FunctionDef)
                                  and n.name.startswith("test")):
                            doc = bench.ast.get_docstring(f) or ""
                            with self.subTest(seed=name, test=f.name):
                                self.assertTrue(doc.startswith(f"{tid}: "), doc)
                                line = doc.split(": ", 1)[1].split("\n")[0]
                                self.assertIn(line, ticket)  # the ticket's own words, quoted

    def test_no_reference_solution_ships_in_bench(self):
        names = {f.name for f in (FIXTURES / "reference").rglob("*.py")}
        for seed in (REPO / "bench").iterdir():
            self.assertFalse((seed / "reference").exists())
            self.assertFalse(names & {f.name for f in (seed / "seed").rglob("*.py")
                                      if f.read_text() == (FIXTURES / "reference" / seed.name / f.name).read_text()
                                      if (FIXTURES / "reference" / seed.name / f.name).exists()})


if __name__ == "__main__":
    unittest.main()
