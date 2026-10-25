"""tools/check_pythons.sh's pool: with no arguments it runs each test module in a process of its own, side by side,
and still prints one summary per version, then imports every module on the newest; with arguments it runs them as one unittest call, as before. Runs the real
script and tools/test_pool.py on a tiny fake tests/ under this test's own Python."""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
# Fails its first run only, as a test that times its own steps can on a busy machine.
FLAKY = """import os, unittest
from pathlib import Path
class Busy(unittest.TestCase):
    def test_window(self):
        mark = Path(os.environ["POOL_SEEN"]).parent / "flaky-ran"
        if not mark.exists():
            mark.write_text("")
            self.fail("missed its window beside the others")
"""
SLEEPY = """import os, time, unittest
from pathlib import Path
class S(unittest.TestCase):
    def test_sleep(self):
        start = time.time()
        time.sleep(1)
        Path(os.environ["POOL_SEEN"], str(os.getpid())).write_text(f"{start} {time.time()}")
"""


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

    def start_script(self, *args, jobs="3"):
        env = {"PATH": str(self.bin), "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
               "POOL_SEEN": str(self.seen), **({"CHECK_JOBS": jobs} if jobs else {})}
        return subprocess.Popen([str(self.root / "tools/check_pythons.sh"), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env)

    def run_script(self, *args, jobs="3"):
        self.seen = self.root / "seen"
        for f in self.seen.iterdir():
            f.unlink()
        proc = self.start_script(*args, jobs=jobs)
        out = proc.communicate(timeout=120)[0]
        return out, proc.returncode

    def processes(self):
        return {f.name: f.read_text().split(" ", 1) for f in self.seen.iterdir()}

    def test_a_failing_module_fails_the_check_with_its_tail(self):
        out, code = self.run_script()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, rf"python{V}: Ran 6 tests in [\d.]+s FAILED \(failures=1, skipped=1\)\n")
        self.assertIn(f"--- python{V}, the end of its output:\n--- test_fail:\n", out)
        self.assertIn("AssertionError: boom from test_fail", out)
        self.assertNotIn("loud line", out, "a passing module's output stays out of the log")
        self.assertIn(f"python{V} imports: Ran 4 imports in ", out)
        self.assertTrue(out.endswith(f"FAILED: the suite on python{V}\n"), out)
        self.assertRegex(out, rf"\nrerun alone: test_fail failed again on {V} \(a real failure: see below\); "
                              r"added \d+s\n")
        log = (self.root / "home/.cache/windvane-check/reruns.log").read_text().splitlines()
        self.assertEqual(len(log), 1, log)
        self.assertRegex(log[0], rf"^\d{{4}}-\d\d-\d\dT[\d:]{{8}} windvane-check-\w+ test_fail failed again \({V}\); "
                                 r"added \d+s$")
        slowest = re.search(rf"^slowest on python{V}: (.*)$", out, re.M).group(1)
        self.assertEqual(sorted(re.findall(r"(test_\w+) \d+s", slowest)), sorted(MODULES))
        seen = self.processes()
        self.assertEqual(len({pid for pid, _ in seen.values()}), 4, "one process a module")
        self.assertEqual(len({tmp for _, tmp in seen.values()}), 4, "and a TMPDIR of its own")
        self.assertFalse([tmp for _, tmp in seen.values() if "test_" in tmp], "not named after it")
        self.assertEqual(list((self.root / "tmp").iterdir()), [], "nothing left in TMPDIR")

    def test_all_passing_passes(self):
        (self.root / "tests/test_fail.py").unlink()
        out, code = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, rf"python{V}: Ran 5 tests in [\d.]+s OK \(skipped=1\)\n")
        self.assertNotIn("the end of its output", out)
        self.assertNotIn("rerun", out)
        log = (self.root / "home/.cache/windvane-check/reruns.log").read_text()
        self.assertRegex(log, r"^\S+ windvane-check-\w+ no reruns\n$", "a clean check is logged too, for the rate")
        self.assertTrue(out.endswith(f"passed: the suite on python{V}, imports on python{V}\n"), out)
        times = json.loads((self.root / f"home/.cache/windvane-check/times{V}.json").read_text())
        self.assertEqual(sorted(times), sorted(m for m in MODULES if m != "test_fail"), "kept for the next run")

    def test_a_module_failing_only_beside_the_others_passes_on_its_rerun_and_is_named(self):
        (self.root / "tests/test_fail.py").unlink()
        (self.root / "tests/test_flaky.py").write_text(FLAKY)
        out, code = self.run_script()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, rf"python{V}: Ran 6 tests in [\d.]+s OK \(skipped=1\)\n", "counted once, by the rerun")
        self.assertRegex(out, rf"\nrerun alone: test_flaky passed on {V} \(flaky beside others, no action\); "
                              r"added \d+s\n")
        self.assertTrue(out.endswith(f"passed: the suite on python{V}, imports on python{V}\n"), out)

    def test_a_module_that_doesnt_import_fails_the_check_by_name_though_no_test_reaches_it(self):
        (self.root / "tests/test_fail.py").unlink()
        (self.root / "windvane").mkdir()
        (self.root / "windvane/__init__.py").write_text("")
        (self.root / "windvane/fine.py").write_text("import json\n")
        (self.root / "windvane/gone.py").write_text("import a_module_this_python_removed\n")
        out, code = self.run_script()
        self.assertEqual(code, 1, out)
        self.assertRegex(out, rf"python{V}: Ran 5 tests in [\d.]+s OK \(skipped=1\)\n")
        self.assertIn(f"python{V} imports: Ran 6 imports in ", out)
        self.assertIn("import failed: windvane.gone: ModuleNotFoundError: No module named "
                      "'a_module_this_python_removed'\n", out)
        self.assertNotIn("windvane.fine", out)
        self.assertTrue(out.endswith(f"FAILED: imports on python{V}\n"), out)
        self.assertEqual(sorted(p.name for p in (self.root / "windvane").iterdir()), ["__init__.py", "fine.py", "gone.py"],
                         "no bytecode left in the tree")

    def test_all_runs_the_suite_with_no_import_pass(self):
        (self.root / "tests/test_fail.py").unlink()
        (self.root / "tests/test_syntax.py").write_text("import unittest\nx = (\n")
        out, code = self.run_script("--all")
        self.assertEqual(code, 1, out)
        self.assertNotIn("imports", out)
        self.assertIn("test_syntax", out)
        self.assertTrue(out.endswith(f"FAILED on: python{V}\n"), out)

    def sleepers(self, n):
        """n modules of one test that sleeps a second and records when it ran, on a machine of 8 CPUs."""
        for f in (self.root / "tests").iterdir():
            f.unlink()
        for i in range(n):
            (self.root / "tests" / f"test_sleep_{i:02}.py").write_text(SLEEPY)
        (self.bin / "nproc").unlink(missing_ok=True)
        (self.bin / "nproc").write_text("#!/bin/sh\necho 8\n")
        (self.bin / "nproc").chmod(0o755)
        self.markers = self.root / "home/.cache/windvane-check/running"
        self.markers.mkdir(parents=True)

    def most_at_once(self):
        spans = [tuple(map(float, f.read_text().split())) for f in self.seen.iterdir()]
        return max(sum(1 for s, e in spans if s <= t < e) for t, _ in spans)

    def test_two_checks_started_together_each_get_half_the_jobs(self):
        self.sleepers(12)
        self.seen = self.root / "seen"
        procs = [self.start_script(jobs=None) for _ in range(2)]
        outs = [p.communicate(timeout=120)[0] for p in procs]
        for proc, out in zip(procs, outs):
            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("\nshared: 4 of 8 jobs at once while 2 checks ran\n", out)
        self.assertEqual(list(self.markers.iterdir()), [], "each check takes its marker away")

    def test_a_running_check_halves_the_jobs_until_it_ends(self):
        self.sleepers(12)
        with open(self.markers / "windvane-check-other.1", "w") as other:
            fcntl.flock(other, fcntl.LOCK_SH)  # a check that is running
            out, code = self.run_script(jobs=None)
        self.assertEqual(code, 0, out)
        self.assertIn("\nshared: 4 of 8 jobs at once while 2 checks ran\n", out)
        self.assertLessEqual(self.most_at_once(), 4, out)

    def test_a_dead_check_s_marker_does_not_count(self):
        self.sleepers(12)
        (self.markers / "windvane-check-killed.1").write_text("")  # SIGKILLed: its lock went with it
        out, code = self.run_script(jobs=None)
        self.assertEqual(code, 0, out)
        self.assertNotIn("shared:", out)
        self.assertGreater(self.most_at_once(), 4, out)
        self.assertEqual(list(self.markers.iterdir()), [], "whoever found it removed it")

    def test_check_jobs_overrides_the_sharing(self):
        self.sleepers(4)
        with open(self.markers / "windvane-check-other.1", "w") as other:
            fcntl.flock(other, fcntl.LOCK_SH)
            out, code = self.run_script(jobs="4")
        self.assertEqual(code, 0, out)
        self.assertNotIn("shared:", out)

    def test_a_check_killed_mid_run_gives_its_jobs_back(self):
        self.sleepers(16)
        self.seen = self.root / "seen"
        other_seen = self.root / "other-seen"
        other_seen.mkdir()
        pool = [sys.executable, str(self.root / "tools/test_pool.py"), "--jobs", "4", "--share", str(self.markers)]
        env = {**os.environ, "TMPDIR": str(self.root / "tmp")}
        other = subprocess.Popen([*pool, "--check", "killed", "--tails", str(self.root / "t2"), str(self.root / "tests")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 env={**env, "POOL_SEEN": str(other_seen)})
        self.addCleanup(lambda: other.poll() is None and other.kill())
        for _ in range(100):  # its marker is up: the next pool shares with it
            if any(not f.name.startswith(".") for f in self.markers.iterdir()):
                break
            time.sleep(0.05)
        mine = subprocess.Popen([*pool, "--check", "mine", "--tails", str(self.root / "t1"), str(self.root / "tests")],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                env={**env, "POOL_SEEN": str(self.seen)})
        time.sleep(2.5)
        other.kill()
        other.wait()
        killed = time.time()
        out = mine.communicate(timeout=60)[0]
        self.assertEqual(mine.returncode, 0, out)
        self.assertIn("\nshared: 2 of 4 jobs at once while 2 checks ran\n", out)
        spans = [tuple(map(float, f.read_text().split())) for f in self.seen.iterdir()]
        before = [(s, e) for s, e in spans if s < killed]
        after = [(s, e) for s, e in spans if s > killed + 1.2]  # a second for its workers to see the lock gone
        self.assertTrue(before and after, spans)
        self.assertLessEqual(max(sum(1 for s, e in before if s <= t < e) for t, _ in before), 2, spans)
        self.assertEqual(max(sum(1 for s, e in after if s <= t < e) for t, _ in after), 4, spans)
        self.assertEqual(list(self.markers.iterdir()), [], "the killed check's marker was swept")

    def test_a_check_stopped_mid_run_leaves_nothing_and_the_next_one_runs_whole(self):
        self.sleepers(12)
        self.seen = self.root / "seen"
        proc = self.start_script(jobs=None)
        for _ in range(100):  # its pool is running modules
            if any(self.seen.iterdir()):
                break
            time.sleep(0.05)
        proc.terminate()
        proc.communicate(timeout=30)
        self.assertEqual(list((self.root / "tmp").iterdir()), [], "its temp dir went with it")
        self.assertEqual(list(self.markers.iterdir()), [], "and its marker")
        for f in self.seen.iterdir():
            f.unlink()
        out, code = self.run_script(jobs=None)
        self.assertEqual(code, 0, out)
        self.assertNotIn("shared:", out, "no share kept for the stopped check")
        self.assertRegex(out, rf"python{V}: Ran 12 tests in [\d.]+s OK\n")
        self.assertIn(f"python{V} imports: Ran 12 imports in ", out)
        self.assertEqual(len(list(self.seen.iterdir())), 12, out)

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

    def test_reruns_fold_into_one_line_across_versions_with_the_time_they_added(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            flaky, real = "(flaky beside others, no action)", "(a real failure: see below)"
            logs = {"3.11": "Ran 3 tests in 1s\n\nOK\nslowest: test_a 1s\n",
                    "3.12": f"OK\nrerun alone: test_bg_keep passed {flaky}; added 12s\n",
                    "3.14": f"OK\nrerun alone: test_bg_keep passed {flaky}, test_x failed again {real}; added 41s\n"}
            for v, text in logs.items():
                Path(d, v).write_text(text)
            self.assertEqual(test_pool.fold((v, Path(d, v)) for v in [*logs, "3.13"]), (
                f"rerun alone: test_bg_keep passed on 3.12, 3.14 {flaky}, test_x failed again on 3.14 {real}; added 41s",
                "test_bg_keep passed (3.12, 3.14), test_x failed again (3.14); added 41s"))
            self.assertEqual(test_pool.fold([("3.11", Path(d, "3.11"))]), (None, None), "no reruns, no line")
            self.assertEqual(test_pool.fold((v, Path(d, v)) for v in ("3.12", "3.14"))[0],
                             f"rerun alone: test_bg_keep passed on every Python {flaky}, test_x failed again on 3.14 "
                             f"{real}; added 41s")

    def test_the_reruns_log_keeps_one_line_a_check_and_only_the_newest(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            log = Path(d, "cache", "reruns.log")
            for i in range(test_pool.LOGGED + 20):
                test_pool.log_check(log, f"check-{i}", "test_x passed (3.12); added 3s" if i % 2 else None)
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), test_pool.LOGGED)
            self.assertRegex(lines[-1], rf"^\S+ check-{test_pool.LOGGED + 19} test_x passed \(3.12\); added 3s$")
            self.assertRegex(lines[-2], rf"^\S+ check-{test_pool.LOGGED + 18} no reruns$")
            self.assertRegex(lines[0], r"^\S+ check-20 ", "the oldest went first")

    def test_checks_ending_at_once_each_keep_their_lines(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            log = Path(d, "reruns.log")
            code = (f"import sys; sys.path.insert(0, {str(REPO / 'tools')!r}); import test_pool\n"
                    f"for i in range(100): test_pool.log_check(__import__('pathlib').Path({str(log)!r}), "
                    "f'check-{sys.argv[1]}-{i}', None)")
            procs = [subprocess.Popen([sys.executable, "-c", code, str(n)]) for n in range(4)]
            self.assertEqual([p.wait(timeout=60) for p in procs], [0] * 4)
            lines = log.read_text().splitlines()
            self.assertEqual(len(lines), 400)
            self.assertFalse([l for l in lines if not re.fullmatch(r"\S+ check-\d-\d+ no reruns", l)])

    def test_a_killed_pool_s_half_made_marker_is_swept_when_the_next_opens(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            folder = Path(d)
            dead = subprocess.Popen(["true"])
            dead.wait()
            gone, live = folder / f".check-k.{dead.pid}", folder / f".check-l.{os.getpid()}"
            stale, fresh = folder / ".check-s.unreadable", folder / ".check-f.unreadable"
            for f in (gone, live, stale, fresh):
                f.write_text("")
            os.utime(stale, (time.time() - test_pool.STALE - 60,) * 2)
            share = test_pool.Share(folder, "check-a", 8)
            self.addCleanup(share.close)
            self.assertFalse(gone.exists(), "its pid is dead")
            self.assertTrue(live.exists(), "a pool may be just about to lock it")
            self.assertFalse(stale.exists(), "no pid to read, and older than an hour")
            self.assertTrue(fresh.exists())
            self.assertEqual(share.jobs_now(), 8)

    def test_a_pool_whose_half_made_marker_is_swept_makes_another(self):
        """A pool that can't see this one's pid may sweep its marker in the instant before it is locked."""
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            folder, replace, calls = Path(d), os.replace, []

            def swept_first(src, dst):
                calls.append(src)
                if len(calls) == 1:
                    Path(src).unlink()
                return replace(src, dst)

            with mock.patch.object(test_pool.os, "replace", swept_first):
                share = test_pool.Share(folder, "check-a", 8)
            self.addCleanup(share.close)
            self.assertEqual(len(calls), 2)
            self.assertEqual([f.name for f in folder.iterdir()], [f"check-a.{os.getpid()}"])
            self.assertTrue(test_pool.held(folder / share.name), "and it holds the lock on it")
            other = test_pool.Share(folder, "check-b", 8)
            self.addCleanup(other.close)
            self.assertEqual(other.jobs_now(), 4, "so the next pool counts it")

    def test_checks_count_by_their_held_markers_once_each(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            folder = Path(d)
            first = test_pool.Share(folder, "check-a", 8)
            self.addCleanup(first.close)
            self.assertEqual(first.jobs_now(), 8, "alone, it keeps them all")
            same = test_pool.Share(folder, "check-a", 8)  # the same check's pool for another Python
            self.addCleanup(same.close)
            self.assertEqual(first.jobs_now(), 8)
            (folder / "check-dead.99").write_text("")
            self.assertEqual(first.jobs_now(), 8, "a marker nobody holds is a dead check")
            self.assertFalse((folder / "check-dead.99").exists())
            others = [test_pool.Share(folder, check, 8) for check in ("check-b", "check-c", "check-d", "check-e")]
            self.assertEqual(first.jobs_now(), 2, "8 among 5 is at least 2 each")
            self.assertEqual(others[0].jobs_now(), 2)
            for other in others:
                other.close()
            self.assertEqual(first.jobs_now(), 8, "and when they end it takes the jobs back")
            self.assertEqual((first.least, first.most), (2, 5))

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

    def test_a_stopped_pool_takes_its_modules_down_with_it(self):
        with tempfile.TemporaryDirectory(prefix="windvane-pool-") as d:
            tests, pids = Path(d, "tests"), Path(d, "pids")
            tests.mkdir()
            (tests / "test_hangs.py").write_text(
                "import os, subprocess, time, unittest\nclass T(unittest.TestCase):\n    def test_x(self):\n"
                "        child = subprocess.Popen(['sleep', '60'])\n"
                f"        open({str(pids)!r}, 'w').write(f'{{os.getpid()}} {{child.pid}}')\n        time.sleep(60)\n")
            pool = subprocess.Popen([sys.executable, str(REPO / "tools/test_pool.py"), "--tails", str(Path(d, "t")),
                                     str(tests)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env={**os.environ, "TMPDIR": d})
            for _ in range(200):
                if pids.exists() and " " in pids.read_text():
                    break
                time.sleep(0.05)
            module, child = map(int, pids.read_text().split())
            pool.terminate()
            self.assertEqual(pool.wait(timeout=10), 128 + signal.SIGTERM)
            for pid in (module, child):
                for _ in range(100):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    if Path(f"/proc/{pid}/stat").exists() and Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z":
                        break  # gone, only waiting for whoever adopted it to reap it
                    time.sleep(0.05)
                else:
                    self.fail(f"pid {pid} outlived the stopped pool")


# A module that imports the changed lib/widget.py; the import is guarded, as the fixture has no lib/ to import.
WIDGET = "try:\n    from lib import widget\nexcept ImportError:\n    pass\n"


def module(name, *, fails=False, uses_widget=False, sleep=0):
    head = RECORD + WIDGET if uses_widget else RECORD
    body = f"        import time; time.sleep({sleep})\n" if sleep else ""
    body += f'        record("{name}")\n' + (f'        self.fail("boom from {name}")\n' if fails else "")
    return head + f"class T(unittest.TestCase):\n    def test_it(self):\n{body}"


class QuickPass(unittest.TestCase):
    """`--quick`: the modules likely to fail for this change, on the oldest Python, stopping at the first real
    failure."""
    setUp, start_script, processes = CheckPool.setUp, CheckPool.start_script, CheckPool.processes

    def tests(self, **modules):
        for f in (self.root / "tests").iterdir():
            f.unlink()
        for name, body in modules.items():
            (self.root / "tests" / f"{name}.py").write_text(body)

    def run_quick(self, changed=(), jobs="3"):
        self.seen = self.root / "seen"
        for f in self.seen.iterdir():
            f.unlink()
        self.checks = self.root / "state/checks"  # windvane writes the change list beside the check's log
        self.checks.mkdir(parents=True, exist_ok=True)
        listed = self.checks / "now.changed"
        listed.write_text("".join(f"{c}\n" for c in changed))
        env = {"PATH": str(self.bin), "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp"),
               "POOL_SEEN": str(self.seen), "CHECK_JOBS": jobs, "WINDVANE_CHANGED": str(listed)}
        proc = subprocess.run([str(self.root / "tools/check_pythons.sh"), "--quick"], capture_output=True,
                              text=True, env=env, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertNotIn("imports", out.splitlines()[0], "no import pass in a quick one")
        return out, proc.returncode

    def test_it_runs_the_modules_importing_the_change_and_the_sweeps_not_the_rest(self):
        self.tests(test_gadget=module("gadget", uses_widget=True), test_push_sweep=module("sweep"),
                   test_unrelated=module("unrelated"), test_broken=module("broken", fails=True))
        out, code = self.run_quick(["lib/widget.py"])
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.processes()), ["gadget", "sweep"], out)
        self.assertIn("quick: 2 of 4 modules (1 sweeps, 1 importing or naming a change)\n", out)
        self.assertRegex(out, rf"^python{V} quick pass: Ran 2 tests in [\d.]+s OK\n")
        self.assertTrue(out.endswith(f"passed: the quick pass on python{V}\n"), out)
        self.assertFalse((self.root / "home/.cache/windvane-check/reruns.log").exists(), "the full check's log")

    def test_it_stops_at_the_first_module_that_fails_alone_too_with_its_tail(self):
        self.tests(test_a_gadget=module("a", fails=True, uses_widget=True),
                   **{f"test_{c}_gadget": module(c, uses_widget=True, sleep=3) for c in "bcd"})
        out, code = self.run_quick(["lib/widget.py"], jobs="2")
        self.assertEqual(code, 1, out)
        self.assertEqual(sorted(self.processes()), ["a"], "b, beside it, is stopped; c and d never start")
        self.assertIn("rerun alone: test_a_gadget failed again (a real failure: see below); added ", out)
        self.assertIn("quick: stopped at test_a_gadget (imports or names lib/widget.py), 1 of 4 modules run\n", out)
        self.assertIn(f"--- python{V} quick pass, the end of its output:\n--- test_a_gadget:\n", out)
        self.assertIn("AssertionError: boom from a", out)
        self.assertTrue(out.endswith(f"FAILED: the quick pass on python{V}\n"), out)

    def test_a_module_failing_beside_others_but_passing_alone_does_not_stop_it(self):
        self.tests(test_flaky=WIDGET + FLAKY, test_slow=module("slow", uses_widget=True, sleep=3),
                   test_other=module("other", uses_widget=True))
        (self.root / "home/.cache/windvane-check").mkdir(parents=True)
        (self.root / f"home/.cache/windvane-check/times{V}.json").write_text(
            json.dumps({"test_slow": 3, "test_flaky": 2, "test_other": 1}))  # slow and flaky start together
        out, code = self.run_quick(["lib/widget.py"], jobs="2")
        self.assertEqual(code, 0, out)
        self.assertIn("rerun alone: test_flaky passed (flaky beside others, no action); added ", out)
        self.assertEqual(sorted(self.processes()), ["other", "slow"], "the module stopped for the rerun ran again")
        self.assertTrue(out.endswith(f"passed: the quick pass on python{V}\n"), out)

    def test_with_no_changes_it_runs_the_sweeps_and_the_recent_real_failures(self):
        self.tests(test_push_fit=module("sweep"), test_logged=module("logged"), test_in_a_log=module("in_a_log"),
                   test_flaked=module("flaked"), test_unrelated=module("unrelated"))
        cache = self.root / "home/.cache/windvane-check"
        cache.mkdir(parents=True)
        (cache / "reruns.log").write_text(f"2026-10-01T10:00:00 windvane-check-a test_logged failed again ({V}); "
                                          "added 3s\n2026-10-01T11:00:00 windvane-check-b test_flaked passed "
                                          f"({V}); added 2s\n2026-10-01T12:00:00 windvane-check-c no reruns\n")
        self.checks = self.root / "state/checks"
        self.checks.mkdir(parents=True)
        (self.checks / "20261001-120000-1-1.log").write_text(
            "rerun alone: test_in_a_log failed again on every Python (a real failure: see below); added 4s\n")
        out, code = self.run_quick([])
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(self.processes()), ["in_a_log", "logged", "sweep"], out)
        self.assertIn("quick: 3 of 5 modules (1 sweeps, 2 recent failures)\n", out)


class Pick(unittest.TestCase):
    def setUp(self):
        self.tests = Path(tempfile.mkdtemp(prefix="windvane-pick-")) / "tests"
        self.tests.mkdir()
        self.addCleanup(shutil.rmtree, self.tests.parent)

    def write(self, **modules):
        for name, body in modules.items():
            (self.tests / f"{name}.py").write_text(body)

    def test_why_each_module_is_picked_in_order(self):
        self.write(test_new="", test_driver_push="", test_uses="from windvane import driver, util\n",
                   test_reads='PROMPTS / "WORKER.md"\n', test_util="", test_push_fit="", test_often="",
                   test_helper_user="from helpers import Sandbox\n", test_none="import os\n", test_bad="def (:\n")
        chosen = test_pool.pick(self.tests, ["tests/test_new.py", "windvane/driver.py", "prompts/WORKER.md",
                                             "tests/helpers.py"], {"test_often": 3, "test_none": 0}, {}, 10 ** 6)
        self.assertEqual(chosen, [
            ("test_new", "changed"), ("test_driver_push", "named after windvane/driver.py"),
            ("test_push_fit", "sweep"), ("test_often", "failed for real in 3 of the recent checks"),
            ("test_bad", "touches tests/test_new.py, windvane/driver.py and 2 more"),
            ("test_helper_user", "imports or names tests/helpers.py"), ("test_reads", "imports or names prompts/WORKER.md"),
            ("test_uses", "imports or names windvane/driver.py")])

    def test_the_budget_keeps_the_first_and_skips_what_does_not_fit_for_smaller_ones(self):
        self.write(test_a="", test_b="", test_c="", test_d="")
        failures = {"test_a": 4, "test_b": 3, "test_c": 2, "test_d": 1}
        times = {"test_a": 50, "test_b": 40, "test_c": 4}
        self.assertEqual([m for m, _ in test_pool.pick(self.tests, [], failures, times, 60, sweeps=())],
                         ["test_a", "test_c"], "test_d, never timed, counts as the median, 40s: no room")
        self.assertEqual([m for m, _ in test_pool.pick(self.tests, [], failures, times, 1, sweeps=())], ["test_a"])

    def test_real_failures_are_the_ones_failing_alone_or_on_every_version(self):
        rerun = "rerun alone: test_a passed (flaky), test_b failed again (real); added 3s\n--- test_b:\n--- test_c:\n"
        self.assertEqual(test_pool.real_failures(rerun), {"test_b"})
        versions = ("--- python3.11, the end of its output:\n--- test_x:\nFAIL: test_it (test_y.T.test_it)\n"
                    "--- python3.12, the end of its output:\nERROR: test_y (unittest.loader._FailedTest.test_y)\n"
                    "--- and 2 more failed: test_x, test_z\n")
        self.assertEqual(test_pool.real_failures(versions), {"test_x", "test_y"})
        self.assertEqual(test_pool.real_failures("FAIL: test_it (test_one.T.test_it)\n"), {"test_one"})
        self.assertEqual(test_pool.real_failures("CHECK FAILED\n"), set())


class Replay(unittest.TestCase):
    """tools/quick_replay.py over a made-up project: a ticket's failed checks, caught when the pick holds a module that
    failed there for real."""
    def test_it_counts_the_failed_checks_the_pick_would_have_caught(self):
        root = Path(tempfile.mkdtemp(prefix="windvane-replay-"))
        self.addCleanup(shutil.rmtree, root)
        repo, state = root / "repo", root / "state"
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
        (repo / "lib").mkdir(parents=True)
        (repo / "tests").mkdir()
        (repo / "lib/widget.py").write_text("x = 1\n")
        (repo / "tests/test_gadget.py").write_text("from lib import widget\n")
        (repo / "tests/test_other.py").write_text("")
        git("init", "-q", "-b", "main")
        git("add", ".")
        git("commit", "-qm", "start")
        git("checkout", "-qb", "t-001-widget")
        (repo / "lib/widget.py").write_text("x = 2\n")
        git("commit", "-qam", "T-001: widget")
        (state / "checks").mkdir(parents=True)
        start = time.time() + 5
        rows = []
        for n, failed in enumerate(["test_gadget", "test_other", "test_other"]):
            log = f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(start + n))}-{n}-1.log"
            (state / "checks" / log).write_text(f"rerun alone: {failed} failed again (a real failure: see below)\n")
            rows += [{"event": "check-start", "ticket": "T-001", "start": start + n, "log": log},
                     {"event": "check", "ticket": "T-001", "start": start + n, "passed": False, "reused": False}]
        (state / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        out = subprocess.run([sys.executable, str(REPO / "tools/quick_replay.py"), "--state", str(state),
                              "--main", "main", "--repo", str(repo), "--jobs", "2"],
                             capture_output=True, text=True, timeout=60).stdout
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^T-001 \S+ caught real=test_gadget by=test_gadget \(imports or names "
                                   r"lib/widget.py\) picked=1 ")
        self.assertRegex(lines[1], r"^T-001 \S+ MISSED real=test_other by=- ")
        self.assertRegex(lines[2], r"^T-001 \S+ caught real=test_other by=test_other \(failed for real in 1 ",
                         "the second check's failure is in the third's history")
        self.assertTrue(lines[3].startswith("caught 2 of 3 failed checks (67%); first catch by: "), out)


if __name__ == "__main__":
    unittest.main()
