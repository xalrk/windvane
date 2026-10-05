"""`windvane stats` gives the gh guard's line one bracket group (T-089): `--by ticket` merges the unread alias list's
note with the ticket that tried most, `(alias list unread; most: T-007, 3)`, never two brackets in a row."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_stats_check_more import Base  # noqa: E402


class GuardLine(Base):
    def refuse(self, ticket, kind=None):
        row = {"at": time.strftime("%Y-%m-%d %H:%M"), "event": "gh-refused", "ticket": ticket, "command": "x"}
        self.add(**row, **({"kind": kind} if kind else {}))

    def guard(self, *argv):
        return [l for l in self.cli("stats", *argv).splitlines() if l.startswith("gh guard refused:")]

    def test_by_ticket_merges_the_unread_note_and_the_top_ticket_into_one_bracket(self):
        for t, kind in (("T-007", "workflow"), ("T-007", "unread"), ("T-007", "alias"), ("T-008", None)):
            self.refuse(t, kind)
        self.add(role="worker", ticket="T-007", input=1000, usd=0.1)
        self.assertEqual(self.guard("--by", "ticket"), ["gh guard refused: 2 workflow starts, 1 alias change, "
                                                        "1 unknown command (alias list unread; most: T-007, 3)"])
        self.assertEqual(self.guard(), ["gh guard refused: 2 workflow starts, 1 alias change, 1 unknown command "
                                        "(alias list unread)"])

    def test_the_top_ticket_alone_keeps_its_own_bracket(self):
        self.refuse("T-007", "alias")
        self.refuse("T-007", "workflow")
        self.assertEqual(self.guard("--by", "ticket"),
                         ["gh guard refused: 1 workflow start, 1 alias change (most: T-007, 2)"])
        self.assertEqual(self.guard(), ["gh guard refused: 1 workflow start, 1 alias change"])


if __name__ == "__main__":
    unittest.main()
