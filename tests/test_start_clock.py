"""Telling a process from another that reuses its number, through a step of the wall clock: Linux moves /proc's btime
when the clock is set, which moves every start time read after it, so a process is named by its start ticks since
boot and the boot id instead. A fake /proc copies the real stat lines of the processes a test uses and lets it step
btime or reboot."""
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy, requests, runner, util  # noqa: E402
from windvane.config import Project  # noqa: E402
import test_hangs  # noqa: E402

REAL = Path("/proc")
HOUR = 3600


@unittest.skipUnless((REAL / "self/stat").exists(), "needs a real /proc to copy from")
class FakeProc:
    """util.PROC pointed at a copy of /proc's parts the start-time readers use: `stat`'s btime, `uptime`, the boot id,
    and `<pid>/stat` for the pids `sync` is given (this process and its ancestors always)."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="windvane-proc-")
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name)
        self.btime = next(int(l.split()[1]) for l in (REAL / "stat").read_text().splitlines()
                          if l.startswith("btime"))
        self.boot_id = (REAL / "sys/kernel/random/boot_id").read_text().strip()
        self.pids = set()
        patch = unittest.mock.patch.object(util, "PROC", self.proc)
        patch.start()
        self.addCleanup(patch.stop)
        self.sync()

    def sync(self, *pids, step=0, boot_id=None):
        """Copy the real stat lines of `pids` (and those copied before, while they live), with btime stepped by
        `step` seconds and, given one, a new boot id."""
        self.btime += step
        self.boot_id = boot_id or self.boot_id
        (self.proc / "stat").write_text(f"cpu  1 2 3 4\nbtime {self.btime}\n")
        shutil.copyfile(REAL / "uptime", self.proc / "uptime")
        (self.proc / "sys/kernel/random").mkdir(parents=True, exist_ok=True)
        (self.proc / "sys/kernel/random/boot_id").write_text(self.boot_id + "\n")
        self.pids |= {os.getpid(), *self.ancestors(), *pids}
        for pid in self.pids:
            try:
                stat = (REAL / str(pid) / "stat").read_text()
            except OSError:
                shutil.rmtree(self.proc / str(pid), ignore_errors=True)
                continue
            (self.proc / str(pid)).mkdir(exist_ok=True)
            (self.proc / str(pid) / "stat").write_text(stat)

    @staticmethod
    def ancestors():
        out, pid = [], os.getppid()
        while pid > 1:
            out.append(pid)
            pid = int((REAL / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[1])
        return out

    def reboot(self):
        """A new boot: a new boot id and btime, the same start ticks (a stranger that got the same number)."""
        self.sync(step=HOUR, boot_id="00000000-0000-4000-8000-000000000000")


class HeavyHolder(FakeProc, unittest.TestCase):
    def claimed(self):
        fd = os.open(self.proc / "heavy.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, fd)
        heavy.claim(fd, "make all", self.proc)
        return heavy.holder(fd)

    def test_a_live_holder_stays_held_when_the_clock_steps(self):
        info = self.claimed()
        for step in (HOUR, -2 * HOUR):
            with self.subTest(step=step):
                self.sync(step=step)
                self.assertTrue(heavy.still_held(info))

    def test_a_line_written_now_reads_by_an_older_windvanes_rule_too(self):
        info = self.claimed()  # an older windvane on the same machine reads `start` alone, as wall-clock seconds
        self.assertLessEqual(abs(util.started_at(info["pid"]) - info["start"]), 2)
        self.assertTrue(info["stamp"].startswith("boot:"))

    def test_a_reused_pid_after_a_reboot_is_free(self):
        info = self.claimed()
        self.reboot()
        self.assertFalse(heavy.still_held(info))

    def test_an_older_line_keeps_the_wall_clock_rule(self):
        info = {"pid": os.getpid(), "start": util.started_at(os.getpid())}
        self.assertTrue(heavy.still_held(info))
        self.sync(step=HOUR)
        self.assertFalse(heavy.still_held(info))  # as before: an older windvane's line can't tell


class Sweep(FakeProc, test_hangs.Hangs):
    """A dead run's worker (owner gone) and a group nested in it, recorded as runner and jobs record them."""

    def setUp(self):
        super().setUp()
        self.p = Project(self.s.root)
        self.owner, self.worker, self.nested = self.group(), self.group(), self.group()
        self.sync(self.owner.pid, self.worker.pid, self.nested.pid)
        d = self.p.state / "nested" / "T-007-worker"
        d.mkdir(parents=True)
        with unittest.mock.patch.object(os, "getpid", return_value=self.owner.pid):
            (d / "session").write_text(runner.owner_line(self.worker.pid))
        self.assertEqual(len((d / "session").read_text().split()), 5)
        (d / str(self.nested.pid)).write_text(util.stamp(self.nested.pid))
        self.owner.kill()
        self.owner.wait()
        self.sync()

    def test_a_dead_runs_live_groups_are_killed_after_the_clock_steps(self):
        self.sync(step=HOUR)
        self.assertEqual(runner.sweep_nested(self.p), 2)
        self.assertTrue(self.gone(self.worker) and self.gone(self.nested))

    def test_numbers_reused_after_a_reboot_are_left_alone(self):
        self.reboot()
        self.assertEqual(runner.sweep_nested(self.p), 0)
        self.assertTrue(test_hangs.alive(self.worker.pid) and test_hangs.alive(self.nested.pid))


for name in [n for n in dir(test_hangs.Hangs) if n.startswith("test_")]:
    setattr(Sweep, name, None)


class ProcessSession(FakeProc, unittest.TestCase):
    """`windvane request` finds the session its parent is, as the file runner wrote records it."""

    def setUp(self):
        super().setUp()
        nested = self.proc / "state" / "windvane" / "proj" / "nested" / "s1"
        nested.mkdir(parents=True)
        (nested / "session").write_text(runner.owner_line(os.getppid()))
        (nested / "role").write_text("worker:T-1\n")
        patch = unittest.mock.patch.dict(os.environ, XDG_STATE_HOME=str(self.proc / "state"))
        patch.start()
        self.addCleanup(patch.stop)

    def test_found_after_the_clock_steps(self):
        self.sync(step=HOUR)
        self.assertEqual(requests.process_session(), ("s1", "worker:T-1"))

    def test_a_reused_pid_after_a_reboot_is_not_it(self):
        self.reboot()
        self.assertEqual(requests.process_session(), ("", ""))


class Stamp(FakeProc, unittest.TestCase):
    def test_a_stamp_is_the_boot_and_start_ticks(self):
        ticks = (REAL / "self/stat").read_text().rsplit(")", 1)[1].split()[19]
        self.assertEqual(util.stamp(os.getpid()), f"boot:{self.boot_id}:{ticks}")
        self.assertIsNone(util.stamp(2 ** 22 + 1))  # no such process
        self.assertIsNone(util.same_process(2 ** 22 + 1, util.stamp(os.getpid())))

    def test_an_empty_mark_goes_by_its_mtime(self):
        mark = self.proc / "123"
        mark.touch()
        self.assertIsNone(util.recorded_stamp(mark))
        mark.write_text("123")  # a pid, not a stamp
        self.assertIsNone(util.recorded_stamp(mark))


class NoProcStamp(unittest.TestCase):
    """macOS: no /proc, so a stamp is the wall-clock start `ps` gives."""

    @unittest.skipUnless(shutil.which("ps"), "needs ps")
    def test_a_stamp_is_the_wall_clock_start(self):
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch.object(util, "PROC", Path(d) / "no-proc"):
            mine = util.stamp(os.getpid())
            self.assertTrue(mine.startswith("wall:"))
            self.assertTrue(util.same_process(os.getpid(), mine))


if __name__ == "__main__":
    unittest.main()
