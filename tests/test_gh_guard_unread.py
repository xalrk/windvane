"""The gh guard fails closed (T-083): when gh's alias list hangs or fails, a call starting with a word gh doesn't know
(it could be an alias for `workflow run`) is refused within a few seconds, while gh's own commands pass; an alias
written to gh's config mid-session is refused at its first use; and `windvane stats` names each kind of refusal.
All through a fake "real" gh that reads aliases from the sandbox's gh config, never the real one."""
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, cli  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

# The "real" gh: `alias list` hangs or fails as FAKE_GH_LIST says, else prints the `aliases:` block of
# $XDG_CONFIG_HOME/gh/config.yml as the real one does; every other call is logged to FAKE_GH_LOG and echoed.
CONFIG_GH = r'''#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv[1:]
if a == ["alias", "list"]:
    mode = os.environ.get("FAKE_GH_LIST", "")
    if mode == "hang":
        time.sleep(60)
    if mode == "fail":
        sys.exit("gh: could not read config")
    f = os.path.join(os.environ["XDG_CONFIG_HOME"], "gh", "config.yml")
    block = False
    for line in open(f).read().splitlines() if os.path.exists(f) else []:
        if not line.startswith(" "):
            block = line.strip() == "aliases:"
        elif block and line.strip():
            print(line.strip())
    sys.exit(0)
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
print("real gh:", " ".join(a))
'''
TIMEOUT = int(re.search(r"^ALIAS_TIMEOUT = (\d+)", actions.GUARD_SCRIPT, re.M)[1])


class Unread(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        real = self.s.tmp / "realbin"
        real.mkdir()
        (real / "gh").write_text(CONFIG_GH)
        (real / "gh").chmod(0o755)
        os.environ["PATH"] = f"{real}{os.pathsep}{os.environ['PATH']}"
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.config = Path(os.environ["XDG_CONFIG_HOME"], "gh", "config.yml")
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text("version: 1\ngit_protocol: https\naliases:\n    co: pr checkout\n")
        os.environ["XDG_DATA_HOME"] = str(self.s.tmp / "data")  # where gh keeps extensions: the sandbox's, never yours
        os.environ.pop("GH_DATA_DIR", None)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.p = Project(self.s.root)
        self.ledger = self.p.state / "ledger.jsonl"
        self.env = {**os.environ, **actions.gh_guard(self.p, self.s.root)}  # set once, as for one session

    def sh(self, command, **env):
        """(exit code, stdout, stderr, seconds) of a Bash-tool command in the session."""
        start = time.monotonic()
        r = subprocess.run(["bash", "-c", command], cwd=self.s.root, capture_output=True, text=True,
                           env={**self.env, **env})
        return r.returncode, r.stdout, r.stderr, time.monotonic() - start

    def calls(self):
        f = Path(os.environ["FAKE_GH_LOG"])
        calls = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        f.unlink(missing_ok=True)
        return calls

    def kinds(self):
        return [(r.get("kind"), r["command"]) for r in read_jsonl(self.ledger) if r.get("event") == "gh-refused"]

    def stats(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats", *argv])
        return out.getvalue()

    def assert_unread_refusal(self, mode, limit):
        code, out, err, took = self.sh("gh wr ci.yml", FAKE_GH_LIST=mode)
        self.assertEqual((code, out), (1, ""), err)
        self.assertIn("`gh wr` isn't a gh command and gh's alias list couldn't be read", err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertLess(took, limit)
        self.assertEqual(self.calls(), [])

    def test_a_hanging_alias_list_refuses_an_unknown_word_within_the_timeout(self):
        self.assertLessEqual(TIMEOUT, 10)
        self.assert_unread_refusal("hang", TIMEOUT + 5)
        code, out, err, took = self.sh("gh pr view 1", FAKE_GH_LIST="hang")  # gh's own: no alias lookup, no wait
        self.assertEqual((code, out, err), (0, "real gh: pr view 1\n", ""))
        self.assertLess(took, TIMEOUT)
        self.assertEqual(self.calls(), [["pr", "view", "1"]])
        self.assertEqual(self.kinds(), [("unread", "wr ci.yml")])

    def test_a_failing_alias_list_refuses_an_unknown_word_the_same_way(self):
        self.assert_unread_refusal("fail", TIMEOUT)
        self.assertEqual(self.sh("gh pr view 1", FAKE_GH_LIST="fail")[:3], (0, "real gh: pr view 1\n", ""))
        self.assertEqual(self.sh("gh workflow run x.yml", FAKE_GH_LIST="fail")[0], 1)
        self.assertEqual(self.kinds(), [("unread", "wr ci.yml"), ("workflow", "workflow run")])

    def said(self):
        f = self.p.state / "autopilot.log"
        return [x for x in f.read_text().splitlines() if "gh guard:" in x] if f.exists() else []

    def test_an_installed_extension_passes_an_unread_list_and_the_log_says_once_a_run(self):
        ext = Path(os.environ["XDG_DATA_HOME"], "gh", "extensions")
        (ext / "gh-dash").mkdir(parents=True)
        (ext / "gh-dash/gh-dash").write_text("#!/bin/sh\n")
        (ext / "gh-dash/gh-dash").chmod(0o755)
        (ext / "gh-wr").mkdir()  # no gh-wr executable in it: not an installed extension
        (ext / "gh-co").write_text("")  # a file, not an extension's dir
        self.assertEqual(self.sh("gh dash", FAKE_GH_LIST="fail")[:3], (0, "real gh: dash\n", ""))
        self.assertEqual(self.calls(), [["dash"]])
        self.assertEqual((self.said(), self.kinds()), ([], []))  # passing isn't a refusal: nothing to say
        self.assert_unread_refusal("fail", TIMEOUT)
        self.assert_unread_refusal("fail", TIMEOUT)
        said = self.said()
        self.assertEqual(len(said), 1, said)
        self.assertRegex(said[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d gh guard: gh's alias list couldn't be read, .* run "
                                  r"`gh alias list` to check it$")
        self.assertEqual(self.kinds(), [("unread", "wr ci.yml")] * 2)  # each refusal still counted
        self.assertEqual(self.sh("gh dash")[:2], (0, "real gh: dash\n"))  # a readable list: as before
        self.assertEqual(self.calls(), [["dash"]])
        with mock.patch.object(actions, "RUN", "next-run"):  # the next driver run says it again
            self.env = {**os.environ, **actions.gh_guard(self.p, self.s.root)}
        self.assert_unread_refusal("fail", TIMEOUT)
        self.assertEqual(len(self.said()), 2, self.said())
        self.assertEqual(sorted(x.name for x in (self.p.state / "gh-guard").glob("unread-said.*")),
                         ["unread-said.next-run"])  # the old run's marker is gone

    def test_an_alias_written_to_the_gh_config_mid_session_is_refused_at_its_first_use(self):
        self.assertEqual(self.sh("gh wr ci.yml")[:3], (0, "real gh: wr ci.yml\n", ""))  # no such alias yet
        self.assertEqual(self.calls(), [["wr", "ci.yml"]])
        # what an Edit of ~/.config/gh/config.yml (or `gh config set`) leaves behind
        self.config.write_text(self.config.read_text() + "    wr: workflow run\n")
        code, out, err, _ = self.sh("gh wr ci.yml")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("`gh wr` is an alias for one", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.sh("gh co 5")[:2], (0, "real gh: co 5\n"))  # the other aliases still pass
        self.assertEqual(self.kinds(), [("workflow", "wr ci.yml")])

    def test_stats_names_each_kind_and_counts_an_old_row_as_a_workflow_start(self):
        self.sh("gh workflow run x.yml", WINDVANE_TICKET="T-007")
        self.sh("gh alias set wr 'workflow run'", WINDVANE_TICKET="T-007")
        self.sh("gh alias import aliases.yml", WINDVANE_TICKET="T-008")
        self.sh("gh wr ci.yml", FAKE_GH_LIST="fail", WINDVANE_TICKET="T-007")
        self.assertEqual([k for k, _ in self.kinds()], ["workflow", "alias", "alias", "unread"])
        self.assertIn("gh guard refused: 1 workflow start, 2 alias changes, 1 unknown command (alias list unread)\n",
                      self.stats())
        # a row written before kinds were logged (T-077) was a workflow start
        append_jsonl(self.ledger, {"at": time.strftime("%Y-%m-%d %H:%M"), "event": "gh-refused", "ticket": "T-008",
                                   "command": "wr ci.yml"})
        self.assertIn("gh guard refused: 2 workflow starts, 2 alias changes, 1 unknown command "
                      "(alias list unread; most: T-007, 3)\n", self.stats("--by", "ticket"))
        append_jsonl(self.ledger, {"role": "worker", "ticket": "T-007", "input": 1000, "usd": 0.1})
        self.assertIn("gh guard refused: 2 workflow starts, 2 alias changes, 1 unknown command", self.stats())


if __name__ == "__main__":
    unittest.main()
