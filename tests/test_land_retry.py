"""T-178: a driver change whose PR merge fails at once is retried (GitHub still working out whether it can merge, or
main moved under it); a desk answer that still can't merge closes its own PR and can be given again, while a plan or
held change keeps its PR open and stays held, so `status` lists it."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import held, preflight  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.worktree import clean_detach, ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402

# A fake `gh` over the sandbox's bare remote: `pr create` opens PR #7 for --head (main moving to `moved` then,
# with a `move` file); `pr merge` fast-forwards main to
# the branch, failing while the `unknown` file counts (each `pr view` counts it down), while `blocked` exists, or when
# main moved (the branch isn't on top of it); `pr view` says which; `pr close --delete-branch` deletes the branch;
# `pr list` shows #7 open.
GH = """#!{python}
import json, subprocess, sys
from pathlib import Path
st, args = Path("{state}"), sys.argv[1:]
with open(st / "log", "a") as f:
    f.write(" ".join(args) + "\\n")
def git(*a):
    return subprocess.run(["git", "--git-dir", "{remote}", *a], capture_output=True, text=True).returncode
head = (st / "head").read_text() if (st / "head").exists() else ""
unknown = int((st / "unknown").read_text()) if (st / "unknown").exists() else 0
behind = bool(head) and git("merge-base", "--is-ancestor", "refs/heads/main", "refs/heads/" + head) != 0
if args[:2] == ["pr", "create"]:
    if (st / "move").exists():
        git("update-ref", "refs/heads/main", "refs/heads/moved")
    (st / "head").write_text(args[args.index("--head") + 1])
    print("https://github.com/o/r/pull/7")
elif args[:2] == ["pr", "merge"]:
    if unknown or (st / "blocked").exists() or behind:
        sys.exit("GraphQL: Pull request is not mergeable")
    git("update-ref", "refs/heads/main", "refs/heads/" + head)
    (st / "merged").write_text(head)
elif args[:2] == ["pr", "view"]:
    if unknown:  # each look brings GitHub closer to knowing
        (st / "unknown").write_text(str(unknown - 1))
    pr = ({{"state": "MERGED"}} if (st / "merged").exists()
          else {{"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}} if unknown
          else {{"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}} if behind
          else {{"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}})
    print(json.dumps({{"state": "OPEN", **pr}}))
elif args[:2] == ["pr", "close"]:
    git("update-ref", "-d", "refs/heads/" + head)
elif args[:2] == ["pr", "list"]:
    print(json.dumps([{{"number": 7, "state": "OPEN", "headRefName": head}}] if head else []))
"""

QUESTION = ("### T-001: Red or blue?\nThe ticket doesn't say.\n- **A**: red\n- **B**: blue\n"
            "Recommendation: A, because.\n")


class LandRetry(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish()
        self.state = self.s.tmp / "gh"
        self.state.mkdir()
        gh = self.s.bin / "gh"
        gh.write_text(GH.format(python=sys.executable, state=self.state, remote=self.s.remote))
        gh.chmod(0o755)
        self.p = Project(self.s.root)
        self.addCleanup(setattr, held, "MERGE_WAIT", held.MERGE_WAIT)
        held.MERGE_WAIT = 0

    def gh(self):
        f = self.state / "log"
        return f.read_text().splitlines() if f.exists() else []

    def merges(self):
        return sum(c.startswith("pr merge 7") for c in self.gh())

    def main_file(self, rel):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("show", f"origin/main:{rel}")

    def change(self, name="plan-1"):
        """A change of the driver's own in the plan tree, off main, as a plan's is: committed, ready to land."""
        cd = self.p.plan_dir
        self.s.git("fetch", "-q", "origin")
        ensure_worktree(self.p, cd, setup=False)
        self.assertTrue(clean_detach(self.p, cd, self.p.upstream))
        (cd / ".windvane/tickets/T-002.md").write_text("# T-002: next\nDepends on: nothing.\n")
        self.s.git("add", "-A", ".windvane", cwd=cd)
        self.s.git("commit", "-qm", "Plan: 1 new ticket", cwd=cd)
        return cd

    def main_moves_when_the_pr_opens(self, rel, text):
        """Main gets a commit of its own, adding `text` to `rel`, between the change's commit and its merge."""
        f = self.s.root / rel
        self.s.write(rel, (f.read_text() if f.exists() else "") + text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "main moves")
        self.s.git("push", "-q", "origin", "HEAD:refs/heads/moved")
        self.s.git("reset", "-q", "--hard", "origin/main")
        (self.state / "move").touch()

    def answer(self, x, letter):
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "preflight.json").write_text(json.dumps({"questions": [x]}))
        return preflight.record(self.p, [(x, letter, None)])

    def test_a_merge_tried_while_github_is_still_working_it_out_lands_on_a_later_try(self):
        (self.state / "unknown").write_text("2")
        cd = self.change()
        ok, where = held.land(self.p, cd, "plan-1", "Plan: 1 new ticket", "body", commit=False)
        self.assertEqual((ok, where), (True, "https://github.com/o/r/pull/7"))
        self.assertEqual(self.merges(), 3)
        self.assertIn("# T-002: next", self.main_file(".windvane/tickets/T-002.md"))
        self.assertEqual(held.held_now(self.p), [])

    def test_a_pr_main_moved_under_is_replayed_pushed_again_and_merged(self):
        cd = self.change()
        self.s.write(".windvane/QUEUE.md", "# Queue\n- T-001 first\n")
        self.s.publish("main moves meanwhile")
        ok, where = held.land(self.p, cd, "plan-1", "Plan: 1 new ticket", "body", commit=False)
        self.assertTrue(ok, where)
        self.assertEqual(self.merges(), 2)  # failed, then merged once replayed
        self.assertIn("- T-001 first", self.main_file(".windvane/QUEUE.md"))  # main's own change kept
        self.assertIn("# T-002: next", self.main_file(".windvane/tickets/T-002.md"))
        self.assertNotIn("pr close", " ".join(self.gh()))

    def test_a_desk_answer_that_still_cant_merge_closes_its_pr_and_can_be_given_again(self):
        (self.state / "unknown").write_text("99")
        x = preflight.question("T-001", "Red or blue?", QUESTION, "new", 1)
        before = self.s.git("rev-parse", "origin/main")
        out = self.answer(x, "B")
        self.assertEqual(out, "answers for T-001 not recorded (PR #7 couldn't merge: GitHub hadn't worked out "
                              "whether it can merge); answer again with "
                              "windvane answer T-001.1 B")
        self.assertEqual(self.merges(), held.MERGE_TRIES)  # bounded
        self.assertIn("pr close 7 --delete-branch", self.gh())
        head = (self.state / "head").read_text()
        self.assertEqual(self.s.git("ls-remote", "--heads", "origin", head), "")  # its branch went with it
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(self.s.git("rev-parse", "origin/main"), before)
        self.assertEqual(preflight.lookup(self.p, "T-001.1"), x)
        self.assertEqual(held.held_now(self.p), [])  # nothing left held for it

    def test_a_plan_that_cant_merge_keeps_its_pr_open_and_stays_held(self):
        (self.state / "blocked").touch()
        cd = self.change()
        ok, where = held.land(self.p, cd, "plan-1", "Plan: 1 new ticket", "body", commit=False)
        self.assertEqual((ok, where), (False, "https://github.com/o/r/pull/7"))
        self.assertNotIn("pr close", " ".join(self.gh()))
        self.assertEqual(self.merges(), 1)  # a required review doesn't clear by waiting
        self.assertNotEqual(self.s.git("ls-remote", "--heads", "origin", "plan-1"), "")
        held.held_branches(self.p)  # the next run's prune keeps it: its PR is open
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {"plan-1": "land"})
        [(branch, why, _, on)] = held.held_now(self.p)
        self.assertEqual((branch, on), ("plan-1", "origin/plan-1"))
        self.assertEqual(why, "PR #7 couldn't merge: a required check or review blocks it; merge it by hand "
                              "(gh pr merge 7 --merge)")
        self.assertIn(f"pushed to origin/plan-1, not merged: plan-1 (Plan: 1 new ticket): {why}",
                      held.held_lines(self.p)[0])  # not "open a PR": it has one

    def test_a_desk_answer_whose_replay_conflicts_closes_its_pr_and_can_be_given_again(self):
        self.s.write(".windvane/QUESTIONS.md", "# Questions\n\n## Open\n\n## Provisional\n\n## Answered\n")
        self.s.publish("questions")
        self.main_moves_when_the_pr_opens(".windvane/QUESTIONS.md", "### T-009: Other?\nAnswer: A (other)\n")
        x = preflight.question("T-001", "Red or blue?", QUESTION, "new", 1)
        out = self.answer(x, "B")
        self.assertEqual(out, "answers for T-001 not recorded (PR #7 couldn't merge: it conflicts with main); answer "
                              "again with windvane answer T-001.1 B")
        self.assertEqual(self.merges(), 1)  # a replay that conflicts isn't tried again
        self.assertIn("pr close 7 --delete-branch", self.gh())
        self.assertNotIn("### T-001", self.main_file(".windvane/QUESTIONS.md"))
        self.assertIn("### T-009: Other?", self.main_file(".windvane/QUESTIONS.md"))  # main's own change stands
        self.assertEqual(preflight.lookup(self.p, "T-001.1"), x)
        self.assertNotIn("rebase", self.s.git("status", cwd=self.p.plan_dir))  # the tree isn't left mid-rebase
        self.assertIn("1 answer recorded on main", self.answer(x, "B"))  # given again, it lands on the moved main
        self.assertIn("### T-001: Red or blue?", self.main_file(".windvane/QUESTIONS.md"))

    def test_a_plan_whose_replay_conflicts_keeps_its_pr_open_and_stays_held(self):
        cd = self.change()
        self.main_moves_when_the_pr_opens(".windvane/tickets/T-002.md", "# T-002: another\nDepends on: nothing.\n")
        ok, where = held.land(self.p, cd, "plan-1", "Plan: 1 new ticket", "body", commit=False)
        self.assertEqual((ok, where), (False, "https://github.com/o/r/pull/7"))
        self.assertNotIn("pr close", " ".join(self.gh()))
        self.assertEqual([(b, why) for b, why, *_ in held.held_now(self.p)], [
            ("plan-1", "PR #7 couldn't merge: it conflicts with main; merge it by hand (gh pr merge 7 --merge)")])

    def test_a_held_change_landed_again_that_cant_merge_stays_held(self):
        (self.state / "blocked").touch()
        cd = self.change()
        self.s.git("branch", "-f", "plan-1", "HEAD", cwd=cd)
        self.assertTrue(clean_detach(self.p, cd, self.p.upstream))
        held.hold(self.p, "plan-1", "land", "push refused")
        self.addCleanup(setattr, held, "push", held.push)
        held.push = lambda *a, **k: None
        self.assertEqual(held.push_held(self.p), 1)
        self.assertIn("pr merge 7 --merge", self.gh())
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {"plan-1": "land"})
        self.assertEqual([b for b, *_ in held.held_now(self.p)], ["plan-1"])


if __name__ == "__main__":
    unittest.main()
