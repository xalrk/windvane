"""What every session turn re-reads: each role starts with only the tools its rules use, no MCP servers and no skills,
and the ledger keeps each run's first-turn context so `windvane stats` can show base context and cached-in per turn."""
import argparse
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Context(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def session(self, role, system="# windvane test"):
        """One session through the fake `claude`: the flags it got."""
        runner.run_claude(self.p, role, f"{role}-run", "hi", system, self.s.root)
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return calls[-1]["flags"]

    def flag(self, flags, name):
        return flags[flags.index(name) + 1] if name in flags else None

    def test_each_role_gets_only_its_tools_and_no_mcp_servers_or_skills(self):
        want = {"worker": "Bash,Read,Edit,Write", "fix": "Bash,Read,Edit,Write", "planner": "Bash,Read,Edit,Write",
                "reviewer": "Bash,Read", "critic": "Read,Glob,Grep", "preflight": "Read,Glob,Grep"}
        for role, tools in want.items():
            flags = self.session(role)
            self.assertEqual(self.flag(flags, "--tools"), tools, role)
            self.assertIn("--disable-slash-commands", flags)
            self.assertIn("--strict-mcp-config", flags)
            self.assertEqual(json.loads(self.flag(flags, "--mcp-config")), {"mcpServers": {}})
            self.assertEqual(self.flag(flags, "--setting-sources"), "project,local")  # no user hooks

    def test_a_periodic_session_keeps_the_default_tools_and_skills_but_no_mcp_servers(self):
        flags = self.session("periodic:playtest")
        self.assertNotIn("--tools", flags)
        self.assertNotIn("--disable-slash-commands", flags)
        self.assertEqual(json.loads(self.flag(flags, "--mcp-config")), {"mcpServers": {}})

    def test_subagents_bring_the_agent_tool_to_editing_roles_only(self):
        self.p.ap["subagents"] = True
        self.assertEqual(self.flag(self.session("worker"), "--tools"), "Bash,Read,Edit,Write,Agent")
        self.assertEqual(self.flag(self.session("critic"), "--tools"), "Read,Glob,Grep")

    def test_a_pilot_run_starts_its_worker_and_reviewer_lean(self):
        from datetime import timedelta
        from windvane import driver
        from windvane.util import now
        from test_parallel import args
        self.s.ticket("T-001")
        self.s.publish()
        pilot = driver.Pilot(Project(self.s.root), args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        tools = {c["system"].split()[2]: self.flag(c["flags"], "--tools") for c in calls
                 if c["system"].startswith("# windvane ")}
        self.assertEqual((tools["worker"], tools["review"]), ("Bash,Read,Edit,Write", "Bash,Read"))

    def test_the_ledger_keeps_the_first_turn_context_and_stats_show_it_per_role(self):
        self.session("worker")
        self.session("worker")
        row = read_jsonl(self.p.state / "ledger.jsonl")[-1]
        self.assertEqual(row["base"], 3 + 4000 + 6000)  # fresh, cache writes and cache reads of turn 1
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_stats(argparse.Namespace(days=7, by="role", critic=False), self.p)
        head, line = (next(x for x in out.getvalue().splitlines() if x.startswith(w)) for w in ("role", "worker"))
        self.assertEqual(head.split()[-2:], ["base", "cached/turn"])
        self.assertEqual(line.split()[-2:], ["10.0k", "0.0k"])  # 2 runs of 10,003; 2 x 100 cached over 6 turns

    def test_runs_from_before_the_base_field_show_a_dash_not_zero(self):
        from windvane.util import append_jsonl, now
        append_jsonl(self.p.state / "ledger.jsonl", {"role": "critic", "turns": 4, "cache_read": 80000,
                                                     "at": f"{now():%Y-%m-%d %H:%M}"})
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_stats(argparse.Namespace(days=7, by="role", critic=False), self.p)
        line = next(x for x in out.getvalue().splitlines() if x.startswith("critic"))
        self.assertEqual(line.split()[-2:], ["-", "20.0k"])


if __name__ == "__main__":
    unittest.main()
