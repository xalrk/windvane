"""A ticket's branch left checked out in another slot's tree by a killed run: the next worker couldn't switch to it,
stayed detached at its head and finished READY, and every landing bounced with "No open PR" while the PR was open.
The driver frees the branch before the worker starts (a tree with changes keeps the ticket instead), lands a detached
READY on the branch it stands on, tells a READY on no branch so, and waits out a failing gh instead of bouncing."""
import json
import re
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402

# A fake `gh` over the sandbox's bare remote: every branch on it has PR #7 open (`pr list --head`), failing once
# after a review comment while the `hiccup` file exists (every time while it says `always`); `pr merge` merges --match-head-commit into main.
GH = """#!{python}
import json, subprocess, sys
from pathlib import Path
st, args = Path("{state}"), sys.argv[1:]
with open(st / "log", "a") as f:
    f.write(" ".join(args) + "\\n")
def git(*a):
    return subprocess.run(["git", "--git-dir", "{remote}", *a], capture_output=True, text=True)
if args[:2] == ["pr", "list"]:
    if (st / "hiccup").exists() and (st / "commented").exists():
        if (st / "hiccup").read_text() != "always":
            (st / "hiccup").unlink()
        sys.exit("HTTP 502: Bad Gateway")
    head = args[args.index("--head") + 1]
    there = git("rev-parse", "--verify", "-q", "refs/heads/" + head).returncode == 0
    print(json.dumps([{{"number": 7, "state": "OPEN", "headRefName": head}}] if there else []))
elif args[:2] == ["pr", "comment"]:
    (st / "commented").write_text("1")
elif args[:2] == ["pr", "merge"]:
    sha = args[args.index("--match-head-commit") + 1]
    tree = git("merge-tree", "--write-tree", "refs/heads/main", sha).stdout.split()[0]
    merge = git("commit-tree", tree, "-p", "refs/heads/main", "-p", sha, "-m", args[args.index("--subject") + 1])
    git("update-ref", "refs/heads/main", merge.stdout.strip())
elif args[:2] == ["pr", "view"]:
    print("MERGED")
"""


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
    a.update(kw)
    return Namespace(**a)


class BranchHeldElsewhere(unittest.TestCase):
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

    def killed_run(self, dirty=False):
        """What a driver killed mid-ticket leaves: slot 1's tree on the ticket's pushed branch (with a half-done file
        when `dirty`), its `finally` never run."""
        tree = self.p.state / "work-1"
        ensure_worktree(self.p, tree, setup=False)
        self.s.git("switch", "-q", "-c", "t-001-work", "origin/main", cwd=tree)
        (tree / "T-001.txt").write_text("started\n")
        self.s.git("add", "-A", cwd=tree)
        self.s.git("commit", "-qm", "T-001: start", cwd=tree)
        self.s.git("push", "-q", "-u", "origin", "HEAD", cwd=tree)
        if dirty:
            (tree / "half-done.txt").write_text("half\n")
        return tree

    def pilot(self, **kw):
        pilot = driver.Pilot(self.p, args(**kw))
        pilot.sleep = lambda s: None
        return pilot

    def log(self):
        """The project log without its timestamps."""
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def prompts(self):
        calls = [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["prompt"] for c in calls if c["system"].startswith("# windvane worker")]

    def test_a_clean_tree_holding_the_branch_is_freed_and_the_ticket_lands_in_one_round(self):
        tree = self.killed_run()
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(len(self.prompts()), 1)  # one worker run, no bounce
        self.assertNotIn("No open PR", self.log())
        self.assertIn("t-001-work was left checked out in work-1: detached it", self.log())
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree), "HEAD")
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "main")  # the user's checkout
        self.assertIn("pr merge 7", (self.state / "log").read_text())

    def test_a_tree_with_changes_on_the_branch_gets_the_ticket(self):
        tree = self.killed_run(dirty=True)
        pilot = self.pilot(parallel=2)
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("T-001: a killed run left its uncommitted work in work-1, on t-001-work: it resumes there",
                      self.log())
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(self.s.git("show", "origin/main:half-done.txt"), "half")  # its leftovers, committed there
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_a_gh_failure_at_landing_waits_and_asks_again_without_a_bounce(self):
        (self.state / "hiccup").write_text("1")
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("T-001: gh couldn't list t-001-work's PRs; asking again after 1, 2, 4, then every 5 min\n"
                      "T-001: gh answered again after 1 min; landing goes on", self.log())
        self.assertNotIn("No open PR", self.log())
        self.assertEqual(len(self.prompts()), 1)
        self.assertEqual(pilot.reviews.get("T-001", 0), 0)

    def test_gh_failing_until_a_stop_bounces_nothing_and_the_next_run_lands_it(self):
        (self.state / "hiccup").write_text("always")
        pilot = self.pilot()
        pilot.sleep = lambda s: (self.p.state / "STOP").write_text("")  # `windvane stop` during the wait
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual((pilot.merged, pilot.failed, pilot.reviews, pilot.bounces), ([], [], {}, {}))
        self.assertNotIn("T-001", pilot.feedback)
        log = self.log()
        self.assertEqual(log.count("gh couldn't list"), 1)  # one line for the whole wait
        self.assertIn("T-001: gh still failing after 1 min; stopped waiting; the next run resumes the ticket", log)
        (self.state / "hiccup").unlink()
        again = self.pilot()
        again.run(now() + timedelta(hours=1))
        self.assertEqual(again.merged, ["T-001"])
        self.assertIn("In progress on branch(es) t-001-work, PR #7. Resume it.", self.prompts()[-1])

    def test_the_users_checkout_on_the_branch_is_left_alone_and_the_ticket_still_lands(self):
        self.s.git("switch", "-q", "-c", "t-001-work", "origin/main")
        self.s.write("T-001.txt", "started\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "T-001: start")
        self.s.git("push", "-q", "-u", "origin", "HEAD")
        mine = self.s.git("rev-parse", "HEAD")
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(len(self.prompts()), 1)
        self.assertIn("`t-001-work` is checked out in the user's checkout, so you can't switch to it here: work "
                      "detached from `origin/t-001-work` and push with `git push origin HEAD:t-001-work`.",
                      self.prompts()[0])
        self.assertIn("T-001: t-001-work is checked out in your checkout: left there; its worker pushes to it "
                      "detached", self.log())
        self.assertIn("on t-001-work's pushed head: landing it as t-001-work", self.log())
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "t-001-work")  # never moved
        self.assertEqual(self.s.git("rev-parse", "HEAD"), mine)
        self.assertEqual(self.s.git("status", "--porcelain"), "")

    def test_a_tree_that_went_dirty_mid_run_takes_the_ticket_when_it_starts(self):
        tree = self.killed_run(dirty=True)
        pilot = self.pilot(parallel=2)
        pilot.claimed = True  # past the run's start: only the start of the worker sees it
        self.assertEqual(pilot.free_branches("T-001", ["t-001-work"], 0), 1)
        self.assertEqual(pilot.owner[1], "T-001")
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD", cwd=tree), "t-001-work")  # kept as it is

    def ready(self, branches, setup):
        """T-001's worker finishing READY in slot 0's tree as `setup` left it."""
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        setup(self.p.work_dir)
        pilot = self.pilot()
        pilot.queue = Queue(self.p)
        landed = []
        pilot.after = lambda tid, branch, sha: landed.append((branch, sha))
        sha = self.s.git("rev-parse", "HEAD", cwd=self.p.work_dir)
        pilot.handle("T-001", f"RESULT: READY T-001 {sha}", None, branches)
        return pilot, landed, sha

    def test_a_detached_ready_on_the_branch_head_lands_as_that_branch(self):
        def on_head(wd):
            self.s.git("switch", "-q", "-c", "t-001-work", "origin/main", cwd=wd)
            self.s.git("commit", "-q", "--allow-empty", "-m", "T-001: build", cwd=wd)
            self.s.git("push", "-q", "-u", "origin", "HEAD", cwd=wd)
            self.s.git("switch", "-q", "--detach", cwd=wd)
        pilot, landed, sha = self.ready(["t-001-work"], on_head)
        self.assertEqual(landed, [("t-001-work", sha)])
        self.assertIn(f"T-001: the tree was detached at {sha[:7]}, on t-001-work's pushed head: landing it as "
                      "t-001-work", self.log())

    def test_a_tree_detached_at_main_is_on_no_branch_though_every_branch_reaches_main(self):
        def at_main(wd):
            self.s.git("switch", "-q", "-c", "t-001-work", "origin/main", cwd=wd)
            self.s.git("commit", "-q", "--allow-empty", "-m", "T-001: build", cwd=wd)
            self.s.git("push", "-q", "-u", "origin", "HEAD", cwd=wd)
            self.s.git("switch", "-q", "--detach", "origin/main", cwd=wd)
        pilot, landed, _ = self.ready(["t-001-work"], at_main)
        self.assertEqual(landed, [])
        self.assertEqual(pilot.feedback["T-001"], "Your tree was detached, on no branch: switch to `t-001-work` or "
                                                  "push HEAD to it, then finish READY.")

    def test_a_ready_on_no_branch_says_so(self):
        def nowhere(wd):
            self.s.git("switch", "-q", "--detach", "origin/main", cwd=wd)
            self.s.git("commit", "-q", "--allow-empty", "-m", "T-001: build", cwd=wd)
        pilot, landed, _ = self.ready([], nowhere)
        self.assertEqual(landed, [])
        fb = pilot.feedback["T-001"]
        self.assertTrue(fb.startswith("Your tree was detached, on no branch: switch to your ticket's branch"), fb)
        self.assertNotIn("No open PR", fb)
        self.assertEqual(pilot.bounces["T-001"]["branch"], 1)
        self.assertIn("T-001: READY on no branch; back to the worker", self.log())

    def test_a_prompt_that_finds_the_open_pr_drops_a_stale_no_pr_item(self):
        self.killed_run()
        self.s.git("switch", "-q", "--detach", cwd=self.p.state / "work-1")
        pilot = self.pilot()
        # an older driver's bounce, still in the state file
        pilot.feedback["T-001"] = "No open PR for your branch: open it (`gh pr create`) and finish READY."
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        prompt = self.prompts()[0]
        self.assertIn("In progress on branch(es) t-001-work, PR #7. Resume it.", prompt)
        self.assertNotIn("No open PR", prompt)


if __name__ == "__main__":
    unittest.main()
