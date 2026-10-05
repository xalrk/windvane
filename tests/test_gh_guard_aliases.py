"""The gh guard and the user's existing aliases (T-077): an alias already in gh's config that expands to a workflow
start, or a shell alias naming one, is refused like the command typed out; every refusal leaves a `gh-refused`
ledger row, which `windvane stats` counts. All through a fake "real" gh, never the real one."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, cli  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

# The "real" gh: `alias list` prints FAKE_GH_ALIASES (or fails when it's "fail"); every other call is logged to
# FAKE_GH_LOG and echoed.
ALIAS_GH = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
if a == ["alias", "list"]:
    if os.environ.get("FAKE_GH_ALIASES") == "fail":
        sys.exit("gh: no config")
    print(os.environ.get("FAKE_GH_ALIASES", ""), end="")
    sys.exit(0)
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
print("real gh:", " ".join(a))
'''

ALIASES = "wr: workflow run\nrr: run rerun\nco: pr checkout\nsh: '!gh workflow run x.yml'\n" \
          "dp: api repos/$1/dispatches -f event_type=go\nls: run list\n" \
          "sa: '!gh api repos/me/proj/actions/runs | head'\n"


class Aliases(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        real = self.s.tmp / "realbin"
        real.mkdir()
        (real / "gh").write_text(ALIAS_GH)
        (real / "gh").chmod(0o755)
        os.environ["PATH"] = f"{real}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        os.environ["FAKE_GH_ALIASES"] = ALIASES
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.p = Project(self.s.root)
        self.ledger = self.p.state / "ledger.jsonl"

    def sh(self, command, **env):
        r = subprocess.run(["bash", "-c", command], cwd=self.s.root, capture_output=True, text=True,
                           env={**os.environ, **actions.gh_guard(self.p, self.s.root), **env})
        return r.returncode, r.stdout, r.stderr

    def calls(self):
        f = Path(os.environ["FAKE_GH_LOG"])
        calls = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        f.unlink(missing_ok=True)
        return calls

    def refusals(self):
        return [r for r in read_jsonl(self.ledger) if r.get("event") == "gh-refused"]

    def stats(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats", *argv])
        return out.getvalue()

    def test_aliases_that_start_a_workflow_are_refused_before_the_real_gh(self):
        for command, alias in [("gh wr ci.yml", "wr"), ("gh rr 1", "rr"), ("gh sh", "sh"), ("gh sh extra", "sh"),
                               ("gh dp me/proj", "dp"), ("gh sa", "sa"), ("env gh wr ci.yml --ref main", "wr")]:
            with self.subTest(command):
                code, out, err = self.sh(command)
                self.assertEqual(code, 1, out + err)
                self.assertIn(f"`gh {alias}` is an alias", err)
                self.assertIn("`windvane ci [workflow]`", err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertEqual(self.calls(), [])

    def test_other_aliases_and_commands_pass_through_unchanged(self):
        for command, args in [("gh co 5", ["co", "5"]), ("gh ls --limit 3", ["ls", "--limit", "3"]),
                              ("gh nope x", ["nope", "x"]), ("gh pr view 1", ["pr", "view", "1"]),
                              ("gh -R me/proj wr", ["-R", "me/proj", "wr"])]:  # gh expands only the first word
            with self.subTest(command):
                code, out, err = self.sh(command)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out, f"real gh: {' '.join(args)}\n")
                self.assertEqual(self.calls(), [args])

    def test_a_gh_whose_alias_list_fails_still_gets_a_working_guard(self):
        for aliases in ("fail", ""):
            with self.subTest(aliases=aliases):
                self.assertEqual(self.sh("gh wr ci.yml", FAKE_GH_ALIASES=aliases)[:2], (0, "real gh: wr ci.yml\n"))
                self.assertEqual(self.sh("gh workflow run ci.yml", FAKE_GH_ALIASES=aliases)[0], 1)
                self.assertEqual(self.calls(), [["wr", "ci.yml"]])

    def test_each_refusal_is_one_ledger_row_with_the_ticket(self):
        self.sh("gh wr ci.yml", WINDVANE_TICKET="T-007")
        self.sh("gh workflow run x.yml", WINDVANE_TICKET="T-007")
        self.sh("gh rr 1")
        self.sh("gh co 5", WINDVANE_TICKET="T-007")  # passes: no row
        rows = self.refusals()
        self.assertEqual([(r["ticket"], r["command"]) for r in rows],
                         [("T-007", "wr ci.yml"), ("T-007", "workflow run"), ("", "rr 1")])
        self.assertRegex(rows[0]["at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_a_failed_ledger_write_keeps_the_refusal(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.mkdir()  # can't be appended to
        code, out, err = self.sh("gh wr ci.yml")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertEqual(self.calls(), [])

    def test_stats_counts_refusals_and_says_nothing_at_zero(self):
        self.assertNotIn("gh guard", self.stats())
        self.assertNotIn("gh guard", self.stats("--by", "ticket"))
        for t in ("T-007", "T-007", "T-008"):
            self.sh("gh wr ci.yml", WINDVANE_TICKET=t)
        self.assertIn("gh guard refused: 3 workflow starts\n", self.stats())
        self.assertIn("gh guard refused: 3 workflow starts (most: T-007, 2)\n", self.stats("--by", "ticket"))
        # with session rows too, the line still shows
        append_jsonl(self.ledger, {"role": "worker", "ticket": "T-007", "input": 1000, "usd": 0.1})
        self.assertIn("gh guard refused: 3 workflow starts\n", self.stats())


if __name__ == "__main__":
    unittest.main()
