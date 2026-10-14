"""`.windvane/` is pushed, so secrets are kept out of what windvane puts there: a typed answer holding one is refused,
a land or a ticket's merge whose new `.windvane/` lines hold one isn't pushed (and the alert names only its kind and
place), and init and doctor warn when the repo is public.

The leaks are built from pieces at run time, so this file itself stays clean for the working-tree scan."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, doctor, driver, held, preflight, privacy  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import git, now  # noqa: E402
from windvane.worktree import clean_detach, ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_plan_retry import Base  # noqa: E402
from test_preflight import QUESTION  # noqa: E402

TOKEN = "ghp_" + "Zq7" * 12
TOPIC = "wv" + "bell" + "4417"
GH = '#!/bin/sh\ncase "$*" in\n  "repo view"*) [ -n "$GH_VIS" ] || exit 1; echo "$GH_VIS";;\nesac\nexit 0\n'


class Scanner(unittest.TestCase):
    def test_kinds_without_the_text(self):
        self.assertEqual(privacy.scan_text(f"use {TOKEN} here\nmail me at someone@" + "fastmail.com"),
                         ["email", "token"])
        self.assertEqual(privacy.scan_text("git@github.com:a/b, bot@users.noreply.github.com, a@example.com, "
                                           "pkg@v2026.10.13"), [])
        self.assertNotIn(TOKEN, privacy.said([("token", "x:1 in abc")]))


class Answers(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        os.environ["FAKE_PREFLIGHT"] = QUESTION
        self.addCleanup(setattr, sys, "stdin", sys.stdin)

    def cli(self, *argv):
        sys.stdin, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def main_head(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("rev-parse", "origin/main")

    def test_an_answer_holding_a_token_is_refused_and_nothing_written(self):
        self.cli("preflight", "--json")
        before = self.main_head()
        with self.assertRaises(SystemExit) as e:
            self.cli("answer", "T-001", f"blue, and the key is {TOKEN}")
        self.assertIn("not recorded: it holds a token", str(e.exception.code))
        self.assertNotIn(TOKEN, str(e.exception.code))
        self.assertEqual(self.main_head(), before)
        self.assertFalse((self.p.state / "answers.json").exists())
        self.assertEqual(len(preflight.pending(self.p)), 1)  # still waits for a clean answer
        self.assertIn("1 answer recorded on main", self.cli("answer", "T-001", "blue"))

    def test_a_terminal_answer_holding_a_token_is_skipped(self):
        said = []
        self.cli("preflight", "--json")
        qs = preflight.pending(self.p)
        before = self.main_head()
        self.assertEqual(preflight.ask(self.p, qs, inp=lambda _: f"blue {TOKEN}", say=said.append), [])
        self.assertIn("not taken: it holds a token", said[-1])
        self.assertNotIn(TOKEN, "\n".join(said))
        self.assertEqual(self.main_head(), before)


class Land(Base):
    def land(self, text, replace=False):
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        clean_detach(self.p, cd, self.p.upstream)
        f = cd / ".windvane/IDEAS.md"
        f.write_text(("" if replace or not f.exists() else f.read_text()) + text + "\n")
        git(cd, "add", "-A", ".windvane")
        return held.land(self.p, cd, "ideas-x", "Ideas", "body")

    def on_main(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("show", "origin/main:.windvane/IDEAS.md")

    def assert_held(self, ok, kind):
        self.assertFalse(ok)
        self.assertEqual(self.git_pushes, 0)
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {"ideas-x": "land"})
        why = json.loads((self.p.state / "held_why.json").read_text())["ideas-x"]["why"]
        self.assertTrue(why.startswith(f"its .windvane/ lines hold {kind} at .windvane/IDEAS.md:1 in "), why)
        [(title, msg)] = self.pushes
        self.assertEqual(title, "Not pushed: Ideas")
        self.assertTrue(msg.startswith(f"Its .windvane/ lines hold {kind} at .windvane/IDEAS.md:1 in "), msg)
        return why, msg

    def test_a_token_is_held_with_its_reason_and_the_alert_hides_it(self):
        ok, where = self.land(f"try {TOKEN}")
        why, msg = self.assert_held(ok, "a token")
        self.assertNotIn(TOKEN, why + msg + where + self.log())
        self.assertIn("(`git switch ideas-x`, edit, `git commit -a --amend`), then the next run pushes it.", msg)
        self.assertEqual(held.push_held(self.p), 0)  # the next run scans it again: still held, no second alert
        self.assertEqual((self.git_pushes, len(self.pushes)), (0, 1))
        self.assertIn("held back for under an hour, not pushed: ideas-x (Ideas): its .windvane/ lines hold a token",
                      held.held_lines(self.p)[0])
        self.s.git("switch", "-q", "ideas-x")  # the fix the push gives, as the user would do it
        (self.s.root / ".windvane/IDEAS.md").write_text("try the key from the vault\n")
        self.s.git("commit", "-qa", "--amend", "--no-edit")
        self.s.git("switch", "-q", "main")
        self.assertEqual(held.push_held(self.p), 1)  # the next run's start: clean now, so it lands
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {})
        self.assertEqual(self.on_main(), "try the key from the vault")
        self.assertNotIn(TOKEN, self.s.git("log", "-p", "origin/main"))

    def test_an_ntfy_topic_is_held(self):
        conf = Path(os.environ["XDG_CONFIG_HOME"]) / "ntfy/config"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(f"NTFY_TOPIC={TOPIC}\n")
        ok, _ = self.land(f"subscribe to {TOPIC} on the phone")
        why, msg = self.assert_held(ok, "this machine's ntfy topic")
        self.assertNotIn(TOPIC, why + msg + self.log())

    def test_a_private_word_is_held_unless_the_project_allows_it(self):
        terms = Path(os.environ["XDG_CONFIG_HOME"]) / "windvane/private-terms.txt"
        terms.parent.mkdir(parents=True, exist_ok=True)
        terms.write_text("# mine\n" + "Bram" + "blewood\n")
        ok, _ = self.land("ask " + "bram" + "blewood about it")
        self.assert_held(ok, "a word from private-terms.txt")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text('scan_allow = ["private-term"]\n' + cfg.read_text())
        self.s.publish("allow")
        self.p = Project(self.s.root)
        self.assertEqual(privacy.guarded(self.p), ("token", "ntfy-url", "ntfy-topic", "email"))
        self.assertEqual(held.land(self.p, self.p.plan_dir, "ideas-x", "Ideas", "body", commit=False)[0], True)

    def test_a_home_path_is_pushed(self):
        ok, where = self.land(f"the build writes to {Path.home()}/out")
        self.assertEqual((ok, where), (True, "main"))
        self.assertIn("/out", self.on_main())
        self.assertEqual(self.pushes, [])

    def test_an_old_leak_already_on_main_doesnt_block_the_next_land(self):
        self.s.write(".windvane/IDEAS.md", f"old {TOKEN}\n")
        self.s.publish("old")
        ok, _ = self.land(f"a new idea\nold {TOKEN}", replace=True)  # the old line moves below a new one
        self.assertTrue(ok)
        self.assertIn("a new idea", self.on_main())
        self.assertEqual(self.pushes, [])


class Merge(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.pushes = []
        self.addCleanup(setattr, driver, "push", driver.push)
        driver.push = lambda title, msg="", *a, **k: self.pushes.append((title, msg))

    def run_pilot(self):
        pilot = driver.Pilot(Project(self.s.root), Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0,
                                                             answer_wait=10, replan=0, goal="", parallel=1))
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def sessions(self):
        f = self.s.tmp / "calls.jsonl"
        return sum(json.loads(l)["system"].startswith("# windvane") for l in f.read_text().splitlines())

    def main_log(self, *args):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("log", *args, "origin/main")

    def test_a_ticket_whose_commits_add_a_token_under_windvane_is_set_aside_until_rewritten(self):
        os.environ["FAKE_IDEA"] = f"idea: {TOKEN}"
        pilot = self.run_pilot()
        self.assertEqual((pilot.merged, pilot.failed, list(pilot.aside)), ([], [], ["T-001"]))
        self.assertEqual(pilot.fail_streak, 0)  # not a failure: two of those in a row would stop the run
        self.assertNotIn("Merge T-001", self.main_log("--format=%s"))
        self.assertIn("refs/heads/t-001-work", self.s.git("ls-remote", "--heads", "origin"))  # the branch is kept
        [msg] = [m for t, m in self.pushes if t.startswith("Set aside:") and "T-001" in t]
        self.assertTrue(msg.startswith("Not merged: its commits add a token at .windvane/IDEAS.md:1 in "), msg)
        self.assertIn("which would stay in main's history.\nRewrite the branch without it: `git fetch origin && git "
                      "switch t-001-work && git rebase -i origin/main`, then `git push --force-with-lease origin "
                      "t-001-work`.", msg)
        self.assertIn("scan_allow", msg)
        self.assertNotIn(TOKEN, msg + (Project(self.s.root).state / "autopilot.log").read_text())

        paid = self.sessions()  # the branch unchanged: the next run pays nothing and says nothing to find it again
        pilot = self.run_pilot()
        self.assertEqual((self.sessions(), pilot.merged, list(pilot.aside)), (paid, [], ["T-001"]))
        self.assertEqual(sum(t.startswith("Set aside:") for t, _ in self.pushes), 1)

        del os.environ["FAKE_IDEA"]  # the push's fix, as the user would do it (one commit: an amend is the rebase)
        self.s.git("fetch", "-q", "origin")
        self.s.git("switch", "-q", "t-001-work")
        (self.s.root / ".windvane/IDEAS.md").write_text("idea: read the key from the vault\n")
        self.s.git("commit", "-qa", "--amend", "--no-edit")
        self.s.git("push", "-q", "--force-with-lease", "origin", "t-001-work")
        self.s.git("switch", "-q", "main")
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertNotIn(TOKEN, self.main_log("-p"))
        self.assertIn("read the key from the vault", self.main_log("-p"))


class Public(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        (self.s.bin / "gh").write_text(GH)
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.p = Project(self.s.root)

    def doctor(self, vis):
        os.environ["GH_VIS"] = vis
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor.main(self.p)
        return code, out.getvalue()

    def init(self, vis):
        os.environ["GH_VIS"] = vis
        root = self.s.tmp / f"new-{vis or 'none'}"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["init", "--forge", "github", str(root)])
        return out.getvalue()

    def test_a_public_repo_warns_a_private_one_doesnt_and_a_failing_gh_is_quiet(self):
        code, public = self.doctor("PUBLIC")
        self.assertIn("WARNING   this repo is public on GitHub, and so is .windvane/ (tickets, your answers, the "
                      "critic's notes)", public)
        self.assertIn("windvane doctor --leaks", public)
        for vis in ("PRIVATE", ""):  # "": gh fails, as offline
            self.assertEqual(self.doctor(vis), (code, public.replace(public[public.index("WARNING   this repo"):]
                                                                      .split("\n", 1)[0] + "\n", "")))
        self.assertIn("WARNING: this repo is public on GitHub", self.init("PUBLIC"))
        for vis in ("PRIVATE", ""):
            self.assertNotIn("public", self.init(vis))

    def test_leaks_names_kinds_and_places_in_history_never_the_text(self):
        self.s.write(".windvane/IDEAS.md", f"key {TOKEN}\n")
        self.s.publish("oops")
        self.s.write(".windvane/IDEAS.md", "nothing here\n")
        self.s.publish("removed")  # deleted from the tree, still in history
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["-C", str(self.s.root), "doctor", "--leaks"])
        self.assertEqual(code, 1)
        self.assertIn("a token: 1 place(s): .windvane/IDEAS.md:1 in ", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())


if __name__ == "__main__":
    unittest.main()
