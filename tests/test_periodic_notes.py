"""[[periodic]] notes: lines a session adds at the end of an open ticket land; its final reply goes with them."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, held  # noqa: E402
from windvane.periodic import cut_lines  # noqa: E402
from test_periodic_more import TRIAGE, Base  # noqa: E402

NOTE = "\n\n## From the playtest\njump fails at x=3\n"


class Notes(Base):
    def round(self, rules, queue="# Queue\n- T-001 one\n- T-002 two\n- T-003 three\n"):
        self.make(TRIAGE, queue=queue)
        self.s.write("tools/TRIAGE.md", f"# Triage\n{rules}\n")
        self.s.publish("rules")
        self.before = self.head()
        self.pilot().periodic(self.p.periodic["triage"])

    def head(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("rev-parse", "origin/main")

    def on_main(self, tid):
        return self.s.git("show", f"origin/main:.windvane/tickets/{tid}.md") + "\n"

    def body(self):
        return self.s.git("log", "-1", "--format=%B", "origin/main")

    def test_lines_added_at_the_end_of_an_open_ticket_land(self):
        self.round("APPEND_NOTE=T-001")
        self.assertNotEqual(self.head(), self.before)  # a notes-only round lands
        self.assertEqual(self.on_main("T-001"), "# T-001: do T-001\nDepends on: nothing." + NOTE)
        self.assertIn("Periodic: notes on T-001", self.body())
        self.assertIn("Periodic: notes on T-001 (no new tickets) on main", self.log())
        self.assertEqual(self.s.git("show", "origin/main:README"), "hi")  # the stray edit is still dropped

    def test_notes_and_a_new_ticket_land_together(self):
        self.round("WRITE_TICKET\nAPPEND_NOTE=T-002")
        self.assertIn("jump fails at x=3", self.on_main("T-002"))
        self.assertIn("# T-004: found", self.on_main("T-004"))
        self.assertIn("Periodic: 1 new ticket, notes on T-002", self.body())

    def test_the_commit_body_holds_the_final_reply(self):
        self.round("APPEND_NOTE=T-001")
        body = self.body()
        self.assertIn("## Its final reply\nplayed\ncovered: the jump by T-001\nnot ticketed: the fog (by design)\n"
                      "PERIODIC: wrote a ticket", body)

    def test_a_long_reply_is_cut_to_80_lines_keeping_its_last(self):
        self.round("APPEND_NOTE=T-001\nREPLY_LINES=200")
        reply = self.body().split("## Its final reply\n", 1)[1].strip().splitlines()
        self.assertEqual(len(reply), 80)
        self.assertEqual(reply[-2:], ["… (125 lines cut)", "PERIODIC: wrote a ticket"])

    def test_a_notes_only_round_that_does_not_merge_says_so(self):
        with mock.patch.object(held, "land", return_value=(False, "periodic-x")), \
                mock.patch("windvane.periodic.push") as push:
            self.round("APPEND_NOTE=T-001")
        self.assertEqual(push.call_args[0][:2], ("periodic change not merged",
                                                 "Periodic: notes on T-001: waits on periodic-x; merge it by hand."))

    def test_an_edit_in_the_middle_is_dropped_and_logged(self):
        self.round("EDIT_NOTE=T-001")
        self.assertEqual(self.head(), self.before)  # nothing else to land
        self.assertNotIn("x=3", self.on_main("T-001"))
        self.assertIn("triage: T-001 changed, not only lines added at its end: that change is dropped", self.log())

    def test_a_deleted_ticket_is_kept_and_logged(self):
        self.round("DELETE_NOTE=T-003\nAPPEND_NOTE=T-001")
        self.assertIn("# T-003", self.on_main("T-003"))
        self.assertIn("x=3", self.on_main("T-001"))
        self.assertIn("triage: T-003 deleted: that change is dropped", self.log())

    def test_an_append_to_a_merged_ticket_is_dropped_and_logged(self):
        self.make(TRIAGE)
        self.s.write("tools/TRIAGE.md", "# Triage\nAPPEND_NOTE=T-001\n")
        self.s.publish("rules")
        self.s.git("switch", "-q", "-c", "t-001-x")
        self.s.write("x.txt", "x\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "work")
        self.s.git("switch", "-q", "main")
        self.s.git("merge", "-q", "--no-ff", "-m", "Merge T-001: one (branch t-001-x)", "t-001-x")
        self.s.git("push", "-q", "origin", "main")
        before = self.head()
        self.pilot().periodic(self.p.periodic["triage"])
        self.assertEqual(self.head(), before)
        self.assertIn("triage: T-001 is done on origin/main: its notes are dropped", self.log())
        self.assertIn("triage: nothing left to land", self.log())

    def test_an_append_to_a_done_ticket_is_dropped(self):
        self.round("APPEND_NOTE=T-002", queue="# Queue\n- T-001 one\n- done: T-002 (folded into T-001)\n")
        self.assertNotIn("x=3", self.on_main("T-002"))
        self.assertIn("triage: T-002 is done on origin/main: its notes are dropped", self.log())

    def test_on_github_the_pr_body_holds_the_reply(self):
        self.make(TRIAGE)
        self.s.write("tools/TRIAGE.md", "# Triage\nAPPEND_NOTE=T-001\n")
        self.s.publish("rules")
        self.p.cfg["forge"] = "github"
        net = mock.MagicMock()
        net.sh.return_value = "https://github.com/o/r/pull/5"
        net.call.return_value.returncode = net.git.return_value.returncode = 0
        with mock.patch.object(driver, "net", net), mock.patch.object(held, "net", net):
            self.pilot().periodic(self.p.periodic["triage"])
        create = next(c.args for c in net.sh.call_args_list if c.args[1:4] == ("gh", "pr", "create"))
        body = create[create.index("--body") + 1]
        self.assertIn("not ticketed: the fog (by design)", body)
        self.assertEqual(create[create.index("--title") + 1], "Periodic: notes on T-001")


class Cut(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(cut_lines("a\nb", 80), "a\nb")

    def test_long_text_keeps_head_and_last_line(self):
        self.assertEqual(cut_lines("\n".join(map(str, range(10))), 4), "0\n1\n… (7 lines cut)\n9")


if __name__ == "__main__":
    unittest.main()
