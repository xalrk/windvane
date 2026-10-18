"""An approved ticket whose PR is a draft: one opened as a draft is marked ready and merged, one a person turned into
a draft is set aside (not failed) with a push saying how to let it go, and a refused merge says why, failing at once
on a state no retry clears."""
import json
import re
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, held  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402

# A fake `gh` over the sandbox's bare remote: every branch on it has PR #7 open. The state dir's files set the PR up:
# `draft` (removed by `pr ready` unless `stuck`), `timeline` (its events, one a line; `fail` for one GitHub won't
# show), `refuse` (the mergeStateStatus a refused merge reports) and `blip` (`ready` or `api`: that call fails once
# with a 502). `pr merge` merges --match-head-commit into main.
GH = """#!{python}
import json, subprocess, sys
from pathlib import Path
st, args = Path("{state}"), sys.argv[1:]
with open(st / "log", "a") as f:
    f.write(" ".join(args) + "\\n")
def git(*a):
    return subprocess.run(["git", "--git-dir", "{remote}", *a], capture_output=True, text=True)
draft, refuse = (st / "draft").exists(), (st / "refuse").exists() and (st / "refuse").read_text()
if (st / "blip").exists() and (st / "blip").read_text() == args[1 - (args[0] == "api")]:
    (st / "blip").unlink()
    sys.exit("HTTP 502: Bad Gateway")
if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    there = git("rev-parse", "--verify", "-q", "refs/heads/" + head).returncode == 0
    print(json.dumps([{{"number": 7, "state": "OPEN", "headRefName": head}}] if there else []))
elif args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "isDraft":
    print("true" if draft else "false")
elif args[:2] == ["pr", "view"]:
    print(json.dumps({{"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": refuse or "CLEAN",
                      "isDraft": draft}}))
elif args[:2] == ["pr", "ready"]:
    if (st / "stuck").exists():
        sys.exit("GraphQL: Resource not accessible by integration (markPullRequestReadyForReview)")
    (st / "draft").unlink()
elif args[0] == "api" and "timeline" in args[2]:
    events = (st / "timeline").read_text() if (st / "timeline").exists() else "committed\\nreviewed\\n"
    if events == "fail":
        sys.exit("gh: Not Found (HTTP 404)")
    print(events)
elif args[:2] == ["pr", "merge"]:
    if draft:
        sys.exit("GraphQL: Pull Request is still a draft (mergePullRequest)")
    if refuse:
        sys.exit("GraphQL: Base branch was modified. Review and try the merge again. (mergePullRequest)")
    sha = args[args.index("--match-head-commit") + 1]
    tree = git("merge-tree", "--write-tree", "refs/heads/main", sha).stdout.split()[0]
    merge = git("commit-tree", tree, "-p", "refs/heads/main", "-p", sha, "-m", args[args.index("--subject") + 1])
    git("update-ref", "refs/heads/main", merge.stdout.strip())
"""


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
    a.update(kw)
    return Namespace(**a)


class MergeDraft(unittest.TestCase):
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
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def run_pilot(self, **files):
        for name, text in files.items():
            (self.state / name).write_text(text)
        pilot = driver.Pilot(self.p, args())
        pilot.sleep = lambda s: None
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def gh(self):
        return (self.state / "log").read_text().splitlines()

    def merges(self):
        return sum(c.startswith("pr merge 7") for c in self.gh())

    def workers(self):
        calls = [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return sum(c["system"].startswith("# windvane worker") for c in calls)

    def outcomes(self):
        ledger = self.p.state / "ledger.jsonl"
        lines = ledger.read_text().splitlines() if ledger.exists() else []
        return [e.get("outcome") for e in map(json.loads, lines) if e.get("event") == "outcome"]

    def test_a_pr_born_a_draft_is_marked_ready_and_merges_on_the_first_try(self):
        pilot = self.run_pilot(draft="")
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("pr ready 7", self.gh())
        self.assertEqual(self.merges(), 1)
        self.assertEqual(self.workers(), 1)  # no worker run after the review
        self.assertIn("T-001: PR #7 was a draft; marked ready", self.log())

    def test_a_pr_turned_into_a_draft_is_a_hold_set_aside_not_failed(self):
        pilot = self.run_pilot(draft="", timeline="committed\nconvert_to_draft\nreviewed\n")
        self.assertEqual((pilot.merged, pilot.failed), ([], []))
        self.assertNotIn("pr ready 7", self.gh())
        self.assertEqual(self.merges(), 0)
        self.assertNotIn("failed", self.outcomes())
        self.assertIn("T-001", pilot.skip)
        [(title, msg)] = [(t, m) for t, m in self.pushes if t.startswith("Held")]
        self.assertIn("`gh pr ready 7`; the next run takes it up again", msg)
        self.assertIn("T-001: PR #7 was made a draft after it was opened; left as it is, set aside", self.log())

    def test_a_timeline_github_wont_show_is_taken_for_a_hold_saying_so(self):
        pilot = self.run_pilot(draft="", timeline="fail")
        self.assertEqual((pilot.merged, pilot.failed), ([], []))
        self.assertNotIn("pr ready 7", self.gh())
        self.assertIn("T-001: couldn't tell whether PR #7 is a draft on purpose (gh: Not Found (HTTP 404)); left "
                      "as it is, set aside", self.log())
        [msg] = [m for t, m in self.pushes if t.startswith("Held")]
        self.assertIn("is a draft and gh couldn't tell who made it one (gh: Not Found (HTTP 404))", msg)
        self.assertNotIn("by hand", msg)

    def test_a_gh_hiccup_while_undrafting_is_waited_out_and_the_ticket_merges(self):
        for call in ("api", "ready"):
            with self.subTest(call):
                self.setUp()
                pilot = self.run_pilot(draft="", blip=call)
                self.assertEqual((pilot.merged, pilot.failed), (["T-001"], []))
                self.assertIn("T-001: gh answered again after 1 min; landing goes on", self.log())
                self.assertIn("T-001: PR #7 was a draft; marked ready", self.log())

    def test_a_merge_blocked_by_a_required_review_fails_after_one_try_saying_so(self):
        pilot = self.run_pilot(refuse="BLOCKED")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertEqual(self.merges(), 1)
        self.assertIn("T-001: merge refused (a required check or review blocks it)\n"
                      "T-001: FAILED: the merge was refused: a required check or review blocks it", self.log())

    def test_a_draft_github_refuses_to_ready_fails_at_once_saying_so(self):
        pilot = self.run_pilot(draft="", stuck="")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertEqual(self.merges(), 0)
        self.assertEqual(sum(c == "pr ready 7" for c in self.gh()), 1)
        self.assertIn("T-001: FAILED: PR #7 is a draft and GitHub refused to mark it ready (GraphQL: Resource not "
                      "accessible by integration", self.log())

    def test_a_draft_refused_at_merge_fails_after_one_try_as_a_draft(self):
        pilot = self.run_pilot(refuse="DRAFT")
        self.assertEqual((pilot.failed, self.merges()), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: it is a draft", self.log())

    def test_a_refusal_github_sees_nothing_behind_is_tried_three_times_with_its_error(self):
        pilot = self.run_pilot(refuse="CLEAN")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertEqual(self.merges(), 3)
        self.assertEqual(self.log().count("trying again"), 3)
        self.assertIn("FAILED: the merge was refused three times (GraphQL: Base branch was modified", self.log())


class MergeState(unittest.TestCase):
    def test_a_draft_reads_as_one_though_its_state_is_clean(self):
        pr = {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "isDraft": True}
        self.assertEqual(held.merge_state(pr, "GraphQL: Pull Request is still a draft"), "it is a draft")

    def test_without_isdraft_it_reads_as_before(self):
        self.assertEqual(held.merge_state({"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}, ""),
                         "a required check or review blocks it")


if __name__ == "__main__":
    unittest.main()
