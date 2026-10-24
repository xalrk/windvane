import argparse
import io
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, heavy, jobs, util, worktree  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

SCALE = "# What a 10 means\n\n## works\n- 10 works: what the ticket promises happens, and again on a second pass.\n"
# The ticket's change: a tool that takes a lock and never clears it, so its second pass fails, and the test that says so
TOOL = "import os, sys\nif os.path.exists('run.lock'):\n    sys.exit('locked')\nopen('run.lock', 'w').close()\n"
TEST = ("python3 tool.py && echo 'ok first pass' || echo 'FAIL first pass'\n"
        "python3 tool.py && echo 'ok second pass' || echo 'FAIL second pass: run.lock is left behind'\n")
PASSES = "Checks:\n- works: yes, pack: the tests pass\nVerdict.\nCRITIC: {tid} fixes=0 fine"


def files(root):
    """Every file of a worktree, its index and HEAD included, by path: to tell it's byte-identical."""
    out = {}
    for f in sorted(Path(root).rglob("*")):
        rel = f.relative_to(root)
        if f.is_file() and not (rel.parts[0] == ".git" and rel.name not in ("index", "HEAD")):
            out[str(rel)] = f.read_bytes()
    return out


class CriticTrial(unittest.TestCase):
    """Through `windvane critic` and the Sandbox's fake `claude`: the ticket critic works in a throwaway copy of the
    ticket's head, runs commands there, and the copy is gone afterwards however the critic ended."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", SCALE)
        s.ticket("T-001")
        s.publish("critic on")
        s.git("switch", "-q", "-c", "t-001-lock")
        s.write("tool.py", TOOL)
        s.write("test.sh", TEST)
        s.git("add", "-A")
        s.git("commit", "-qm", "a tool with a lock")
        s.write("scratch.txt", "the worker's own, uncommitted\n")
        self.s, self.p = s, Project(s.root)
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\n`sh test.sh` shows the promise: the tool runs, twice.\n")
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)
        self.env("FAKE_CRITIC_TEXT", PASSES)

    def env(self, key, value):
        os.environ[key] = value
        self.addCleanup(os.environ.pop, key, None)

    def critic(self):
        return self.critic_code()[1]

    def critic_code(self):
        """`windvane critic T-001` in this process: (its exit code, its output)."""
        out, code = io.StringIO(), 0
        with redirect_stdout(out), redirect_stderr(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0 if e.code is None else 1
        return code, out.getvalue()

    def in_session(self, seconds):
        """`windvane critic` as a worker session runs it, its tool call stopping after `seconds` (10 minutes for real)."""
        for k in (IN_JOB, IN_SANDBOX):  # a suite run as a background job is in one
            if k in os.environ:
                self.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
        self.env(heavy.STOP_AT, str(time.time() + 600))
        self.env("WINDVANE_TICKET", "T-001")
        base = jobs.job_base(self.p, jobs.SESSION_CRITIC)
        self.addCleanup(lambda: jobs.end_job(base, "test over") if jobs.alive(base) else None)
        limit = mock.patch.object(jobs, "AWAIT_S", seconds)
        limit.start()
        self.addCleanup(limit.stop)

    def calls(self):
        return [c for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]

    def tried(self):
        return read_jsonl(Path(str(self.s.tmp / "calls.jsonl") + ".tried"))

    def assertGone(self):
        """No trial left: not on disk, not in git's list of worktrees."""
        trials = self.p.state / worktree.TRIALS
        self.assertEqual([], list(trials.iterdir()) if trials.is_dir() else [])
        listed = self.s.git("worktree", "list", "--porcelain")
        self.assertNotIn(worktree.TRIALS, listed)

    def test_runs_in_a_copy_of_the_head_and_leaves_the_worker_tree_alone(self):
        self.env("FAKE_CRITIC_TRY", "git rev-parse HEAD; ls; echo $GIT_CONFIG_COUNT; sh test.sh; ls")
        before = files(self.s.root)
        out = self.critic()
        self.assertEqual(before, files(self.s.root), "the worker's tree changed")
        [seen] = self.tried()
        self.assertTrue(seen["cwd"].startswith(str(self.p.state / worktree.TRIALS)), seen["cwd"])
        self.assertIn(self.s.git("rev-parse", "HEAD"), seen["out"])  # the ticket's head, committed only
        self.assertNotIn("scratch.txt", seen["out"])  # what the worker hasn't committed isn't judged
        self.assertIn("run.lock", seen["out"])  # the tool's lock, left in the copy
        self.assertFalse((self.s.root / "run.lock").exists())
        [call] = self.calls()
        self.assertNotIn("Bash", call["deny"])  # it may run commands...
        for rule in ("Bash(git commit *)", "Bash(git push *)", "Bash(git switch *)", "Bash(gh *)"):
            self.assertIn(rule, call["deny"])  # ...but not these
        self.assertIn("Bash", call["flags"][call["flags"].index("--tools") + 1])
        self.assertIn("## Your copy\n", call["prompt"])
        self.assertIn("15 minutes", call["prompt"])
        self.assertRegex(out, r"CRITIC STEP: (revise|rethink)\.")
        self.assertGone()

    def test_a_failing_case_it_runs_sends_the_ticket_back_with_that_case(self):
        self.env("FAKE_CRITIC_TRY", "sh test.sh")
        out = self.critic()
        self.assertRegex(out, r"CRITIC STEP: (revise|rethink)\.")
        self.assertIn("FAIL second pass: run.lock is left behind", out)
        last = [r for r in read_jsonl(self.p.state / "critic.jsonl") if r.get("ticket") == "T-001"][-1]
        self.assertEqual(("rethink", "ran"), (last["step"], last["trial"]))  # its only rung failed
        self.assertIn("FAIL second pass", last["critique"])
        self.assertGone()

    def test_it_passes_once_the_case_is_fixed(self):
        self.s.write("tool.py", TOOL.replace("open('run.lock', 'w').close()", "print('done')"))
        self.s.git("commit", "-qam", "no lock")
        self.env("FAKE_CRITIC_TRY", "sh test.sh")
        self.assertIn("CRITIC STEP: pass", self.critic())

    def test_commits_and_pushes_are_refused_in_the_copy(self):
        self.env("FAKE_CRITIC_TRY", "git commit -q --allow-empty -m mine; echo commit=$?; "
                                    "git push -q origin HEAD:refs/heads/critic; echo push=$?")
        head = self.s.git("rev-parse", "HEAD")
        self.critic()
        [seen] = self.tried()
        self.assertIn("commit=1", seen["out"])
        self.assertIn("push=1", seen["out"])
        self.assertIn("no commits or pushes", seen["out"])
        self.assertEqual("", self.s.git("ls-remote", str(self.s.remote), "refs/heads/critic"))
        self.assertEqual(head, self.s.git("rev-parse", "HEAD"))
        self.assertGone()

    def test_it_never_drives_the_autopilot(self):
        self.env("FAKE_CRITIC_TRY", "windvane stop; echo stop=$?; bin=$(command -v windvane); "
                                    "$(readlink -f $bin) run --for 1h; echo run=$?; "
                                    "windvane quiet; echo quiet=$?")
        self.critic()
        [seen] = self.tried()
        self.assertIn("stop=1", seen["out"])
        self.assertIn("run=1", seen["out"])  # its own copy of the CLI too: the role is in its environment
        self.assertIn("a critic never drives the autopilot: `windvane stop` is refused", seen["out"])
        self.assertIn("`windvane run` is refused on every state root", seen["out"])
        self.assertIn("quiet=0", seen["out"])  # what only reads still runs
        [call] = self.calls()
        self.assertNotIn("Bash(windvane stop *)", call["deny"])  # it would turn away the scratch form too
        self.assertFalse((self.p.state / "STOP").exists())

    def test_it_drives_windvane_on_scratch_state(self):
        self.env("FAKE_CRITIC_TRY", "XDG_STATE_HOME=$TMPDIR/state windvane stop; echo stop=$?; "
                                    "ls $TMPDIR/state/windvane/*/STOP; windvane doctor --clean; echo clean=$?")
        self.critic()
        [seen] = self.tried()
        self.assertIn("stop=0", seen["out"])
        self.assertIn("/STOP\n", seen["out"])  # the scratch run's stop file
        self.assertIn("`windvane doctor --clean` is refused on the live state", seen["out"])  # it names the scratch form
        self.assertIn("`XDG_STATE_HOME=$TMPDIR/state windvane doctor --clean ...`", seen["out"])
        self.assertIn("clean=1", seen["out"])
        self.assertFalse((self.p.state / "STOP").exists())

    def test_what_reaches_past_the_state_dir_is_refused_on_scratch_state_too(self):
        # scratch state moves only the state dir: a run there would still push, open PRs and merge on the real remote
        self.env("FAKE_CRITIC_TRY", "export XDG_STATE_HOME=$TMPDIR/state; windvane run --for 1h; echo run=$?; "
                                    "windvane ci; echo ci=$?; windvane answer T-001 A; echo answer=$?")
        self.critic()
        [seen] = self.tried()
        for cmd in ("run", "ci", "answer"):
            self.assertIn(f"{cmd}=1", seen["out"])
            self.assertIn(f"`windvane {cmd}` is refused on every state root", seen["out"])
        self.assertNotIn("XDG_STATE_HOME=$TMPDIR/state windvane run", seen["out"])  # no form that would run it
        self.assertEqual(1, len(self.calls()))  # the critic's own session: no worker or reviewer started

    def test_a_retry_gets_its_own_time_box(self):
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = true", "enabled = true\ntrial_minutes = 0.03"))
        self.p = Project(self.s.root)
        self.env("FAKE_FAIL", "error,ok")
        waits = []

        def retry(error):  # the backoff outlasts the box: the second attempt must still get its whole box
            waits.append(error)
            time.sleep(2.5)
            return len(waits) == 1
        entry, _ = critic.judge(self.p, "T-001", str(self.s.root), retry=retry)
        self.assertEqual(1, len(waits))
        self.assertEqual(("pass", "ran"), (entry["step"], entry["trial"]))
        self.assertEqual(2, len(self.calls()))  # the error, then the verdict: no time-up turn
        self.assertGone()

    def test_gone_when_the_critic_dies(self):
        self.env("FAKE_ERROR_FOR", "Critique T-001")
        self.env("FAKE_CRITIC_TRY", "true")
        before = files(self.s.root)
        entry, out = critic.judge(self.p, "T-001", str(self.s.root))  # no retry: `windvane critic` would wait minutes
        self.assertEqual("unavailable", entry["step"])
        self.assertEqual(before, files(self.s.root))
        self.assertGone()

    def test_stopped_at_its_time_box_it_answers_on_what_it_saw(self):
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = true", "enabled = true\ntrial_minutes = 0.02"))
        self.p = Project(self.s.root)
        self.env("FAKE_CRITIC_SLOW", "30")
        self.env("FAKE_CRITIC_REASK", PASSES)
        started = time.time()
        out = self.critic()
        self.assertLess(time.time() - started, 20)  # stopped at about a second, never left to run on
        self.assertIn("CRITIC STEP: pass", out)
        first, late = self.calls()
        self.assertIn("--resume", late["flags"])
        self.assertTrue(late["prompt"].startswith("Your 0.02 minutes for trying it are up"))
        self.assertIn("Bash", late["deny"])  # answering only: nothing more to run
        last = [r for r in read_jsonl(self.p.state / "critic.jsonl") if r.get("ticket") == "T-001"][-1]
        self.assertEqual("timed out", last["trial"])
        rows = [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("role") == "critic"]
        self.assertEqual(critic.TRIAL_UP, rows[0]["killed"])
        self.assertGone()

    def test_out_of_time_before_its_session_starts_the_record_says_so(self):
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = true", "enabled = true\ntrial_minutes = 0.02"))
        self.p = Project(self.s.root)
        self.env("FAKE_CRITIC_PID", str(self.s.tmp / "critic.pid"))  # sleeps before any event: no session id
        out = self.critic()
        self.assertIn("CRITIC STEP: none", out)
        self.assertEqual(1, len(self.calls()))  # nothing to resume
        last = [r for r in read_jsonl(self.p.state / "critic.jsonl") if r.get("ticket") == "T-001"][-1]
        self.assertEqual(("none", "timed out"), (last["step"], last["trial"]))
        self.assertGone()

    def test_gone_when_stopped_by_the_run(self):
        self.env("FAKE_CRITIC_SLOW", "30")
        before = files(self.s.root)
        stop = {"at": time.time() + 1}
        entry, text = critic.judge(self.p, "T-001", str(self.s.root),
                                   stop_if=lambda: "stop now" if time.time() >= stop["at"] else None)
        self.assertIsNone(entry)  # no verdict, never a hang
        self.assertEqual(before, files(self.s.root))
        self.assertGone()

    def test_a_killed_critic_s_copy_goes_with_the_next_trial(self):
        self.env("FAKE_CRITIC_SLOW", "3")
        proc = subprocess.Popen(["windvane", "critic", "T-001"], cwd=self.s.root, start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        trials = self.p.state / worktree.TRIALS
        until = time.time() + 20
        while not self.calls() and time.time() < until:  # its critic started
            time.sleep(0.05)
        self.assertTrue(self.calls(), "no critic started")
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        self.assertTrue(any(trials.iterdir()))  # killed outright: nothing ran its cleanup
        os.environ.pop("FAKE_CRITIC_SLOW")
        self.critic()
        self.assertGone()

    def test_a_copy_killed_while_it_was_made_goes_too(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        trials = self.p.state / worktree.TRIALS
        left = trials / f"T-009.{dead.pid}"
        self.s.git("worktree", "add", "-q", "--detach", str(left), "HEAD")
        self.s.git("worktree", "lock", "--reason", "initializing", str(left))  # as git leaves one it was killed making
        with worktree.trial(self.p, str(self.s.root), "HEAD", "T-001"):
            self.assertFalse(left.exists())
        self.assertGone()

    def test_a_locked_entry_whose_folder_is_gone_goes_too(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        left = self.p.state / worktree.TRIALS / f"T-009.{dead.pid}"
        self.s.git("worktree", "add", "-q", "--detach", str(left), "HEAD")
        self.s.git("worktree", "lock", "--reason", "initializing", str(left))
        Path(f"{left}.hooks").mkdir()
        import shutil
        shutil.rmtree(left)  # its folder went, git's locked entry stayed
        with worktree.trial(self.p, str(self.s.root), "HEAD", "T-001"):
            pass
        self.assertGone()

    def test_a_copy_whose_pid_was_reused_goes_too(self):
        trials = self.p.state / worktree.TRIALS
        if not str(util.stamp(os.getpid()) or "").startswith("boot:"):
            self.skipTest("no /proc here: a reused pid's copy waits for that process to end")
        old, new = trials / f"T-008.{os.getpid()}", trials / f"T-007.{os.getpid()}"
        for left, owner in ((old, "boot:another-boot:12"), (new, util.stamp(os.getpid()))):  # old: an earlier process's
            self.s.git("worktree", "add", "-q", "--detach", str(left), "HEAD")
            Path(f"{left}.hooks").mkdir()
            Path(f"{left}.hooks", "owner").write_text(owner)
        worktree.sweep_trials(str(self.s.root), trials)
        self.assertEqual((False, True), (old.exists(), new.exists()))  # this process's own copy stays
        worktree.drop_trial(str(self.s.root), new)

    def test_a_clock_step_leaves_a_live_critics_copy(self):
        trials = self.p.state / worktree.TRIALS
        mine = trials / f"T-007.{os.getpid()}"
        self.s.git("worktree", "add", "-q", "--detach", str(mine), "HEAD")
        Path(f"{mine}.hooks").mkdir()
        Path(f"{mine}.hooks", "owner").write_text(util.stamp(os.getpid()) or "")
        os.utime(mine, (time.time() - 3600, time.time() - 3600))  # the wall clock stepped an hour since it was made
        worktree.sweep_trials(str(self.s.root), trials)
        self.assertTrue(mine.exists())
        worktree.drop_trial(str(self.s.root), mine)

    def test_a_session_gets_its_verdict_from_a_critique_longer_than_one_call(self):
        # a session's tool call stops at 10 minutes, a critique can take longer: it runs as the session's bg job
        self.in_session(1)
        self.env("FAKE_CRITIC_SLOW", "4")
        self.env("FAKE_CRITIC_TRY", "true")
        code, out = self.critic_code()
        self.assertEqual(heavy.TIMED_OUT, code, out)
        self.assertIn("call windvane critic T-001 again", out)
        self.assertNotIn("CRITIC STEP", out)
        old = self.s.git("rev-parse", "HEAD")[:7]
        self.s.git("commit", "-q", "--allow-empty", "-m", "a new head meanwhile")
        code, out = self.critic_code()  # asked again while it runs: the same critique, not a second one
        self.assertEqual(1, len(self.calls()))
        self.assertIn(f"critic: the running critique started on {old}, not {self.s.git('rev-parse', 'HEAD')[:7]}: "
                      "once it ends, run `windvane critic T-001` again", out)
        for _ in range(20):
            if code != heavy.TIMED_OUT:
                break
            code, out = self.critic_code()
        self.assertEqual(0, code, out)
        self.assertIn("CRITIC STEP: pass", out)
        self.assertEqual(1, len(self.calls()))
        self.assertGone()
        os.environ.pop("FAKE_CRITIC_SLOW")
        code, out = self.critic_code()  # its verdict was read: asked again (a refreshed pack), it asks anew
        self.assertEqual((0, 2), (code, len(self.calls())), out)

    def test_a_session_s_finished_critique_it_never_read_is_given_not_asked_again(self):
        self.in_session(0.5)
        self.env("FAKE_CRITIC_SLOW", "2")
        code, out = self.critic_code()
        self.assertEqual(heavy.TIMED_OUT, code, out)
        base = jobs.job_base(self.p, jobs.SESSION_CRITIC)
        until = time.time() + 30
        while not Path(f"{base}.exit").exists() and time.time() < until:  # it ends between the worker's calls
            time.sleep(0.1)
        code, out = self.critic_code()
        self.assertIn("CRITIC STEP: pass", out)
        self.assertEqual(1, len(self.calls()))

    def test_off_it_reads_the_worker_tree_with_bash_denied(self):
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = true", "enabled = true\ntrial_minutes = 0"))
        self.p = Project(self.s.root)
        self.critic()
        [call] = self.calls()
        self.assertIn("Bash", call["deny"])
        self.assertNotIn("## Your copy", call["prompt"])
        self.assertFalse((self.p.state / worktree.TRIALS).exists())

    def test_a_copy_that_cant_be_made_leaves_the_critic_reading(self):
        with worktree.trial(self.p, str(self.s.root), "no-such-rev", "T-001") as (tree, env, note):
            self.assertIsNone(tree)
            self.assertIn("judge read-only", note)
        self.assertGone()

    def test_bad_setting_is_refused(self):
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = true", "enabled = true\ntrial_minutes = -1"))
        with self.assertRaises(SystemExit) as e:
            Project(self.s.root)
        self.assertIn("trial_minutes", str(e.exception))


if __name__ == "__main__":
    unittest.main()
