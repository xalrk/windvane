"""Every push from windvane's own worktrees is scanned by a pre-push hook it owns (and the repo's own pre-push still
runs); every branch is scanned again before the driver or a desk PR merges it; the text windvane posts to GitHub or
writes into a commit message is censored first; `windvane init` adds the secret-file patterns to .gitignore.

The leaks are built from pieces at run time, so this file itself stays clean for the working-tree scan."""
import json
import os
import socket
import subprocess
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, critic, driver, held, planner, privacy  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import clean_detach, ensure_worktree, guard_pushes  # noqa: E402
from helpers import Sandbox  # noqa: E402
import test_desk  # noqa: E402
import test_gh_guard  # noqa: E402
import test_renumber  # noqa: E402
from test_plan_retry import Base  # noqa: E402

TOKEN = "ghp_" + "Wq4" * 12
TOPIC = "wv" + "chime" + "8812"
HOST = "quiet" + "box"
KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3Blbn\n-----END " + "OPENSSH PRIVATE KEY-----"


class Hook(unittest.TestCase):
    """The pre-push hook in a worktree of windvane's own, as a session's `git push` meets it."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        theirs = self.s.root / ".git/hooks/pre-push"  # the repo's own: it records that it ran, and what it was given
        theirs.write_text(f'#!/bin/sh\necho "$2 $(cat)" >> {self.s.tmp}/theirs.log\n')
        theirs.chmod(0o755)
        self.wt = self.s.tmp / "wt"
        ensure_worktree(self.p, self.wt, setup=False)
        self.env = {**os.environ, **guard_pushes(self.p, self.wt)}  # what the driver gives a worker session
        self.git("switch", "-q", "-c", "t-001-work")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.wt, capture_output=True, text=True, env=self.env)

    def push(self, files):
        for f, text in files.items():
            (self.wt / f).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / f).write_text(text + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "work")
        return self.git("push", "-q", "-u", "origin", "HEAD")

    def theirs(self):
        f = self.s.tmp / "theirs.log"
        return f.read_text() if f.exists() else ""

    def assert_refused(self, r, kind, place):
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"windvane: push refused: it would make public {kind} at {place} in ", r.stderr)
        self.assertIn("`git commit --amend`", r.stderr)
        self.assertNotIn("t-001-work", self.s.git("ls-remote", "--heads", "origin"))
        self.assertEqual(self.theirs(), "")  # the scan comes first

    def test_a_token_is_refused_by_kind_and_place_and_goes_through_once_amended(self):
        r = self.push({"src/a.py": "x = 1", "src/b.py": f"KEY = '{TOKEN}'"})
        self.assert_refused(r, "a token", "src/b.py:1")
        self.assertNotIn(TOKEN, r.stderr + r.stdout)
        (self.wt / "src/b.py").write_text("KEY = os.environ['KEY']\n")  # the fix the refusal gives
        self.git("commit", "-qa", "--amend", "--no-edit")
        self.assertEqual(self.git("push", "-q", "-u", "origin", "HEAD").returncode, 0)
        self.assertIn("t-001-work", self.s.git("ls-remote", "--heads", "origin"))

    def test_an_env_file_a_key_and_an_ntfy_topic_are_refused(self):
        self.assert_refused(self.push({".env.local": "DEBUG=1"}), "a file named like a secret", ".env.local")
        self.git("reset", "-q", "--hard", "origin/main")
        self.assert_refused(self.push({"deploy/key.txt": KEY}), "a private key", "deploy/key.txt:1")
        self.git("reset", "-q", "--hard", "origin/main")
        conf = Path(os.environ["XDG_CONFIG_HOME"]) / "ntfy/config"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(f"topic = {TOPIC}\n")
        r = self.push({"notes.md": f"pushes go to {TOPIC}"})
        self.assert_refused(r, "this machine's ntfy topic", "notes.md:1")
        self.assertNotIn(TOPIC, r.stderr)

    def test_a_home_path_and_an_env_example_go_through_and_the_repos_own_hook_runs(self):
        r = self.push({".env.example": "TOKEN=", "docs/where.md": f"built in {Path.home()}/proj"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("t-001-work", self.s.git("ls-remote", "--heads", "origin"))
        self.assertRegex(self.theirs(), rf"^{self.s.remote} HEAD \w+ refs/heads/t-001-work 0+$")  # its args and refs

    def test_a_line_already_on_main_doesnt_count(self):
        self.s.write("old.txt", f"{TOKEN}\n")  # an old leak on main: moving it mustn't stop every push
        self.s.publish("old")
        self.git("fetch", "-q", "origin")
        self.git("reset", "-q", "--hard", "origin/main")
        self.git("mv", "old.txt", "moved.txt")
        self.assertEqual(self.push({}).returncode, 0)

    def test_the_users_checkout_config_is_untouched_and_its_own_hooks_path_is_followed(self):
        own = self.s.root / ".githooks"  # the user's own hooks folder, in their repo's config
        own.mkdir()
        for hook, said in (("pre-push", "githooks"), ("pre-commit", "committed")):
            (own / hook).write_text(f"#!/bin/sh\necho {said} >> {self.s.tmp}/theirs.log\n")
            (own / hook).chmod(0o755)
        self.s.git("config", "core.hooksPath", ".githooks")  # relative: git reads it from the worktree's top
        config = (self.s.root / ".git/config").read_text()
        (self.wt / ".githooks").symlink_to(own)
        self.env = {**os.environ, **guard_pushes(self.p, self.wt)}  # the next session's
        self.assertEqual(self.push({"a.txt": "a"}).returncode, 0)
        self.assertEqual(self.theirs(), "committed\ngithooks\n")
        r = self.push({"b.txt": TOKEN})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)
        after = (self.s.root / ".git/config").read_text().split('[branch "t-001-work"]')[0]  # (`push -u`'s own)
        self.assertEqual(after, config)  # the user's checkout: untouched
        self.assertEqual(self.s.git("config", "core.hooksPath"), ".githooks")
        self.assertFalse(list(self.s.root.glob(".git/**/config.worktree")))


class Merge(unittest.TestCase):
    """The driver's scan before it merges: for a branch whose push got past the hook."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        os.environ.update(FAKE_NO_VERIFY="1", FAKE_LEAK=f"settings.py=KEY = '{TOKEN}'")
        self.addCleanup(setattr, driver, "push", driver.push)
        driver.push = lambda *a, **k: None

    def pilot(self):
        return driver.Pilot(Project(self.s.root), Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0,
                                                      answer_wait=10, replan=0, goal="", parallel=1))

    def log(self):
        return (Project(self.s.root).state / "autopilot.log").read_text()

    def on_remote(self):
        return subprocess.run(["git", "--git-dir", str(self.s.remote), "log", "-p", "--all"], capture_output=True,
                              text=True).stdout

    def test_a_workers_own_push_of_a_token_is_refused(self):
        del os.environ["FAKE_NO_VERIFY"]
        self.pilot().run(now() + timedelta(hours=1))
        self.assertNotIn(TOKEN, self.on_remote())
        refused = Path(os.environ["FAKE_LOG"] + ".refused").read_text()  # what the session saw
        self.assertIn("windvane: push refused: it would make public a token at settings.py:1 in ", refused)
        self.assertNotIn(TOKEN, refused)

    def test_a_squashed_branch_sent_back_red_is_resumed_from_its_squash_and_pushes(self):
        os.environ["FAKE_NO_VERIFY"] = "first"  # only the first push gets past the hook
        orig, red = driver.check, self.s.tmp / "red.log"

        def check(p, cd, **k):  # red once, on the squash's merge (as when main moved meanwhile)
            if "Its commits as one" in driver.gout(cd, "log", "-1", "--format=%B", "HEAD^2") and not red.exists():
                red.write_text("FAILED test_x\n")
                return False, "1 failed", red
            return orig(p, cd, **k)
        driver.check = check
        self.addCleanup(setattr, driver, "check", orig)
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        log = self.log()
        self.assertTrue(red.exists())
        self.assertIn("pushed as one commit", log)
        self.assertIn("T-001: the merged tree fails the check; back to the worker", log)
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertFalse(Path(os.environ["FAKE_LOG"] + ".refused").exists())  # its third run's push went through
        self.assertNotIn(TOKEN, self.on_remote())
        self.assertEqual(self.s.git("show", "origin/main:T-001.txt"), "built\nbuilt\nbuilt")

    def test_a_squash_push_that_loses_its_lease_is_tried_again(self):
        orig, lost = driver.net.git, []

        def git(cwd, *args):
            if args[0] == "push" and args[2].startswith("--force-with-lease=refs/heads/t-001-work") and not lost:
                lost.append(args)
                return subprocess.CompletedProcess(["git", *args], 1, "", "! [rejected] (stale info)")
            return orig(cwd, *args)
        driver.net.git = git
        self.addCleanup(setattr, driver.net, "git", orig)
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(len(lost), 1)
        self.assertIn("T-001: couldn't push its branch as one commit to t-001-work; trying again", self.log())
        self.assertEqual(pilot.merged, ["T-001"])
        self.s.git("fetch", "-q", "origin")
        self.assertNotIn(TOKEN, self.s.git("log", "-p", "origin/main"))

    def test_a_token_goes_back_to_the_worker_and_its_commit_stays_off_main(self):
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        log = self.log()
        self.assertIn("T-001: not merged: its commits add a token at settings.py:1 in ", log)
        self.assertIn("; back to the worker", log)
        self.assertIn("pushed as one commit", log)  # its second run took it out with a commit on top
        self.assertEqual(pilot.merged, ["T-001"])
        self.s.git("fetch", "-q", "origin")
        main = self.s.git("log", "-p", "origin/main")
        self.assertNotIn(TOKEN, main + log)
        self.assertIn("Its commits as one: one of them added a token, which a later one took out.", main)
        self.assertIn("built", self.s.git("show", "origin/main:T-001.txt"))


class ResolutionHook(unittest.TestCase):
    """The driver's commit of a resolved conflict runs the repo's commit hooks now (no --no-verify)."""
    setUp, tearDown = test_renumber.TwoTickets.setUp, test_renumber.TwoTickets.tearDown

    def test_a_commit_hook_refusing_the_resolution_is_named_as_such(self):
        hook = self.s.root / ".git/hooks/pre-commit"  # refuses merge commits only: the workers' own commits pass
        hook.write_text("#!/bin/sh\ngit rev-parse -q --verify MERGE_HEAD >/dev/null && "
                        "{ echo 'lint: merge commits need a ticket id' >&2; exit 1; }\nexit 0\n")
        hook.chmod(0o755)
        os.environ["FAKE_WORKER"] = "ready"
        p = Project(self.s.root)
        pilot = driver.Pilot(p, test_renumber.args())
        pilot.run(now() + timedelta(minutes=15))
        self.assertRegex((p.state / "autopilot.log").read_text(),
                         r"T-00\d: conflicts with main only in appended entries, but a commit hook refused the "
                         r"resolution \(lint: merge commits need a ticket id\); back to the worker")


class Desk(unittest.TestCase):
    """In test_desk's setting (forge github, a fake gh that keeps the comments), without running its tests again."""
    setUp, new_pilot, pr, commit, step = (getattr(test_desk.DeskPRs, n) for n in ("setUp", "new_pilot", "pr", "commit",
                                                                              "step"))

    def test_a_check_logs_home_path_and_hostname_are_masked_in_the_comment(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('check = "test -f README && test ! -f BROKEN"',
                                               f'check = "test ! -f BROKEN || {{ echo built in $HOME/w on {HOST}; '
                                               'exit 1; }"'))
        self.s.publish("check")
        self.p = Project(self.s.root)
        self.pilot = self.new_pilot()
        self.pr(5, {"BROKEN": "x"})
        with mock.patch.object(socket, "gethostname", return_value=HOST):
            self.step()
        [body] = [b for n, b in json.loads(self.gh.read_text())["comments"] if "check fails" in b]
        self.assertIn("built in ~/w on <host>", body)
        self.assertNotIn(HOST, body)
        self.assertNotIn(os.environ["HOME"], body)

    def test_a_desk_pr_adding_a_token_isnt_merged(self):
        self.pr(6, {"conf.py": f"KEY = '{TOKEN}'"})
        self.step()
        st = json.loads(self.gh.read_text())
        self.assertEqual(st["prs"][0]["state"], "OPEN")
        [body] = [b for n, b in st["comments"]]
        self.assertIn("**windvane: not merged**", body)
        self.assertIn("its commits add a token, which would stay in main's history", body)
        self.assertIn("Desk PR #6: holds a secret, left for the desk", self.pushes)
        self.assertNotIn(TOKEN, json.dumps(st) + (self.p.state / "autopilot.log").read_text())

    def test_reviewer_and_critic_text_is_masked_before_gh(self):
        critic.comment(self.p, {"step": "revise", "score": 6, "sha": "abc1234", "critique": f"it logs {TOKEN}"}, "9")
        [(n, body)] = json.loads(self.gh.read_text())["comments"]
        self.assertEqual(n, "9")
        self.assertIn("it logs [token]", body)
        self.s.ticket("T-001")
        self.s.publish()
        st = json.loads(self.gh.read_text())  # the worker's PR, as `gh pr create` would make it
        st["prs"].append({"number": 3, "title": "T-001", "author": {"login": "bot"}, "state": "OPEN",
                          "headRefName": "t-001-work", "isCrossRepository": False, "isDraft": False, "labels": []})
        self.gh.write_text(json.dumps(st))
        os.environ["FAKE_SAY"] = f"seen {TOKEN} at {Path.home()}/x"
        pilot = self.new_pilot()
        pilot.run(now() + timedelta(hours=1))
        [body] = [b for n, b in json.loads(self.gh.read_text())["comments"] if b.startswith("**windvane review")]
        self.assertIn("seen [token] at ~/x", body)


class WorkerPRText(unittest.TestCase):
    """The worker's `gh` guard, in test_gh_guard's setting (a fake "real" gh that logs its arguments)."""
    setUp, tearDown, fake, worker_env, sh, calls = (getattr(test_gh_guard.Guard, n) for n in (
        "setUp", "tearDown", "fake", "worker_env", "sh", "calls"))

    def test_a_pr_title_body_and_body_file_are_censored_before_the_real_gh(self):
        body = self.s.tmp / "body.md"
        body.write_text(f"logs at {Path.home()}/x hold {TOKEN}\n")
        code, out, err = self.sh(f"gh pr create --title 'fix {TOKEN}' --body-file {body} && "
                                 f"gh pr comment 4 --body='see {TOKEN}' && gh pr view 4 --json body")
        self.assertEqual(code, 0, err)
        [create, comment, view] = self.calls()
        self.assertEqual(create[:4], ["pr", "create", "--title", "fix [token]"])
        self.assertNotEqual(create[5], str(body))  # a censored copy, the worker's file left as it was
        self.assertEqual(Path(create[5]).read_text(), "logs at ~/x hold [token]\n")
        self.assertIn(TOKEN, body.read_text())
        self.assertEqual(comment, ["pr", "comment", "4", "--body=see [token]"])
        self.assertEqual(view, ["pr", "view", "4", "--json", "body"])


class Relayed(Base):
    def test_planner_text_is_masked_in_its_commit(self):
        os.environ["FAKE_SAY"] = f"read {TOKEN} in {Path.home()}/notes"
        self.assertEqual(planner.plan(self.p, "make it great"), 1)
        self.s.git("fetch", "-q", "origin")
        branch = self.s.git("branch", "-r", "--list", "origin/plan-*").split()[0]
        body = self.s.git("log", "-1", "--format=%B", branch)
        self.assertIn("read [token] in ~/notes", body)

    def test_a_periodic_reply_is_masked_in_what_it_lands(self):
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        clean_detach(self.p, cd, self.p.upstream)
        (cd / ".windvane/IDEAS.md").write_text("an idea\n")
        subprocess.run(["git", "add", "-A"], cwd=cd)
        ok, _ = held.land(self.p, cd, "periodic-x", "Periodic: notes", f"## Its final reply\nfound {TOKEN}")
        self.assertTrue(ok)
        self.s.git("fetch", "-q", "origin")
        self.assertIn("found [token]", self.s.git("log", "-1", "--format=%B", "origin/main"))


class Censor(unittest.TestCase):
    def test_what_it_masks_and_what_it_leaves(self):
        with mock.patch.object(socket, "gethostname", return_value=HOST), \
                mock.patch.dict(os.environ, {"USER": "zelda" + "fake"}):
            text = privacy.censor(f"{Path.home()}/x on {HOST} by {'zelda' + 'fake'}: {TOKEN}\n{KEY}\n"
                                  f"mail jane.roe@{'gmail.com'}; bot 1+x@users.noreply.github.com; sha {'ab12' * 10}")
        self.assertEqual(text, f"~/x on <host> by <user>: [token]\n[private key]\nmail [email]; bot "
                               f"1+x@users.noreply.github.com; sha {'ab12' * 10}")


class Init(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def test_it_adds_only_the_missing_patterns_or_makes_the_file(self):
        (self.s.root / ".gitignore").write_text("node_modules/\n.env\n*.pem")
        self.assertEqual(cli.gitignore(self.s.root), [".env.*", "!.env.example", "*.key", "id_rsa*",
                                                      "credentials*.json", "*.p12"])
        self.assertEqual((self.s.root / ".gitignore").read_text(),
                         "node_modules/\n.env\n*.pem\n\n# files named like secrets\n.env.*\n!.env.example\n*.key\n"
                         "id_rsa*\ncredentials*.json\n*.p12\n")
        self.assertEqual(cli.gitignore(self.s.root), [])
        (self.s.root / ".gitignore").unlink()
        self.assertEqual(len(cli.gitignore(self.s.root)), len(privacy.GITIGNORE))
        self.assertTrue((self.s.root / ".gitignore").read_text().startswith("# files named like secrets\n.env\n"))


if __name__ == "__main__":
    unittest.main()
