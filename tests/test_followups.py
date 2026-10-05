"""T-039, the port critics' follow-ups: in place, a ticket's check and merge stash the desk's uncommitted changes; a
failed periodic round reaches the phone; a renumbering is in the merged push and the PR comment; a desk PR is checked
merged with main when main moved past its base. Each with the case where nothing changes."""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import desk, driver, periodic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
import test_desk  # noqa: E402  (a module, so its tests aren't collected here again)
from test_pipeline import args  # noqa: E402

LOG_RULE = "[[append_only]]\nfile = \"LOG.md\"\nentry = '^- L(\\d+):'\nref = 'L{n}'\n"


def capture(case, *mods):
    """Every push from `mods` as (title, message), into case.pushes."""
    case.pushes = []
    for mod in mods:
        orig = mod.push
        mod.push = lambda title, msg="", *a, **k: case.pushes.append((title, msg))
        case.addCleanup(setattr, mod, "push", orig)


class Base(unittest.TestCase):
    def make(self, extra="", isolate=True, forge="git"):
        self.s = Sandbox(extra)
        self.addCleanup(self.s.close)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', f'forge = "{forge}"'
                                               + ("" if isolate else "\nisolate = false")))
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\n")
        self.s.ticket("T-001")
        self.s.publish("setup")
        self.p = Project(self.s.root)
        capture(self, driver, desk, periodic)
        self.pilot = driver.Pilot(self.p, args())
        self.pilot.deadline = now() + timedelta(hours=1)
        self.assertTrue(self.pilot.prepare(0))
        self.pilot.queue = driver.Queue(self.p)

    def branch(self, files, name="t-001-x"):
        """A ticket's pushed branch off main writing `files`; the checkout goes back to main. Its head."""
        self.s.git("switch", "-q", "-c", name, "origin/main")
        for f, text in files.items():
            self.s.write(f, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "work")
        self.s.git("push", "-q", "origin", name)
        sha = self.s.git("rev-parse", "HEAD")
        self.s.git("switch", "-q", "main")
        return sha

    def on_main(self, files, msg="main moves"):
        for f, text in files.items():
            self.s.write(f, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", msg)
        self.s.git("push", "-q", "origin", "main")

    def titles(self):
        return [t for t, _ in self.pushes]

    def log(self):
        return (self.p.state / "autopilot.log").read_text()


class InPlaceMerge(Base):
    def desk_edits(self):
        self.s.write("notes.txt", "my notes\n")
        self.s.write("README", "hi, edited at the desk\n")

    def stashes(self):
        return self.s.git("stash", "list", "--format=%s").splitlines()

    def test_a_tickets_check_and_merge_stash_the_desk_edits(self):
        self.make(isolate=False)
        sha = self.branch({"a.py": "built\n"})
        self.desk_edits()
        self.pilot.after("T-001", "t-001-x", sha)
        self.assertEqual(self.pilot.merged, ["T-001"])
        [msg] = self.stashes()
        self.assertRegex(msg, r"windvane kept: .* \(checking T-001\)$")
        self.s.git("stash", "apply", "-q")
        self.assertEqual((self.s.root / "notes.txt").read_text(), "my notes\n")
        self.assertEqual((self.s.root / "README").read_text(), "hi, edited at the desk\n")
        self.assertIn("your changes were stashed", self.titles())

    def test_edits_made_during_the_review_are_stashed_before_the_merge(self):
        self.make(isolate=False)
        sha = self.branch({"a.py": "built\n"})
        self.desk_edits()
        self.pilot.integrate("T-001", "t-001-x", sha)
        self.assertEqual(self.pilot.merged, ["T-001"])
        [msg] = self.stashes()
        self.assertTrue(msg.endswith("(merging T-001)"), msg)

    def test_a_failed_stash_waits_with_the_approval_kept_and_merges_once_it_clears(self):
        self.make(isolate=False)
        self.addCleanup(setattr, driver, "STASH_WAIT", driver.STASH_WAIT)
        self.addCleanup(setattr, driver, "NAP", driver.NAP)
        driver.STASH_WAIT, driver.NAP = 0.3, 0.1
        sha = self.branch({"a.py": "built\n"})
        self.desk_edits()
        lock = self.s.root / ".git/index.lock"  # a git left running at the desk, say
        lock.write_text("")
        seen = []

        def nap(until):
            seen.append(((self.s.root / "notes.txt").read_text(), self.stashes()))
            if len(seen) == 3:
                lock.unlink()
        self.pilot.nap = nap
        self.pilot.after("T-001", "t-001-x", sha)
        self.assertEqual(seen, [("my notes\n", [])] * 3)  # nothing deleted, nothing stashed while it waited
        self.assertEqual((self.pilot.merged, self.pilot.failed), (["T-001"], []))
        self.assertEqual(len(self.stashes()), 1)
        [warn] = [m for t, m in self.pushes if t == "checkout not cleaned"]  # one push for the whole wait
        self.assertTrue(warn.startswith("Commit or stash your checkout's changes: T-001 waits (its approval stands)"),
                        warn)
        self.assertIn("again every 0.3 s.\nwindvane couldn't stash them, and deleted nothing.", warn)
        self.assertEqual(self.log().count("checking T-001: waits: couldn't stash"), 1)
        self.assertNotIn("checking T-001", self.pilot.stash_failed)  # a later wait is pushed again

    def test_a_stop_ends_the_wait_without_failing_the_ticket(self):
        self.make(isolate=False)
        sha = self.branch({"a.py": "built\n"})
        self.desk_edits()
        (self.s.root / ".git/index.lock").write_text("")
        self.pilot.nap = lambda until: (self.p.state / "STOP").write_text("")
        self.pilot.after("T-001", "t-001-x", sha)
        (self.s.root / ".git/index.lock").unlink()
        self.assertEqual((self.pilot.merged, self.pilot.failed), ([], []))
        self.assertEqual((self.s.root / "notes.txt").read_text(), "my notes\n")

    def test_unchanged_isolated_the_checkout_is_left_alone(self):
        self.make()
        sha = self.branch({"a.py": "built\n"})
        self.desk_edits()
        self.pilot.after("T-001", "t-001-x", sha)
        self.assertEqual(self.pilot.merged, ["T-001"])
        self.assertEqual(self.stashes(), [])
        self.assertEqual((self.s.root / "notes.txt").read_text(), "my notes\n")
        self.assertNotIn("your changes were stashed", self.titles())


TRIAGE = '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n'


class PeriodicFailures(Base):
    def round(self, extra=""):
        self.make(TRIAGE + extra)
        self.pilot.periodic(self.p.periodic["triage"])
        return [(t, m) for t, m in self.pushes if t.startswith("triage")]

    def test_a_failed_setup_is_pushed_once(self):
        self.assertEqual(self.round('setup = "echo broken build; exit 3"\n'), [
            ("triage round failed", "The triage round didn't finish: its setup `echo broken build; exit 3` failed "
                                    "(exit 3: broken build).")])

    def test_no_periodic_line_is_pushed(self):
        self.make(TRIAGE)
        self.s.write("tools/TRIAGE.md", "# Triage\nNO_LINE\n")
        self.s.publish("rules")
        self.pilot.periodic(self.p.periodic["triage"])
        self.assertEqual(self.pushes, [("triage round failed", "The triage round didn't finish: it ended without its "
                                                               "PERIODIC line; its changes were dropped.")])

    def test_a_cut_off_session_is_pushed_but_not_a_stop(self):
        self.make(TRIAGE)
        for killed, error in (("time", None), (None, "API Error: 529"), ("stop now", None)):
            self.pilot.claude = lambda *a, **k: ("", killed, error)
            self.pilot.periodic(self.p.periodic["triage"])
        self.assertEqual(self.pushes, [
            ("triage round failed", "The triage round didn't finish: it was cut off (time); its changes were dropped."),
            ("triage round failed", "The triage round didn't finish: the session failed (API Error: 529); its "
                                    "changes were dropped.")])

    def test_a_missing_prompt_is_pushed(self):
        self.make(TRIAGE.replace("tools/TRIAGE.md", "tools/GONE.md"))
        self.pilot.periodic(self.p.periodic["triage"])
        self.assertEqual(self.titles(), ["triage round failed"])
        self.assertIn("its prompt tools/GONE.md isn't on origin/main", self.pushes[0][1])

    def test_unchanged_a_round_that_finishes_pushes_only_its_result(self):
        self.assertEqual(self.round(), [("triage", "wrote a ticket")])


class Renumbering(Base):
    def collide(self, forge="git"):
        """T-001 appends L2 on its branch while main appends its own L2: the merge renumbers T-001's to L3."""
        self.make(LOG_RULE, forge=forge)
        self.on_main({"LOG.md": "- L1: start\n"})
        sha = self.branch({"LOG.md": "- L1: start\n- L2: ticket\n"})
        self.on_main({"LOG.md": "- L1: start\n- L2: main\n"})
        return sha

    def done(self):
        return [m for t, m in self.pushes if t.startswith("Done:")]

    def test_the_merged_push_names_the_renumbering(self):
        sha = self.collide()
        self.pilot.integrate("T-001", "t-001-x", sha)
        self.assertEqual(self.pilot.merged, ["T-001"])
        [msg] = self.done()
        self.assertTrue(msg.endswith("\nMerged with main: renumbered L2 → L3 (LOG.md)."), msg)

    def test_the_pr_comment_says_the_pr_keeps_the_old_numbers(self):
        sha = self.collide(forge="github")
        gh = fake_gh(self.s)
        st = json.loads(gh.read_text())
        st["prs"].append({"number": 7, "title": "T-001", "author": {"login": "me"}, "state": "OPEN",
                          "headRefName": "t-001-x", "isCrossRepository": False, "isDraft": False, "labels": []})
        gh.write_text(json.dumps(st))
        self.pilot.integrate("T-001", "t-001-x", sha)
        self.assertEqual(self.pilot.merged, ["T-001"])
        [comment] = [b for _, b in json.loads(gh.read_text())["comments"]]
        self.assertIn("renumbered L2 → L3 (LOG.md). The PR's title, body and commit messages still use the old "
                      "numbers.", comment)
        self.assertIn("renumbered L2 → L3 (LOG.md).", self.done()[0])

    def test_unchanged_a_clean_merge_says_nothing_of_numbers(self):
        self.make(LOG_RULE)
        sha = self.branch({"a.py": "built\n"})
        self.pilot.integrate("T-001", "t-001-x", sha)
        [msg] = self.done()
        self.assertNotIn("Merged with", msg)
        self.assertNotIn("renumbered", msg)


def fake_gh(s):
    gh = s.tmp / "gh.json"
    gh.write_text(json.dumps({"prs": []}))
    fake = s.bin / "gh"
    fake.write_text(f"#!/bin/sh\nexec python3 - {gh} {s.remote} \"$@\" <<'EOF'\n{test_desk.FAKE_GH}EOF\n")
    fake.chmod(0o755)
    return gh


class DeskOnMain(unittest.TestCase):
    """Desk PRs, on test_desk's fake gh; the check fails only when both a.txt and b.txt are there."""
    setUp, new_pilot, pr, commit, step, state, comments = (
        test_desk.DeskPRs.setUp, test_desk.DeskPRs.new_pilot, test_desk.DeskPRs.pr, test_desk.DeskPRs.commit,
        test_desk.DeskPRs.step, test_desk.DeskPRs.state, test_desk.DeskPRs.comments)

    def reload(self, extra=""):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('check = "test -f README && test ! -f BROKEN"',
                                               'check = "test ! -f a.txt || test ! -f b.txt"') + extra)
        self.commit({})
        self.s.git("push", "-q", "origin", "main")
        self.p = Project(self.s.root)
        self.pilot = self.new_pilot()
        self.assertTrue(self.pilot.prepare(0))
        self.pilot.desk_fix = lambda *a: None  # what's checked is the point here, not the fix run

    def test_main_moved_past_its_base_the_merge_is_checked(self):
        self.reload()
        self.pr(1, {"a.txt": "a\n"})
        self.commit({"b.txt": "b\n"}, "main moves")
        self.s.git("push", "-q", "origin", "main")
        main = self.s.git("rev-parse", "--short=7", "main")
        head = self.s.git("rev-parse", "desk-1")
        self.step()
        self.assertEqual(self.state()["prs"][0]["state"], "OPEN")
        [comment] = self.comments()
        self.assertTrue(comment.startswith(f"**windvane: check fails** at {head[:7]}\n\nThe check ran on {head[:7]} "
                                           f"merged with main at {main}: "), comment)

    def test_a_merge_that_passes_merges_and_says_so(self):
        self.reload()
        self.pr(1, {"a.txt": "a\n"})
        self.commit({"c.txt": "c\n"}, "main moves")
        self.s.git("push", "-q", "origin", "main")
        head = self.s.git("rev-parse", "desk-1")
        self.step()
        self.assertEqual(self.state()["prs"][0]["state"], "MERGED")
        self.assertIn(f"**windvane check: PASSED** on {head[:7]} merged with main at ", self.comments()[0])

    def test_a_renumbered_desk_pr_says_so_in_its_merged_push(self):
        self.reload(LOG_RULE)
        self.commit({"LOG.md": "- L1: start\n"})
        self.s.git("push", "-q", "origin", "main")
        self.pr(1, {"LOG.md": "- L1: start\n- L2: desk\n"})
        self.commit({"LOG.md": "- L1: start\n- L2: main\n"})
        self.s.git("push", "-q", "origin", "main")
        msgs = []
        desk.push = lambda title, msg="", *a, **k: msgs.append((title, msg))
        self.step()
        self.assertEqual(self.state()["prs"][0]["state"], "MERGED")
        [(title, msg)] = msgs
        self.assertEqual(title, "Desk PR #1 merged")
        self.assertTrue(msg.endswith("\nMerged with main: renumbered L2 → L3 (LOG.md)."), msg)
        self.assertIn("still use the old numbers", self.comments()[0])

    def test_unchanged_on_current_main_its_bare_head_is_checked(self):
        self.reload()
        self.pr(1, {"a.txt": "a\n"})
        head = self.s.git("rev-parse", "desk-1")
        self.step()
        self.assertEqual(self.state()["prs"][0]["state"], "MERGED")
        self.assertIn(f"**windvane check: PASSED** on {head[:7]}: ", self.comments()[0])
        self.assertNotIn("merged with", self.comments()[0])


if __name__ == "__main__":
    unittest.main()
