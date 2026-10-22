"""The branches the driver lands its own tickets on (a plan's, a periodic chain's, the desk answers') keep those
tickets' ids taken until they merge. Every one is named by `planner.branch_name` from a kind in `OWN_BRANCHES`; their
ids stay taken while the forge can't be asked; and pickers running at once never hand out the same id."""
import contextlib
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import held, planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402

QUESTION = ("### {tid}: Big or small?\n- **A**: big\n- **B**: small\nRecommendation: A, because.\n"
            "Built: A; to change it: SIZE in x.py\n")
REFUSE_MAIN = "#!/bin/sh\nwhile read old new ref; do [ \"$ref\" = refs/heads/main ] && exit 1; done\nexit 0\n"
GH_DOWN = "#!/bin/sh\necho 'error connecting to api.github.com' >&2\nexit 1\n"
ANSWERS = "answers-20261020-120000"


def questions(*tids):
    return "# Q\n\n## Open\n\n## Provisional\n" + "".join(QUESTION.format(tid=t) for t in tids) + "\n## Answered\n"


@contextlib.contextmanager
def landed_names():
    """Every branch name handed to `held.land` or `held.push_branch`, through whichever module imported them: the
    only ways the driver pushes a branch of its own."""
    names = []

    def spy(real):
        def call(p, cd, branch, *a, **k):
            names.append(branch)
            return real(p, cd, branch, *a, **k)
        return call

    with contextlib.ExitStack() as stack:
        for real in (held.land, held.push_branch):
            for m in [m for n, m in list(sys.modules.items()) if n.startswith("windvane")]:
                for attr in [a for a, v in vars(m).items() if v is real]:
                    stack.enter_context(mock.patch.object(m, attr, spy(real)))
        yield names


def sandbox(test, *tids, refuse_main=True):
    s = Sandbox()
    test.addCleanup(s.close)
    test.addCleanup(os.chdir, os.getcwd())  # `-C` changes into the sandbox, which close() removes
    for tid in tids:
        s.ticket(tid)
    s.write(".windvane/QUESTIONS.md", questions(*tids))
    s.publish()
    if refuse_main:  # every landed branch stays on the remote, where its tickets can be read
        hook = s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_MAIN)
        hook.chmod(0o755)
    return s


def windvane(s, *argv):
    return subprocess.Popen(["windvane", "-C", str(s.root), *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


class BranchName(unittest.TestCase):
    def test_an_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            planner.branch_name(None, None, "hotfix")

    def test_every_site_names_its_branch_with_a_listed_kind(self):
        """A plan, a periodic chain and the desk answers, each run for real: a new kind of branch named any other way
        than `branch_name` (missing from `OWN_BRANCHES`) would hand its ids out again."""
        import test_periodic  # the module, not its class: a class imported here would run all its tests again
        s = sandbox(self, "T-001")
        p = Project(s.root)
        with landed_names() as names:
            subprocess.run(["windvane", "-C", str(s.root), "answer", "T-001", "B"], capture_output=True, check=True)
            planner.plan(p, "make it great")
            t = test_periodic.Periodic()
            t.make(test_periodic.PLAYTEST.replace("PLAYTEST", "TRIAGE"), tickets=2)
            self.addCleanup(t.doCleanups)
            t.run_pilot()
        # `windvane answer` ran in its own process: its branch is read off the remote
        names += [line.split("refs/heads/")[1] for line in s.git("ls-remote", "--heads", "origin").splitlines()]
        names = set(names) - {"main"}
        self.assertTrue(all(n.startswith(planner.OWN_BRANCHES) for n in names), names)
        self.assertEqual({n.split("-")[0] + "-" for n in names}, set(planner.OWN_BRANCHES))


class ForgeDown(unittest.TestCase):
    def setUp(self):
        self.s = sandbox(self, "T-001", refuse_main=False)
        (self.s.bin / "gh").write_text(GH_DOWN)
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")
        self.s.git("switch", "-q", "-c", ANSWERS)
        self.s.ticket("T-002")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Answers at the desk: T-001")
        self.s.git("push", "-q", "origin", ANSWERS)
        self.s.git("switch", "-q", "main")
        self.s.git("branch", "-q", "-D", ANSWERS)
        self.p = Project(self.s.root)

    def test_pending_answers_keep_their_ids_while_gh_fails(self):
        self.assertEqual(planner.pending_branches(self.p).get("T-002"), ANSWERS)
        self.assertEqual(planner.pick_ids(self.p, "plan", 1), ["T-003"])
        self.assertFalse((self.p.state / "closed_plans.json").exists())  # nothing taken for closed

    def test_a_known_closed_answers_branch_keeps_its_ids_while_gh_fails(self):
        """It may have been reopened: without the open PRs' list, that can't be told."""
        (self.p.state / "closed_plans.json").parent.mkdir(parents=True, exist_ok=True)
        (self.p.state / "closed_plans.json").write_text(f'["{ANSWERS}"]')
        self.assertEqual(planner.pending_branches(self.p).get("T-002"), ANSWERS)
        self.assertEqual(planner.pick_ids(self.p, "plan", 1), ["T-003"])


class AtOnce(unittest.TestCase):
    def tickets(self, s):
        """{ticket id: every distinct text it has on the remote's branches and the local ones (a refused push's)}."""
        s.git("fetch", "-q", "--prune", "origin")
        out = {}
        for ref in s.git("for-each-ref", "--format=%(refname)", "refs/remotes/origin", "refs/heads").split():
            for line in s.git("ls-tree", ref, ".windvane/tickets/").splitlines():
                blob, name = line.split()[2], line.split("\t")[1]
                out.setdefault(Path(name).stem, set()).add(blob)
        return out

    def test_two_answers_at_once_hand_out_disjoint_ids(self):
        """Both claim under plan.lock and id_lock: whichever goes first, the other counts its follow-up. Landed in
        the same second, the second's branch gets a suffix instead of being refused."""
        s = sandbox(self, "T-001", "T-002")
        runs = [windvane(s, "answer", "T-001", "B"), windvane(s, "answer", "T-002", "B")]
        outs = [r.communicate(timeout=120)[0] for r in runs]
        self.assertEqual([r.returncode for r in runs], [0, 0], outs)
        tickets = self.tickets(s)
        self.assertEqual(sorted(tickets), ["T-001", "T-002", "T-003", "T-004"], outs)
        self.assertEqual({t: b for t, b in tickets.items() if len(b) > 1}, {})  # one text per id: none reused
        pending = planner.pending_branches(Project(s.root), ask_forge=False)
        self.assertEqual(len({pending["T-003"], pending["T-004"]}), 2)  # each on its own answers branch

    def test_answers_during_a_plan_session_take_the_ids_after_its_own(self):
        """Started while the planner writes its ticket under its claim, two answers wait for plan.lock and then count
        the plan's ticket."""
        s = sandbox(self, "T-001", "T-002")
        reached, go = s.tmp / "reached", s.tmp / "go"
        os.mkfifo(reached)
        os.mkfifo(go)
        env = dict(os.environ, FAKE_SESSION_RUN=f"echo in > {reached}; cat {go} > /dev/null")
        plan = subprocess.Popen(["windvane", "-C", str(s.root), "plan", "make it great"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        reached.read_text()  # the planner session is running: its id is claimed, plan.lock held
        answers = [windvane(s, "answer", "T-001", "B"), windvane(s, "answer", "T-002", "B")]
        time.sleep(0.5)  # long enough for both to be waiting on plan.lock
        go.write_text("go\n")
        runs = [plan, *answers]
        outs = [r.communicate(timeout=120)[0] for r in runs]
        self.assertEqual([r.returncode for r in runs], [0, 0, 0], outs)
        tickets = self.tickets(s)
        self.assertEqual(sorted(tickets), ["T-001", "T-002", "T-003", "T-004", "T-005"], outs)
        self.assertEqual({t: b for t, b in tickets.items() if len(b) > 1}, {})  # one text per id: none reused
        branch = s.git("for-each-ref", "--format=%(refname)", "refs/remotes/origin/plan-*").strip()
        plan_tickets = s.git("ls-tree", "--name-only", branch, ".windvane/tickets/").split()
        self.assertEqual(plan_tickets[-1], ".windvane/tickets/T-003.md")  # its own, after main's T-001, T-002

if __name__ == "__main__":
    unittest.main()
