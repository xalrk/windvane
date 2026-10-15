"""A run's wind-down (`run`'s `finally`) always ends: a Ctrl-C there, as a user hurrying it presses, kills the sessions
but still lets the save, the teardown and the unlock happen; only a third one quits at once. The "started" push says
a failed teardown until it really went out. And a guarded command can't stop on a password prompt it can't show."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, held, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now, save_json  # noqa: E402
from windvane.worktree import TEARDOWN_FAILED  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes, self.sent = [], True
        self.on_push = lambda title: None
        orig, driver.push = driver.push, self.push
        self.addCleanup(setattr, driver, "push", orig)

    def push(self, title, msg="", *a, **k):
        self.pushes.append(title)
        self.on_push(title)
        return self.sent

    def config(self, text):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(text + cfg.read_text())
        self.s.publish("config")
        self.p = Project(self.s.root)

    def run_once(self):
        self.pilot = driver.Pilot(self.p, args())
        with contextlib.redirect_stdout(io.StringIO()):
            return self.pilot.run(now() + timedelta(hours=1))

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def lock_free(self):
        with open(self.p.state / "lock") as fh:
            return not util.held(fh)


def ctrl_c(times=1):
    for _ in range(times):
        os.kill(os.getpid(), signal.SIGINT)


class CtrlCInWindDown(Base):
    def setUp(self):
        super().setUp()
        self.marker = self.s.tmp / "torn-down"
        self.config(f'teardown = "echo teardown >> {self.marker}"\n')

    def test_a_ctrl_c_before_the_save_still_saves_tears_down_and_unlocks(self):
        def stopped(title):
            if title == "windvane stopped":  # the wind-down's push: the save, the teardown and the unlock follow
                self.pilot.state_file.unlink(missing_ok=True)
                ctrl_c()
        self.on_push = stopped
        self.run_once()  # no KeyboardInterrupt
        self.assertTrue(self.pilot.state_file.exists())
        self.assertEqual(self.marker.read_text().split(), ["teardown"])
        self.assertTrue(self.lock_free())
        self.assertEqual((self.p.state / "lock").read_text(), "")  # torn down: the next run has nothing to do
        self.assertIn("Ctrl-C: already stopping: finishing the wind-down (the save, the teardown); press Ctrl-C 2 "
                      "times more to quit at once, skipping them", self.log())
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)  # the handlers are back
        self.assertTrue(self.pilot.kill_all)

    def test_two_presses_from_a_slow_teardown_still_end_it_whole(self):
        orig = driver.tear_down
        with mock.patch.object(driver, "tear_down", lambda p, **k: ctrl_c(2) or orig(p, **k)):
            self.run_once()
        self.assertEqual(self.marker.read_text().split(), ["teardown"])
        self.assertTrue(self.lock_free())
        self.assertIn("press Ctrl-C 1 time more to quit at once", self.log())

    def test_a_third_press_quits_at_once(self):
        with mock.patch.object(driver, "tear_down", lambda p, **k: ctrl_c(3)):
            with self.assertRaises(KeyboardInterrupt):
                self.run_once()
        self.assertIn("Ctrl-C a third time: quitting now, without the rest of the wind-down", self.log())
        self.assertFalse(self.lock_free())  # skipped: the process's end lets go of it
        self.pilot.lockfile.close()

    def test_the_ctrl_c_that_ended_the_run_is_the_first_of_the_three(self):
        def loop(pilot):
            raise KeyboardInterrupt
        with mock.patch.object(driver.Pilot, "loop", loop), \
                mock.patch.object(driver, "tear_down", lambda p, **k: ctrl_c()):
            with self.assertRaises(KeyboardInterrupt):  # the first press's own, once the wind-down is done
                self.run_once()
        self.assertIn("press Ctrl-C 1 time more to quit at once", self.log())
        self.assertTrue(self.pilot.state_file.exists())
        self.assertTrue(self.lock_free())

    def test_a_sigterm_in_the_wind_down_is_logged_and_changes_nothing(self):
        with mock.patch.object(driver, "tear_down", lambda p, **k: os.kill(os.getpid(), signal.SIGTERM)):
            self.run_once()
        self.assertIn("SIGTERM: already stopping: finishing the wind-down", self.log())
        self.assertTrue(self.lock_free())
        self.assertNotIn("SIGTERM", self.pilot.stop_reason)  # the run had stopped for its own reason

    def test_a_ctrl_c_outside_the_wind_down_and_guards_is_still_python_s_own(self):
        pilot = driver.Pilot(self.p, args())
        with self.assertRaises(KeyboardInterrupt):
            pilot.on_signal(signal.SIGINT, None)


class StartedPush(Base):
    def setUp(self):
        super().setUp()
        self.p.state.mkdir(parents=True, exist_ok=True)
        save_json(self.p.state / TEARDOWN_FAILED, {"at": "", "cmd": "adb kill-server", "how": "exit 1", "last": ""})

    def test_a_started_push_that_fails_keeps_the_failed_teardown_for_the_next_run(self):
        self.sent = False
        self.run_once()
        self.assertIn("windvane started", self.pushes)
        self.assertTrue((self.p.state / TEARDOWN_FAILED).exists())

    def test_a_started_push_that_goes_out_clears_it(self):
        self.run_once()
        self.assertIn("windvane started", self.pushes)
        self.assertFalse((self.p.state / TEARDOWN_FAILED).exists())


class NoPrompts(unittest.TestCase):
    """A guarded command has no terminal: git and ssh must fail rather than wait on a prompt there."""

    def setUp(self):
        self.addCleanup(setattr, util, "shielded", util.shielded)
        util.shielded = True

    def env_of(self, env=None, cwd="."):
        r = util.run(["sh", "-c", 'echo "$GIT_TERMINAL_PROMPT|$GIT_SSH_COMMAND"'], cwd, env=env)
        return r.stdout.strip()

    def test_a_shielded_command_gets_no_prompts_and_batch_mode(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_SSH_COMMAND", None)
            self.assertEqual(self.env_of(), "0|ssh -o BatchMode=yes")

    def test_the_users_own_ssh_command_is_kept(self):
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -i ~/.ssh/deploy"}):
            self.assertEqual(self.env_of(), "0|ssh -i ~/.ssh/deploy")
        self.assertEqual(self.env_of({"PATH": os.environ["PATH"], "GIT_SSH_COMMAND": "mine"}), "0|mine")

    def test_an_unshielded_command_keeps_its_env_as_it_is(self):
        util.shielded = False
        with mock.patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "1"}):
            os.environ.pop("GIT_SSH_COMMAND", None)
            self.assertEqual(self.env_of(), "1|")

    def test_a_push_needing_a_passphrase_fails_at_once_as_a_login_problem(self):
        """A real `git push` over ssh, to a fake ssh that refuses as a locked key does under BatchMode."""
        s = Sandbox()
        self.addCleanup(s.close)
        fake = s.tmp / "fakebin"
        fake.mkdir()
        (fake / "ssh").write_text(f'#!/bin/sh\necho "$@" > {s.tmp}/ssh-args\n'
                                  'echo "git@example.invalid: Permission denied (publickey)." >&2\nexit 255\n')
        (fake / "ssh").chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{fake}:{os.environ['PATH']}"}):
            os.environ.pop("GIT_SSH_COMMAND", None)
            r = util.git(s.root, "push", "git@example.invalid:x/y.git", "HEAD:refs/heads/x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BatchMode=yes", (s.tmp / "ssh-args").read_text())
        self.assertTrue(held.push_refusal(r.stderr).startswith("login refused"))

    def test_an_unknown_host_key_says_to_accept_it_not_that_the_network_is_down(self):
        stderr = "Host key verification failed.\nfatal: Could not read from remote repository.\n"
        self.assertEqual(held.push_refusal(stderr), "the remote's ssh host key isn't trusted yet; connect once by hand "
                                                    "(`ssh -T` to its host) to check and accept it, and the next run "
                                                    "pushes it")

    def test_a_users_core_ssh_command_or_git_ssh_is_kept(self):
        """Ours in GIT_SSH_COMMAND would override them, and the key they pick would never be used."""
        s = Sandbox()
        self.addCleanup(s.close)
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("GIT_SSH_COMMAND", None)
            self.assertEqual(self.env_of(cwd=s.root), "0|ssh -o BatchMode=yes")
            os.environ["GIT_SSH"] = "/usr/local/bin/my-ssh"
            self.assertEqual(self.env_of(cwd=s.root), "0|")
            del os.environ["GIT_SSH"]
            s.git("config", "core.sshCommand", "ssh -i ~/.ssh/work_key")
            self.assertEqual(self.env_of(cwd=s.root), "0|")


if __name__ == "__main__":
    unittest.main()
