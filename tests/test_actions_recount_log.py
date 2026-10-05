"""A recount that fails on a gh call says why in the log at once, once a run, instead of only a day later through the
stale rule; one held up by another driver's count says nothing; and the phone's line marks a stale count (T-084)."""
import contextlib
import io
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402

LOGGED_OUT = """#!/bin/sh
if [ "$1" = repo ]; then echo me/proj; exit 0; fi
echo "To get started with GitHub CLI, please run:  gh auth login" >&2
echo "Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token." >&2
exit 4
"""


class RecountLog(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("actions_minutes = 2000\n" + cfg.read_text())
        self.s.publish("budget")
        self.p = Project(self.s.root)
        self.gh(LOGGED_OUT)
        actions.recount_logged.clear()  # other files' sessions may have recounted under this name

    def tearDown(self):
        actions.recount_logged.clear()
        self.s.close()

    def gh(self, script):
        gh = self.s.bin / "gh"
        gh.write_text(script)
        gh.chmod(0o755)

    def refresh(self):
        """One background recount, as the driver's start and each session's end start it."""
        self.assertTrue(actions.counting.acquire(blocking=False))
        with contextlib.redirect_stdout(io.StringIO()):  # util.log echoes to stdout
            actions.refresh_now(self.p)

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text().splitlines() if f.exists() else []

    def cache(self, used, age):
        save_json(actions.cache_file(), {"month": actions.month(), "runs": {},
                                         "total": {"used": used, "at": time.time() - age}})

    def status_line(self):
        return subprocess.run(["windvane", "status", "--line"], cwd=self.s.root, capture_output=True,
                              text=True).stdout

    def test_a_logged_out_gh_is_logged_at_once_with_its_cause(self):
        self.refresh()
        self.assertEqual(len(self.log()), 1, self.log())
        said = self.log()[0]
        self.assertIn("actions: recount failed: `gh api repos/me/proj/actions/runs`: To get started with GitHub CLI",
                      said)
        self.assertIn("run `gh auth status`", said)
        self.assertNotIn("GH_TOKEN", said)  # only gh's first line
        self.refresh()  # the next session's recount fails the same way
        self.assertEqual(len([x for x in self.log() if "recount failed" in x]), 1, self.log())

    def test_a_failed_repo_list_names_that_call(self):
        self.gh("#!/bin/sh\necho 'error connecting to api.github.com' >&2\nexit 1\n")
        self.refresh()
        self.assertEqual(len(self.log()), 1, self.log())
        self.assertIn("`gh repo list`: error connecting to api.github.com", self.log()[0])

    def test_a_count_held_by_another_driver_logs_nothing(self):
        with actions.locked() as got, mock.patch.object(actions, "WAIT_S", 0.3):
            self.assertTrue(got)
            self.refresh()
        self.assertEqual(self.log(), [])
        self.refresh()  # the lock is free again: now gh is asked, and fails
        self.assertEqual(len(self.log()), 1, self.log())

    def test_status_line_says_stale_past_a_day(self):
        self.cache(1234, actions.STALE_S + 3600)
        out = self.status_line()
        self.assertRegex(out, r"Actions 1,234/2,000 min \(stale: counted [A-Z][a-z]{2} \d{1,2}\)")
        self.assertNotRegex(out, r"\d\d:\d\d")  # the date says enough: the line stays short

    def test_status_line_under_an_hour_old_has_no_time_or_date(self):
        self.cache(1234, 600)
        out = self.status_line()
        self.assertIn("Actions 1,234/2,000 min", out)
        self.assertNotIn("stale", out)
        self.assertNotRegex(out, r"\d\d:\d\d|counted|" + re.escape(time.strftime("%b ")))


if __name__ == "__main__":
    unittest.main()
