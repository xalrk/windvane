"""`shared_notes`: a notes file every ticket appends its findings to doesn't make tickets wait for each other, and a
conflict in it that only adds sections on both sides merges without a worker run; an edit, or a conflict in code
too, still goes back to the worker."""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, renumber  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox, tmpdir  # noqa: E402
from test_parallel import args  # noqa: E402
from test_renumber import ENV  # noqa: E402

NOTES = "# Notes\n\nWhat each ticket found.\n"


class Run(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        os.environ["FAKE_WORK_S"] = "1.5"  # side by side shows in the start times, and both fork the same main

    def tearDown(self):
        self.s.close()

    def run_pilot(self, shared=True, touches=("a.py, NOTES.md", "b.py, NOTES.md"), run=True):
        conf = self.s.root / ".windvane/config.toml"
        top = (f'shared_notes = ["NOTES.md"]\n' * shared
               + f"check = \"grep -c '^## T-' NOTES.md >> {self.s.tmp}/sections.txt; test -f README\"\n")
        conf.write_text(top + conf.read_text().replace('check = "test -f README"\n', ""))
        self.s.write("NOTES.md", NOTES)
        self.s.ticket("T-001", touches=touches[0])
        self.s.ticket("T-002", touches=touches[1])
        self.s.publish()
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args())
        if not run:
            return pilot
        pilot.run(now() + timedelta(minutes=40))
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        self.log = (self.p.state / "autopilot.log").read_text()
        return pilot

    def calls(self):
        return [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]

    def starts(self):
        out = {}
        for c in self.calls():
            if c["system"] == "span":
                tid, _, at = c["prompt"].split()
                out.setdefault(tid, float(at))
        return out

    def workers(self):
        return [c for c in self.calls() if c["system"].startswith("# windvane worker")]

    def test_appended_sections_run_side_by_side_and_merge_on_their_own(self):
        os.environ["FAKE_NOTES"] = "append"
        pilot = self.run_pilot()
        at = self.starts()
        self.assertLess(abs(at["T-002"] - at["T-001"]), 1.0)  # both started before either finished
        self.assertNotIn("waits: overlaps", self.log)
        self.assertEqual(len(self.workers()), 2)  # no second worker run for the conflict
        first, second = pilot.merged
        self.s.git("pull", "-q", "origin", "main")
        self.assertEqual((self.s.root / "NOTES.md").read_text(),
                         NOTES + f"\n## {first}\nFound things.\n\n## {second}\nFound things.\n")
        self.assertIn(f"{second}: conflicted with main only in appended entries: NOTES.md: kept main's, appended 1 "
                      "section; resolved without a worker run", self.log)
        self.assertIn("NOTES.md: kept main's, appended 1 section", self.s.git("log", "-1", "--format=%B", "origin/main"))
        self.assertIn("2", (self.s.tmp / "sections.txt").read_text().split())  # the check ran on the merged tree
        self.assertNotIn("changed outside its Touches line: NOTES.md", self.log)
        self.assertIn("Shared notes (NOTES.md): add to them as a new section", self.workers()[0]["prompt"])

    def test_without_shared_notes_they_run_one_after_the_other(self):
        os.environ["FAKE_NOTES"] = "append"
        self.run_pilot(shared=False)
        at = self.starts()
        self.assertGreater(at["T-002"] - at["T-001"], 1.4)  # only after T-001 merged
        self.assertIn("T-002 waits: overlaps T-001 on NOTES.md", self.log)
        self.assertNotIn("Shared notes", self.workers()[0]["prompt"])

    def test_notes_changed_off_the_touches_line_dont_hold_a_ticket_back(self):
        pilot = self.run_pilot(touches=("a.py", "b.py, NOTES.md, c.py"), run=False)
        pilot.queue, pilot.running = Queue(self.p), {"T-001": 0}
        changed = driver.changed
        driver.changed = lambda *a: {"a.py", "NOTES.md", "c.py"}  # T-001's branch so far
        try:
            extra = pilot.outside(["T-001"])
        finally:
            driver.changed = changed
        self.assertEqual(extra, {"T-001": ["c.py"]})
        self.assertEqual(pilot.clash("T-002", ["T-001"], extra), "overlaps T-001 on c.py (changed, not on its Touches line)")
        self.assertEqual(pilot.clash("T-002", ["T-001"], {"T-001": []}), "")

    def test_an_edited_line_goes_back_to_the_worker(self):
        os.environ["FAKE_NOTES"] = "edit"
        pilot = self.run_pilot()
        second = pilot.merged[1]
        self.assertIn(f"{second}: conflicts with main; back to the worker", self.log)
        self.assertEqual(len(self.workers()), 3)
        self.assertNotIn("appended entries", self.log)

    def test_a_conflict_in_code_too_goes_back_to_the_worker(self):
        os.environ["FAKE_NOTES"] = "append"
        os.environ["FAKE_CONFLICT"] = "1"  # every worker also rewrites shared.txt
        pilot = self.run_pilot()
        self.assertIn(f"{pilot.merged[1]}: conflicts with main; back to the worker", self.log)
        self.assertEqual(len(self.workers()), 3)

    def test_a_slot_ending_during_the_pick_doesnt_end_the_run(self):
        """The pick holds back a running ticket; when its slot ends (bounced) before the loop looks again, the run
        picks again rather than ending with "queue empty" and the bounced ticket left behind."""
        os.environ["FAKE_NOTES"] = "append"
        os.environ["FAKE_CONFLICT"] = "1"
        pick = driver.Pilot.pick

        def late(pilot, free):
            got = pick(pilot, free)
            until = time.time() + 30
            while got[0] is None and pilot.running and time.time() < until:  # every slot ends meanwhile
                time.sleep(0.05)
            return got
        driver.Pilot.pick = late
        try:
            pilot = self.run_pilot()
        finally:
            driver.Pilot.pick = pick
        self.assertEqual(len(pilot.merged), 2)


class Resolve(unittest.TestCase):
    """resolve() on a repo where main and a branch each change NOTES.md from a common base."""

    def setUp(self):
        self.dir = tmpdir(self, "windvane-notes-")
        self.git("init", "-q", "-b", "main")
        self.commit({"NOTES.md": NOTES, "docs/NOTES.md": NOTES})
        self.git("branch", "topic")

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.dir, capture_output=True, text=True,
                              env={**os.environ, **ENV}).stdout

    def commit(self, files, on=None):
        if on:
            self.git("switch", "-q", on)
        for f, text in files.items():
            (self.dir / f).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / f).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "change")
        self.git("switch", "-q", "main")

    def merge(self, shared=("NOTES.md",)):
        self.assertNotEqual(subprocess.run(["git", "merge", "-q", "--no-ff", "-m", "m", "topic"], cwd=self.dir,
                                           capture_output=True, env={**os.environ, **ENV}).returncode, 0)
        return renumber.resolve(self.dir, [], list(shared))

    def test_sections_count_by_heading_or_the_hunk_itself(self):
        self.commit({"NOTES.md": NOTES + "\n## main\nx\n"}, on="main")
        self.commit({"NOTES.md": NOTES + "Loose text first.\n\n## one\n```\n# not a heading\n```\n### two\ny\n"},
                    on="topic")
        self.assertEqual(self.merge(), ["NOTES.md: kept main's, appended 3 sections"])
        self.assertEqual((self.dir / "NOTES.md").read_text(), NOTES + "\n## main\nx\nLoose text first.\n\n## one\n"
                         "```\n# not a heading\n```\n### two\ny\n")
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U"), "")

    def test_globs_and_directories_match(self):
        for pattern in ("*NOTES.md", "docs/"):
            self.assertTrue(renumber.noted("docs/NOTES.md", [pattern]), pattern)
        self.assertFalse(renumber.noted("docs/NOTES.md", ["NOTES.md"]))
        self.commit({"docs/NOTES.md": NOTES + "main\n"}, on="main")
        self.commit({"docs/NOTES.md": NOTES + "topic\n"}, on="topic")
        self.assertEqual(self.merge(["docs/*.md"]), ["docs/NOTES.md: kept main's, appended 1 section"])

    def test_a_deleted_line_is_refused(self):
        self.commit({"NOTES.md": NOTES + "main\n"}, on="main")
        self.commit({"NOTES.md": NOTES.replace("What each ticket found.\n", "") + "topic\n"}, on="topic")
        self.assertIsNone(self.merge())

    def test_a_file_not_named_is_refused(self):
        self.commit({"NOTES.md": NOTES + "main\n"}, on="main")
        self.commit({"NOTES.md": NOTES + "topic\n"}, on="topic")
        self.assertIsNone(self.merge(["OTHER.md"]))

    def test_said_names_the_notes_and_the_renumbering(self):
        self.assertEqual(renumber.said(["NOTES.md: kept main's, appended 2 sections", "D-5 → D-6 (d.md)"]),
                         "NOTES.md: kept main's, appended 2 sections; renumbered D-5 → D-6 (d.md)")
        self.assertEqual(renumber.old_numbers(["NOTES.md: kept main's, appended 2 sections"]), "")
        self.assertEqual(renumber.said([]), "no number collided")

    def test_a_bad_setting_stops_at_load(self):
        for bad in ("NOTES.md", [""], [3]):
            with self.assertRaises(SystemExit):
                renumber.notes(bad)


if __name__ == "__main__":
    unittest.main()
