"""tools/check_pythons.sh's pool: with no arguments it runs each test module in a process of its own, side by side,
and still prints one summary per version; with arguments it runs them as one unittest call, as before. Runs the real
script and tools/test_pool.py on a tiny fake tests/ under this test's own Python."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_install import REPO

sys.path.insert(0, str(REPO / "tools"))
import test_pool  # noqa: E402

V = f"{sys.version_info.major}.{sys.version_info.minor}"
TOOLS = ("bash", "sh", "dirname", "mktemp", "rm", "mkdir", "ln", "grep", "tr", "tail", "cat", "nproc")
# Each fake module records the process and TMPDIR it ran in, so a test can tell pooled from one unittest call.
RECORD = """import os, unittest
from pathlib import Path
def record(name):
    Path(os.environ["POOL_SEEN"], name).write_text(f"{os.getpid()} {os.environ['TMPDIR']}")
"""
MODULES = {
    "test_pass_a": RECORD + """
class A(unittest.TestCase):
    def test_one(self): record("a")
    def test_two(self): pass
""",
    "test_pass_b": RECORD + """
class B(unittest.TestCase):
    def test_one(self): record("b")
    @unittest.skip("not here")
    def test_skipped(self): pass
""",
    "test_fail": RECORD + """
class F(unittest.TestCase):
    def test_breaks(self):
        record("fail")
        self.fail("boom from test_fail")
""",
    "test_loud": RECORD + """
class L(unittest.TestCase):
    def test_prints(self):
        record("loud")
        for i in range(5000):
            print("loud line", i)
""",
}


class CheckPool(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="windvane-pool-"))
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "tools").mkdir()
        for name in ("check_pythons.sh", "test_pool.py"):
            shutil.copy2(REPO / "tools" / name, self.root / "tools" / name)
        (self.root / "tests").mkdir()
        for name, body in MODULES.items():
            (self.root / "tests" / f"{name}.py").write_text(body)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for tool in TOOLS:
            if shutil.which(tool):
                (self.bin / tool).symlink_to(shutil.which(tool))
        (self.bin / f"python{V}").symlink_to(sys.executable)  # the only Python the script finds
        for d in ("seen", "tmp", "home"):
            (self.root / d).mkdir()

    def run_script(self, *args):
        self.seen = self.root / "seen"
        for f in self.seen.iterdir():
            f.unlink()
        env = {"PATH": str(self.bin), "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
               "POOL_SEEN": str(self.seen), "CHECK_JOBS": "3"}
        r = subprocess.run([str(self.root / "tools/check_pythons.sh"), *args], capture_output=True, text=True,
                           timeout=120, env=env)
        return r.stdout + r.stderr, r.returncode

    def processes(self):
        return {f.name: f.read_text().split(" ", 1) for f in self.seen.iterdir()}

    def test_a_failing_module_fails_the_check_with_its_tail(self):
        out, code = self.run_script()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, rf"python{V}: Ran 6 tests in [\d.]+s FAILED \(failures=1, skipped=1\)\n")
        self.assertIn(f"--- python{V}, the end of its output:\n--- test_fail:\n", out)
        self.assertIn("AssertionError: boom from test_fail", out)
        self.assertNotIn("loud line", out, "a passing module's output stays out of the log")
        self.assertTrue(out.endswith(f"FAILED on: python{V}\n"), out)
        slowest = re.search(rf"^slowest on python{V}: (.*)$", out, re.M).group(1)
        self.assertEqual(sorted(re.findall(r"(test_\w+) \d+s", slowest)), sorted(MODULES))
        seen = self.processes()
        self.assertEqual(len({pid for pid, _ in seen.values()}), 4, "one process a module")
        self.assertEqual(len({tmp for _, tmp in seen.values()}), 4, "and a TMPDIR of its own")
        self.assertEqual(list((self.root / "tmp").iterdir()), [], "nothing left in TMPDIR")

    def test_all_passing_passes(self):
        (self.root / "tests/test_fail.py").unlink()
        out, code = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, rf"python{V}: Ran 5 tests in [\d.]+s OK \(skipped=1\)\n")
        self.assertNotIn("the end of its output", out)
        self.assertTrue(out.endswith(f"passed on: python{V}\n"), out)
        times = json.loads((self.root / f"home/.cache/windvane-check/times{V}.json").read_text())
        self.assertEqual(sorted(times), sorted(m for m in MODULES if m != "test_fail"), "kept for the next run")

    def test_explicit_arguments_run_as_one_unittest_call(self):
        out, code = self.run_script("discover", "-s", "tests", "-q")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, rf"python{V}: Ran 6 tests in [\d.]+s FAILED \(failures=1, skipped=1\)\n")
        self.assertEqual(len({pid for pid, _ in self.processes().values()}), 1, out)
        self.assertNotIn("slowest", out)
        self.assertTrue(out.endswith(f"FAILED on: python{V}\n"), out)


class Pool(unittest.TestCase):
    def test_longest_first_then_unknown_ones_by_name(self):
        times = {"test_b": 3.0, "test_d": 90.0}
        self.assertEqual(test_pool.order(["test_c", "test_b", "test_a", "test_d"], times),
                         ["test_d", "test_b", "test_a", "test_c"])

    def test_counts_come_from_unittest_s_summary(self):
        self.assertEqual(test_pool.parse("...\nRan 4 tests in 0.2s\n\nFAILED (failures=1, errors=2, skipped=1)\n"),
                         (4, {"failures": 1, "errors": 2, "skipped": 1}, False))
        self.assertEqual(test_pool.parse("Ran 1 test in 0.0s\n\nOK\n"), (1, {}, True))
        self.assertEqual(test_pool.parse("Traceback ...\nKilled\n"), (None, {}, False), "a crash has no summary")

    def test_a_module_that_dies_without_a_summary_is_an_error(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            tests = Path(d, "tests")
            tests.mkdir()
            (tests / "test_dies.py").write_text("import os\nos._exit(3)\n")
            (tests / "test_fine.py").write_text("import unittest\nclass T(unittest.TestCase):\n"
                                                "    def test_x(self): pass\n")
            tails = Path(d, "tails")
            r = subprocess.run([sys.executable, str(REPO / "tools/test_pool.py"), "--jobs", "2", "--tails", str(tails),
                                str(tests)], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "TMPDIR": d})
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("Ran 1 test in ", r.stdout)
            self.assertIn("\nFAILED (errors=1)\n", r.stdout)
            self.assertIn("--- test_dies:\n", tails.read_text())


if __name__ == "__main__":
    unittest.main()
