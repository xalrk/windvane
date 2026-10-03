"""A project's own permission rules per session role (`[permissions]`, a `[[periodic]]` entry's allow/deny): each
list reaches its role's sessions only, a project without them gets today's settings, and a rule the CLI couldn't
parse stops the run at its start instead of being dropped by every session."""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, doctor, driver, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

PERMS = """[permissions]
worker_allow = ["Bash(tools/*)"]
worker_deny = ["WebFetch"]
critic_deny = ["Edit", "Write"]
review_allow = ["Bash(tools/lint.sh)"]
plan_deny = ["Edit(src/**)"]
fix_allow = ["Bash(make *)"]
periodic_deny = ["WebSearch"]
"""
PLAYTEST = """[[periodic]]
name = "playtest"
every = 1
minutes = 30
prompt = "tools/PLAYTEST.md"
allow = ["Bash(tools/record.sh *)", "Write(out/**)"]
deny = ["Edit"]
"""


class Permissions(unittest.TestCase):
    def make(self, config=""):
        self.s = Sandbox(config)
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def calls(self):
        f = self.s.tmp / "calls.jsonl"
        return [c for c in map(json.loads, f.read_text().splitlines() if f.exists() else []) if "flags" in c]

    def settings(self, call):
        flags = call["flags"]
        return flags[flags.index("--settings") + 1]

    def session(self, role, allow=(), deny=()):
        """The `--settings` one session of `role` got from the fake `claude`, as text."""
        runner.run_claude(self.p, role, f"{role}-run", "hi", "# windvane test", self.s.root, allow, deny)
        return self.settings(self.calls()[-1])

    def test_each_roles_lists_reach_that_role_only(self):
        self.make(PERMS)
        want = {"worker": (["Bash(tools/*)"], ["WebFetch"]), "critic": ([], ["Edit", "Write"]),
                "reviewer": (["Bash(tools/lint.sh)"], []), "planner": ([], ["Edit(src/**)"]),
                "fix": (["Bash(make *)"], []), "preflight": ([], [])}
        for role, (allow, deny) in want.items():
            got = json.loads(self.session(role, ["Bash(git *)"], ["Agent"]))["permissions"]
            self.assertEqual(got, {"allow": ["Bash(git *)", *allow], "deny": ["Agent", *deny]}, role)

    def test_no_permissions_keeps_todays_settings_byte_for_byte(self):
        self.make()
        allow, deny = driver.worker_perms(self.p)
        before = json.dumps({"permissions": {"allow": list(allow), "deny": list(deny)}})  # runner.py before T-031
        self.assertEqual(self.session("worker", allow, deny), before)
        self.assertEqual(self.session("critic", (), ["Bash"]), '{"permissions": {"allow": [], "deny": ["Bash"]}}')

    def test_a_run_gives_the_worker_its_rules_after_the_drivers_own(self):
        self.make(PERMS)
        self.s.ticket("T-001")
        self.s.publish()
        driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        work = [c for c in self.calls() if c["system"].startswith("# windvane worker")]
        review = [c for c in self.calls() if c["system"].startswith("# windvane review")]
        self.assertTrue(work and review)
        got = json.loads(self.settings(work[0]))["permissions"]
        own_allow, own_deny = driver.worker_perms(self.p)
        self.assertEqual(got, {"allow": [*own_allow, "Bash(tools/*)"], "deny": [*own_deny, "WebFetch"]})
        self.assertIn("Bash(tools/lint.sh)", json.loads(self.settings(review[0]))["permissions"]["allow"])
        self.assertNotIn("Bash(tools/lint.sh)", got["allow"])

    def test_a_periodic_entrys_lists_reach_its_session(self):
        self.make(PERMS + PLAYTEST)
        self.s.ticket("T-001")
        self.s.write("tools/PLAYTEST.md", "# Playtest\nPLAY\n")
        self.s.publish()
        driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        play = [c for c in self.calls() if c["system"].startswith("# windvane periodic")]
        self.assertTrue(play, "the playtest ran")
        got = json.loads(self.settings(play[0]))["permissions"]
        self.assertEqual(got["allow"][-2:], ["Bash(tools/record.sh *)", "Write(out/**)"])
        self.assertEqual(got["deny"][-2:], ["WebSearch", "Edit"])  # every periodic session's, then its own
        work = [c for c in self.calls() if c["system"].startswith("# windvane worker")]
        self.assertNotIn("Write(out/**)", self.settings(work[0]))

    def test_a_rule_the_cli_cant_parse_stops_the_run_at_its_start(self):
        self.make('[permissions]\nworker_allow = ["bash(tools/*)", "Bash(ok)"]\ncritic_deny = "Edit"\n'
                  'wroker_allow = ["Bash"]\n')
        self.s.ticket("T-001")
        self.s.publish()
        pushes, orig = [], driver.push
        driver.push = lambda title, msg, *a, **k: pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(self.calls(), [])  # no session ran without its rules
        self.assertEqual(pilot.merged, [])
        self.assertEqual(len(pushes), 1)  # a single push, saying why
        title, msg = pushes[0]
        self.assertIn("windvane stopped", title)
        for part in ("3 bad permission rules", "wroker_allow: no such key", "'bash(tools/*)'",
                     "and 1 more (windvane doctor lists them)"):  # the first two fit a push
            self.assertIn(part, msg)
            self.assertIn(part, (self.p.state / "autopilot.log").read_text())

    def test_a_periodic_session_by_hand_or_a_desk_session_stops_on_a_bad_rule(self):
        self.make('[[periodic]]\nname = "playtest"\nprompt = "tools/P.md"\nallow = ["Write(out/**"]\n')
        self.s.write("tools/P.md", "# P\nPLAY\n")
        self.s.publish()
        self.addCleanup(os.chdir, os.getcwd())  # `-C` moves into the sandbox, which goes at the end
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), "periodic", "playtest"])
        self.assertIn("1 bad permission rule in config.toml: [[periodic]] playtest allow: 'Write(out/**'",
                      str(e.exception.code))  # before its setup, not at the session
        self.assertFalse(driver.locked(self.p))
        with self.assertRaises(SystemExit) as e:  # `windvane plan` or `windvane critic` at the desk
            runner.run_claude(self.p, "planner", "plan", "hi", "# windvane plan", self.s.root)
        self.assertIn("'Write(out/**'", str(e.exception.code))
        self.assertEqual(self.calls(), [])  # no session ran without its rules

    def test_doctor_names_a_bad_rule(self):
        self.make('[[periodic]]\nname = "pt"\nprompt = "x.md"\ndeny = ["Bash()"]\n')
        rows = [r for r in doctor.checks(self.p) if "[permissions]" in r[1]]
        self.assertEqual(rows[0][0], "need")
        self.assertIn("[[periodic]] pt deny: 'Bash()'", rows[0][1])
        self.make('[permissions]\nworker_allow = ["Bash(tools/*)", "mcp__files__read", "mcp__my-db__*", "WebFetch(domain:x.org)"]\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual([r[0] for r in doctor.checks(self.p) if "[permissions]" in r[1]], ["ok"])


if __name__ == "__main__":
    unittest.main()
