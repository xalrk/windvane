"""The Actions budget's gaps (T-064): re-runs counted in the month they ran, a cached run's new attempt fetched,
workers unable to start workflows through `gh api`, their own commits tagged `[skip ci]` at the cap, one count at a
time across drivers, and doctor's flow-style matrix. All through test_actions_budget's fake `gh`, never the real one."""
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, doctor, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now as local_now, save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_actions_budget import FAKE_GH, iso, job, run  # noqa: E402
from test_pipeline import args  # noqa: E402

UTC = timezone.utc
T = datetime(2026, 10, 5, 12, tzinfo=UTC)  # the counts here run at a fixed time; the driver's at the real one


class Gaps(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.data = {"repos": ["me/proj"], "runs": {}, "jobs": {}, "workflows": {}}
        self.gh = self.s.bin / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        os.environ["FAKE_GH_DATA"] = str(self.s.tmp / "gh.json")
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.serve()

    def tearDown(self):
        actions.logged.clear()
        actions.asked.clear()
        self.s.close()

    def serve(self):
        Path(os.environ["FAKE_GH_DATA"]).write_text(json.dumps(self.data))

    def add(self, rid, jobs, **kw):
        extra = {k: kw.pop(k) for k in ("run_attempt", "run_started_at", "updated_at") if k in kw}
        self.data["runs"].setdefault("me/proj", []).append({**run(rid, **kw), **extra})
        self.data["jobs"][f"me/proj#{rid}"] = jobs
        self.serve()

    def gh_calls(self, part=""):
        f = Path(os.environ["FAKE_GH_LOG"])
        calls = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        return [c for c in calls if part in " ".join(c)]

    def project(self, minutes=100):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f"actions_minutes = {minutes}\n" + cfg.read_text())
        self.s.publish("budget")
        return Project(self.s.root)

    # Re-runs

    def test_a_run_created_last_month_and_rerun_this_month_counts_only_this_months_jobs(self):
        sept = datetime(2026, 9, 20, tzinfo=UTC)
        self.add(3, [job(60, start=T - timedelta(hours=1))], created=T - timedelta(hours=1))
        self.add(2, [job(600, start=sept, old_attempt=True), job(150, start=T - timedelta(hours=3))], created=sept,
                 run_attempt=2, run_started_at=iso(T - timedelta(hours=3)), updated_at=iso(T - timedelta(hours=2)))
        self.add(1, [job(600, start=sept)], created=sept, updated_at=iso(sept + timedelta(minutes=10)))
        self.add(0, [job(600, start=datetime(2026, 8, 1, tzinfo=UTC))], created=datetime(2026, 8, 1, tzinfo=UTC))
        self.assertEqual(actions.count(self.s.root, T)["used"], 1 + 3)  # not September's 10 minutes
        fetched = [c[1].split("/")[5] for c in self.gh_calls("/jobs")]
        self.assertEqual(fetched, ["3", "2"])  # September's run that didn't run again isn't fetched

    def test_a_cached_run_rerun_is_fetched_again(self):
        self.add(1, [job(60, start=T - timedelta(hours=2))], created=T - timedelta(hours=2))
        self.assertEqual(actions.count(self.s.root, T)["used"], 1)
        self.data["runs"]["me/proj"][0].update(run_attempt=2, run_started_at=iso(T - timedelta(hours=1)))
        self.data["jobs"]["me/proj#1"].append(job(120, start=T - timedelta(hours=1)))
        self.serve()
        self.assertEqual(actions.count(self.s.root, T, max_age=0)["used"], 1 + 2)
        self.assertEqual(len(self.gh_calls("runs/1/jobs")), 2)
        self.assertEqual(actions.count(self.s.root, T, max_age=0)["used"], 3)
        self.assertEqual(len(self.gh_calls("runs/1/jobs")), 2)  # attempt 2, finished, now comes from the cache

    # One count at a time

    def test_two_counts_started_together_make_one_set_of_gh_calls(self):
        self.add(1, [job(60, start=T - timedelta(hours=1))], created=T - timedelta(hours=1))
        slow = self.s.bin / "gh-fake"
        self.gh.rename(slow)
        self.gh.write_text(f"#!/bin/sh\nsleep 0.5\nexec {slow} \"$@\"\n")
        self.gh.chmod(0o755)
        got = []
        threads = [threading.Thread(target=lambda: got.append(actions.count(self.s.root, T))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([g["used"] for g in got], [1, 1])
        self.assertEqual(len(self.gh_calls("repo list")), 1)
        self.assertEqual(len(self.gh_calls("/jobs")), 1)

    def test_a_count_that_cant_get_the_lock_gives_up_and_the_cap_falls_back_on_the_last_count(self):
        p = self.project()
        self.add(1, [job(60)])
        save_json(actions.cache_file(), {"month": actions.month(), "runs": {}, "total": {"used": 95, "at": 0}})
        self.addCleanup(setattr, actions, "WAIT_S", actions.WAIT_S)
        actions.WAIT_S = 0.3
        with actions.locked() as got:  # another driver's count, stuck
            self.assertTrue(got)
            started = time.time()
            self.assertIsNone(actions.count(self.s.root))
            self.assertLess(time.time() - started, 2)
            self.assertTrue(actions.over_cap(p))  # 95 of 100 counted before, cap 90
        self.assertEqual(self.gh_calls(), [])
        self.assertFalse(actions.over_cap(p))  # with the lock free, it counts again: 1 min

    # Workers

    def test_workers_cant_start_rerun_or_enable_a_workflow_through_gh_api(self):
        """Matched as Claude Code does with `*` (any text): what starts a workflow is denied, what only reads isn't."""
        _, deny = driver.worker_perms(Project(self.s.root))
        rules = [r[5:-1] for r in deny if r.startswith("Bash(")]
        for rule in ("gh api *dispatches*", "gh api *rerun*"):
            self.assertIn(rule, rules)
        denied = lambda cmd: any(fnmatchcase(cmd, r) for r in rules)  # noqa: E731
        for cmd in ("gh api repos/me/p/actions/workflows/t.yml/dispatches -f ref=main",
                    "gh api repos/me/p/dispatches -f event_type=go",
                    "gh api -X POST repos/me/p/actions/runs/1/rerun",
                    "gh api repos/me/p/actions/runs/1/rerun-failed-jobs --method POST",
                    "gh api --method POST repos/me/p/actions/jobs/5/rerun",
                    "gh api -X PUT repos/me/p/actions/workflows/7/enable",
                    "gh api repos/me/p/actions/workflows/7/enable --method=PUT",
                    "gh run rerun", "gh run rerun 12 --failed", "gh run -R me/p rerun 12",
                    "gh workflow run t.yml", "gh workflow -R me/p run t.yml", "gh workflow enable t.yml"):
            self.assertTrue(denied(cmd), cmd)
        for cmd in ("windvane ci t.yml", "gh api repos/me/p/actions/runs", "gh run view 12", "gh workflow list",
                    "gh pr create --title T-064 --body 'gh workflow run is denied'"):
            self.assertFalse(denied(cmd), cmd)

    def pilot_commits(self, minutes_used):
        self.s.ticket("T-001")
        self.add(1, [job(minutes_used * 60)])
        p = self.project(minutes=100)
        config = (self.s.root / ".git/config").read_text()
        actions.push, orig = (lambda *a, **k: None), actions.push
        self.addCleanup(setattr, actions, "push", orig)
        driver.Pilot(p, args()).run(local_now() + timedelta(hours=1))
        self.assertEqual((self.s.root / ".git/config").read_text(), config)  # the user's checkout: untouched
        self.s.git("fetch", "-q", "origin")
        return [m for m in self.s.git("log", "origin/main", "--format=%s").splitlines() if m.startswith("T-001")]

    def test_a_workers_commit_at_the_cap_carries_skip_ci(self):
        self.assertEqual(self.pilot_commits(95), ["T-001: build [skip ci]"])

    def test_a_workers_commit_under_the_cap_doesnt(self):
        self.assertEqual(self.pilot_commits(10), ["T-001: build"])

    def test_the_hook_runs_the_repos_own_and_tags_once(self):
        p = self.project()
        self.add(1, [job(95 * 60)])
        own = self.s.root / ".git/hooks/prepare-commit-msg"
        own.write_text('#!/bin/sh\necho "Signed: me" >> "$1"\n')
        own.chmod(0o755)
        env, line = actions.worker_git(p, self.s.root)
        self.assertEqual(line, "")
        self.s.write("a.txt", "a\n")
        self.s.git("add", "a.txt")
        os.environ.update(env)
        try:
            self.s.git("commit", "-qm", "one [skip ci]")
            self.s.write("a.txt", "b\n")
            self.s.git("commit", "-qam", "two")
        finally:
            for k in env:
                os.environ.pop(k)
        self.assertEqual(self.s.git("log", "-2", "--format=%B").split(),
                         "two [skip ci] Signed: me one [skip ci] Signed: me".split())

    def test_a_projects_own_hooks_path_is_kept_and_the_prompt_asks(self):
        p = self.project()
        self.add(1, [job(95 * 60)])
        self.s.git("config", "core.hooksPath", ".githooks")
        for _ in range(2):
            env, line = actions.worker_git(p, self.s.root)
            self.assertEqual(env, {})
            self.assertIn("[skip ci]", line)
        self.assertEqual((p.state / "autopilot.log").read_text().count("core.hooksPath"), 1)

    # doctor

    def wf(self, text):
        folder = self.s.root / ".github/workflows"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "test.yml").write_text(text)
        return [what for what, _ in doctor.ci_warnings(self.s.root, Project(self.s.root))]

    def test_doctor_counts_a_flow_style_matrix(self):
        flow = 'on: [push]\njobs:\n  test:\n    runs-on: x\n    strategy:\n      matrix: {python: ["3.11", "3.12"]}'
        self.assertEqual(doctor.workflow_runs(flow), (["push"], 2))
        self.assertIn("test.yml runs on push: about 2 hosted jobs a ticket (2 a run, for its merge)", self.wf(flow)[0])
        flow = ("on: [push]\njobs:\n  t:\n    runs-on: x\n    strategy:\n      matrix: {os: [a, b], py: ['1,2', 3], "
                "include: [{os: c, py: 4}]}\n  lint:\n    runs-on: x\n")
        self.assertEqual(doctor.workflow_runs(flow), (["push"], 2 * 2 + 1 + 1))

    def test_doctor_says_some_hosted_jobs_when_it_cant_count(self):
        for matrix in ("${{ fromJSON(needs.a.outputs.m) }}", "\n        py: ${{ fromJSON(needs.a.outputs.m) }}",
                       "{py: [3.11,\n"):
            text = f"on: push\njobs:\n  t:\n    runs-on: x\n    strategy:\n      matrix: {matrix}\n"
            self.assertIsNone(doctor.workflow_runs(text)[1], matrix)
            self.assertIn("test.yml runs on push: some hosted jobs on every ticket, for its merge", self.wf(text)[0])

    def test_doctors_warning_names_the_budget(self):
        text = "on: [push, pull_request]\njobs:\n  t:\n    runs-on: x\n"
        self.assertNotIn("budget", self.wf(text)[0])
        self.project(minutes=2000)
        self.assertIn("it spends the capped minutes of the Actions budget (2,000 min, cap 1,800; not counted yet "
                      "this month)", self.wf(text)[0])
        save_json(actions.cache_file(), {"month": actions.month(), "runs": {},
                                         "total": {"used": 1234.0, "at": time.time()}})
        self.assertIn("(2,000 min, cap 1,800; 1,234 counted this month)", self.wf(text)[0])


if __name__ == "__main__":
    unittest.main()
