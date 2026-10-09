"""`windvane request`'s gaps (T-125): an accept held on a plan whose PR was closed unmerged goes back to the inbox,
and a session claiming to be the desk from outside its tree is still the session its env names."""
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import planner, requests, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
import test_requests  # noqa: E402
from test_requests import TRIAGE  # noqa: E402

CLOSED = "plan-20261008-120000"
GH = f"""#!/bin/sh
echo "$*" >> "$(dirname "$0")/gh.log"
case "$*" in *"--head {CLOSED} "*) echo CLOSED;; *"--head "*) echo OPEN;; esac
exit 0
"""


class RequestReopen(unittest.TestCase):
    R = test_requests.Requests  # its helpers, not its tests
    make, cli, file, inbox = R.make, R.cli, R.file, R.inbox

    def github(self):
        """The forge set to github, with a fake `gh` that reports the plan branch CLOSED's PR closed unmerged."""
        (self.s.bin / "gh").write_text(GH)
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")  # and any ticket written before
        self.p = Project(self.s.root)

    def plan_branch(self, tid, branch):
        """A plan pushed but not merged: `branch` adds ticket `tid`."""
        self.s.git("checkout", "-q", "-b", branch)
        self.s.ticket(tid)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", f"Plan: {tid}")
        self.s.git("push", "-q", "origin", branch)
        self.s.git("checkout", "-q", "main")
        self.s.git("branch", "-q", "-D", branch)

    def accept(self, text, held):
        given = requests.files(False)
        requests.apply(given, text, set(), lambda t: t == "T-001", lambda m: None, held)

    def test_an_accept_held_on_a_closed_plan_goes_back_to_the_inbox_once(self):
        self.make(TRIAGE)
        self.s.ticket("T-001")  # landed
        self.github()
        self.plan_branch("T-009", CLOSED)
        self.plan_branch("T-010", "plan-20261008-130000")  # its PR still open
        for title in ("Retry the check on a flaky network", "A stats column for cost", "Quiet hours on Sunday"):
            self.assertEqual(self.file(title)[0], 0)
        self.accept("REQUEST: R-001 accept T-009\nREQUEST: R-002 accept T-001\nREQUEST: R-003 accept T-010",
                    {"T-009": CLOSED, "T-010": "plan-20261008-130000"})
        self.assertEqual(sorted(self.inbox(done=True)), ["R-001", "R-002", "R-003"])
        self.assertEqual(len(requests.unseen(self.p)), 3)  # the filer saw each first verdict
        self.s.git("fetch", "-q", "origin")

        self.assertEqual(set(planner.pending_branches(self.p)) - {"T-001"}, {"T-010"})  # T-009's plan is closed
        self.assertEqual(list(self.inbox()), ["R-001"])
        text = self.inbox()["R-001"]
        self.assertIn(f"- Verdict: reopened: {CLOSED} was closed unmerged", text)
        self.assertEqual(text.count("\n## Reopened ("), 1)
        self.assertIn(f"): {CLOSED}, which held its ticket, was closed unmerged\n", text)
        self.assertEqual(sorted(self.inbox(done=True)), ["R-002", "R-003"])  # landed, or held on an open PR
        self.assertEqual([f.stem for f in requests.pending()], ["R-001"])  # the next triage gets it
        self.assertIn(f"back in the request inbox: {CLOSED}", (self.p.state / "autopilot.log").read_text())

        # a stale accept onto the same closed plan: the branch was dealt with once, and isn't again
        self.accept("REQUEST: R-001 accept T-009", {"T-009": CLOSED})
        planner.pending_branches(self.p)
        self.assertEqual(self.inbox(), {})
        self.assertEqual(self.inbox(done=True)["R-001"].count("\n## Reopened ("), 1)
        self.assertEqual((self.s.bin / "gh.log").read_text().count(f"--head {CLOSED} "), 1)
        # the filer hears the new verdict, though it saw R-001's first one
        self.assertEqual(requests.unseen(self.p), ["R-001 accepted → windvane's T-009 (held on "
                                                   f"{CLOSED})  (Retry the check on a flaky network)"])

    def test_a_project_without_triage_leaves_the_inbox_alone(self):
        self.make()
        self.github()
        self.plan_branch("T-009", CLOSED)
        self.file("Retry the check on a flaky network")
        self.accept("REQUEST: R-001 accept T-009", {"T-009": CLOSED})
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(planner.pending_branches(self.p), {})
        self.assertEqual(list(self.inbox(done=True)), ["R-001"])

    def recorded(self, session, role):
        """A session's role as runner.py records it in the state dir of the project it runs for."""
        d = self.p.state / "nested" / session
        d.mkdir(parents=True)
        (d / "role").write_text(role)

    def test_a_desk_claim_inside_a_session_is_the_sessions_role(self):
        self.make()
        os.chdir(self.s.tmp)  # outside windvane's state dir, as a worker that cd'd away or ran in the user's tree
        with unittest.mock.patch.dict(os.environ, {requests.ROLE: "desk"}):
            self.assertEqual(requests.role_here(), "desk")  # the user's own shell
        self.recorded("20261009-031029-T-009", "worker")
        os.chdir(self.s.root)
        for session, role in (("20261009-031029-T-009", "worker"), ("20261009-031030-critic", "session"),
                              ("../../x", "session")):
            with unittest.mock.patch.dict(os.environ, {requests.ROLE: "desk:x", requests.NEST: session}):
                self.assertEqual(requests.role_here(), role)
        self.recorded("20261009-031031-odd", "desk")  # no session is the desk, whatever its record says
        with unittest.mock.patch.dict(os.environ, {requests.ROLE: "desk", requests.NEST: "20261009-031031-odd"}):
            self.assertEqual(requests.role_here(), "session")
        desk = {requests.ROLE: "desk", requests.NEST: "20261009-031029-T-009"}
        self.assertEqual(self.file("Retry the check on a flaky network", **desk)[0], 0)
        self.assertIn("- Role: worker", self.inbox()["R-001"])
        self.assertEqual(self.file("A stats column for cost", **desk)[0], 0)
        code, out = self.file("Quiet hours on Sunday", **desk)  # the worker's cap holds outside its tree too
        self.assertEqual(code, 1)
        self.assertIn("refused: a worker run files at most 2 requests", out)
        code, out = self.file("Quiet hours", **{requests.ROLE: "desk", requests.NEST: "20261009-031030-critic"})
        self.assertEqual(code, 1)
        self.assertIn("only desk sessions and workers file requests (this is a windvane session)", out)

    def test_a_session_records_its_role_while_it_runs(self):
        self.make()
        role = self.p.state / "nested" / "$WINDVANE_SESSION" / "role"
        (self.s.bin / "claude").write_text(
            '#!/bin/sh\nr=$(cat "' + str(role).replace("$WINDVANE_SESSION", '"$WINDVANE_SESSION"') + '")\n'
            'echo \'{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"\'"$r"\'"}\'\n')
        text, killed, error = runner.run_claude(self.p, "worker", "T-009", "hi", "# windvane test", self.s.root)
        self.assertEqual((text, killed, error), ("worker", None, None))
        self.assertEqual(list((self.p.state / "nested").glob("*")), [])  # gone with the session


if __name__ == "__main__":
    unittest.main()
