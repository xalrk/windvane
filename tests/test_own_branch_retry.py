"""A branch the driver just named (a plan's, a periodic chain's, the desk answers') that another machine pushed between
the name's lookup and the push goes to the next free suffix instead of being held back; a second refusal is held with
a reason that says the name was taken. A pushed branch with no PR names the commands that land it, and the name's
lookup waits out a network outage."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, held, net, planner, preflight  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.worktree import clean_detach, ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_land_retry import GH  # noqa: E402

NAME = "plan-20261020-120000"
PROVISIONAL = ("# Q\n\n## Open\n\n## Provisional\n### T-001: Big or small?\n- **A**: big\n- **B**: small\n"
               "Recommendation: A, because.\nBuilt: A; to change it: SIZE in x.py\n\n## Answered\n")
REFUSE_MAIN = "#!/bin/sh\nwhile read old new ref; do [ \"$ref\" = refs/heads/main ] && exit 1; done\nexit 0\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.s.ticket("T-001")
        self.s.write(".windvane/QUESTIONS.md", PROVISIONAL)

    def start(self, forge="git"):
        if forge != "git":
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace('forge = "git"', f'forge = "{forge}"'))
        self.s.publish()
        self.p = Project(self.s.root)

    def theirs(self, *names):
        """Another clone pushes a commit of its own to each of `names`, unseen here (no fetch). Returns its sha."""
        other = self.s.tmp / "other"
        self.s.git("clone", "-q", str(self.s.remote), str(other), cwd=self.s.tmp)
        self.s.git("commit", "-q", "--allow-empty", "-m", "theirs", cwd=other)
        for name in names:
            self.s.git("push", "-q", "origin", f"HEAD:refs/heads/{name}", cwd=other)
        return self.s.git("rev-parse", "HEAD", cwd=other)

    def remote_files(self, branch):
        self.s.git("fetch", "-q", "origin", branch)
        return self.s.git("ls-tree", "-r", "--name-only", "FETCH_HEAD")

    def logged(self):
        return (self.p.state / "autopilot.log").read_text()


class TakenMeanwhile(Base):
    def setUp(self):
        super().setUp()
        self.start()
        # the name looked up before the other clone pushed it
        self.enterContext(mock.patch.object(planner, "branch_name", lambda *a: NAME))

    def plan(self, *names):
        """A whole plan run (fake `claude`), the other clone pushing `names` while its session runs (after the plan's
        fetch). Returns (the plan's result, the other clone's sha)."""
        real, theirs = planner.run_claude, []

        def session(*a, **k):
            theirs.append(self.theirs(*names))
            return real(*a, **k)

        with mock.patch.object(planner, "run_claude", session):
            return planner.plan(self.p, "make it great"), theirs[0]

    def test_a_plan_whose_name_was_pushed_meanwhile_lands_on_the_next_suffix(self):
        n, theirs = self.plan(NAME)
        self.assertGreater(n, 0)
        self.assertIn(".windvane/tickets/T-002.md", self.remote_files(f"{NAME}-2"))
        self.assertEqual(self.s.git("ls-remote", "origin", NAME).split()[0], theirs)  # theirs left as it was
        self.assertEqual(held.load_json(self.p.state / "held.json", {}), {})
        self.assertIn(f"{NAME} was pushed from elsewhere meanwhile; pushing to {NAME}-2", self.logged())
        self.assertIn(f"plan: 1 ticket pushed to {NAME}-2", self.logged())

    def test_a_second_refusal_is_held_saying_the_name_was_taken(self):
        with mock.patch.object(held, "remote_heads", lambda *a: set()):  # -2 pushed after the remote was asked too
            self.assertEqual(self.plan(NAME, f"{NAME}-2")[0], 0)
        self.assertEqual(held.load_json(self.p.state / "held.json", {}), {f"{NAME}-2": "push"})
        self.assertEqual(self.why(), f"refused: the name {NAME}-2 was taken on the remote meanwhile; push it by hand "
                                     f"with `git push origin {NAME}-2:{NAME}-2-b` and merge it into main: "
                                     f"`git merge origin/{NAME}-2-b` on main, then push it")
        self.assertEqual(held.push_held(self.p), 0)  # the next run's push is refused the same way, and says so again
        self.assertIn(f"refused: the name {NAME}-2 was taken on the remote meanwhile", self.why())
        self.assertEqual(held.load_json(self.p.state / "held.json", {}), {f"{NAME}-2": "push"})

    def why(self):
        return held.load_json(self.p.state / "held_why.json", {})[f"{NAME}-2"]["why"]

    def test_on_github_the_hand_push_step_names_the_pr_command(self):
        why = held.push_refusal(" ! [rejected]        HEAD -> x (fetch first)", "github", "x", "up", "trunk")
        self.assertEqual(why, "refused: the name x was taken on the remote meanwhile; push it by hand with "
                              "`git push up x:x-b` and open a PR from it and merge it: "
                              "`gh pr create --head x-b --base trunk --fill`, then `gh pr merge`")

    def test_a_held_branch_pushed_again_keeps_its_name(self):
        """No `fresh`: the user may have been told its name, so it isn't moved."""
        self.theirs(NAME)
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        self.assertTrue(clean_detach(self.p, cd, self.p.upstream))
        self.s.git("commit", "-q", "--allow-empty", "-m", "Plan: 0 tickets", cwd=cd)
        self.assertTrue(held.is_local(held.push_branch(self.p, cd, NAME, tries=1)))
        self.assertEqual(self.s.git("ls-remote", "--heads", "origin", f"{NAME}-*"), "")


class GithubLand(Base):
    def test_a_land_whose_name_was_pushed_meanwhile_opens_its_pr_on_the_suffix(self):
        self.start("github")
        state = self.s.tmp / "gh"
        state.mkdir()
        gh = self.s.bin / "gh"
        gh.write_text(GH.format(python=sys.executable, state=state, remote=self.s.remote))
        gh.chmod(0o755)
        self.theirs(NAME)
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        self.assertTrue(clean_detach(self.p, cd, self.p.upstream))
        (cd / ".windvane/tickets/T-002.md").write_text("# T-002: next\nDepends on: nothing.\n")
        self.s.git("add", "-A", ".windvane", cwd=cd)
        landed = held.land(self.p, cd, NAME, "Plan: 1 new ticket", "body", fresh=True)
        self.assertEqual(tuple(landed), (True, "https://github.com/o/r/pull/7"))
        self.assertEqual(landed.branch, f"{NAME}-2")
        self.assertIn(f"pr create --head {NAME}-2 --base main", (state / "log").read_text())
        self.assertEqual(held.load_json(self.p.state / "held.json", {}), {})


class AnswersLine(Base):
    def answer(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), "answer", "T-001", "B"])
        return out.getvalue()

    def test_on_github_a_pushed_branch_without_a_pr_names_the_pr_command(self):
        self.start("github")
        with mock.patch.object(preflight, "land", lambda *a, **k: (False, "answers-20261020-120000")):
            out = self.answer()
        self.assertIn("they wait on answers-20261020-120000 until you open a PR from it and merge it: "
                      "`gh pr create --head answers-20261020-120000 --base main --fill`, then `gh pr merge`", out)

    def test_on_plain_git_a_branch_main_refused_names_the_merge(self):
        """For real: main's hook refuses the push, so the answers wait on their own pushed branch."""
        self.start()
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_MAIN)
        hook.chmod(0o755)
        out = self.answer()
        branch = self.s.git("ls-remote", "--heads", "origin", "answers-*").split("refs/heads/")[1].strip()
        self.assertIn(f"they wait on {branch} until you merge it into main: `git merge origin/{branch}` on main, "
                      "then push it", out)


class Outage(Base):
    def test_a_name_looked_up_during_an_outage_waits_once_and_still_comes_back(self):
        self.start()
        up, waits, asked, real = [False], [], [], net.run

        def run(cmd, *a, **k):
            if "ls-remote" in cmd:
                asked.append(up[0])
                if not up[0]:
                    return subprocess.CompletedProcess(cmd, 128, "", "fatal: Could not resolve host: example.org")
            return real(cmd, *a, **k)

        def sleep(_):
            waits.append(1)
            up[0] = True

        far = datetime.now() + timedelta(days=1)
        self.enterContext(mock.patch.object(net, "run", run))
        self.enterContext(mock.patch.object(net, "probe", lambda: up[0]))
        self.enterContext(mock.patch.object(
            net, "waiter", lambda: net.wait(self.p, lambda: far, lambda: False, sleep=sleep)))
        with mock.patch.object(planner, "now", lambda: datetime(2026, 10, 20, 12, 0, 0)):
            self.assertEqual(planner.branch_name(self.p, self.s.root, "plan"), NAME)
        self.assertEqual((asked, len(waits)), ([False, True], 1))


if __name__ == "__main__":
    unittest.main()
