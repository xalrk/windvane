"""[[periodic]] sessions: a project's own session (a playtest, then a triage) every N merges, between tickets."""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

PLAYTEST = '[[periodic]]\nname = "playtest"\nevery = 2\nminutes = 30\nprompt = "tools/PLAYTEST.md"\n'


class Periodic(unittest.TestCase):
    def make(self, config="", tickets=3, rules="PLAY"):
        self.s = Sandbox(config)
        for i in range(1, tickets + 1):
            self.s.ticket(f"T-00{i}", f"T-00{i - 1}" if i > 1 else "")
        self.s.write("tools/PLAYTEST.md", f"# Playtest\n{rules}\n")
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\n")
        self.s.publish()
        self.p = Project(self.s.root)
        self.addCleanup(self.s.close)

    def run_pilot(self, hours=1):
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=hours))
        return pilot

    def sessions(self):
        """Each session in order: the ticket it worked or reviewed, or `periodic` (with its prompt)."""
        f = self.s.tmp / "calls.jsonl"
        out = []
        for c in map(json.loads, f.read_text().splitlines() if f.exists() else []):
            if c["system"].startswith("# windvane periodic"):
                out.append(("periodic", c["prompt"]))
            elif c["system"].startswith("# windvane worker"):
                out.append(("work", c["prompt"].split()[1].rstrip(".")))
        return out

    def log(self):
        return (self.p.state / "autopilot.log").read_text() if (self.p.state / "autopilot.log").exists() else ""

    def test_runs_after_every_n_merges_and_not_before(self):
        self.make(PLAYTEST, tickets=3)
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001", "T-002", "T-003"])
        order = [k if k == "periodic" else v for k, v in self.sessions()]
        self.assertEqual(order, ["T-001", "T-002", "periodic", "T-003"])  # once, after the 2nd merge only
        self.assertIn("playtest: PERIODIC: wrote a ticket", self.log())
        roles = [json.loads(l)["role"] for l in (self.p.state / "ledger.jsonl").read_text().splitlines()]
        self.assertIn("periodic:playtest", roles)

    def test_skipped_when_the_deadline_is_nearer_than_its_minutes(self):
        self.make(PLAYTEST.replace("minutes = 30", "minutes = 90"), tickets=2)
        pilot = self.run_pilot(hours=1)
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertNotIn("periodic", [k for k, _ in self.sessions()])
        self.assertIn("playtest: skipped: 90 min don't fit before the deadline", self.log())

    def test_failed_setup_skips_the_round(self):
        self.make(PLAYTEST + 'setup = "echo broken build; exit 3"\n', tickets=2)
        self.run_pilot()
        self.assertNotIn("periodic", [k for k, _ in self.sessions()])
        self.assertIn("playtest: skipped this round: setup failed (exit 3: broken build)", self.log())

    def test_a_hung_setup_ends_at_stop_now(self):
        import threading
        import time
        self.make(PLAYTEST + 'setup = "touch started; read x; sleep 60"\n', tickets=2)  # waits on stdin, then hangs

        def stop_when_started():
            while not (self.p.ci_dir / "started").exists():
                time.sleep(0.1)
            (self.p.state / "STOP").write_text("now")
        threading.Thread(target=stop_when_started, daemon=True).start()
        began = time.time()
        pilot = self.run_pilot()
        self.assertLess(time.time() - began, 30)
        self.assertNotIn("periodic", [k for k, _ in self.sessions()])
        self.assertIn("playtest: skipped this round: setup failed (stopped)", self.log())
        self.assertIn("stopped now", pilot.stop_reason)

    def test_setup_runs_on_current_main_first(self):
        self.make(PLAYTEST + 'setup = "test -f T-002.txt && touch built.flag"\n', tickets=2)
        self.run_pilot()
        self.assertIn("periodic", [k for k, _ in self.sessions()])
        self.assertFalse((self.p.ci_dir / "built.flag").exists())  # the tree is cleaned afterwards

    def test_then_chains_with_the_first_output_and_its_files(self):
        triage = '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n'
        self.make(PLAYTEST + 'then = "triage"\n' + triage, tickets=2, rules="WRITE_REPORT")
        self.run_pilot()
        runs = [v for k, v in self.sessions() if k == "periodic"]
        self.assertEqual(len(runs), 2)  # triage has no `every`: it runs only after the playtest
        self.assertIn("PERIODIC: see report.txt", runs[1])
        self.assertIn("the jump is broken", runs[1])  # the file the playtest named, inlined
        self.assertIn("T-003", runs[1])  # the triage gets the next free ids
        self.assertIn("triage: PERIODIC: wrote a ticket", self.log())

    def test_tickets_land_on_main_and_enter_the_queue_other_edits_dropped(self):
        self.make(PLAYTEST.replace("PLAYTEST", "TRIAGE"), tickets=2)
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001", "T-002", "T-003"])  # the session's ticket was worked this run
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(self.s.git("show", "origin/main:README"), "hi")  # its stray edit is gone
        queue = self.s.git("show", "origin/main:.windvane/QUEUE.md")
        self.assertEqual(queue.splitlines()[:2], ["# Queue", "- T-003 found"])  # lines only added, none dropped
        self.assertIn("T-003", Queue(self.p).files)
        self.assertIn("1 other changed path will be dropped", self.log())
        self.assertEqual(self.s.git("status", "--porcelain", cwd=self.p.ci_dir), "")

    def test_runs_in_the_checkout_without_isolate(self):
        self.make(PLAYTEST.replace("PLAYTEST", "TRIAGE"), tickets=2)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "git"\nisolate = false'))
        self.s.publish("isolate off")
        self.p = Project(self.s.root)
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001", "T-002", "T-003"])
        self.assertEqual(self.s.git("status", "--porcelain"), "")  # the stray README edit is gone here too

    def test_no_periodic_line_lands_nothing(self):
        self.make(PLAYTEST.replace("PLAYTEST", "TRIAGE"), tickets=2, rules="NO_LINE")
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\nNO_LINE\n")
        self.s.publish("no line")
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertIn("no PERIODIC line", self.log())

    def test_no_periodic_config_changes_nothing(self):
        self.make("", tickets=3)
        self.assertEqual(self.p.periodic, {})
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001", "T-002", "T-003"])
        self.assertEqual([k for k, _ in self.sessions()], ["work"] * 3)

    def test_the_count_carries_across_a_restart(self):
        self.make(PLAYTEST, tickets=0)
        pilot = driver.Pilot(self.p, args())
        pilot.merged, pilot.periodic_at = ["T-001", "T-002", "T-003"], {"playtest": 2}
        self.assertIsNone(pilot.periodic_due())  # one merge since it ran
        pilot.hand_over()
        again = driver.Pilot(self.p, args())
        again.carry_over(True)
        again.merged.append("T-004")
        self.assertEqual(again.periodic_due()["name"], "playtest")


class Config(unittest.TestCase):
    def test_mistakes_stop_at_load(self):
        from windvane.config import periodic
        with self.assertRaises(SystemExit):
            periodic([{"name": "a", "prompt": "x.md", "then": "b"}])
        with self.assertRaises(SystemExit):
            periodic([{"name": "a"}])
        self.assertEqual(periodic([{"prompt": "x.md"}])["periodic-1"]["every"], 0)

    def test_queue_lines_are_only_added(self):
        self.assertEqual(driver.added_lines("a\nb\nc", "b\nX\nc\nY"), "a\nb\nX\nc\nY")


if __name__ == "__main__":
    unittest.main()
