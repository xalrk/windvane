"""`stats --projects`: every project state dir under the state root read side by side, then each measure's spread.
Two fake projects (one whose repo the phone registry records, one it doesn't) and shared dirs it must skip."""
import hashlib
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402

T = lambda d: f"{d:%Y-%m-%d %H:%M}"
DAY = lambda d: f"{d:%Y-%m-%d}"


class StatsProjects(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        base = now().replace(hour=12, minute=0, second=0, microsecond=0)
        self.recent, self.mid, self.old = base - timedelta(days=2), base - timedelta(days=20), base - timedelta(days=60)
        self.since = DAY(base - timedelta(days=10))  # keeps the recent rows, drops the mid ones
        self.root = Path(os.environ["XDG_STATE_HOME"]) / "windvane"
        self.repo_project()
        self.loose_project()
        self.shared_dirs()

    def repo_project(self):
        s, R, M, O = self.s, T(self.recent), T(self.mid), T(self.old)
        for tid in ("T-001", "T-003", "T-004", "T-005", "T-006", "T-007"):
            s.ticket(tid)
        s.write(".windvane/tickets/T-002.md", "# T-002: start\nDepends on: nothing.\n\nFixes T-001's crash on start.\n")
        s.write(".windvane/tickets/T-008.md", "# T-008: more\nDepends on: nothing.\n\nT-005 is fine; the fix for "
                "the menu comes later.\n")
        s.write(".windvane/QUEUE.md", "# Queue\n- parked: T-003 (waits for a sound card (T-001; D-4))\n")
        s.publish()
        for when, n in ((self.old, 5), (self.mid, 2), (self.recent, 3)):
            f = s.root / ".windvane/CALIBRATION.md"
            f.write_text((f.read_text() if f.exists() else "") + "".join(f"- line {when:%d} {i}\n" for i in range(n)))
            date = f"{when:%Y-%m-%dT%H:%M:%S}"
            os.environ.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
            s.publish("calibration")
        for k in ("GIT_AUTHOR_DATE", "GIT_COMMITTER_DATE"):
            os.environ.pop(k)
        d = self.root / "sandbox"
        rows = [dict(at=R, role="worker", ticket="T-001", input=30000),
                dict(at=M, role="worker", ticket="T-005", input=50000),
                dict(at=R, role="critic", ticket="T-001", input=10000),
                dict(at=R, role="reviewer", ticket="T-001", output=2000),
                dict(at=O, role="worker", ticket="T-009", input=99999),
                dict(at=R, event="outcome", ticket="T-004", outcome="failed"),
                dict(at=R, event="check-start", ticket="T-001", role="worker", start=1.0, log="a.log"),
                dict(at=R, event="check", ticket="T-001", role="worker", start=1.0, minutes=30, passed=True),
                dict(at=R, event="check", ticket="T-001", role="worker", start=2.0, minutes=10, passed=False),
                dict(at=M, event="check", ticket="T-005", role="worker", start=3.0, minutes=20, passed=True),
                dict(at=R, event="check", ticket="T-005", role="driver", minutes=0, passed=True, reused=True),
                dict(at=R, event="await", ticket="T-001", role="worker", seconds=600),
                dict(at=M, event="await", ticket="T-005", role="worker", seconds=1200),
                dict(at=R, event="await", ticket="T-001", role="reviewer", seconds=300)]
        killed_start = time.time() - 3600
        rows += [dict(at=R, event="check-start", ticket="T-006", role="worker", start=killed_start, log="b.log"),
                 dict(at=R, event="check-start", ticket="T-007", role="worker", start=time.time(), log="c.log"),
                 dict(at=R, event="check-start", ticket="T-010", role="worker", start=killed_start, log="d.log")]
        for r in rows:
            append_jsonl(d / "ledger.jsonl", r)
        (d / "checks").mkdir()
        for name in ("a.log", "b.log", "c.log"):
            (d / "checks" / name).write_text("check output\n")
        os.utime(d / "checks/b.log", (killed_start + 600, killed_start + 600))  # cut off 10 minutes in
        (d / "checks/d.log").write_text("heavy: waiting for the machine lock: pid 9 (render.sh); next in line\n")
        os.utime(d / "checks/d.log", (killed_start + 1200, killed_start + 1200))  # cancelled after 20 min queued
        Rc = T(self.recent - timedelta(hours=1))
        for r in [dict(at=Rc, ticket="T-001", score=5, step="revise"), dict(at=Rc, ticket="T-001", score=8, step="pass"),
                  dict(at=Rc, ticket="T-006", score=9, step="pass"),
                  dict(at=M, ticket="T-005", score=3, step="revise"), dict(at=M, ticket="T-005", score=7, step="accept"),
                  dict(at=Rc, ticket="PLAN", score=2, step="pass")]:
            append_jsonl(d / "critic.jsonl", r)
        (d / "autopilot.log").write_text("".join(f"{line}\n" for line in [
            f"{O} T-009: merged 7654321 · 5h 1%", f"{M} T-005: merged abcdef1 · 5h 1%",
            f"{R} T-001: review changes: it needs a test", f"{R} T-006: review approve: fine",
            f"{R} T-001: merged 1234567 · 5h 2%", f"{R} T-004: FAILED: the check kept failing on a missing font"]))
        (self.root / "phone").mkdir(parents=True, exist_ok=True)
        (self.root / "phone/projects.json").write_text(json.dumps({"sandbox": str(s.root)}))

    def loose_project(self):
        d = self.root / "other"
        d.mkdir(parents=True)
        for r in [dict(at=T(self.recent), role="worker", ticket="OP-001", input=2000),
                  dict(at=T(self.recent), event="check", ticket="OP-001", role="worker", start=5.0, minutes=5,
                       passed=True)]:
            append_jsonl(d / "ledger.jsonl", r)
        (d / "autopilot.log").write_text(f"{T(self.recent)} OP-001: merged 0abcdef · 5h 1%\n")

    def shared_dirs(self):
        for name in ("phone", "_bench"):
            append_jsonl(self.root / name / "ledger.jsonl", dict(at=T(self.recent), role="worker", input=7))
        (self.root / "no-ledger").mkdir()

    def run_cli(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(self.s.tmp)  # outside any project: it needs none
        with redirect_stdout(out):
            cli.main(["stats", "--projects", *argv])
        return out.getvalue()

    def snapshot(self):
        files = [f for top in (self.root, self.s.root) for f in sorted(top.rglob("*")) if f.is_file()]
        return {str(f): (f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest()) for f in files}

    def test_the_digest_over_30_days(self):
        out = self.run_cli()
        self.assertIn("2 projects since ", out)
        self.assertNotIn("_bench", out)
        self.assertNotIn("no-ledger", out)
        self.assertNotIn("phone", out)
        lines = out.splitlines()
        start = lines.index(f"sandbox  {self.s.root}")
        self.assertEqual(lines[start:start + 11], [
            f"sandbox  {self.s.root}",
            "  tickets: 2 merged, 1 failed, 1 parked now",
            "  weighted tokens: 100k, 50k a merged ticket; by role, share (per merged ticket): worker 80% (40k),",
            "    critic 10% (5k), reviewer 10% (5k)",
            "  critic: 3 tickets, 1 passed first time, median score 5 first, 8 final, 1 final on the pass mark (8)",
            "  reviews after a critic pass: 2, 1 sent back",
            "  checks: 4 runs (23 min per checked ticket), 20 min waiting for the machine, 1 failed, 1 cut off, "
            "1 cancelled in the queue; workers 30 min in await",
            "  re-fixes: 1 (T-001 by T-002)",
            "  CALIBRATION.md: 5 lines added",
            "  failed T-004: the check kept failing on a missing font",
            "  parked T-003: waits for a sound card (T-001; D-4)"])
        other = lines.index("other  repo unknown")
        self.assertEqual(lines[other + 1:other + 5], [
            "  tickets: 1 merged, 0 failed",
            "  weighted tokens: 2k, 2k a merged ticket; by role, share (per merged ticket): worker 100% (2k)",
            "  checks: 1 run (5.0 min per checked ticket), 0 failed, 0 cut off; workers 0.0 min in await",
            "  repo unknown: no re-fixes, CALIBRATION.md lines or parked tickets (it hasn't run since runs recorded "
            "their checkout)"])
        across = lines[lines.index("across projects                  lowest        highest") + 1:]
        self.assertEqual(across, [
            "  merged tickets                 1 other       2 sandbox",
            "  worker share of tokens         80% sandbox   100% other",
            "  weighted per merged ticket     2k other      50k sandbox",
            "  check min per checked ticket   5 other       23.3 sandbox",
            "  worker min in await            0 other       30 sandbox",
            "  failed tickets                 0 other       1 sandbox"])
        self.assertFalse(any("re-fixes" in x or "CALIBRATION" in x for x in across))  # only one project has them

    def test_json_matches_the_table(self):
        d = json.loads(self.run_cli("--json"))
        self.assertEqual([x["name"] for x in d["projects"]], ["other", "sandbox"])
        other, sb = d["projects"]
        self.assertIsNone(other["repo"])
        self.assertIsNone(other["refixes"])
        self.assertEqual(sb["repo"], str(self.s.root))
        self.assertEqual(sb["tickets"], {"merged": 2, "failed": 1, "parked": 1})
        self.assertEqual(sb["tokens"], {"weighted": 100000, "per_merged": 50000, "roles": {
            "worker": {"share": 80.0, "per_merged": 40000}, "critic": {"share": 10.0, "per_merged": 5000},
            "reviewer": {"share": 10.0, "per_merged": 5000}}})
        self.assertEqual(sb["critic"], {"tickets": 3, "bar": 8, "first_pass": 1, "median_first": 5, "median_final": 8,
                                        "final_on_bar": 1, "reviews": 2, "sent_back": 1})
        self.assertEqual(sb["checks"], {"runs": 4, "tickets": 3, "per_ticket": 23.3, "minutes": 70.0, "waited": 20.0,
                                        "failed": 1, "cut_off": 1, "cancelled_queued": 1, "worker_await": 30.0})
        self.assertEqual(sb["refixes"], [["T-001", "T-002"]])
        self.assertEqual(sb["calibration_lines"], 5)
        self.assertEqual(sb["failed"], {"T-004": "the check kept failing on a missing font"})
        self.assertEqual(sb["parked"], {"T-003": "waits for a sound card (T-001; D-4)"})
        merged = next(x for x in d["across"] if x["measure"] == "merged tickets")
        self.assertEqual(merged, {"measure": "merged tickets", "low": 1, "low_projects": ["other"], "high": 2,
                                  "high_projects": ["sandbox"]})

    def test_since_narrows_it(self):
        sb = json.loads(self.run_cli("--json", "--since", self.since))["projects"][1]
        self.assertEqual(sb["tickets"]["merged"], 1)
        self.assertEqual(sb["tokens"], {"weighted": 50000, "per_merged": 50000, "roles": {
            "worker": {"share": 60.0, "per_merged": 30000}, "critic": {"share": 20.0, "per_merged": 10000},
            "reviewer": {"share": 20.0, "per_merged": 10000}}})
        self.assertEqual(sb["critic"], {"tickets": 2, "bar": 8, "first_pass": 1, "median_first": 7,
                                        "median_final": 8.5, "final_on_bar": 1, "reviews": 2, "sent_back": 1})
        self.assertEqual(sb["checks"], {"runs": 3, "tickets": 2, "per_ticket": 25.0, "minutes": 50.0, "waited": 20.0,
                                        "failed": 1, "cut_off": 1, "cancelled_queued": 1, "worker_await": 10.0})
        self.assertEqual(sb["calibration_lines"], 3)
        out = self.run_cli("--since", self.since)
        self.assertIn(f"2 projects since {self.since}", out)
        self.assertIn("median score 7 first, 8.5 final", out)

    def test_since_takes_only_a_date(self):
        for bad in ("HEAD", "2026-13-45", "2026-10-32"):
            with self.subTest(bad), self.assertRaises(SystemExit) as e:
                self.run_cli("--since", bad)
            self.assertIn("isn't a date", str(e.exception))

    def test_json_goes_with_projects(self):
        with self.assertRaises(SystemExit) as e:
            cli.main(["stats", "--json"])
        self.assertIn("--json goes with --projects", str(e.exception))

    def test_ends_alike_print_once(self):
        append_jsonl(self.root / "other/ledger.jsonl", dict(at=T(self.recent), event="outcome", ticket="OP-002",
                                                            outcome="failed"))
        out = self.run_cli()
        self.assertIn("  failed tickets                 1 in all 2", out.splitlines())
        both = json.loads(self.run_cli("--json"))["across"][-1]
        self.assertEqual(both, {"measure": "failed tickets", "low": 1, "low_projects": ["other", "sandbox"],
                                "high": 1, "high_projects": ["other", "sandbox"]})

    def test_nothing_changes(self):
        before = self.snapshot()
        self.run_cli()
        self.run_cli("--json")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
