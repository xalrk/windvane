"""T-079, T-093: every phone push leads with a line that holds its point, and carries its free text whole (no "…").
Each `push(` call in driver.py, held.py and planner.py is driven here through their own functions with a
300-character error or reason; a new call with no case here fails the coverage test, so it gets one."""
import ast
import json
import signal
import subprocess
import sys
import time
import traceback
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, net, planner, questions, scratch  # noqa: E402
from windvane import held as held_mod  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_set_aside import args  # noqa: E402

LONG = ("API Error: Request timed out while waiting for the model; the connection was reset by the peer. " * 4)[:300]
MODULES = (driver, held_mod, planner)  # every module whose pushes are covered here
SOURCES = {m.__file__: Path(m.__file__) for m in MODULES}


def push_calls(source):
    """The line of every `push(` call in `source`."""
    return sorted(n.lineno for n in ast.walk(ast.parse(source))
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "push")


def pilot(p, worktree=False):
    if worktree:
        ensure_worktree(p, p.work_dir, setup=False)
    pl = driver.Pilot(p, args())
    pl.sleep, pl.deadline = (lambda s: None), now() + timedelta(hours=2)
    return pl


def picks(text):
    return [{"ticket": "T-001", "question": LONG, "built": "A", "option": LONG}]


# Each site: (project, patch, sandbox) -> {title prefix: words its push's first line must hold}

def set_aside(p, patch, s):
    pilot(p).set_aside("T-001", LONG)
    return {"Set aside": ("Sessions died 4×: API Error", "Branch kept, run goes on.")}


def own_limit(p, patch, s):
    pilot(p).handle("T-001", "", "max " + LONG)
    return {"Set aside": ("Hit its max API Error", "Split the ticket.", "Branch kept.")}


def dollar_budget(p, patch, s):
    pilot(p).handle("T-001", "", "dollar budget")  # windvane passes no --max-budget-usd: splitting is the only fix
    return {"Set aside": ("Hit its dollar budget. Split the ticket. Branch kept.",)}


def partials(p, patch, s):
    pl = pilot(p, worktree=True)
    pl.partials["T-001"] = p.ap["partials"] - 1
    pl.handle("T-001", f"RESULT: PARTIAL T-001 {LONG}", None)
    return {"Set aside": ("ran out of time", "Split it.", "Branch kept.")}


def held_for_good(p, patch, s):
    s.ticket("T-002")
    s.write(".windvane/QUEUE.md", "# Queue\n- parked: T-002 (later)\n")
    s.publish()
    pl = pilot(p, worktree=True)
    s.git("switch", "-q", "-c", "t-001-x", cwd=p.work_dir)
    (p.work_dir / ".windvane/tickets/T-001.md").write_text("# T-001: do T-001\nDepends on: T-002.\n")
    s.git("commit", "-qam", "waits", cwd=p.work_dir)
    s.git("push", "-q", "-u", "origin", "t-001-x", cwd=p.work_dir)
    pl.queue = Queue(p)
    pl.handle("T-001", f"RESULT: PARTIAL T-001 {LONG}", None)
    return {"Held": ("waits for T-002 (parked)", "To release it", "on t-001-x.")}


def in_checkout(p, patch, s):
    pl = pilot(p)
    patch(driver.Pilot, "cd", property(lambda self: p.main_root()))
    (s.root / "dirty.txt").write_text("x")
    return pl


def stashed(p, patch, s):
    in_checkout(p, patch, s).keep_changes(LONG)
    return {"your changes were stashed": ("stashed before API Error",)}


def not_stashed(p, patch, s):
    orig = driver.git
    patch(driver, "git", lambda cd, *a: subprocess.CompletedProcess(a, 1, "", LONG) if a[0] == "stash"
          else orig(cd, *a))
    in_checkout(p, patch, s).keep_changes(LONG)
    return {"checkout not cleaned": ("Commit or stash your checkout's changes: API Error",)}


def merged(p, patch, s):
    patch(driver, "with_ids", lambda text, p=None: [{**q, "id": "T-001.p1"} for q in picks(text)])
    pilot(p, worktree=True).merged_ok("T-001", "", s.git("rev-parse", "HEAD"), renumbered=["T-002 to T-003"] * 20)
    return {"Done": ("Built, checked, reviewed and merged.",)}


def failed(p, patch, s):
    patch(net, "online", lambda: True)
    pilot(p).fail("T-001", LONG)
    return {"Failed": ("API Error", "Branch kept, run goes on.")}


def no_answer(p, patch, s):
    pl = pilot(p)
    pl.questions["T-001"] = {"q": LONG, "opts": {"A": LONG, "B": "b"}, "rec": "A", "must_wait": False, "branch": "",
                             "asked": time.time() - 3600}
    pl.poll_questions()
    return {"No answer": ("Going with A: API Error",)}


def late_answer(p, patch, s):
    pl = pilot(p)
    pl.questions["T-001"] = {"q": LONG, "opts": {"A": LONG, "B": "b"}, "rec": "A", "must_wait": False, "branch": "",
                             "asked": time.time() - 3600, "result": "settled on its timeout"}
    questions.give_answer(p, "T-001", "B", LONG)  # given after the pass that settled it read the answers
    pl.poll_questions()
    return {"Answer not taken": ("wasn't taken", "windvane answer T-001 <letter>")}


def quiet_unreadable(p, patch, s):
    patch(driver, "quiet_now", lambda p: (None, LONG))
    pilot(p).quiet_check()
    return {"quiet times ignored": ("API Error", "that file is ignored")}


def quiet_over(p, patch, s):
    patch(driver, "quiet_now", lambda p: (None, None))
    patch(driver, "provisional", picks)
    pl = pilot(p)
    pl.was_quiet = True
    pl.quiet_check()
    return {"Review": ("To change one: windvane answer",)}


def paused(p, patch, s):
    pl = pilot(p)
    pl.usage.five_reset = time.time() + 600
    patch(pl, "nap", lambda *a, **k: None)
    patch(pl.usage, "probe", lambda p: None)
    pl.wait_for_reset()
    return {"paused": ("Resuming at",)}


def started_and_stopped(p, patch, s):
    pl = pilot(p)
    patch(pl, "prepare", lambda slot: True)
    patch(pl, "loop", lambda: setattr(pl, "stop_reason", LONG))
    pl.run(now() + timedelta(hours=2))
    return {"windvane started": ("Until",), "windvane stopped": ("Why: API Error",)}


def couldnt_start(p, patch, s):
    pl = pilot(p)
    patch(pl, "prepare", lambda slot: setattr(pl, "halt", LONG))
    pl.run(now() + timedelta(hours=2))
    return {"windvane couldn't start": ("API Error",)}


def killed(p, patch, s):
    p.state.mkdir(parents=True, exist_ok=True)
    driver.supervise(p, ["run"], now() + timedelta(hours=2), start=lambda a, e: -signal.SIGKILL)
    return {"windvane stopped": ("killed (SIGKILL)", "Not restarted.")}


def start_gone(p, patch, s):
    p.state.mkdir(parents=True, exist_ok=True)

    def start(a, e):
        raise FileNotFoundError(2, "No such file or directory", f"/old/{LONG}/bin/python3")
    driver.supervise(p, ["run"], now() + timedelta(hours=2), start=start)
    return {"windvane stopped": ("couldn't start its driver",)}


def teardown_failed(p, patch, s):
    cfg = s.root / ".windvane/config.toml"
    cfg.write_text(f'teardown = "exit 3 # {LONG}"\n' + cfg.read_text())
    s.publish("config")
    driver.tear_down(Project(s.root), tell=True)
    return {"windvane teardown failed": ("helpers may still be up (exit 3)", "Run it by hand: exit 3 # API Error",
                                         "reset by the peer")}


def leftovers(p, patch, s):
    patch(driver, "leftover_helpers", lambda p, since: [(4242, LONG)])  # a command line pages long, as a JVM's
    driver.report_leftovers(p, 0)
    return {"windvane left helpers running": ("Still running in the autopilot's worktrees: API Error",
                                              "(the rest is in the log) (pid 4242)",
                                              "Set `teardown` in .windvane/config.toml")}


def crashing(p, stop):
    p.state.mkdir(parents=True, exist_ok=True)
    if stop:
        (p.state / "STOP").write_text("")
    runs = []

    def start(a, e):
        runs.append(a)
        if len(runs) == 1:
            (p.state / "crash.json").write_text(json.dumps({"error": LONG, "summary": "Merged: none."}))
            return 1
        return 0
    driver.supervise(p, ["run"], now() + timedelta(hours=2), start=start, sleep=lambda s: None)


def restarted(p, patch, s):
    crashing(p, stop=False)
    return {"windvane restarted": ("The driver crashed: API Error", "Restart 1 of")}


def crashed_stopped(p, patch, s):
    crashing(p, stop=True)
    return {"windvane stopped": ("Why: the driver crashed (API Error", "not restarted: asked to stop.")}


def held(p, patch, s):
    patch(held_mod, "held_branches", lambda p: {"plan-1": "push"})
    patch(held_mod, "ensure_worktree", lambda *a, **k: None)
    patch(held_mod, "clean_detach", lambda *a: True)
    patch(held_mod, "gout", lambda cd, *a: LONG if "--format=%s" in a else "")
    patch(held_mod, "push_branch", lambda *a, **k: "plan-1")
    held_mod.push_held(p)
    return {"1 held-back change pushed": ("API Error", ": on plan-1, pushed.")}


def unmerged_local(p, patch, s):
    patch(planner, "clean_detach", lambda *a: True)
    planner.unmerged(p, 1, f"the local branch plan-1 ({LONG})")
    return {"plan not merged": ("API Error",)}


def unmerged_remote(p, patch, s):
    patch(planner, "clean_detach", lambda *a: True)
    planner.unmerged(p, 1, LONG)
    return {"plan not merged": ("1 ticket waits on API Error", ": merge it by hand.")}


def plan_not_pushed(p, patch, s):
    orig = net.git
    patch(net, "git", lambda cd, *a: subprocess.CompletedProcess(a, 1, "", "refused") if a[0] == "push"
          else orig(cd, *a))
    patch(held_mod, "push_refusal", lambda *a, **k: LONG)
    planner.plan(p, "make it great")
    return {"plan not pushed": ("API Error",)}


def planned(p, patch, s):
    orig = planner.run_claude

    def run_claude(*a, **k):
        text, killed, error = orig(*a, **k)
        return text.replace("one ticket", LONG), killed, error
    patch(planner, "run_claude", run_claude)
    planner.plan(p, "make it great")
    return {"planned 1 ticket": ("API Error",)}


def secret_aside(p, patch, s):
    pilot(p).secret_aside("T-001", "t-001-x", "0" * 40, [("token", f".windvane/{'x' * 250}.md:1 in 1a2b3c4")])
    return {"Set aside": ("Not merged: its commits add a token at .windvane/", "which would stay in main's history.")}


def draft_held(p, patch, s):
    patch(driver, "gh_answer", lambda cmd, cwd: subprocess.CompletedProcess(cmd, int("view" not in cmd), "true", LONG))
    pilot(p).undraft("T-001", "7")
    return {"Held": ("Approved, but PR #7 is a draft and gh couldn't tell who made it one", "so it isn't merged.")}


def kept_secret(p, patch, s):
    ensure_worktree(p, p.plan_dir, setup=False)
    (p.plan_dir / ".windvane/IDEAS.md").write_text("try " + "ghp_" + "Zq7" * 12 + "\n")
    s.git("add", "-A", ".windvane", cwd=p.plan_dir)
    held_mod.land(p, p.plan_dir, "ideas-1", LONG, "body")
    return {"Not pushed": ("Its .windvane/ lines hold a token at .windvane/IDEAS.md:1 in ",
                           "then the next run pushes it.")}


def tmp_full(p, patch, s):
    patch(scratch, "fullness", lambda path=None: (93.4, 20))
    pilot(p).clean_tmp()
    return {"Temp dir nearly full": ("93% full of inodes", "isn't windvane's")}


def not_taken_up(p, patch, s):
    patch(driver, "exe", lambda: [str(s.tmp / "venv/bin/windvane")])
    pilot(p).can_restart()
    return {"windvane update not taken up": ("venv/bin/windvane is gone", "Reinstall windvane")}


def scale_warned(p, patch, s):
    s.write(".windvane/CRITIC.md", "## works\n- 5 safe: safe?\n- 8 safe: still safe?\n")
    pilot(p).scale_note()
    return {"Critic scale": ("Rename one in .windvane/CRITIC.md", "checks named `safe`")}


SITES = [set_aside, own_limit, dollar_budget, partials, held_for_good, stashed, not_stashed, merged, failed, no_answer, late_answer,
         quiet_unreadable, quiet_over, paused, started_and_stopped, couldnt_start, killed, start_gone, teardown_failed, leftovers, restarted, crashed_stopped,
         held, secret_aside, draft_held, kept_secret, unmerged_local, unmerged_remote, plan_not_pushed, planned, tmp_full,
         not_taken_up, scale_warned]


def drive(site):
    """One site in a fresh sandbox: ([((file, line) of the push call, title, body)], words), or the error."""
    got = []

    def push(title, msg="", *a, **k):
        caller = sys._getframe(1)
        if caller.f_code.co_filename in SOURCES:
            got.append(((caller.f_code.co_filename, caller.f_lineno), title, msg))
    s = Sandbox()
    try:
        s.ticket("T-001")
        s.publish()
        with ExitStack() as stack:
            patch = lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value))
            for module in MODULES:
                patch(module, "push", push)
            return got, site(Project(s.root), patch, s)
    except Exception:
        return got, traceback.format_exc()
    finally:
        s.close()


class PushFit(unittest.TestCase):
    driven = None

    @classmethod
    def results(cls):
        """Every site, driven once for both tests."""
        if cls.driven is None:
            cls.driven = {site.__name__: drive(site) for site in SITES}
        return cls.driven

    def test_each_push_leads_with_a_line_that_holds_its_point(self):
        for name, (got, words) in self.results().items():
            with self.subTest(name):
                self.assertIsInstance(words, dict, words)
                for key in words:
                    self.assertTrue(any(t.startswith(key) for _, t, _ in got), (key, got))
                for _, title, msg in got:
                    first = msg.split("\n", 1)[0]
                    self.assertNotIn("…", title + msg)
                    for key, need in words.items():
                        for w in need * title.startswith(key):
                            self.assertIn(w, first, msg)

    def test_every_push_call_in_the_driver_has_a_case(self):
        covered = {at for got, _ in self.results().values() for at, _, _ in got}
        missing = [(path.name, n) for f, path in SOURCES.items() for n in push_calls(path.read_text())
                   if (f, n) not in covered]
        self.assertEqual(missing, [], "these push( calls (file, line) have no case in test_push_fit.py")

    def test_a_new_push_call_is_seen(self):
        source = Path(driver.__file__).read_text()
        self.assertEqual(len(push_calls(source + '\n\ndef new():\n    push("new", "body")\n')),
                         len(push_calls(source)) + 1)


if __name__ == "__main__":
    unittest.main()
