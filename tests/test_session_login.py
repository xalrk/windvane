"""Claude Code's login file is out of every session's reach, in auto mode: deny rules keep the file tools off it,
the Bash sandbox keeps commands off it and off the login variables, and windvane's own commands, which must run
outside the sandbox, stop inside it with how to run them. CLAUDE_CODE_SUBPROCESS_ENV_SCRUB never reaches a session:
with it Claude Code forces the default permission mode, and a headless session can then write nothing."""
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, jobs, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

SCRUB = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"


class SessionLogin(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        os.environ["FAKE_ENV"] = str(self.s.tmp / "env.jsonl")

    def calls(self):
        """The role sessions the fake `claude` saw: not the usage probe (it sends "ok" from an empty folder, and reads
        nothing a project holds) or `claude auth status`."""
        return [c for c in map(json.loads, (self.s.tmp / "calls.jsonl").read_text().splitlines())
                if "--append-system-prompt" in c.get("flags", ())]

    def settings(self, call):
        flags = call["flags"]
        return json.loads(flags[flags.index("--settings") + 1])

    def guard(self, settings, login):
        """`settings` keep the file tools and Bash commands off `login`, and the session in auto mode."""
        self.assertEqual(settings["permissions"]["deny"][:2], [f"Read(/{login})", f"Edit(/{login})"])
        box = settings["sandbox"]
        self.assertTrue(box["enabled"])
        self.assertEqual(box["credentials"]["files"], [{"path": str(login), "mode": "deny"}])
        self.assertEqual({v["name"] for v in box["credentials"]["envVars"] if v["mode"] == "deny"},
                         {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"})
        self.assertEqual(box["excludedCommands"], ["windvane *", "git *"])

    def test_every_session_of_a_run_is_kept_off_the_login_file_and_gets_no_scrub(self):
        os.environ[SCRUB] = "1"  # the user's shell may export it
        self.s.ticket("T-001")
        self.s.publish()
        driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        login = Path(os.environ["HOME"]).resolve() / ".claude/.credentials.json"
        for role in ("# windvane worker", "# windvane review"):
            self.assertTrue(any(c["system"].startswith(role) for c in self.calls()), role)
        for call in self.calls():
            self.guard(self.settings(call), login)
            flags = call["flags"]
            self.assertEqual(flags[flags.index("--permission-mode") + 1], "auto")
        envs = [json.loads(x)["env"] for x in Path(os.environ["FAKE_ENV"]).read_text().splitlines()]
        self.assertTrue(envs)
        for env in envs:
            self.assertNotIn(SCRUB, env)

    def test_a_config_dir_moves_the_rules_to_its_login_file(self):
        conf = self.s.tmp / "claude-conf"
        os.environ["CLAUDE_CONFIG_DIR"] = str(conf)
        runner.run_claude(self.p, "critic", "critic-run", "hi", "# windvane test", self.s.root)
        self.guard(self.settings(self.calls()[-1]), conf.resolve() / ".credentials.json")
        self.assertNotIn(str(Path(os.environ["HOME"]) / ".claude"), json.dumps(self.settings(self.calls()[-1])))

    def test_inside_the_sandbox_a_session_or_job_stops_and_says_how_to_run_it(self):
        os.environ["SANDBOX_RUNTIME"] = "1"
        with self.assertRaises(SystemExit) as stop:
            runner.run_claude(self.p, "critic", "critic-run", "hi", "# windvane test", self.s.root)
        self.assertIn("on its own", str(stop.exception))
        self.assertFalse((self.s.tmp / "calls.jsonl").exists(), "no session started")
        with self.assertRaises(SystemExit) as stop:
            jobs.bg(self.p, "build", ["true"], self.s.root)
        self.assertIn("Bash sandbox", str(stop.exception))
        self.assertFalse(Path(f"{jobs.job_base(self.p, 'build')}.pid").exists(), "no job started")


if __name__ == "__main__":
    unittest.main()
