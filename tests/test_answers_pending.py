"""Desk answers that change a provisional answer write a follow-up ticket and land on an `answers-*` branch. One that
can't merge keeps the follow-up's id taken, as an unmerged plan does, so a later `windvane new` never reuses it."""
import contextlib
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, planner, preflight  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402

PROVISIONAL = ("# Q\n\n## Open\n\n## Provisional\n### T-001: Big or small?\n- **A**: big\n- **B**: small\n"
               "Recommendation: A, because.\nBuilt: A; to change it: SIZE in x.py\n\n## Answered\n")
REFUSE = "#!/bin/sh\nwhile read old new ref; do {test} && {{ echo refused >&2; exit 1; }}; done\nexit 0\n"
CLOSED = "answers-20261020-120000"
GH = f"""#!/bin/sh
case "$*" in
  *"--head {CLOSED} "*) echo '[{{"number": 1, "state": "CLOSED"}}]';;
  *"--head "*) echo '[{{"number": 2, "state": "OPEN"}}]';;
  *) echo '[]';; esac
"""


class AnswersPending(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())  # `-C` changes into the sandbox, which close() removes
        self.s.ticket("T-001")
        self.s.write(".windvane/QUESTIONS.md", PROVISIONAL)
        self.s.publish()
        self.p = Project(self.s.root)

    def cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def new(self):
        """`windvane new`'s ticket id."""
        return Path(self.cli("new", "next").strip()).stem

    def refuse(self, test):
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE.format(test=test))
        hook.chmod(0o755)

    def test_answers_kept_on_a_pushed_branch_keep_the_followups_id(self):
        self.refuse('[ "$ref" = refs/heads/main ]')
        out = self.cli("answer", "T-001", "B")
        branch = re.search(r"they wait on (answers-\S+)", out)
        self.assertTrue(branch, out)
        self.assertIn(".windvane/tickets/T-002.md",
                      self.s.git("ls-tree", "-r", "--name-only", f"origin/{branch[1]}"))
        self.assertEqual(planner.pending_branches(self.p)["T-002"], branch[1])
        self.assertEqual(self.new(), "T-003")

    def test_answers_kept_on_a_local_branch_keep_the_followups_id(self):
        self.refuse("true")
        out = self.cli("answer", "T-001", "B")
        self.assertIn("not merged", out)
        self.assertIn("T-002", planner.pending_branches(self.p))
        self.assertEqual(self.new(), "T-003")

    def test_new_while_record_holds_its_claim_takes_the_id_after_it(self):
        real, during = preflight.land, []

        def land(*a, **k):
            during.append(self.new())
            return real(*a, **k)

        with mock.patch.object(preflight, "land", land):
            self.assertIn("T-002 will change what was built", self.cli("answer", "T-001", "B"))
        self.assertEqual(during, ["T-003"])
        self.assertEqual(list((self.p.state / "id_claims").glob("*")), [])  # released once landed

    def test_a_closed_answers_pr_frees_its_ids(self):
        (self.s.bin / "gh").write_text(GH)
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")
        self.p = Project(self.s.root)
        for tid, branch in (("T-002", "answers-20261020-110000"), ("T-003", CLOSED)):  # the first's PR is open
            self.s.git("switch", "-q", "-c", branch)
            self.s.ticket(tid)
            self.s.git("add", "-A")
            self.s.git("commit", "-qm", "Answers at the desk: T-001")
            self.s.git("push", "-q", "origin", branch)
            self.s.git("switch", "-q", "main")
            self.s.git("branch", "-q", "-D", branch)
        self.assertEqual(planner.pending_ids(self.p) - {"T-001"}, {"T-002"})  # its tree has main's too
        self.assertEqual(self.new(), "T-003")  # `new` asks no forge: closed_plans.json now lists it


if __name__ == "__main__":
    unittest.main()
