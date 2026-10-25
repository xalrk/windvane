"""`[critic] show`: the driver runs the project's own command for showing its output (a render, a recorded smoke run)
in the critic's copy of the head, once per tree, and hands the critic the images it leaves and its last lines. A show
that fails or runs out of time is no verdict on the ticket: the critic hears why and judges on the pack."""
import argparse
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, heavy  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_critic_trial import PASSES, SCALE, files  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402

# The project's show: two frames in WINDVANE_SHOW_DIR, a note there that isn't an image, a file where it runs, and a
# count of its runs outside every tree
SHOW = ("import os, sys\nout = os.environ['WINDVANE_SHOW_DIR']\nassert not os.listdir(out), 'not empty'\n"
        "for n in ('a', 'b'):\n    open(os.path.join(out, f'frame-{n}.png'), 'wb').write(b'\\x89PNG')\n"
        "open(os.path.join(out, 'log.txt'), 'w').write('x')\nopen('made-here', 'w').close()\n"
        "open(os.environ['SHOW_COUNT'], 'a').write('ran\\n')\n"
        "print('held:', os.environ.get('WINDVANE_HEAVY_HELD', 'no'))\nprint('rendered 2 frames')\n")


class CriticShow(unittest.TestCase):
    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        self.plain = s.root.joinpath(".windvane/config.toml").read_text().replace("enabled = false", "enabled = true")
        s.write(".windvane/config.toml", self.plain)
        s.write(".windvane/CRITIC.md", SCALE)
        s.write("show.py", SHOW)
        s.ticket("T-001")
        s.publish("critic on")
        s.git("switch", "-q", "-c", "t-001-ui")
        s.write("tool.py", "print('hi')\n")
        s.git("add", "-A")
        s.git("commit", "-qm", "a tool")
        self.s = s
        self.count = s.tmp / "show-runs"
        self.env("SHOW_COUNT", str(self.count))
        self.env("FAKE_CRITIC_TEXT", PASSES)
        for k in (IN_SANDBOX, IN_JOB, "WINDVANE_HEAVY_HELD"):  # a suite run as a background job is in one
            if k in os.environ:
                self.addCleanup(os.environ.__setitem__, k, os.environ.pop(k))
        self.config('show = "python3 show.py"')
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\n`python3 tool.py` prints hi.\n")
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)

    def env(self, key, value):
        old = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(lambda: os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old))

    def config(self, extra):
        """The config with `extra` in [critic]: the driver reads it from the worker's tree, so the head stays the same."""
        self.s.write(".windvane/config.toml", self.plain.replace("enabled = true", f"enabled = true\n{extra}"))
        self.p = Project(self.s.root)

    def critic(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit:
                pass
        return out.getvalue()

    def prompts(self):
        return [c["prompt"] for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]

    def last(self):
        return [r for r in read_jsonl(self.p.state / "critic.jsonl") if r.get("ticket") == "T-001"][-1]

    def runs(self):
        return len(self.count.read_text().splitlines()) if self.count.exists() else 0

    def test_its_images_reach_the_critic_and_nothing_lands_in_the_worker_tree(self):
        self.s.write("scratch.txt", "the worker's own, uncommitted\n")
        before = files(self.s.root)
        self.assertIn("CRITIC STEP: pass", self.critic())
        self.assertEqual(before, files(self.s.root), "the worker's tree changed")
        [prompt] = self.prompts()
        d = critic.show_dir(self.p, "T-001", str(self.s.root), "HEAD")
        shown = [str(d / "images" / f"frame-{n}.png") for n in "ab"]
        self.assertIn("## What the project's show made (`python3 show.py`, run by the driver on this head; Read the "
                      "images)\n" + "\n".join(shown) + "\n", prompt)
        self.assertTrue(all(Path(f).is_file() for f in shown))
        self.assertFalse((d / "images/log.txt").exists())  # only images are kept
        self.assertIn("held: no\nrendered 2 frames\n", prompt)
        self.assertLess(prompt.index("## The worker's pack notes"), prompt.index("## What the project's show made"))
        self.assertEqual(("ran", 1), (self.last()["show"], self.runs()))
        self.assertIn("show_minutes", self.last())

    def test_a_second_critique_of_the_same_tree_reuses_it(self):
        self.critic()
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.write_text(notes.read_text() + "A revised pack.\n")  # only the pack changed: the same tree
        self.critic()
        first, second = self.prompts()
        self.assertEqual(1, self.runs())
        part = first[first.index("## What the project's show made"):first.index("## Files the change touches")]
        self.assertIn(part, second)
        self.assertEqual("reused", self.last()["show"])

    def test_a_new_tree_runs_it_again_and_drops_the_old_results(self):
        self.critic()
        old = critic.show_dir(self.p, "T-001", str(self.s.root), "HEAD")
        self.s.write("tool.py", "print('hello')\n")
        self.s.git("commit", "-qam", "a revision")
        self.critic()
        self.assertEqual(2, self.runs())
        self.assertFalse(old.exists())
        self.assertTrue(critic.show_dir(self.p, "T-001", str(self.s.root), "HEAD").is_dir())

    def test_a_failing_show_gives_the_reason_and_still_a_verdict(self):
        self.config('show = "echo cannot open display; exit 3"')
        out = self.critic()
        self.assertIn("CRITIC STEP: pass", out)
        [prompt] = self.prompts()
        self.assertIn("## The project's show\nshow failed: exit 3: cannot open display. That says nothing of the "
                      "ticket: judge on the pack.", prompt)
        self.assertEqual(("failed", "pass"), (self.last()["show"], self.last()["step"]))
        said = [l for l in out.splitlines() if "show failed (" in l]  # the log's line, as `windvane critic` echoes it
        self.assertEqual(1, len(said), said)

    def test_a_timed_out_show_gives_the_reason_and_still_a_verdict(self):
        self.config('show = "sleep 30"\nshow_minutes = 0.02')
        started = time.time()
        self.assertIn("CRITIC STEP: pass", self.critic())
        self.assertLess(time.time() - started, 20)
        [prompt] = self.prompts()
        self.assertIn("show failed: it ran past show_minutes = 0.02.", prompt)
        self.assertEqual("failed", self.last()["show"])

    def test_show_heavy_goes_through_the_heavy_lock(self):
        run_dir = self.s.tmp / "run"
        run_dir.mkdir()
        for k, v in {**machine(run_dir, 60), heavy.STOP_AT: str(time.time() + 600)}.items():
            self.env(k, v)
        self.config('show = "python3 show.py"\nshow_heavy = true')
        self.critic()
        [prompt] = self.prompts()
        self.assertIn(f"held: {run_dir / 'windvane-heavy.lock'}\n", prompt)  # it ran holding the machine's lock

    def test_show_heavy_waits_for_the_machine_within_its_minutes(self):
        run_dir = self.s.tmp / "run"
        run_dir.mkdir()
        for k, v in {**machine(run_dir, 60), heavy.STOP_AT: str(time.time() + 600)}.items():
            self.env(k, v)
        hold(self, run_dir)  # another heavy job has the machine
        self.config('show = "python3 show.py"\nshow_heavy = true\nshow_minutes = 0.05')
        self.assertIn("CRITIC STEP: pass", self.critic())
        self.assertEqual(0, self.runs())  # it never got the machine
        [prompt] = self.prompts()
        self.assertIn("show failed: it ran past show_minutes = 0.05.", prompt)

    def test_a_failed_or_timed_out_show_runs_again_next_time(self):
        run_dir = self.s.tmp / "run"
        run_dir.mkdir()
        for k, v in {**machine(run_dir, 60), heavy.STOP_AT: str(time.time() + 600)}.items():
            self.env(k, v)
        holder = hold(self, run_dir)  # a busy machine: the show waits past its minutes
        self.config('show = "python3 show.py"\nshow_heavy = true\nshow_minutes = 0.05')
        self.critic()
        (run_dir / "release").touch()
        holder.wait()
        self.critic()  # the same tree: tried again, and it gets the machine now
        self.assertEqual((1, "ran"), (self.runs(), self.last()["show"]))
        self.assertIn("rendered 2 frames", self.prompts()[-1])

    def test_it_can_neither_commit_nor_push(self):
        self.config('show = "git commit -q --allow-empty -m mine; echo commit=$?; '
                    'git push -q origin HEAD:refs/heads/from-show; echo push=$?"')
        head = self.s.git("rev-parse", "HEAD")
        self.critic()
        [prompt] = self.prompts()
        self.assertIn("commit=1", prompt)
        self.assertIn("push=1", prompt)
        self.assertEqual("", self.s.git("ls-remote", str(self.s.remote), "refs/heads/from-show"))
        self.assertEqual(head, self.s.git("rev-parse", "HEAD"))

    def test_nor_from_a_light_read_s_copy(self):
        self.plain = self.plain.replace('depth = "full"', 'depth = "light"')
        self.config('show = "git push -q origin HEAD:refs/heads/from-show; echo push=$?"')
        self.critic()
        self.assertIn("push=1", self.prompts()[0])
        self.assertEqual("", self.s.git("ls-remote", str(self.s.remote), "refs/heads/from-show"))

    def test_a_light_read_gets_it_from_a_copy_of_its_own(self):
        self.plain = self.plain.replace('depth = "full"', 'depth = "light"')
        self.config('show = "python3 show.py"')
        self.critic()
        [prompt] = self.prompts()
        self.assertIn("## Depth: light", prompt)
        self.assertIn("rendered 2 frames", prompt)
        self.assertFalse((self.s.root / "made-here").exists())

    def test_unset_the_prompt_is_as_before(self):
        self.config("")
        self.critic()
        self.assertFalse(any(k.startswith("show") for k in self.last()))
        self.assertEqual(0, self.runs())
        (self.p.state / "critic.jsonl").unlink()  # a first critique again, of the same head, with `show` set
        self.config('show = "python3 show.py"')
        self.critic()
        before, after = self.prompts()
        self.assertNotIn("show", before.lower().replace("show.py", ""))
        start = after.index("## What the project's show made")
        self.assertEqual(before, after[:start] + after[after.index("## Files the change touches"):])


if __name__ == "__main__":
    unittest.main()
