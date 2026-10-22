"""T-167: the heavy lock's status lines read plainly (one pair of parentheses, a paused job's way out, a free lock
said as free), and the worker's rules tell it about `--mem`."""
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy, util  # noqa: E402
from test_heavy_check_wait import machine  # noqa: E402

ROOT = Path(__file__).parents[1]


class HeavyWording(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        env = {**machine(self.d, 60), "XDG_STATE_HOME": str(self.d / "st")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("WINDVANE_HEAVY_HELD", None)
        heavy._ps_seen.clear()
        self.addCleanup(heavy._ps_seen.clear)

    def hold(self, **info):
        """A lock line naming this process (alive, its own start) as the holder."""
        me = os.getpid()
        heavy.lock_path().write_text(json.dumps({"pid": me, "start": util.started_at(me), "since": time.time(),
                                                 "cmd": "make", "cwd": "a/b", **info}))
        return me

    def test_a_paused_holder_says_so_inside_its_parentheses_with_the_way_out(self):
        me = self.hold()
        with mock.patch.object(heavy, "paused", lambda pid: pid == me):
            line = heavy.status()[0]
        self.assertRegex(line, rf"^held by pid {me} \(make in a/b, since \d\d:\d\d; running .*; "
                               rf"paused, so everyone waits: resume it with kill -CONT -{os.getpgid(me)}\)$")
        self.assertEqual((line.count("("), line.count(")")), (1, 1), line)

    def test_a_pause_note_names_the_bare_pid_when_its_group_cant_be_read(self):
        with mock.patch.object(heavy.os, "getpgid", side_effect=ProcessLookupError):
            self.assertEqual(heavy.paused_note(12, "skipped"), "paused, so skipped: resume it with kill -CONT 12")

    def test_a_running_holder_has_no_pause_note(self):
        me = self.hold()
        with mock.patch.object(heavy, "paused", lambda pid: False):
            line = heavy.status()[0]
        self.assertTrue(line.startswith(f"held by pid {me} (make in a/b, since "), line)
        self.assertNotIn("paused", line)

    def test_a_waiter_whose_holder_has_gone_says_the_lock_is_free(self):
        info = {"pid": 2 ** 22 + 12345, "since": time.time(), "cmd": "make", "cwd": "a/b"}  # no such process
        self.assertEqual(heavy.waiting_line(info, 2), "heavy: the machine lock is free; 2 ahead of this job")
        self.assertEqual(heavy.waiting_line(info, 0, "about 5 min of work ahead"),
                         "heavy: the machine lock is free; next in line; about 5 min of work ahead")
        me = self.hold()
        self.assertTrue(heavy.waiting_line(json.loads(heavy.lock_path().read_text()), 1)
                        .startswith(f"heavy: waiting for the machine lock: held by pid {me} (make in a/b, since "))

    def test_the_worker_is_told_about_mem(self):
        text = " ".join((ROOT / "prompts" / "WORKER.md").read_text().split())
        self.assertRegex(text, r"`windvane heavy -- <command>`.*`--mem <GB>` when it needs more or less free memory")
        self.assertTrue(re.search(r"`windvane bg --heavy", text))


if __name__ == "__main__":
    unittest.main()
