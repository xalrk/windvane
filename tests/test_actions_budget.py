"""The month's GitHub Actions minutes (windvane/actions.py), `windvane ci` within the cap, and `[skip ci]` on the
driver's commits at it, all through a fake `gh` serving a made-up run history (never the real one)."""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now as local_now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

NOW = datetime.now(timezone.utc).replace(microsecond=0)  # `windvane ci` and the driver count at the real time

# Serves FAKE_GH_DATA: {"repos": [repo], "runs": {repo: [run]}, "jobs": {"repo#id": [job]}, "workflows": {repo: [..]}},
# paging lists as GitHub does (per_page, page), and logs each call's arguments to FAKE_GH_LOG.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlsplit
a = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
d = json.load(open(os.environ["FAKE_GH_DATA"]))
if d.get("fail") and any(d["fail"] in x for x in a):
    sys.exit(1)
# A repo is "owner/name" (private) or {"nameWithOwner": ..., "visibility": "PUBLIC"}; --json without -q prints JSON.
repos = [r if isinstance(r, dict) else {"nameWithOwner": r, "visibility": "PRIVATE"} for r in d["repos"]]
if a[:2] == ["repo", "list"]:
    print(json.dumps(repos) if "-q" not in a else "\n".join(r["nameWithOwner"] for r in repos))
elif a[:2] == ["repo", "view"]:
    print(json.dumps(repos[0]) if "-q" not in a else repos[0]["nameWithOwner"])
elif a[:2] == ["workflow", "run"]:
    sys.exit(d.get("dispatch_exit", 0))
elif a[0] == "api":
    u = urlsplit(a[1])
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    parts = u.path.split("/")
    repo = "/".join(parts[1:3])
    if u.path.endswith("/jobs"):
        key, items = "jobs", d["jobs"].get(f"{repo}#{parts[5]}", [])
        if q.get("filter") != "all":  # the latest attempt only: a count that forgets filter=all misses re-runs
            items = [j for j in items if not j.get("old_attempt")]
    elif "/workflows/" in u.path:  # a workflow's runs, newest first
        key = "workflow_runs"
        items = [r for r in d["runs"].get(repo, []) if r["path"].endswith(parts[5])
                 and r["status"] == q.get("status", r["status"])]
    elif u.path.endswith("/workflows"):
        key, items = "workflows", d["workflows"].get(repo, [])
    else:
        key, items = "workflow_runs", d["runs"].get(repo, [])
    n, page = int(q.get("per_page", 30)), int(q.get("page", 1))
    print(json.dumps({"total_count": len(items), key: items[(page - 1) * n:page * n]}))
'''


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def job(secs, labels=("ubuntu-latest",), start=NOW - timedelta(hours=2), done=True, **kw):
    return {"started_at": iso(start), "completed_at": iso(start + timedelta(seconds=secs)) if done else None,
            "labels": list(labels), **kw}


def run(rid, created=NOW, status="completed", path=".github/workflows/test.yml"):
    return {"id": rid, "created_at": iso(created), "status": status, "path": path}


class Budget(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.data = {"repos": ["me/proj"], "runs": {}, "jobs": {}, "workflows": {}}
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["FAKE_GH_DATA"] = str(self.s.tmp / "gh.json")
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.serve()

    def tearDown(self):
        actions.logged.clear()
        self.s.close()

    def serve(self):
        Path(os.environ["FAKE_GH_DATA"]).write_text(json.dumps(self.data))

    def add(self, repo, rid, jobs, **kw):
        self.data["runs"].setdefault(repo, []).append(run(rid, **kw))
        self.data["jobs"][f"{repo}#{rid}"] = jobs
        self.serve()

    def gh_calls(self):
        f = Path(os.environ["FAKE_GH_LOG"])
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def project(self, minutes=2000, cap=None):
        """The sandbox's project with the budget set (published, so the driver's worktrees have it too)."""
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f"actions_minutes = {minutes}\n" + (f"actions_cap = {cap}\n" if cap else "") + cfg.read_text())
        self.s.publish("budget")
        return Project(self.s.root)

    # The count

    def test_jobs_round_up_and_macos_and_windows_cost_more(self):
        self.add("me/proj", 1, [job(60), job(61), job(1), job(0),  # 1 + 2 + 1 + 0
                                job(90, ["macos-14"]), job(30, ["windows-latest"]),  # 2 * 10 + 1 * 2
                                job(600, ["self-hosted", "linux"]),  # free
                                {"started_at": None, "completed_at": None, "labels": ["ubuntu-latest"]}])  # skipped
        self.assertEqual(actions.count(self.s.root, NOW)["used"], 1 + 2 + 1 + 0 + 20 + 2)

    def test_running_and_cancelled_jobs_count_to_now_and_their_end(self):
        self.add("me/proj", 1, [job(0, start=NOW - timedelta(minutes=4, seconds=30), done=False)], status="in_progress")
        self.add("me/proj", 2, [job(150, conclusion="cancelled")], status="completed")
        self.assertEqual(actions.count(self.s.root, NOW)["used"], 5 + 3)

    def test_rerun_attempts_are_billed_too(self):
        self.add("me/proj", 1, [job(60, old_attempt=True), job(60)])
        self.assertEqual(actions.count(self.s.root, NOW)["used"], 2)

    def test_only_runs_created_this_month_utc(self):
        self.add("me/proj", 3, [job(60)], created=datetime(2026, 10, 1, 0, 0, 5, tzinfo=timezone.utc))
        self.add("me/proj", 2, [job(600)], created=datetime(2026, 9, 30, 23, 59, 50, tzinfo=timezone.utc))
        self.add("me/proj", 1, [job(600)], created=datetime(2026, 9, 2, tzinfo=timezone.utc))
        self.assertEqual(actions.count(self.s.root, datetime(2026, 10, 5, tzinfo=timezone.utc))["used"], 1)
        fetched = [c[1] for c in self.gh_calls() if c[0] == "api" and "/jobs" in c[1]]
        self.assertEqual(len(fetched), 1)  # last month's runs aren't even fetched

    def test_every_repo_of_the_login_counts(self):
        self.data["repos"] = ["me/proj", "me/other", "me/quiet"]
        self.add("me/proj", 1, [job(60)])
        self.add("me/other", 7, [job(120, ["macos-latest"])])
        self.assertEqual(actions.count(self.s.root, NOW)["used"], 1 + 20)

    def test_more_than_100_runs_are_read_page_by_page(self):
        for i in range(250, 0, -1):  # newest first, as GitHub lists them
            self.add("me/proj", i, [job(30)])
        self.assertEqual(actions.count(self.s.root, NOW)["used"], 250)
        pages = [c[1] for c in self.gh_calls() if c[0] == "api" and c[1].startswith("repos/me/proj/actions/runs?")]
        self.assertEqual(len(pages), 3)

    def test_a_finished_run_is_fetched_once(self):
        self.add("me/proj", 1, [job(60)])
        self.add("me/proj", 2, [job(60, done=False)], status="in_progress")
        actions.count(self.s.root, NOW)
        Path(os.environ["FAKE_GH_LOG"]).unlink()
        self.assertEqual(actions.count(self.s.root, NOW, max_age=0)["used"], 1 + 120)
        jobs = [c[1] for c in self.gh_calls() if "/jobs" in c[1]]
        self.assertEqual(jobs, ["repos/me/proj/actions/runs/2/jobs?filter=all&per_page=100&page=1"])

    def test_a_failed_count_is_none_not_zero(self):
        (self.s.bin / "gh").write_text("#!/bin/sh\nexit 1\n")
        self.assertIsNone(actions.count(self.s.root, NOW))

    # windvane ci

    def ci(self, *extra):
        r = subprocess.run(["windvane", "ci", *extra], cwd=self.s.root, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def test_ci_refuses_a_workflow_that_would_pass_the_cap(self):
        self.project(minutes=2000)
        self.data["workflows"]["me/proj"] = [{"name": "test", "path": ".github/workflows/test.yml", "state": "active"}]
        self.add("me/proj", 1, [job(1790 * 60)])  # 1,790 used of a 1,800 cap; its last run billed 1,790 too
        code, out = self.ci("test.yml")
        self.assertEqual(code, 1, out)
        self.assertIn("1,790 of 2,000 min used; the cap is 1,800 (90%), 10 left.", out)
        self.assertIn("Refused: 1,790 + 1,790 = 3,580 min would pass the cap of 1,800.", out)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["workflow", "run"]])

    def test_ci_dispatches_under_the_cap_on_the_current_branch(self):
        self.project(minutes=2000)
        self.s.git("switch", "-q", "-c", "t-009-thing")
        self.data["workflows"]["me/proj"] = [{"name": "test", "path": ".github/workflows/test.yml", "state": "active"}]
        self.add("me/proj", 2, [job(12 * 60, ["windows-latest"])])  # 24 billed
        self.add("me/proj", 1, [job(100 * 60)])
        code, out = self.ci()  # the repo's only workflow
        self.assertEqual(code, 0, out)
        self.assertIn("124 of 2,000 min used; the cap is 1,800 (90%), 1,676 left.", out)
        self.assertIn("test.yml: its last finished run billed 24 min.", out)
        self.assertIn("After it, about 1,652 min left under the cap.", out)
        self.assertIn(["workflow", "run", "test.yml", "--ref", "t-009-thing"], self.gh_calls())

    def test_ci_dry_run_starts_nothing(self):
        self.project()
        self.data["workflows"]["me/proj"] = [{"name": "test", "path": ".github/workflows/test.yml", "state": "active"}]
        self.serve()
        code, out = self.ci("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("no finished run to estimate from", out)
        self.assertIn("Dry run: would run gh workflow run test.yml --ref main", out)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["workflow", "run"]])

    def test_ci_refuses_when_it_cant_count(self):
        self.project()
        self.data["workflows"]["me/proj"] = [{"name": "test", "path": ".github/workflows/test.yml", "state": "active"}]
        self.data.update(repos=["me/proj", "me/broken"], fail="repos/me/broken")  # its runs can't be listed
        self.serve()
        code, out = self.ci("test")
        self.assertEqual(code, 1, out)
        self.assertIn("couldn't count this month's Actions minutes", out)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["workflow", "run"]])

    def test_ci_says_a_disabled_workflow_cant_start(self):
        self.project()
        self.data["workflows"]["me/proj"] = [{"name": "tests", "path": ".github/workflows/test.yml",
                                              "state": "disabled_manually"}]
        self.serve()
        code, out = self.ci()
        self.assertEqual(code, 1, out)
        self.assertIn("0 of 2,000 min used", out)
        self.assertIn("test.yml is disabled manually on GitHub", out)
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["workflow", "run"]])

    # [skip ci]

    def test_skip_ci_at_or_over_the_cap_and_not_under(self):
        p = self.project(minutes=100)
        self.add("me/proj", 1, [job(89 * 60)])
        self.assertEqual(actions.tag(p, "Merge T-001: x"), "Merge T-001: x")
        self.add("me/proj", 2, [job(60)])  # 90 = the cap
        Path(actions.cache_file()).unlink()
        self.assertEqual(actions.tag(p, "Merge T-001: x"), "Merge T-001: x [skip ci]")

    def test_the_driver_merges_with_skip_ci_at_the_cap_and_pushes_once_a_month(self):
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.add("me/proj", 1, [job(95 * 60)])
        p = self.project(minutes=100)
        pushes, orig = [], actions.push
        actions.push = lambda title, *a, **k: pushes.append(title)
        self.addCleanup(setattr, actions, "push", orig)
        pilot = driver.Pilot(p, args())
        pilot.run(local_now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.s.git("fetch", "-q", "origin")
        log = self.s.git("log", "origin/main", "--merges", "--format=%s").splitlines()
        self.assertEqual(log, ["Merge T-002: do T-002 (branch t-002-work) [skip ci]",
                               "Merge T-001: do T-001 (branch t-001-work) [skip ci]"])
        self.assertEqual(pushes, ["Actions minutes at the cap"])
        self.assertEqual((p.state / "autopilot.log").read_text().count("at the cap"), 1)

    def test_the_driver_merges_without_skip_ci_under_the_cap(self):
        self.s.ticket("T-001")
        self.add("me/proj", 1, [job(10 * 60)])
        p = self.project(minutes=100)
        actions.count(p.root)  # the worker's hook reads the cache at commit time; the driver's start counts in the
        # background, so a fake worker's quick commit could beat it
        pilot = driver.Pilot(p, args())
        pilot.run(local_now() + timedelta(hours=1))
        self.s.git("fetch", "-q", "origin")
        self.assertNotIn("[skip ci]", self.s.git("log", "origin/main", "--format=%s"))

    # Off by default

    def test_the_budget_is_off_by_default(self):
        p = Project(self.s.root)
        self.assertIsNone(actions.budget(p))
        self.assertEqual(actions.tag(p, "Merge T-001: x"), "Merge T-001: x")
        self.assertEqual(actions.line(p), "")
        out = subprocess.run(["windvane", "status", "--line"], cwd=self.s.root, capture_output=True, text=True).stdout
        self.assertNotIn("Actions", out)
        self.assertEqual(self.gh_calls(), [])  # nothing counted, nothing asked of GitHub

    def test_status_and_overview_show_the_months_minutes(self):
        self.add("me/proj", 1, [job(1983 * 60)])
        self.project(minutes=2000)
        actions.count(self.s.root)  # as the driver does at its start and at each merge
        Path(os.environ["FAKE_GH_LOG"]).unlink()
        for cmd in (["status", "--line"], ["status"], ["overview"]):
            out = subprocess.run(["windvane", *cmd], cwd=self.s.root, capture_output=True, text=True).stdout
            self.assertIn("Actions 1,983/2,000 min", out, cmd)
        self.assertEqual(self.gh_calls(), [])  # they read the cache: the phone never waits on 18 repos' runs

    def test_status_shows_when_an_old_count_was_made_without_recounting(self):
        self.add("me/proj", 1, [job(1983 * 60)])
        self.project(minutes=2000)
        self.stale_cache()
        (self.s.bin / "gh").write_text("#!/bin/sh\nsleep 30\n")  # a recount would hang the phone's status
        t = time.time()
        out = subprocess.run(["windvane", "status", "--line"], cwd=self.s.root, capture_output=True, text=True).stdout
        self.assertLess(time.time() - t, 10)
        self.assertRegex(out, r"Actions 1,983/2,000 min \(counted (\w{3} \d+ )?\d\d:\d\d\)")  # dated after midnight

    def stale_cache(self):
        """This month's count, made two hours ago."""
        actions.count(self.s.root)
        c = json.loads(actions.cache_file().read_text())
        c["total"]["at"] -= 7200
        actions.cache_file().write_text(json.dumps(c))

    def test_the_driver_uses_the_last_count_when_gh_fails_at_a_merge(self):
        self.s.ticket("T-001")
        self.add("me/proj", 1, [job(95 * 60)])
        p = self.project(minutes=100)
        self.stale_cache()  # over the cap, and too old to reuse without trying a recount
        (self.s.bin / "gh").write_text("#!/bin/sh\nexit 1\n")
        driver.Pilot(p, args()).run(local_now() + timedelta(hours=1))
        self.s.git("fetch", "-q", "origin")
        self.assertIn("Merge T-001: do T-001 (branch t-001-work) [skip ci]", self.s.git("log", "origin/main", "--format=%s"))

    def test_the_driver_doesnt_tag_when_gh_fails_and_nothing_was_counted(self):
        self.s.ticket("T-001")
        p = self.project(minutes=100)
        (self.s.bin / "gh").write_text("#!/bin/sh\nexit 1\n")
        pilot = driver.Pilot(p, args())
        pilot.run(local_now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.s.git("fetch", "-q", "origin")
        driver_own = [m for m in self.s.git("log", "origin/main", "--format=%s").splitlines() if "T-001: build" not in m]
        self.assertNotIn("[skip ci]", "\n".join(driver_own))  # the worker's own is tagged: its hook reads no count

    def test_workers_cant_start_or_rerun_a_workflow_past_claude_ci(self):
        allow, deny = driver.worker_perms(Project(self.s.root))
        for rule in ("Bash(gh workflow run*)", "Bash(gh workflow enable*)", "Bash(gh run rerun*)"):
            self.assertIn(rule, deny)

    def test_a_bad_budget_stops_at_once(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("actions_cap = 90\n" + cfg.read_text())
        with self.assertRaises(SystemExit) as e:
            Project(self.s.root)
        self.assertIn("actions_cap = 90", str(e.exception))


if __name__ == "__main__":
    unittest.main()
