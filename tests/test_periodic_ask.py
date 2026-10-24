"""`windvane periodic <name>` while a run is on: the ask waits in the state dir for the run's next gap."""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, periodic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_followups import capture  # noqa: E402
from test_pipeline import args  # noqa: E402

AUDIT = '[[periodic]]\nname = "audit"\nminutes = 30\nprompt = "tools/AUDIT.md"\nthen = "triage"\n'
TRIAGE = '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n'


class Ask(unittest.TestCase):
    def make(self, config=AUDIT + TRIAGE):
        self.s = Sandbox(config)
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.write(".windvane/QUEUE.md", "# Queue\n- T-001 one\n- T-002 two\n")
        self.s.write("tools/AUDIT.md", "# Audit\nPLAY\n")
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\n")
        self.s.publish()
        self.p = Project(self.s.root)
        capture(self, driver, periodic)

    def wv(self, *argv):
        """The real `windvane` command, from the user's checkout: its output and exit code."""
        r = subprocess.run(["windvane", *argv], cwd=self.s.root, capture_output=True, text=True)
        return r.stdout + r.stderr, r.returncode

    def during_t001(self, *commands):
        """Shell commands T-001's worker runs from its Bash tool, as the user at the desk would mid-run."""
        cmd = " && ".join(f"(cd {shlex.quote(str(self.s.root))} && {c})" for c in commands)
        os.environ["FAKE_WORKER_RUN"] = f'if [ "$WINDVANE_TICKET" = T-001 ]; then {cmd}; fi'
        self.addCleanup(os.environ.pop, "FAKE_WORKER_RUN", None)

    def ran(self):
        f = self.s.tmp / "calls.jsonl.ran"
        return f.read_text() if f.exists() else ""

    def run_pilot(self, hours=2):
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=hours))
        return pilot

    def order(self):
        """The sessions in order: a ticket's worker by its id, a periodic one by its entry's name."""
        f, out = self.s.tmp / "calls.jsonl", []
        for c in map(json.loads, f.read_text().splitlines() if f.exists() else []):
            if c["system"].startswith("# windvane periodic"):
                out.append(c["prompt"].split()[2])
            elif c["system"].startswith("# windvane worker"):
                out.append(c["prompt"].split()[1].rstrip("."))
        return out

    def hold_lock(self):
        """A run on, as far as the desk command can tell."""
        self.p.state.mkdir(parents=True, exist_ok=True)
        lock = open(self.p.state / "lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lock.close)
        return lock

    def test_an_ask_runs_the_chain_at_the_next_gap_and_lands_its_tickets(self):
        self.make()
        self.during_t001("windvane periodic audit")
        pilot = self.run_pilot()
        self.assertIn("the running autopilot runs periodic audit at its next gap between tickets; "
                      "to take it back: windvane periodic audit --cancel", self.ran())
        self.assertEqual(self.order()[:3], ["T-001", "audit", "triage"])
        self.assertIn("T-003", Queue(self.p).files)  # the triage's ticket landed, and this run worked it
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        self.assertEqual(periodic.asked(self.p), [])

    def test_asked_twice_is_one_round(self):
        self.make()
        self.during_t001("windvane periodic audit", "windvane periodic audit")
        self.run_pilot()
        self.assertIn("(asked already: it runs once)", self.ran())
        self.assertEqual([x for x in self.order() if not x.startswith("T-")], ["audit", "triage"])

    def test_cancel_before_the_gap_gives_none(self):
        self.make()
        self.during_t001("windvane periodic audit", "windvane periodic audit --cancel")
        self.run_pilot()
        self.assertIn("periodic audit: ask taken back", self.ran())
        self.assertEqual(self.order(), ["T-001", "T-002"])

    def test_an_entry_added_on_main_after_the_run_started(self):
        self.make(TRIAGE)
        self.during_t001(f"printf %s {shlex.quote(AUDIT)} >> .windvane/config.toml",
                         "git commit -qam 'an audit entry'", "git push -q origin main", "windvane periodic audit")
        self.assertNotIn("audit", self.p.periodic)  # the run's own config hasn't got it
        self.run_pilot()
        self.assertEqual(self.order()[:3], ["T-001", "audit", "triage"])

    def test_a_restart_between_ask_and_gap_keeps_it(self):
        self.make()
        with self.hold_lock():  # the driver that crashed, or re-execs for a self-update
            self.wv("periodic", "audit")
        os.environ["WINDVANE_RESTARTED"] = "1"  # the one it hands over to (which pops it)
        self.run_pilot()
        self.assertEqual(self.order()[:2], ["audit", "triage"])
        self.assertEqual(periodic.asked(self.p), [])

    def test_a_fresh_run_drops_a_killed_runs_ask(self):
        self.make()
        with self.hold_lock():
            self.wv("periodic", "audit")
        self.run_pilot()
        self.assertEqual(self.order(), ["T-001", "T-002"])
        self.assertIn("dropped an earlier run's ask for audit", (self.p.state / "autopilot.log").read_text())

    def test_a_run_that_ends_first_drops_it_and_says_so(self):
        self.make()
        self.during_t001("windvane periodic audit", "touch " + shlex.quote(str(self.p.state / "STOP")))
        self.run_pilot()
        self.assertNotIn("audit", self.order())
        self.assertEqual(periodic.asked(self.p), [])
        [summary] = [m for t, m in self.pushes if t == "windvane stopped"]
        self.assertIn("Asked for, not run (the run ended first): audit.", summary)

    def test_a_failed_setup_drops_it_with_one_push(self):
        self.make(AUDIT.replace("then", 'setup = "echo broken; exit 3"\nthen') + TRIAGE)
        self.during_t001("windvane periodic audit")
        pilot = self.run_pilot()
        self.assertEqual(self.order(), ["T-001", "T-002"])
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual([t for t, _ in self.pushes if "audit" in t], ["audit round failed"])
        self.assertEqual(periodic.asked(self.p), [])

    def test_no_time_before_the_deadline_drops_it_with_one_push(self):
        self.make(AUDIT.replace("minutes = 30", "minutes = 90") + TRIAGE)
        self.during_t001("windvane periodic audit")
        self.run_pilot(hours=1)
        self.assertEqual(self.order(), ["T-001", "T-002"])
        [(_, msg)] = [(t, m) for t, m in self.pushes if t == "audit round not run"]
        self.assertIn("its 90 min don't fit before the run's deadline", msg)

    def test_an_unknown_name_is_refused_naming_the_entries(self):
        self.make()
        with self.hold_lock():
            out, code = self.wv("periodic", "quality")
        self.assertEqual(code, 1)
        self.assertIn("no [[periodic]] entry 'quality' in .windvane/config.toml (there is: audit, triage)", out)
        self.assertEqual(periodic.asked(self.p), [])

    def test_status_and_help_show_the_ask(self):
        self.make()
        with self.hold_lock():
            self.wv("periodic", "triage")
            self.assertIn("asked: periodic triage, at the next gap", self.wv("status")[0])
            self.wv("periodic", "triage", "--cancel")
            self.assertNotIn("asked:", self.wv("status")[0])
        self.assertIn("--cancel", self.wv("periodic", "--help")[0])


if __name__ == "__main__":
    unittest.main()
