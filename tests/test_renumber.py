"""Conflicts in append-only numbered files: main's entries kept, the branch's appended and renumbered past them, and
only the branch's own references follow; anything else is refused. And a run where two tickets each append D-5."""
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, renumber  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox, tmpdir  # noqa: E402
from test_parallel import args  # noqa: E402

RULE = {"file": "docs/DECISIONS.md", "entry": r"^\| D-(\d+) \|", "ref": "D-{n}"}
LOG = "# Decisions\n\n| id | what |\n|---|---|\n| D-1 | one |\n| D-2 | two |\n"
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class Resolve(unittest.TestCase):
    """A repo where main and a branch each change files from a common base, merged into main by hand."""

    def setUp(self):
        self.dir = tmpdir(self, "windvane-renumber-")
        self.git("init", "-q", "-b", "main")
        self.commit({"docs/DECISIONS.md": LOG, "notes.md": "about D-1\n"})
        self.git("branch", "topic")

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.dir, capture_output=True, text=True,
                              env={**os.environ, **ENV}).stdout

    def read(self, f):
        return (self.dir / f).read_text()

    def commit(self, files, on=None):
        if on:
            self.git("switch", "-q", on)
        for f, text in files.items():
            (self.dir / f).parent.mkdir(parents=True, exist_ok=True)
            (self.dir / f).write_bytes(text) if isinstance(text, bytes) else (self.dir / f).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "change")
        self.git("switch", "-q", "main")

    def merge(self, rule=RULE):
        """Merges topic into main, then resolves: the renumbering, or None (the merge is left to abort)."""
        self.assertNotEqual(subprocess.run(["git", "merge", "-q", "--no-ff", "-m", "m", "topic"], cwd=self.dir,
                                           capture_output=True, env={**os.environ, **ENV}).returncode, 0)
        return renumber.resolve(self.dir, renumber.rules([rule]))

    def test_rows_only_conflict_keeps_mains_rows_and_renumbers_the_branchs_after_them(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's three |\n| D-4 | main's four |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | topic's three, after D-2 |\n| D-4 | topic's four |\n"},
                    on="topic")
        self.assertEqual(self.merge(), ["D-3 → D-5 (docs/DECISIONS.md)", "D-4 → D-6 (docs/DECISIONS.md)"])
        self.assertEqual(self.read("docs/DECISIONS.md"), LOG + "| D-3 | main's three |\n| D-4 | main's four |\n"
                         "| D-5 | topic's three, after D-2 |\n| D-6 | topic's four |\n")
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U"), "")  # staged, ready to commit

    def test_the_branchs_references_follow_and_mains_stay(self):
        notes = "about D-1\n\nmore\n\nend\n"
        self.commit({"notes.md": notes}, on="main")
        self.git("branch", "-f", "topic", "main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n", "notes.md": "main: see D-3\n" + notes,
                     "main.py": "# D-3 says so\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | topic's, unlike D-30 |\n",
                     "topic.py": "# D-3 says so; D-2 too\n", "notes.md": notes + "topic: see D-3\n"}, on="topic")
        self.assertEqual(self.merge(), ["D-3 → D-4 (docs/DECISIONS.md)"])
        self.assertIn("| D-4 | topic's, unlike D-30 |\n", self.read("docs/DECISIONS.md"))
        self.assertEqual(self.read("topic.py"), "# D-4 says so; D-2 too\n")  # D-2 is no branch entry
        self.assertEqual(self.read("main.py"), "# D-3 says so\n")  # main's line keeps main's D-3
        self.assertEqual(self.read("notes.md"), "main: see D-3\n" + notes + "topic: see D-4\n")

    def test_no_collision_keeps_both_rows_as_they_are(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-4 | topic's |\n"}, on="topic")
        self.assertEqual(self.merge(), [])
        self.assertTrue(self.read("docs/DECISIONS.md").endswith("| D-3 | main's |\n| D-4 | topic's |\n"))

    def test_multi_line_entries_with_padded_numbers(self):
        rule = {"file": "docs/adr.md", "entry": r"^## ADR-(\d+):", "ref": "ADR-{n}"}
        base = "# ADRs\n\n## ADR-001: start\nwhy\n"
        self.commit({"docs/adr.md": base}, on="main")
        self.git("branch", "-f", "topic", "main")
        self.commit({"docs/adr.md": base + "\n## ADR-002: main's\nbecause\n"}, on="main")
        self.commit({"docs/adr.md": base + "\n## ADR-002: topic's\nsee ADR-001, ADR-002\n"}, on="topic")
        self.assertEqual(self.merge(rule), ["ADR-2 → ADR-3 (docs/adr.md)"])
        self.assertEqual(self.read("docs/adr.md"), base + "\n## ADR-002: main's\nbecause\n"
                         "\n## ADR-003: topic's\nsee ADR-001, ADR-003\n")

    def test_crlf_files_keep_their_line_endings(self):
        crlf = LOG.replace("\n", "\r\n")
        self.commit({"docs/DECISIONS.md": crlf, "win notes.txt": "x\r\ny\r\n"}, on="main")
        self.git("branch", "-f", "topic", "main")
        self.commit({"docs/DECISIONS.md": crlf + "| D-3 | main's |\r\n"}, on="main")
        self.commit({"docs/DECISIONS.md": crlf + "| D-3 | topic's\x0cpage |\r\n",
                     "win notes.txt": "x\r\ny\r\nsee D-3\x0c and D-3\r\n"}, on="topic")
        self.assertEqual(self.merge(), ["D-3 → D-4 (docs/DECISIONS.md)"])
        self.assertEqual((self.dir / "win notes.txt").read_bytes(), b"x\r\ny\r\nsee D-4\x0c and D-4\r\n")
        self.assertEqual((self.dir / "docs/DECISIONS.md").read_bytes(),
                         (crlf + "| D-3 | main's |\r\n| D-4 | topic's\x0cpage |\r\n").encode())

    def test_lines_keep_their_own_edge_spaces(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | topic's |\n", "zz.md": "  see D-3  \n"}, on="topic")
        self.assertEqual(self.merge(), ["D-3 → D-4 (docs/DECISIONS.md)"])
        self.assertEqual(self.read("zz.md"), "  see D-4  \n")  # the diff's last line, spaces and all

    def test_non_ascii_paths_are_followed(self):
        rule = {**RULE, "file": "docs/décisions.md"}
        self.commit({"docs/décisions.md": LOG}, on="main")
        self.git("branch", "-f", "topic", "main")
        self.commit({"docs/décisions.md": LOG + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/décisions.md": LOG + "| D-3 | topic's |\n", "notes é.md": "see D-3\n"}, on="topic")
        self.assertEqual(self.merge(rule), ["D-3 → D-4 (docs/décisions.md)"])
        self.assertEqual(self.read("notes é.md"), "see D-4\n")

    def test_a_path_git_still_quotes_is_refused(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | topic's |\n", 'say "D-3".md': "see D-3\n"}, on="topic")
        self.assertIsNone(self.merge())

    def test_mains_last_line_without_a_newline_gets_the_files_own(self):
        crlf = LOG.replace("\n", "\r\n")
        self.commit({"docs/DECISIONS.md": crlf}, on="main")
        self.git("branch", "-f", "topic", "main")
        self.commit({"docs/DECISIONS.md": crlf + "| D-3 | main's |"}, on="main")
        self.commit({"docs/DECISIONS.md": crlf + "| D-3 | topic's |\r\n"}, on="topic")
        self.assertEqual(self.merge(), ["D-3 → D-4 (docs/DECISIONS.md)"])
        self.assertEqual((self.dir / "docs/DECISIONS.md").read_bytes(),
                         (crlf + "| D-3 | main's |\r\n| D-4 | topic's |\r\n").encode())

    def test_a_file_that_isnt_utf8_is_refused_not_raised(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | topic's |\n", "x.txt": "caf\xe9 D-3\n".encode("latin-1")},
                    on="topic")
        self.assertIsNone(self.merge())

    def test_a_changed_entry_is_refused(self):
        self.commit({"docs/DECISIONS.md": LOG.replace("two", "2") + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG.replace("two", "II") + "| D-3 | topic's |\n"}, on="topic")
        self.assertIsNone(self.merge())

    def test_a_conflict_in_another_file_too_is_refused(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n", "notes.md": "main\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | topic's |\n", "notes.md": "topic\n"}, on="topic")
        self.assertIsNone(self.merge())
        self.assertIn("<<<<<<<", self.read("notes.md"))  # left as git made it, for the caller's abort

    def test_text_that_isnt_an_entry_is_refused(self):
        self.commit({"docs/DECISIONS.md": LOG + "| D-3 | main's |\n"}, on="main")
        self.commit({"docs/DECISIONS.md": LOG + "Some prose.\n| D-3 | topic's |\n"}, on="topic")
        self.assertIsNone(self.merge())

    def test_bad_rules_stop_at_load(self):
        for bad in ({"file": "x", "entry": "D-\\d+", "ref": "D-{n}"}, {"file": "x", "entry": "(", "ref": "D-{n}"},
                    {"file": "x", "entry": "D-(\\d+)", "ref": "D-"}, {"entry": "D-(\\d+)", "ref": "D-{n}"}):
            with self.assertRaises(SystemExit):
                renumber.rules([bad])


class TwoTickets(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox("\n[[append_only]]\nfile = \"docs/DECISIONS.md\"\nentry = '^\\| D-(\\d+) \\|'\n"
                         "ref = 'D-{n}'\n")
        self.s.write("docs/DECISIONS.md", "| id | what |\n|---|---|\n| D-1 | a |\n| D-2 | b |\n| D-3 | c |\n"
                     "| D-4 | d |\n")
        self.s.ticket("T-001", touches="a.py")
        self.s.ticket("T-002", touches="b.py")
        self.s.publish()
        os.environ["FAKE_DECISION"] = "1"  # each worker appends the next decision (D-5) and refers to it
        os.environ["FAKE_WORK_S"] = "1.5"  # side by side: both branch off main before either merges

    def tearDown(self):
        self.s.close()

    def test_two_tickets_each_appending_d5_merge_without_a_second_worker_run(self):
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args())
        pilot.run(now() + timedelta(minutes=40))
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        rows = [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r["system"].startswith("# windvane worker")]), 2)
        self.s.git("pull", "-q", "origin", "main")
        first, second = sorted(["T-001", "T-002"], key=pilot.merged.index)
        self.assertTrue(self.s.root.joinpath("docs/DECISIONS.md").read_text()
                        .endswith(f"| D-4 | d |\n| D-5 | {first} |\n| D-6 | {second} |\n"))
        self.assertEqual(self.s.root.joinpath(f"{first}.txt").read_text(), "built\nsee D-5\n")
        self.assertEqual(self.s.root.joinpath(f"{second}.txt").read_text(), "built\nsee D-6\n")
        self.assertIn(f"{second}: conflicted with main only in appended entries: renumbered D-5 → D-6 "
                      "(docs/DECISIONS.md); resolved without a worker run", (p.state / "autopilot.log").read_text())
        self.assertIn("D-5 → D-6", self.s.git("log", "-3", "--format=%B", "origin/main"))


if __name__ == "__main__":
    unittest.main()
