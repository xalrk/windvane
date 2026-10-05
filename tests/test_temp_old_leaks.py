"""Before windvane named its tests' temp dirs, they leaked as plain `tmpXXXXXXXX` dirs, ten thousand of them in a day:
the cleaner removes those that are a day old and hold only what windvane makes, and the driver pushes when the temp
dir is nearly full even after cleaning."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import Sandbox
from test_install import REPO
from test_pipeline import args

sys.path.insert(0, str(REPO))
from windvane import driver, scratch  # noqa: E402
from windvane.config import Project  # noqa: E402
from test_temp_clean import age  # noqa: E402

DAYS_AGO = time.time() - 2 * scratch.DAY


class OldLeaks(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.tmp = Path(os.environ["TMPDIR"])  # the Sandbox's own

    def dir(self, name, *files, old=True):
        d = self.tmp / name
        d.mkdir()
        for f in files:
            (d / f).parent.mkdir(parents=True, exist_ok=True)
            (d / f).write_text("x")
        if old:
            age(d, DAYS_AGO)
        return d

    def clone(self, name, origin):
        d = self.tmp / name
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", str(origin)], check=True)
        (d / "README").write_text("x")
        age(d, DAYS_AGO)
        return d

    def test_it_removes_only_old_leaks_shaped_like_windvanes(self):
        alive = self.s.root / "windvane-test-alive/remote.git"  # outside the temp dir: not a leftover itself
        alive.mkdir(parents=True)
        gone = [self.dir("tmpa1b2c3d4"),  # empty
                self.dir("tmpruns0001", "runs/T-001.jsonl"),
                self.dir("tmpstate002", "settings.json", "windvane-mentioned", "Critic", "critic.jsonl",
                         "windvane-heavy.lock", "state.json", "answers.json"),
                self.clone("tmpclone003", "/nowhere/windvane-test-x1y2z3/remote.git")]
        kept = [self.dir("tmpgame0004", "Content/BP_Car.uasset", "runs/x"),  # another project's files
                self.dir("tmpfresh005", "runs/T-002.jsonl", old=False),  # under a day old
                self.dir("tmpfresh006", old=False),
                self.dir("pnqcache007"),  # another name
                self.dir("tmpwayTooLong8"),
                self.clone("tmplive0009", alive),  # its Sandbox's remote is still there
                self.clone("tmpother010", "/nowhere/elsewhere/remote.git")]
        self.assertEqual(sorted(d.name for d, _, _ in scratch.leftovers()), sorted(d.name for d in gone))
        self.assertEqual(scratch.clean()[0], len(gone))
        self.assertEqual([d.name for d in gone if d.exists()], [])
        self.assertEqual([d.name for d in kept if not d.exists()], [])

    def test_an_old_leak_touched_today_or_in_use_stays(self):
        d = self.dir("tmptouched1", "runs/a")
        os.utime(d / "runs/a")
        e = self.dir("tmpinuse002", "runs/b")
        with subprocess.Popen(["sleep", "30"], cwd=e) as proc:
            try:
                time.sleep(0.2)
                self.assertEqual(scratch.clean(), (0, 0))
            finally:
                proc.kill()
        self.assertTrue(d.exists())


class FullPush(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.pilot = driver.Pilot(Project(self.s.root), args())

    def test_one_push_while_full_and_another_only_after_it_went_under_and_back(self):
        sent = []
        with mock.patch.object(driver, "push", side_effect=lambda *a: sent.append(a)):
            for f in ((93.4, 20), (95, 20), (40, 89), (10, 91.2), (10, 99)):
                with mock.patch.object(scratch, "fullness", return_value=f):
                    self.pilot.clean_tmp()
        self.assertEqual([a[0] for a in sent], ["Temp dir nearly full"] * 2)
        self.assertIn("is 93% full of inodes and 20% of space after windvane removed its leftovers: the rest isn't "
                      "windvane's", sent[0][1])
        self.assertIn("is 10% full of inodes and 91% of space", sent[1][1])


if __name__ == "__main__":
    unittest.main()
