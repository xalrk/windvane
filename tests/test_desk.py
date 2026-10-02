"""Desk PRs: the user's own labelled PRs, checked and merged between tickets, and fixed when red or conflicting."""
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import desk, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

# A stand-in for `gh` on a bare remote: PRs live in a JSON file, heads are the remote's branches, and a merge really
# merges into main (refused when the head moved, or with FAKE_GH_REFUSE).
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, tempfile
STATE, REMOTE = sys.argv.pop(1), sys.argv.pop(1)
a = sys.argv[1:]
st = json.load(open(STATE))
st.setdefault("calls", []).append(a[:2])
def head(b):
    return subprocess.run(["git", "--git-dir", REMOTE, "rev-parse", "--verify", "-q", "refs/heads/" + b],
                          capture_output=True, text=True).stdout.strip()
def pr(n):
    return next(x for x in st["prs"] if str(x["number"]) == n)
if a[:2] == ["repo", "view"]:
    print(json.dumps({"owner": {"login": "me"}}))
elif a[:2] == ["pr", "list"]:
    print(json.dumps([{**x, "headRefOid": head(x["headRefName"])} for x in st["prs"] if x["state"] == "OPEN"]))
elif a[:2] == ["pr", "view"]:
    x = pr(a[2])
    print(json.dumps({"headRefOid": head(x["headRefName"]), "state": x["state"]}))
elif a[:2] == ["pr", "comment"]:
    st.setdefault("comments", []).append([a[2], a[a.index("--body") + 1]])
elif a[:2] == ["pr", "merge"]:
    x, sha = pr(a[2]), a[a.index("--match-head-commit") + 1]
    d = tempfile.mkdtemp()
    run = lambda *c: subprocess.run(["git", *c], cwd=d, capture_output=True, text=True)
    run("clone", "-q", REMOTE, ".")
    if os.environ.get("FAKE_GH_REFUSE") or head(x["headRefName"]) != sha or run("merge", "--no-ff", "-m", "Merge PR",
                                                                                 sha).returncode:
        json.dump(st, open(STATE, "w"))
        sys.exit("refused")
    run("push", "-q", "origin", "HEAD:main")
    x["state"] = "MERGED"
json.dump(st, open(STATE, "w"))
'''


class DeskPRs(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"')
                       .replace('check = "test -f README"', 'check = "test -f README && test ! -f BROKEN"'))
        self.s.publish("github")
        self.p = Project(self.s.root)
        self.gh = self.s.tmp / "gh.json"
        self.gh.write_text(json.dumps({"prs": []}))
        fake = self.s.bin / "gh"
        fake.write_text(f"#!/bin/sh\nexec python3 - {self.gh} {self.s.remote} \"$@\" <<'EOF'\n{FAKE_GH}EOF\n")
        fake.chmod(0o755)
        self.pushes = []
        orig = desk.push
        desk.push = lambda title, *a, **k: self.pushes.append(title)
        self.addCleanup(setattr, desk, "push", orig)
        self.pilot = self.new_pilot()
        self.assertTrue(self.pilot.prepare(0))

    def new_pilot(self):
        pilot = driver.Pilot(self.p, args())
        pilot.deadline = now() + timedelta(hours=1)
        return pilot

    def pr(self, n, files, label=True, author="me", base="main"):
        """Open PR #n on a new branch off `base` that writes `files` ({path: text, or None to delete})."""
        branch = f"desk-{n}"
        self.s.git("switch", "-q", "-c", branch, base)
        self.commit(files)
        self.s.git("push", "-q", "origin", branch)
        self.s.git("switch", "-q", "main")
        st = json.loads(self.gh.read_text())
        st["prs"].append({"number": n, "title": f"desk change {n}", "author": {"login": author}, "state": "OPEN",
                          "headRefName": branch, "isCrossRepository": False, "isDraft": False,
                          "labels": [{"name": "windvane merge"}] if label else []})
        self.gh.write_text(json.dumps(st))

    def commit(self, files, msg="change"):
        for f, text in files.items():
            if text is None:
                self.s.git("rm", "-q", f)
            else:
                self.s.write(f, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", msg)

    def user_push(self, n, files):
        """The user pushes a new head to PR #n's branch from the desk."""
        self.s.git("switch", "-q", f"desk-{n}")
        self.s.git("pull", "-q", "origin", f"desk-{n}")
        self.commit(files, "desk fix")
        self.s.git("push", "-q", "origin", f"desk-{n}")
        self.s.git("switch", "-q", "main")

    def step(self, pilot=None):
        """One of the loop's desk passes: look at the PRs now and handle the first due one. The PRs it found due."""
        pilot = pilot or self.pilot
        pilot.desk_looked = 0
        due = pilot.desk_due()
        if due:
            pilot.desk_one(due[0])
        return [pr["number"] for pr in due]

    def state(self):
        return json.loads(self.gh.read_text())

    def comments(self):
        return [body for _, body in self.state().get("comments", [])]

    def fixes(self):
        f = self.s.tmp / "calls.jsonl"
        return [c["prompt"] for c in map(json.loads, f.read_text().splitlines() if f.exists() else [])
                if c["system"].startswith("# windvane fix run")]

    def on_main(self, path):
        self.s.git("fetch", "-q", "origin")
        return path in self.s.git("ls-tree", "-r", "--name-only", "origin/main").split()

    def checks(self):
        d = self.p.state / "checks"
        return len(list(d.iterdir())) if d.is_dir() else 0

    def test_green_pr_merges_in_a_run_with_a_comment_and_its_branch_deleted(self):
        self.pr(1, {"feature.txt": "new\n"})
        self.pilot.run(now() + timedelta(hours=1))
        self.assertTrue(self.on_main("feature.txt"))
        self.assertEqual(self.state()["prs"][0]["state"], "MERGED")
        self.assertIn("**windvane check: PASSED**", self.comments()[0])
        self.assertEqual(self.pushes, ["Desk PR #1 merged"])
        self.assertEqual(self.s.git("ls-remote", "--heads", "origin", "desk-1"), "")
        self.assertEqual(self.pilot.desk_merged, ["1"])
        self.assertEqual(self.pilot.stop_reason, "queue empty")
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "main")  # the checkout never moved

    def test_red_pr_gets_a_fix_run_then_merges_on_the_next_pass(self):
        self.pr(1, {"feature.txt": "new\n", "BROKEN": "x\n"})
        self.assertEqual(self.step(), [1])
        self.assertIn("**windvane: check fails**", self.comments()[0])
        self.assertEqual(len(self.fixes()), 1)
        self.assertIn("check fails", self.fixes()[0])
        self.assertFalse(self.on_main("feature.txt"))
        self.assertEqual(self.step(), [1])
        self.assertTrue(self.on_main("feature.txt"))
        self.assertFalse(self.on_main("BROKEN"))
        self.assertEqual(self.pushes, ["Fixed: desk PR #1 merged"])
        roles = [json.loads(line)["role"] for line in (self.p.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(roles, ["fix"])

    def test_local_conflict_skips_the_check_and_goes_to_a_fix_run(self):
        self.pr(1, {"README": "from the desk\n"})
        self.commit({"README": "from main\n"})
        self.s.git("push", "-q", "origin", "main")
        self.assertEqual(self.step(), [1])
        self.assertEqual(self.checks(), 0)  # GitHub's mergeable answer isn't waited for, nor the check spent
        self.assertIn("conflicts with main", self.fixes()[0])
        self.assertIn("README", self.comments()[0])
        self.step()
        self.assertEqual(self.state()["prs"][0]["state"], "MERGED")
        self.assertEqual(self.pushes, ["Fixed: desk PR #1 merged"])

    def test_refused_merge_goes_to_a_fix_run(self):
        os.environ["FAKE_GH_REFUSE"] = "1"
        os.environ["FAKE_FIX"] = "giveup"
        self.pr(1, {"feature.txt": "new\n"})
        self.step()
        self.assertIn("merge refused", self.fixes()[0])
        self.assertEqual(self.pushes, ["Desk PR #1: merge refused, left for the desk"])

    def test_a_fix_that_gives_up_pushes_once_and_the_head_is_not_checked_again(self):
        os.environ["FAKE_FIX"] = "giveup"
        self.pr(1, {"BROKEN": "x\n"})
        self.step()
        self.assertEqual(self.pushes, ["Desk PR #1: check fails, left for the desk"])
        self.assertIn("**windvane fix gave up**: needs a product call", self.comments()[-1])
        checks = self.checks()
        self.assertEqual(self.step(), [])
        self.assertEqual(self.step(self.new_pilot()), [])  # a restarted driver remembers it
        self.assertEqual((len(self.fixes()), self.checks(), len(self.pushes)), (1, checks, 1))

    def test_a_new_head_after_a_give_up_is_tried_again(self):
        os.environ["FAKE_FIX"] = "giveup"
        self.pr(1, {"feature.txt": "new\n", "BROKEN": "x\n"})
        self.step()
        self.user_push(1, {"BROKEN": None})
        self.assertEqual(self.step(), [1])
        self.assertTrue(self.on_main("feature.txt"))
        self.assertEqual(self.pushes, ["Desk PR #1: check fails, left for the desk", "Desk PR #1 merged"])

    def test_out_of_fix_runs_it_is_left_for_the_desk(self):
        os.environ["FAKE_FIX"] = "red"  # pushes a new head that is still red
        self.pr(1, {"BROKEN": "x\n"})
        for _ in range(3):  # red, fixed; still red, fixed again; still red: out of tries (desk_fixes = 2)
            self.assertEqual(self.step(), [1])
        self.assertEqual(self.step(), [])
        self.assertEqual(len(self.fixes()), 2)
        self.assertEqual(self.pushes, ["Desk PR #1: check fails, left for the desk"])

    def test_only_the_owners_labelled_prs_are_candidates(self):
        self.pr(1, {"a.txt": "a\n"}, label=False)
        self.pr(2, {"b.txt": "b\n"}, author="someone")
        self.pr(3, {"c.txt": "c\n"})
        self.assertEqual([pr["number"] for pr in self.pilot.desk_due()], [3])

    def test_off_without_a_label_or_on_forge_git(self):
        self.pr(1, {"a.txt": "a\n"})
        self.p.ap["desk_label"] = ""
        self.assertEqual(self.step(), [])
        self.p.ap["desk_label"], self.p.cfg["forge"] = "windvane merge", "git"
        self.assertEqual(self.step(), [])
        self.assertNotIn(["pr", "list"], self.state().get("calls", []))


if __name__ == "__main__":
    unittest.main()
