"""Every path that writes a ticket (`windvane new`, a plan, a periodic session, a desk answer's follow-up) takes its id
from one picker, so none can hand out an id an open plan or a checkout's own files already hold."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, planner, preflight  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Stop(Exception):
    """Ends a path once it has picked its ids: nothing after that matters here."""


class TicketIds(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())  # `-C` changes into the sandbox, which close() removes
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)

    def open_plan(self, tid):
        """An unmerged `plan-*` branch on the remote holding `tid`, as a plan PR still open leaves it."""
        self.s.git("switch", "-q", "-c", "plan-20261019-120000")
        self.s.ticket(tid)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Plan: 1 new ticket")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")

    def only_in(self, cd, tid):
        """A ticket file in checkout `cd` that's on no branch."""
        (cd / ".windvane/tickets").mkdir(parents=True, exist_ok=True)
        (cd / f".windvane/tickets/{tid}.md").write_text(f"# {tid}: only here\n")

    def test_new_skips_an_open_plans_ids(self):
        self.open_plan("T-002")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "new", "next"])
        self.assertEqual(out.getvalue().strip(), str(self.s.root / ".windvane/tickets/T-003.md"))
        self.assertFalse((self.s.root / ".windvane/tickets/T-002.md").exists())

    def test_new_asks_no_forge(self):
        """On github, `new` doesn't look up the open plan's PR (each lookup is a network call): it keeps its ids."""
        (self.s.bin / "gh").write_text(f"#!/bin/sh\necho \"$*\" >> {self.s.tmp}/gh.log\necho '[]'\n")
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")
        self.open_plan("T-002")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), "new", "next"])
        self.assertTrue(out.getvalue().strip().endswith("T-003.md"))
        self.assertFalse((self.s.tmp / "gh.log").exists())

    def test_new_that_loses_a_race_takes_the_id_after_the_winners(self):
        (self.s.root / ".windvane/tickets/T-002.md").write_text("# T-002: the winner\n")  # made after it looked
        out = io.StringIO()
        with mock.patch.object(planner, "free_id", side_effect=["T-002", "T-003"]), contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "new", "loser"])
        self.assertEqual(out.getvalue().strip(), str(self.s.root / ".windvane/tickets/T-003.md"))
        self.assertEqual((self.s.root / ".windvane/tickets/T-002.md").read_text(), "# T-002: the winner\n")

    def test_plan_skips_a_file_only_in_its_checkout(self):
        real, prompts = planner.clean_detach, []

        def detach(p, cd, rev):  # the planner's tree, with a ticket file main doesn't have
            ok = real(p, cd, rev)
            self.only_in(cd, "T-004")
            return ok

        def claude(*a, **k):
            prompts.append(next(x for x in [*a, *k.values()] if isinstance(x, str) and "## Ids for new" in x))
            raise Stop

        with mock.patch.object(planner, "clean_detach", detach), mock.patch.object(planner, "run_claude", claude), \
                self.assertRaises(Stop):
            planner.plan(self.p, "more", use_followups=False, max_new=2)
        self.assertIn("## Ids for new tickets, in order (at most 2)\nT-005, T-006", prompts[0])

    def test_preflight_skips_a_file_only_in_its_checkout(self):
        real, made = preflight.clean_detach, []

        def detach(p, cd, rev):
            ok = real(p, cd, rev)
            self.only_in(cd, "T-007")
            return ok

        def followup(p, cd, tid, *a):
            made.append(tid)
            if len(made) == 2:
                raise Stop

        x = {"id": "q1", "kind": "provisional", "ticket": "T-001", "built": "A", "question": "which?",
             "options": {"A": "one", "B": "two"}}
        with mock.patch.object(preflight, "clean_detach", detach), \
                mock.patch.object(preflight, "followup", followup), self.assertRaises(Stop):
            preflight.record(self.p, [(x, "B", None), (dict(x, id="q2"), "B", None)])
        self.assertEqual(made, ["T-008", "T-009"])  # and the second skips the first's


class PeriodicIds(unittest.TestCase):
    def test_a_chained_session_skips_the_ticket_the_one_before_wrote(self):
        import test_periodic  # the module, not its class: a class imported here would run all its tests again
        triage = '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n'
        t = test_periodic.Periodic()
        t.make(test_periodic.PLAYTEST + 'then = "triage"\n' + triage, tickets=2, rules="WRITE_TICKET")
        self.addCleanup(t.doCleanups)
        t.s.write("tools/TRIAGE.md", "# Triage\nwrites nothing\n")  # or each round's ticket brings another round
        t.s.publish()
        t.run_pilot()
        runs = [v for k, v in t.sessions() if k == "periodic"]
        self.assertIn("## Ids for new tickets, in order (at most 6)\nT-003, ", runs[0])
        self.assertIn("## Ids for new tickets, in order (at most 6)\nT-004, ", runs[1])  # T-003.md is in its tree


class RacingNew(unittest.TestCase):
    def test_two_news_at_once_get_two_ids_or_one_refusal(self):
        s = Sandbox()
        self.addCleanup(s.close)
        tickets = s.root / ".windvane/tickets"
        for round_ in range(4):
            procs = [subprocess.Popen(["windvane", "-C", str(s.root), "new", f"r{round_} p{i}"], text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
            outs = [(i, *pr.communicate(timeout=60), pr.returncode) for i, pr in enumerate(procs)]
            made = [(i, Path(out.strip())) for i, out, _, code in outs if code == 0]
            refused = [err for _, _, err, code in outs if code]
            self.assertEqual(len({f for _, f in made}), len(made), outs)  # never the same file twice
            self.assertLessEqual(len(refused), 1, outs)
            for err in refused:
                self.assertIn("already exists: not overwritten. Run `windvane new` again", err)
            for i, f in made:  # each file holds what its own process wrote
                self.assertIn(f"# {f.stem}: r{round_} p{i}\n", f.read_text())
        self.assertEqual(len(list(tickets.glob("*.md"))),
                         len({f.read_text() for f in tickets.glob("*.md")}))


if __name__ == "__main__":
    unittest.main()
