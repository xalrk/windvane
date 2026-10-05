"""T-082: a held change pushed by hand and landed on main by a squash or rebase merge (which git never counts as
merged) drops off `status` without a run in between (it fetches first, only when one is pushed by hand) and out of
held.json; one main lacks stays. On github its line says to open a PR and merge it. A push protection refusal names the
file and the page that allows a false positive. `status` shows a long commit subject whole."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, held, planner  # noqa: E402
from test_held_age import HOOK  # noqa: E402
import test_held_secret  # noqa: E402
from test_plan_retry import Base  # noqa: E402

UNBLOCK = "https://github.com/octo-org/octo-repo/security/secret-scanning/unblock-secret/2Y0Ylt0cdWmPddTuWFFh0KK7PBZ"


class HeldMerged(Base):
    windvane = test_held_secret.HeldSecret.windvane
    hand_pushed_land = test_held_secret.HeldSecret.hand_pushed_land

    def setUp(self):
        super().setUp()
        self.other = self.s.tmp / "other"  # someone else's clone: merging there leaves the project's view stale

    def on_remote(self, *steps):
        """Run git `steps` in a fresh clone of the remote whose main has moved on (so no merge is a fast-forward)."""
        subprocess.run(["git", "clone", "-q", str(self.s.remote), str(self.other)], check=True, capture_output=True)
        (self.other / "MOVED").write_text("main moved\n")
        for step in (["add", "MOVED"], ["commit", "-qm", "main moved"], *steps):
            self.s.git(*step, cwd=self.other)

    def held(self):
        return json.loads((self.p.state / "held.json").read_text())

    def assert_dropped(self, branch):
        self.assertNotEqual(self.s.git("rev-parse", "origin/main"), self.s.git("rev-parse", "origin/main", cwd=self.other))
        self.assertNotIn("held", self.windvane("status"))  # its own fetch saw the merge
        self.assertEqual(self.windvane("status", "--line"), "Sandbox: autopilot off\n")
        self.assertIn(branch, self.held())  # read only so far
        self.s.git("fetch", "-q", "--prune", "origin")  # as a run's start does: GitHub deleted the merged branch
        pushes = self.git_pushes
        self.assertEqual(held.held_branches(self.p), {})
        self.assertEqual(self.held(), {})
        self.assertEqual(json.loads((self.p.state / "held_why.json").read_text()), {})
        self.assertEqual(held.push_held(self.p), 0)
        self.assertEqual(self.git_pushes, pushes)  # not pushed again under its own name

    def test_a_github_squash_merge_drops_it(self):
        self.github()
        branch = self.hand_pushed_land()
        self.assertIn("pushed, not merged", self.windvane("status", "--line"))
        # GitHub's squash: one commit on main with the merged tree, then the head branch deleted; no PR found for it
        tree = f"$(git merge-tree --write-tree HEAD origin/{branch}-b)"
        self.on_remote(["fetch", "-q"])
        subprocess.run(f'git reset -q --hard "$(git commit-tree {tree} -p HEAD -m "Plan (#7)")"', shell=True,
                       cwd=self.other, check=True, capture_output=True)
        self.s.git("push", "-q", "origin", "main", f":{branch}-b", cwd=self.other)
        self.assert_dropped(branch)

    def test_a_rebase_merge_drops_it(self):
        branch = self.hand_pushed_land()
        self.on_remote(["checkout", "-q", "-b", "r", f"origin/{branch}-b"], ["rebase", "-q", "main"],
                       ["push", "-q", "origin", "r:main", f":{branch}-b"])
        self.assert_dropped(branch)

    def test_a_plain_git_merge_squash_drops_it(self):
        branch = self.hand_pushed_land()
        self.on_remote(["merge", "-q", "--squash", f"origin/{branch}-b"], ["commit", "-qm", "squashed"],
                       ["push", "-q", "origin", "main"])
        self.assert_dropped(branch)

    def test_before_git_2_38_cherry_finds_a_rebase_merge(self):
        branch = self.hand_pushed_land()
        self.on_remote(["checkout", "-q", "-b", "r", f"origin/{branch}-b"], ["rebase", "-q", "main"],
                       ["push", "-q", "origin", "r:main"])
        self.s.git("fetch", "-q", "origin")
        orig = held.git
        self.addCleanup(setattr, held, "git", orig)
        held.git = lambda cwd, *a: (subprocess.CompletedProcess(a, 129, "", "unknown option") if a[0] == "merge-tree"
                                    else orig(cwd, *a))
        self.assertEqual(held.held_now(self.p), [])
        gout = held.gout
        self.addCleanup(setattr, held, "gout", gout)
        held.gout = lambda cwd, *a: "" if a[0] == "cherry" else gout(cwd, *a)
        self.assertEqual([b for b, *_ in held.held_now(self.p)], [branch])  # can't tell: still listed

    def test_a_change_main_lacks_stays_listed(self):
        branch = self.hand_pushed_land()
        self.on_remote(["push", "-q", "origin", "main"])
        self.assertIn(f"pushed to origin/{branch}-b, not merged", self.windvane("status"))
        self.assertEqual(self.s.git("rev-parse", "origin/main"), self.s.git("rev-parse", "main", cwd=self.other))
        self.assertEqual(held.held_branches(self.p), {})  # not pushed again, but still noted
        self.assertEqual(self.held(), {branch: "land"})

    def status_calls(self, line=False):
        """The network calls `status` makes, run in this process."""
        calls, orig = [], cli.net.call
        cli.net.call = lambda cmd, *a, **k: calls.append(cmd) or orig(cmd, *a, **k)
        self.addCleanup(setattr, cli.net, "call", orig)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_status(Namespace(line=line), self.p)
        cli.net.call = orig
        return calls

    def test_status_fetches_only_for_a_hand_pushed_change(self):
        self.assertEqual(self.status_calls(), [])  # nothing held
        self.refusal = HOOK
        planner.plan(self.p, "make it great")
        self.refusal = None
        self.assertEqual(self.status_calls() + self.status_calls(line=True), [])  # held, not pushed: no network

    def test_status_fetches_for_a_hand_pushed_change(self):
        self.hand_pushed_land()
        self.assertEqual(self.status_calls(), [["git", "fetch", "-q", "origin"]])
        self.assertEqual(self.status_calls(line=True), [["git", "fetch", "-q", "origin"]])

    def test_a_failed_fetch_changes_nothing(self):
        branch = self.hand_pushed_land()
        before = self.windvane("status")
        self.s.git("remote", "set-url", "origin", str(self.s.tmp / "gone.git"))
        r = subprocess.run(["windvane", "status"], cwd=self.s.root, capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(r.stdout, before)
        self.assertIn(branch, r.stdout)

    def test_the_hand_pushed_line_says_what_to_do_per_forge(self):
        self.hand_pushed_land()
        self.assertIn(", not merged: ", self.windvane("status"))
        self.assertIn("): merge it into main\n", self.windvane("status"))
        self.github()
        self.assertIn("): open a PR from it and merge it\n", self.windvane("status"))

    def test_push_protection_names_the_file_and_the_unblock_page(self):
        self.refusal = test_held_secret.SECRET
        planner.plan(self.p, "make it great")
        body = self.pushes[-1][1]
        first = body.splitlines()[0]
        self.assertTrue(first.startswith("Refused: it holds a secret in `README.md:4`"), first)
        self.assertIn(f"or allow it on GitHub if it isn't a secret: {UNBLOCK}.", first)  # whole, in reading order
        why = json.loads((self.p.state / "held_why.json").read_text())[self.local()]["why"]
        self.assertTrue(why.endswith(UNBLOCK), why)  # whole in `status` too
        bare = held.push_refusal("remote: error: Secret scanning found a token", "git", "x")
        self.assertNotIn(" in `", bare)
        self.assertNotIn("allow it", bare)

    def test_status_shows_a_long_subject_whole(self):
        self.refusal = HOOK
        planner.plan(self.p, "make it great")
        branch, subject = self.local(), "Plan: " + "a very long subject " * 5 + "and its ending"
        self.assertEqual(len(subject), 120)
        fix = self.s.git("commit-tree", f"{branch}^{{tree}}", "-p", f"{branch}~1", "-m", subject)
        self.s.git("branch", "-f", branch, fix)
        self.assertIn(f"not pushed: {branch} ({subject}): refused by a hook", self.windvane("status"))
        self.assertIn(f"({subject[:60]}): ", self.windvane("overview"))  # a push stays short
        self.assertNotIn(subject[:40], self.windvane("status", "--line"))


if __name__ == "__main__":
    unittest.main()
