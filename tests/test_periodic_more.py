"""[[periodic]] sessions, the rest: where their tickets land, what a triage reads, a run by hand, scores."""
import fcntl
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, critic, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.periodic import place  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

TRIAGE = '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n'
PLAYTEST = '[[periodic]]\nname = "playtest"\nminutes = 30\nprompt = "tools/PLAYTEST.md"\nthen = "triage"\n'


class Base(unittest.TestCase):
    def make(self, config, play="PLAY", queue="# Queue\n- T-001 one\n- T-002 two\n- T-003 three\n"):
        self.s = Sandbox(config)
        self.addCleanup(self.s.close)
        for t in ("T-001", "T-002", "T-003"):
            self.s.ticket(t)
        self.s.write(".windvane/QUEUE.md", queue)
        self.s.write("tools/PLAYTEST.md", f"# Playtest\n{play}\n")
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\n")
        self.s.publish()
        self.p = Project(self.s.root)

    def pilot(self, **kw):
        pilot = driver.Pilot(self.p, args(**kw))
        pilot.deadline = now() + timedelta(hours=2)
        self.assertTrue(pilot.prepare(0))
        return pilot

    def wip(self, tid):
        """An unmerged branch for `tid`: the ticket in progress."""
        self.s.git("switch", "-q", "-c", f"{tid.lower()}-wip")
        self.s.write("wip.txt", "x\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "wip")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")

    def main_queue(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("show", "origin/main:.windvane/QUEUE.md")

    def prompts(self):
        f = self.s.tmp / "calls.jsonl"
        return [c["prompt"] for c in map(json.loads, f.read_text().splitlines() if f.exists() else [])
                if c["system"].startswith("# windvane periodic")]

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text() if f.exists() else ""


class Land(Base):
    def landed(self, land, **kw):
        self.make(TRIAGE + (f'land = "{land}"\n' if land else ""))
        self.wip("T-002")
        pilot = self.pilot(**kw)
        pilot.periodic(self.p.periodic["triage"])
        return pilot, Queue(self.p)

    def test_near_by_default_right_after_the_ticket_in_progress(self):
        _, q = self.landed("")
        self.assertEqual(q.order, ["T-001", "T-002", "T-004", "T-003"])
        self.assertIn("- T-002 two\n- T-004 found\n", self.main_queue())  # the session's bullet, moved
        self.assertIn("T-004, right after T-002", self.log())

    def test_next_at_the_top(self):
        _, q = self.landed("next")
        self.assertEqual(q.order, ["T-004", "T-001", "T-002", "T-003"])

    def test_end_after_everything(self):
        _, q = self.landed("end")
        self.assertEqual(q.order, ["T-001", "T-002", "T-003", "T-004"])
        self.assertNotIn("T-004", self.main_queue())

    def test_hold_parks_them_for_the_desk(self):
        pilot, q = self.landed("hold")
        self.assertEqual(q.status("T-004"), "parked")
        self.assertIn("- parked: T-004 (held by periodic triage", self.main_queue())
        self.assertEqual(pilot.held, ["T-004"])

    def test_ask_without_an_answer_holds(self):
        with mock.patch.object(driver.phone, "ask", return_value=True) as ask, \
                mock.patch.object(driver.phone, "answer", return_value=""):
            pilot, q = self.landed("ask", answer_wait=0.01)
        self.assertEqual(ask.call_count, 1)
        self.assertIn("triage wrote 1 ticket: T-004 found. Queue them? No answer in 0.01 min: hold.",
                      ask.call_args[0][1])  # titles: the one push is all the user decides on
        self.assertEqual(ask.call_args[0][2][2], "hold them, parked in QUEUE.md, for you at the desk")
        self.assertEqual(q.status("T-004"), "parked")
        self.assertIn("triage: no answer; the tickets are held", self.log())

    def test_ask_answered_end_queues_them_at_the_end(self):
        with mock.patch.object(driver.phone, "ask", return_value=True), \
                mock.patch.object(driver.phone, "answer", return_value="ANSWER B\tqueue them after everything else"):
            _, q = self.landed("ask")
        self.assertEqual(q.order[-1], "T-004")
        self.assertEqual(q.status("T-004"), "todo")

    def meanwhile(self, rel, text):
        """A phone question during which the user pushes `text` to `rel` on main."""
        def ask(*_):
            self.s.write(rel, text)
            self.s.publish("meanwhile")
            return True
        return mock.patch.object(driver.phone, "ask", side_effect=ask)

    def test_queue_edits_pushed_on_main_meanwhile_stay(self):
        with self.meanwhile(".windvane/QUEUE.md", "# Queue\n- T-003 three\n- T-001 one\n- parked: T-002 later\n"), \
                mock.patch.object(driver.phone, "answer", return_value=""):
            self.landed("ask", answer_wait=0.01)
        self.assertEqual(self.main_queue(), "# Queue\n- T-003 three\n- T-001 one\n- parked: T-002 later\n"
                         "- parked: T-004 (held by periodic triage: delete this line to queue it)")

    def test_an_id_taken_on_main_meanwhile_is_not_overwritten(self):
        with self.meanwhile(".windvane/tickets/T-004.md", "# T-004: the plan's\n"), \
                mock.patch.object(driver.phone, "answer", return_value="ANSWER B"):
            self.landed("ask")
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(self.s.git("show", "origin/main:.windvane/tickets/T-004.md"), "# T-004: the plan's")
        self.assertIn("triage: T-004 taken on origin/main meanwhile", self.log())
        self.assertIn("not merged; merge it by hand", self.log())

    def test_ask_with_no_phone_holds_at_once(self):
        _, q = self.landed("ask")  # the Sandbox has no phone
        self.assertEqual(q.status("T-004"), "parked")
        self.assertIn("triage: no phone; the tickets are held", self.log())

    def test_held_tickets_are_in_the_run_summary_and_not_worked(self):
        self.make(TRIAGE.replace("minutes", "every = 3\nland = \"hold\"\nminutes"))
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002", "T-003"])
        self.assertIn("Held for you (parked in QUEUE.md): found (T-004).", self.log())

    def test_a_then_entry_takes_land_from_the_one_before(self):
        self.make(PLAYTEST + 'land = "end"\n' + TRIAGE)
        self.pilot().periodic(self.p.periodic["playtest"])
        self.assertEqual(Queue(self.p).order[-1], "T-004")
        self.assertNotIn("T-004", self.main_queue())

    def test_a_bad_land_stops_at_load(self):
        from windvane.config import periodic
        with self.assertRaises(SystemExit):
            periodic([{"name": "a", "prompt": "x.md", "land": "later"}])


class Place(unittest.TestCase):
    """The QUEUE.md edit alone."""

    def setUp(self):
        self.p = mock.Mock(id_re=r"T-\d+[a-z]?", norm=lambda t: t.upper())

    def test_new_bullets_keep_the_sessions_order_and_others_stay(self):
        q = "# Queue\n- T-001 a\n- T-005 second\n- done: T-000 (old)\n- T-004 first\n"
        out = place(q, ["T-004", "T-005"], "next", None, {}, "triage", self.p)
        self.assertEqual(out, "# Queue\n- T-005 second\n- T-004 first\n- T-001 a\n- done: T-000 (old)\n")

    def test_near_adds_a_bullet_for_an_unlisted_ticket_in_progress(self):
        out = place("# Queue\n", ["T-004"], "near", "T-002", {"T-002": "two", "T-004": "four"}, "t", self.p)
        self.assertEqual(out, "# Queue\n- T-002 two\n- T-004 four\n")


class Triage(Base):
    def test_a_then_session_gets_the_notes_once_and_the_queue(self):
        self.make(PLAYTEST + TRIAGE)
        append_jsonl(self.p.state / "critic.jsonl",
                     {"ticket": "T-001", "score": 7, "critique": "Beyond this ticket:\n- retry the upload\n"})
        self.pilot().periodic(self.p.periodic["playtest"])
        play, triage = self.prompts()
        self.assertNotIn("retry the upload", play)  # only an entry another names in `then`
        self.assertIn("- (T-001) retry the upload", triage)
        self.assertIn("## Existing tickets (id, status, title)\nT-001", triage)
        self.assertEqual(critic.followups(self.p), [])  # the next plan doesn't get them again
        self.pilot().periodic(self.p.periodic["playtest"])
        self.assertNotIn("retry the upload", self.prompts()[3])

    def test_notes_stay_for_the_plan_when_the_triage_does_not_finish(self):
        self.make(PLAYTEST + TRIAGE)
        self.s.write("tools/TRIAGE.md", "# Triage\nNO_LINE\n")
        self.s.publish("triage fails")
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-001", "critique": "Beyond this ticket:\n- x\n"})
        self.pilot().periodic(self.p.periodic["playtest"])
        self.assertEqual(critic.followups(self.p), [("T-001", "x")])

    def test_each_session_reads_its_rules_from_main_not_the_tree(self):
        self.make(PLAYTEST + TRIAGE, play="EDIT_RULES")
        self.pilot().periodic(self.p.periodic["playtest"])
        self.assertIn("triage: PERIODIC: wrote a ticket", self.log())  # not the NO_LINE the playtest wrote
        self.assertIn("T-004", Queue(self.p).files)


class ByHand(Base):
    def cli(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            try:
                cli.main(["-C", str(self.s.root), *argv])
            except SystemExit as e:
                return f"{out.getvalue()}{e.code}"
        return out.getvalue()

    def test_runs_the_chain_now(self):
        self.make(PLAYTEST + 'land = "hold"\n' + TRIAGE)
        out = self.cli("periodic", "playtest")
        self.assertEqual(len(self.prompts()), 2)
        self.assertIn("Held for you (parked in QUEUE.md): T-004", out)
        self.assertEqual(Queue(self.p).status("T-004"), "parked")
        self.assertFalse(driver.locked(self.p))  # the lock goes with it

    def test_refused_while_a_run_is_busy(self):
        self.make(PLAYTEST + TRIAGE)
        self.p.state.mkdir(parents=True, exist_ok=True)
        with open(self.p.state / "lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("a run is busy", self.cli("periodic", "playtest"))
        self.assertEqual(self.prompts(), [])

    def test_an_unknown_name(self):
        self.make(PLAYTEST + TRIAGE)
        self.assertIn("no [[periodic]] entry 'audit'", self.cli("periodic", "audit"))


class Scores(Base):
    def test_scores_go_to_the_ledger_and_show_as_a_trend(self):
        self.make(TRIAGE)
        for n in (6, 8):  # its rules change on main between runs: each run reads them afresh
            self.s.write("tools/TRIAGE.md", f"# Triage\nSCORE={n}\n")
            self.s.publish(f"score {n}")
            self.pilot().periodic(self.p.periodic["triage"])
        rows = [json.loads(line) for line in (self.p.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([r["score"] for r in rows if r.get("event") == "score"], [6, 8])
        out = self.cli_stats("--periodic")
        self.assertIn("triage: 6 → 8  (2 runs, +2 since", out)
        self.assertIn("last: wrote a ticket score=8", out)
        self.assertRegex(self.cli_stats(), r"periodic:tri\s+2 ")  # two sessions; the score rows aren't runs

    def test_no_scores_yet(self):
        self.make(TRIAGE)
        self.assertIn("no periodic scores in 7 days", self.cli_stats("--periodic"))

    def cli_stats(self, *flags):
        return ByHand.cli(self, "stats", *flags)


if __name__ == "__main__":
    unittest.main()
