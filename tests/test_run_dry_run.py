"""`windvane run --dry-run`: the run's settings as a run with those flags would use them, and nothing started."""
import contextlib
import fcntl
import io
import unittest

from helpers import Sandbox

from windvane import cli
from windvane.config import Project

TOP = """\
replan = 2
goal = "Make the queue faster\\nand more besides"
protected = ["docs/", ".windvane/config.toml:experiment_*"]
"""
TABLES = """
[experiments]
week_below = 60
week_share = 20
plateau = 5

[[periodic]]
name = "playtest"
prompt = "PLAYTEST.md"
every = 4
land = "near"

[[periodic]]
name = "scout"
builtin = "scout"
scout_every = 12
"""


class DryRun(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox(TABLES)
        self.addCleanup(self.sb.close)
        cfg = self.sb.root / ".windvane/config.toml"
        cfg.write_text(TOP + cfg.read_text())  # top-level keys before the Sandbox's own tables
        self.p = Project(self.sb.root)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.sb.root), "run", *args])
        return out.getvalue()

    def refusal(self, *args):
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()):
            cli.main(["-C", str(self.sb.root), "run", *args])
        return str(e.exception.code)

    def test_it_prints_each_setting_from_the_config(self):
        out = self.run_cli("--for", "3h", "--dry-run")
        self.assertIn("dry run: nothing starts\n", out)
        self.assertIn("caps: 5-hour 75%, weekly 95%", out)
        self.assertIn("ticket hours: 3; min slot: 30 min", out)
        self.assertIn("replan: up to 2 plans; goal: Make the queue faster\n", out)
        self.assertIn("protected: docs/, .windvane/config.toml:experiment_*, .windvane/config.toml:protected", out)
        self.assertIn("experiments: benches start below 60% weekly usage and use up to 20% of the week; "
                      "plateau after 5 rejected in a row", out)
        self.assertIn("periodic playtest: every 4 merges; tickets land near", out)
        self.assertIn("periodic scout: scout every 12 tickets finished on main", out)
        self.assertRegex(out, r"periodic scout: [^\n]*; tickets land near\n")  # no `land`: where the driver puts them
        self.assertNotIn("running now", out)

    def test_a_flag_overrides_the_config_value_printed(self):
        out = self.run_cli("--for", "3h", "--cap", "50", "--replan", "forever", "--goal", "Other goal",
                           "--ticket-hours", "1.5", "--parallel", "2", "--dry-run")
        self.assertIn("caps: 5-hour 50%, weekly 95%", out)
        self.assertIn("replan: forever; goal: Other goal", out)
        self.assertIn("ticket hours: 1.5;", out)
        self.assertIn("parallel: 2\n", out)

    def test_a_daily_window_is_printed_and_says_it_needs_detach(self):
        out = self.run_cli("--daily", "23:00-07:00", "--dry-run")
        self.assertIn("window: daily 23:00-07:00 until windvane stop, the first ", out)
        self.assertIn("(a run needs --detach for it)", out)
        self.assertNotIn("needs --detach", self.run_cli("--daily", "23:00-07:00", "--detach", "--dry-run"))

    def test_a_too_short_run_is_refused_as_a_real_run_is(self):
        dry = self.refusal("--for", "20m", "--dry-run")
        self.assertIn("min_slot", dry)
        self.assertEqual(dry, self.refusal("--for", "20m"))
        self.assertEqual(self.refusal("--dry-run"), "say when to stop: --until HH:MM or --for 6h")

    def test_a_bad_config_is_refused(self):
        cfg = self.sb.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace("plateau = 5", "plateau = 0"))
        self.assertIn("plateau", self.refusal("--for", "3h", "--dry-run"))

    def test_it_writes_nothing_and_needs_no_claude(self):
        (self.sb.bin / "claude").unlink()
        self.assertIn("parallel:", self.run_cli("--for", "3h", "--dry-run"))
        self.assertFalse(self.p.state.exists())
        self.assertFalse((self.sb.tmp / "state").exists() and any((self.sb.tmp / "state").iterdir()))
        self.assertFalse((self.sb.tmp / "calls.jsonl").exists())
        self.assertEqual(self.sb.git("status", "--porcelain"), "M .windvane/config.toml")

    def test_it_says_so_while_a_run_is_going(self):
        self.p.state.mkdir(parents=True)
        with open(self.p.state / "lock", "w") as fh:  # held as a running driver holds it
            fcntl.flock(fh, fcntl.LOCK_EX)
            out = self.run_cli("--for", "3h", "--dry-run")
        self.assertIn("an autopilot is running now", out)
        self.assertIn("caps:", out)


if __name__ == "__main__":
    unittest.main()
