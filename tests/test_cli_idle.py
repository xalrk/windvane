"""The command line with no autopilot running says what's true: `stop` has nothing to stop and leaves no STOP behind,
and `retry` says what the next run will do with that ticket, read from a fetched main: none, merged, parked, waiting
on another or a question, or take it up. And every argument of every command has a line of help."""
import argparse
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Idle(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        for tid in ("T-001", "T-002", "T-003", "T-005"):
            self.s.ticket(tid)
        self.s.ticket("T-004", deps="T-001")
        self.s.write(".windvane/QUESTIONS.md", "# Q\n\n## Open\n\n### T-005: red or blue?\nThe button.\n"
                     "- **A**: red\n- **B**: blue\nRecommendation: A, because it shows.\n\n## Provisional\n\n"
                     "## Answered\n")
        self.s.write(".windvane/QUEUE.md", "# Queue\n\n- parked: T-002 (needs a new API key)\n")
        self.s.publish()
        self.s.git("checkout", "-q", "-b", "t-003-work")
        self.s.write("done.txt", "x\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "work")
        self.s.git("checkout", "-q", "main")
        self.s.git("merge", "-q", "--no-ff", "-m", "Merge T-003: do T-003 (branch t-003-work)", "t-003-work")
        self.s.git("push", "-q", "origin", "main")
        self.p = Project(self.s.root)

    def run_cli(self, *argv):
        return subprocess.run(["windvane", *argv], cwd=self.s.root, capture_output=True, text=True)

    def left_behind(self):
        return [f for f in ("STOP", driver.RETRY) if (self.p.state / f).exists()]

    def test_stop_with_no_run_has_nothing_to_stop(self):
        r = self.run_cli("stop")
        self.assertEqual((r.returncode, r.stdout), (0, "Sandbox: autopilot isn't running; nothing to stop\n"), r.stderr)
        self.assertEqual(self.left_behind(), [])

    def test_stop_if_running_stays_quiet(self):
        r = self.run_cli("stop", "--if-running")
        self.assertEqual((r.returncode, r.stdout), (0, "Sandbox: autopilot isn't running\n"), r.stderr)
        self.assertEqual(self.left_behind(), [])

    def test_retry_with_no_run_says_what_the_next_run_does(self):
        cases = [("T-001", 0, "Sandbox: autopilot isn't running; the next `windvane run` takes T-001 up\n"),
                 ("T-003", 0, "Sandbox: autopilot isn't running; T-003 is already merged\n"),
                 ("T-002", 0, "Sandbox: autopilot isn't running; T-002 is parked in .windvane/QUEUE.md: delete its "
                              "`parked:` line on main to queue it\n"),
                 ("T-004", 0, "Sandbox: autopilot isn't running; T-004 waits on T-001: the next `windvane run` takes "
                              "it up once they're done\n"),
                 ("T-005", 0, "Sandbox: autopilot isn't running; the next `windvane run` doesn't take T-005 up yet: "
                              "T-005 has an open question: `windvane answer T-005 <letter>`\n")]
        for tid, code, out in cases:
            with self.subTest(tid):
                r = self.run_cli("retry", tid)
                self.assertEqual((r.returncode, r.stdout), (code, out), r.stderr)
        r = self.run_cli("retry", "t-999")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", "T-999 isn't a ticket on origin/main\n"))
        self.s.ticket("T-009")  # written, but not pushed: the run can't see it
        r = self.run_cli("retry", "T-009")
        self.assertEqual((r.returncode, r.stderr), (1, "T-009 isn't a ticket on origin/main; commit and push its file\n"))
        self.assertEqual(self.left_behind(), [])

    def test_retry_reads_main_as_the_next_run_will_fetched(self):
        other = self.s.tmp / "other"
        subprocess.run(["git", "clone", "-q", str(self.s.remote), str(other)], check=True)
        (other / ".windvane/tickets/T-006.md").write_text("# T-006: do T-006\nDepends on: nothing.\n")
        for cmd in (["add", "-A"], ["commit", "-qm", "T-006"], ["push", "-q"]):
            subprocess.run(["git", *cmd], cwd=other, check=True)
        r = self.run_cli("retry", "T-006")
        self.assertEqual((r.returncode, r.stdout),
                         (0, "Sandbox: autopilot isn't running; the next `windvane run` takes T-006 up\n"), r.stderr)


class Help(unittest.TestCase):
    def test_every_argument_of_every_command_has_help(self):
        missing, todo = [], [("windvane", cli.parser())]
        while todo:
            name, ap = todo.pop()
            for act in ap._actions:
                if isinstance(act, argparse._HelpAction):
                    continue
                if isinstance(act, argparse._SubParsersAction):
                    helps = {c.dest: c.help for c in act._choices_actions}
                    for cmd, sub in act.choices.items():
                        if not helps.get(cmd):
                            missing.append(f"{name} {cmd}")
                        todo.append((f"{name} {cmd}", sub))
                elif not act.help:
                    missing.append(f"{name} {'/'.join(act.option_strings) or act.dest}")
        self.assertEqual(missing, [], "give these a help= line")


if __name__ == "__main__":
    unittest.main()
