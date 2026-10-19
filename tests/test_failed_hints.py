"""A failed or set-aside ticket's push and hint name the exact command or setting that lets it go: a failure the user
clears by hand (a refused merge) ends with `windvane retry`, the work's own doesn't; max turns names the variable that
sets them; the queue's failed hint fits a phone line; a retry taken just before a restart isn't reported as never
failed; and a ticket set aside for a secret is refused a retry with what lets it go."""
import fcntl
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_merge_blocked import FAILED, GH, RUNNING, args  # noqa: E402

RETRY = "`windvane retry T-001`"


class Base(unittest.TestCase):
    github = False

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        if self.github:
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish()
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.gh = self.s.tmp / "gh"
        self.gh.mkdir()
        gh = self.s.bin / "gh"
        gh.write_text(GH.format(python=sys.executable, state=self.gh, remote=self.s.remote, run_state=self.p.state))
        gh.chmod(0o755)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        self.addCleanup(setattr, driver, "CHECKS_EVERY", driver.CHECKS_EVERY)
        driver.CHECKS_EVERY = 0

    def run_pilot(self, **files):
        for name, text in files.items():
            (self.gh / name).write_text(text)
        pilot = driver.Pilot(self.p, args())
        pilot.sleep = lambda s: None
        pilot.pause = lambda seconds=30: time.sleep(0.05)
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def pushed(self, kind):
        [msg] = [m for t, m in self.pushes if t.startswith(kind)]
        return msg

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)


class FailedPush(Base):
    github = True

    def test_a_refused_merge_ends_with_the_retry_after_its_first_line(self):
        self.run_pilot(checks=FAILED)
        first, last = self.pushed("Failed").splitlines()
        self.assertEqual(first, "the merge was refused: a check failed; see `gh pr checks 7`. Branch kept, run goes "
                                "on.")
        self.assertEqual(last, f"Once fixed, it merges the kept approval: {RETRY}")

    def test_a_required_review_says_when_and_names_the_retry_once(self):
        self.run_pilot(review="REVIEW_REQUIRED", checks=RUNNING)
        msg = self.pushed("Failed")
        self.assertTrue(msg.endswith(f"\nOnce it's given, it merges the kept approval: {RETRY}"), msg)
        self.assertEqual(msg.count("windvane retry"), 1, msg)

    def test_a_ready_refused_by_github_ends_with_the_retry(self):
        self.run_pilot(draft="", stuck="gh: Resource not accessible by integration (HTTP 403)")
        msg = self.pushed("Failed")
        self.assertTrue(msg.startswith("PR #7 is a draft and GitHub refused to mark it ready: the gh login lacks write "
                                       "access to pull requests"), msg)
        self.assertTrue(msg.endswith(f". Branch kept, run goes on.\nOnce fixed, it merges the kept approval: {RETRY}"),
                        msg)

    def test_the_workers_own_failed_has_no_retry(self):
        os.environ["FAKE_WORKER"] = "failed"
        pilot = self.run_pilot()
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertEqual(self.pushed("Failed"), "the API it needs is gone. Branch kept, run goes on.")


class PushRefused(Base):
    def test_a_push_to_main_refused_three_times_ends_with_the_retry(self):
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nif grep -q ' refs/heads/main$'; then echo 'main is protected' >&2; exit 1; fi\n")
        hook.chmod(0o755)
        pilot = self.run_pilot()
        self.assertEqual(pilot.failed, ["T-001"])
        first, last = self.pushed("Failed").splitlines()
        self.assertTrue(first.startswith("the merge was refused three times ("), first)
        self.assertTrue(first.endswith(". Branch kept, run goes on."), first)
        self.assertEqual(last, f"Once fixed, it merges the kept approval: {RETRY}")


class Hints(Base):
    def test_max_turns_names_the_variable_that_sets_them(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        self.run_pilot()
        self.assertEqual(self.pushed("Set aside"), "Hit its max turns. Split it, or raise CLAUDE_CODE_MAX_TURNS. "
                                                   "Branch kept.")
        docs = Path(__file__).resolve().parents[1] / "docs"
        for page in ("troubleshooting.md", "phone.md"):
            self.assertIn("`CLAUDE_CODE_MAX_TURNS`", (docs / page).read_text(), page)

    def test_the_queues_failed_hint_names_the_log_line_not_its_path(self):
        hint = Queue(self.p).release("T-001", [], {"failed": ["T-001"]})
        self.assertEqual(hint, "T-001 failed this run (the log's `T-001: FAILED` line says why); `windvane retry "
                               "T-001` puts it back in this run, or the next `windvane run` takes it up")
        self.assertNotIn(str(self.p.state), hint)


class Restart(Base):
    def test_a_restart_between_the_save_and_the_unlink_is_silent(self):
        """The driver dies once the state saying T-001 is back in the queue is saved, before the request is gone; the
        crash hands its memory over (`hand_over`), and the restarted driver reads the request again."""
        pilot = driver.Pilot(self.p, args())
        pilot.failed, pilot.skip = ["T-001"], {"T-001"}
        (self.p.state / driver.RETRY).write_text("T-001\n")
        save = pilot.save

        def save_then_die():
            save()
            raise RuntimeError("killed")
        pilot.save = save_then_die
        with self.assertRaises(RuntimeError):
            pilot.take_retries()
        pilot.save = save
        pilot.hand_over()
        again = driver.Pilot(self.p, args())
        again.carry_over(True)
        again.take_retries()
        self.assertEqual((again.failed, again.skip), ([], set()))
        self.assertEqual(self.log().count("T-001: back in the queue (windvane retry)"), 1)
        self.assertNotIn("hasn't failed", self.log())
        self.assertEqual(list(self.p.state.glob(driver.RETRY + "*")), [])


class SecretRetry(Base):
    def test_a_ticket_set_aside_for_a_secret_is_refused_with_what_lets_it_go(self):
        pilot = driver.Pilot(self.p, args())
        pilot.secret_aside("T-001", "t-001-work", "a" * 40, [("token", ".windvane/QUESTIONS.md:3 in 1a2b3c4")])
        fh = open(self.p.state / "lock", "a+")  # a running autopilot holds it
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = subprocess.run(["windvane", "retry", "T-001"], cwd=self.s.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001 isn't retried: only a failed ticket is. T-001 was set aside this run (its .windvane/ "
                      "lines hold a token at .windvane/QUESTIONS.md:3 in 1a2b3c4); so it isn't set aside again, "
                      "rewrite its branch without the secret, as its push says", r.stderr)
        self.assertFalse((self.p.state / driver.RETRY).exists())


if __name__ == "__main__":
    unittest.main()
