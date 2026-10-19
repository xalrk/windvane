"""A critic scale's ids that parse but will be misread (T-198): an id two checks of one dimension share, two headings
with one id. doctor warns of each and the run logs each at its start; neither refuses to run. A word of the critic's
reply format is no longer one (T-205): a bulleted answer is never read as a heading."""
import contextlib
import io
import sys
import unittest
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from windvane import critic, doctor, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402

from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

BAD = """# Scale
## works
- 2 runs: it runs
- 5 beyond: it does more than asked
- 8 safe: no known way it loses work
- 10 safe: still safe under load
## checks
- 5 green: the checks pass
- 10 fast: they pass fast
"""


class ScaleIds(unittest.TestCase):
    def make(self, scale):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.write(".windvane/CRITIC.md", scale)
        self.p = Project(self.s.root)

    def warnings(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return [(what, fix) for level, what, fix in doctor.checks(self.p) if what.startswith("critic scale")]

    def test_doctor_warns_once_for_each_misread_id_and_names_the_file_to_fix(self):
        self.make(BAD)
        got = self.warnings()
        self.assertEqual(len(got), 1, got)
        self.assertIn("dimension works has 2 checks named `safe`", got[0][0])
        self.assertTrue(all(fix.endswith("in .windvane/CRITIC.md") for _, fix in got), got)
        self.assertEqual(critic.rubric(self.p, self.s.root)["works"][1][1], "beyond")  # it still parses as before

    def test_a_word_of_the_reply_format_is_no_warning(self):
        self.make("## checks\n- 5 beyond: more\n- 8 fix: fixed\n## critic\n- 5 fixes: yes\n- 8 for: yes\n")
        self.assertEqual(self.warnings(), [])

    def test_two_headings_with_one_first_word_are_one_warning(self):
        self.make("## on the course\n- 5 reachable: yes\n## on time\n- 5 late: no\n## Notes\n## Notes\n")
        got = self.warnings()
        self.assertEqual(len(got), 1, got)
        self.assertIn("2 headings read as dimension `on`", got[0][0])

    def test_the_shipped_scale_is_clean(self):
        rel = "templates/CRITIC.md"  # what windvane ships, not this checkout's own project settings
        self.assertEqual(critic.misread((REPO / rel).read_text(), rel), [], rel)
        self.make((REPO / rel).read_text())
        self.assertEqual(self.warnings(), [])

    def test_the_run_logs_each_once_at_its_start_and_runs_on(self):
        self.make(BAD)
        pilot = driver.Pilot(self.p, args())
        with contextlib.redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(minutes=1))
        log = (self.p.state / "autopilot.log").read_text()
        self.assertEqual(log.count("critic scale:"), 1, log)
        self.assertIn("dimension works has 2 checks named `safe`, so one answer scores them all: rename one in "
                      ".windvane/CRITIC.md", log)
        self.assertFalse(pilot.halt)


if __name__ == "__main__":
    unittest.main()
