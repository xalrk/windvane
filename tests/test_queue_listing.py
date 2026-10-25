"""The compact ticket listing (T-295): plan and periodic sessions re-read their prompt on every turn, so their listing
holds the open tickets in full and the done ones as a count, keeping a done ticket an open one depends on or one
merged this run; `windvane queue` leads with the open ones and `--all` lists every ticket."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from helpers import Sandbox  # noqa: E402

LONG = ("the retry backoff doubles after each failed push and gives up after the fifth, saying which remote "
        "refused it and why")


class Listing(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        for n in range(1, 206):
            tid = f"T-{n:03d}"
            deps = "T-050" if tid == "T-203" else "nothing"
            name = LONG if tid == "T-202" else f"make part {n} of the build report its own timing in the summary line"
            self.s.write(f".windvane/tickets/{tid}.md", f"# {tid}: {name}\nDepends on: {deps}.\n")
        self.s.write(".windvane/QUEUE.md", "# Queue\n- T-204 four\n" + "".join(f"- done: T-{n:03d}\n"
                                                                             for n in range(1, 201)))
        self.s.publish()

    def queue_cli(self, *args):
        r = subprocess.run([str(self.s.bin / "windvane"), "queue", *args], cwd=self.s.root, capture_output=True,
                           text=True)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r.stdout

    def test_the_compact_listing_holds_the_open_tickets_and_counts_the_done_ones(self):
        out = Queue(self.p).listing(compact=True)
        self.assertLess(len(out.encode()), 2048)
        rows = out.splitlines()
        self.assertEqual([r.split()[0] for r in rows[:5]], ["T-204", "T-201", "T-202", "T-203", "T-205"])
        self.assertTrue(all(r.split()[1] in ("todo", "waiting") for r in rows[:5]))
        self.assertEqual(rows[5].split()[:2], ["T-050", "done"])  # T-203 depends on it
        self.assertEqual(rows[6], "199 done: their titles are in .windvane/tickets/ "
                                  "(grep before filing one that may repeat them)")
        self.assertEqual(len(rows), 7)

    def test_a_ticket_merged_this_run_stays(self):
        rows = Queue(self.p).listing(compact=True, keep=["T-007"]).splitlines()
        self.assertIn("T-007", [r.split()[0] for r in rows])
        self.assertTrue(rows[-1].startswith("198 done:"))

    def test_a_long_title_is_cut_at_a_word(self):
        row = next(r for r in Queue(self.p).listing(compact=True).splitlines() if r.startswith("T-202"))
        title = row.split(None, 2)[2]
        self.assertTrue(title.endswith("…"))
        self.assertTrue(LONG.startswith(title[:-1]) and LONG[len(title) - 1] == " ", title)

    def test_the_full_listing_is_unchanged_in_order(self):
        rows = Queue(self.p).listing().splitlines()
        self.assertEqual(len(rows), 205)
        self.assertEqual(rows[0].split()[0], "T-204")  # QUEUE.md's order, then the rest by number

    def test_queue_leads_with_the_open_tickets_and_all_lists_every_one(self):
        rows = self.queue_cli().splitlines()
        self.assertEqual(rows[0].split()[0], "T-204")
        self.assertIn("199 done: their titles are in .windvane/tickets/ (`windvane queue --all` lists them)", rows)
        self.assertLess(len(rows), 10)
        every = [r.split()[0] for r in self.queue_cli("--all").splitlines() if r.startswith("T-")]
        self.assertEqual(len(every), 205)
        self.assertEqual(set(every), {f"T-{n:03d}" for n in range(1, 206)})

    def test_the_plan_gets_the_compact_listing(self):
        planner.plan(self.p, "more", use_followups=False, merged=["T-007"])
        calls = [json.loads(c) for c in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        prompt = next(c["prompt"] for c in calls if c["system"].startswith("# windvane plan"))
        listing = prompt.split("## Existing tickets (id, status, title)\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual([r.split()[0] for r in listing[:7]], ["T-204", "T-201", "T-202", "T-203", "T-205", "T-007",
                                                                "T-050"])
        self.assertTrue(listing[7].startswith("198 done:"))


if __name__ == "__main__":
    unittest.main()
