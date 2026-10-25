"""A scale check asking for the project's check or full suite passing (T-299): the driver runs the check on the final
head after the critic, so no critic sees it pass and the check reads as `no` on every ticket. doctor and each
critique's start warn of one; the critic's rules say not to answer `no` for it."""
import argparse
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, critic, doctor  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX  # noqa: E402

from helpers import Sandbox  # noqa: E402
from test_critic_trial import PASSES  # noqa: E402

ASKS = ["check passed on the final head", "the test suite passes", "the full suite is green", "a passing suite",
        "Its checks pass", "every check passes", "the project's CI runs green", "suite passing, no warnings"]
NEAR_MISSES = ["the health check passes after a restart", "a green check mark shows in the list", "it checks the input",
               "the suite of commands reads alike", "the check command prints its name", "passing a bad path fails"]


def scale(question):
    return f"# Scale\n## works\n- 2 runs: it runs\n- 5 green: {question}\n"


class CheckAsked(unittest.TestCase):
    def test_a_check_asking_for_the_check_or_suite_passing_is_one_warning(self):
        for q in ASKS:
            got = critic.check_asked(scale(q), ".windvane/CRITIC.md")
            self.assertEqual(len(got), 1, q)
            what, fix = got[0]
            self.assertTrue(what.startswith("critic scale: check `green` asks for the project's check"), what)
            self.assertIn("which the critic can't see", what)
            self.assertEqual(fix, "drop it from its rung in .windvane/CRITIC.md: the driver runs the check on the final "
                                  "head after the critic")

    def test_a_product_check_or_other_wording_is_no_warning(self):
        for q in NEAR_MISSES:
            self.assertEqual(critic.check_asked(scale(q), "x"), [], q)

    def test_the_configured_command_or_its_script_is_named(self):
        cmd = "tools/check.sh --quick"
        for q in ("`tools/check.sh` exits 0", "tools/check.sh --quick shows no failure"):
            self.assertEqual(len(critic.check_asked(scale(q), "x", cmd)), 1, q)
        self.assertEqual(critic.check_asked(scale("tools/check.shell exists"), "x", cmd), [])
        self.assertEqual(len(critic.check_asked(scale("npm test shows nothing red"), "x", "npm test")), 1)
        self.assertEqual(critic.check_asked(scale("npm prints its version"), "x", "npm test"), [], "a bare tool word")
        self.assertEqual(critic.check_asked(scale("it runs"), "x", ""), [])

    def test_a_bare_one_word_command_counts_only_in_backticks_or_passing(self):
        for cmd in ("make", "pytest", "tox"):
            self.assertEqual(critic.check_asked(scale(f"it doesn't {cmd} the page slower"), "x", cmd), [], cmd)
            self.assertEqual(critic.check_asked(scale(f"`{cmd}` builds the docs"), "x", cmd), [], cmd)
            for q in (f"`{cmd}` passes", f"{cmd} passes on the head"):
                self.assertEqual(len(critic.check_asked(scale(q), "x", cmd)), 1, q)

    def test_a_wrapper_script_counts_only_with_its_command_or_passing(self):
        cmd = "./gradlew check"
        self.assertEqual(critic.check_asked(scale("`./gradlew assembleDebug` builds the APK"), "x", cmd), [])
        for q in ("`./gradlew check` shows no failure", "./gradlew passes on the head"):
            self.assertEqual(len(critic.check_asked(scale(q), "x", cmd)), 1, q)

    def test_the_shipped_scale_is_clean(self):
        self.assertEqual(critic.check_asked((REPO / "templates/CRITIC.md").read_text(), "x", "make check"), [])


class Warned(unittest.TestCase):
    """doctor and the critic's start log, through the Sandbox."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        self.s = s
        cfg = s.root.joinpath(".windvane/config.toml").read_text()
        s.write(".windvane/config.toml", cfg.replace('check = "test -f README"', 'check = "tools/check.sh"')
                .replace("enabled = false", "enabled = true"))
        self.p = Project(s.root)

    def doctor(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return [what for level, what, fix in doctor.checks(self.p) if "asks for the project's check" in what]

    def test_doctor_warns_of_the_command_and_of_a_passing_suite(self):
        self.s.write(".windvane/CRITIC.md", "## works\n- 2 green: `tools/check.sh` passes on the head\n"
                                            "- 5 suite: the test suite passes\n- 8 safe: no way it loses work\n")
        got = self.doctor()
        self.assertEqual([w.split("`")[1] for w in got], ["green", "suite"], got)

    def test_doctor_is_quiet_on_a_scale_that_asks_what_a_user_meets(self):
        self.s.write(".windvane/CRITIC.md", (REPO / "templates/CRITIC.md").read_text())
        self.assertEqual(self.doctor(), [])

    def critique(self, question):
        """`windvane critic T-001` as a worker runs it, on a scale whose one check is `question`; its log."""
        s = self.s
        s.write(".windvane/CRITIC.md", f"## works\n- 10 works: {question}\n")
        s.ticket("T-001")
        s.publish("critic on")
        s.git("switch", "-q", "-c", "t-001-ui")
        s.write("tool.py", "print('hi')\n")
        s.git("add", "-A")
        s.git("commit", "-qm", "a tool")
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\n`python3 tool.py` prints hi.\n")
        env = {"FAKE_CRITIC_TEXT": PASSES}
        for k in (IN_SANDBOX, IN_JOB, "WINDVANE_HEAVY_HELD"):  # a suite run as a background job is in one
            if k in os.environ:
                env[k] = None
        old_env = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in old_env.items()])
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit:
                pass
        self.assertIn("CRITIC STEP: pass", out.getvalue())
        log = self.p.state / "autopilot.log"
        return log.read_text() if log.exists() else ""

    def test_the_critic_start_log_warns_of_a_check_asking_for_the_suite(self):
        log = self.critique("it works, and the full test suite passes")
        self.assertEqual(log.count("asks for the project's check"), 1, log)
        self.assertIn("T-001: critic scale: check `works` asks for the project's check or suite passing (\"the full "
                      "test suite passes\"), which the critic can't see: drop it from its rung in .windvane/CRITIC.md", log)

    def test_a_second_critique_on_the_same_scale_logs_nothing_more(self):
        self.critique("it works, and the full test suite passes")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit:
                pass
        log = (self.p.state / "autopilot.log").read_text()
        self.assertEqual(log.count("asks for the project's check"), 1, log)

    def test_a_run_start_that_warned_keeps_the_first_critique_quiet(self):
        self.s.write(".windvane/CRITIC.md", "## works\n- 10 works: the full test suite passes\n")
        critic.told_check(self.p, self.s.root)  # what the run's start does after logging `scale_warnings`
        critic.scale_check_note(self.p, "T-001", self.s.root)
        log = self.p.state / "autopilot.log"
        self.assertFalse(log.exists() and "asks for" in log.read_text())
        self.s.write(".windvane/CRITIC.md", "## works\n- 10 works: the full suite is green\n")  # a changed scale warns
        critic.scale_check_note(self.p, "T-001", self.s.root)
        self.assertIn("asks for the project's check", log.read_text())

    def test_the_critic_start_log_is_quiet_otherwise(self):
        log = self.critique("what the ticket promises happens, and again on a second pass")
        self.assertNotIn("critic scale", log)


class Rules(unittest.TestCase):
    def test_the_critic_never_answers_no_for_a_check_run_it_cant_see(self):
        text = " ".join((REPO / "prompts/CRITIC.md").read_text().split())
        self.assertIn("The driver runs the project's check on the final head after you: judge the tests you ran on the "
                      "change, never `no` for a full-suite or check run the pack lacks.", text)


if __name__ == "__main__":
    unittest.main()
