import argparse
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

SCALE = ("# What a 10 means\n\n## works\n- 5 runs: the promise happens.\n- 8 again: and on a second pass.\n\n"
         "## clear\n- 5 said: it says what it did.\n- 8 short: in a line.\n")
FIRST = ("Checks:\n- runs: yes, ran: tool.py prints\n- again: no, ran: a second run crashed on the lock\n- clear: -\n"
         "It breaks on a rerun.\nFix in this ticket:\n1. a second run crashes -> it runs again cleanly\n"
         "Beyond this ticket:\n- no progress line -> add one\nCRITIC: {tid} fixes=1 crashes on a rerun")
FIXED = "Checks:\n- again: yes, ran: two runs, no crash\nFixed.\nCRITIC: {tid} fixes=0 runs twice"


class Recritique(unittest.TestCase):
    """T-264, through `windvane critic` and the Sandbox's fake `claude`: a re-critique on a ladder judges the diff
    since the head it last judged, answers that verdict's failed checks and points, and the `yes` answers it leaves
    out carry over; a first critique's unanswered check still counts as `no`."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", SCALE)
        s.ticket("T-001")
        s.publish("critic on")
        s.git("switch", "-q", "-c", "t-001-x")
        self.s, self.p = s, Project(s.root)
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\n`python3 tool.py` shows it.\n")
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)
        for k in ("FAKE_CRITIC_TEXT", "FAKE_CRITIC_REASK"):
            self.addCleanup(os.environ.pop, k, None)
        self.change("print('one')\n")

    def change(self, text, path="tool.py"):
        self.s.write(path, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", f"{path} changed")

    def critic(self, reply):
        """`windvane critic T-001` answered with `reply`: (its output, the critic's prompt, its critic.jsonl line)."""
        os.environ["FAKE_CRITIC_TEXT"] = reply
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit:
                pass
        calls = [c for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]
        return out.getvalue(), calls[-1]["prompt"], read_jsonl(self.p.state / "critic.jsonl")[-1]

    def test_a_recritique_holds_the_delta_the_failed_checks_and_the_yes_answers(self):
        _, first_prompt, first = self.critic(FIRST)
        self.assertNotIn("## What changed since", first_prompt)
        self.assertEqual(first["step"], "revise")
        self.change("import fcntl\nprint('one')\n")
        _, prompt, _ = self.critic(FIXED)
        now = self.s.git("rev-parse", "--short", "HEAD")
        self.assertIn(f"## What changed since your last critique ({first['sha']} → {now})\ntool.py | 1 +", prompt)
        self.assertIn("+import fcntl", prompt)
        self.assertIn("failed checks (answer each afresh):\n- again: no, ran: a second run crashed on the lock",
                      prompt)
        self.assertIn("points (is each fixed?):\n1. a second run crashes -> it runs again cleanly", prompt)
        self.assertIn("then answer them again):\n- runs: yes, ran: tool.py prints", prompt)
        self.assertIn("reach the planner):\n- no progress line -> add one", prompt)
        self.assertNotIn("- clear:", prompt.split("## What changed since")[1], "a `-` dimension is neither")
        self.assertNotIn("## Your earlier critiques", prompt, "the section replaces them on a ladder")

    def test_a_reply_answering_only_the_failed_checks_scores_with_the_carried_ones(self):
        self.critic(FIRST)
        self.change("import fcntl\nprint('one')\n")
        out, _, rec = self.critic(FIXED)
        self.assertEqual((rec["step"], rec["score"]), ("pass", 8))
        self.assertEqual(rec["carried"], ["runs", "clear"])
        self.assertEqual(rec["answers"], {"runs": "yes", "again": "yes"})
        self.assertEqual(rec["unanswered"], [])
        self.assertEqual(rec["yes"], {"runs": "ran: tool.py prints", "again": "ran: two runs, no crash"})
        self.assertIn("Carried from the last critique", rec["critique"])
        self.assertIn("CRITIC STEP: pass", out)

    def test_a_carried_answer_carries_again(self):
        self.critic(FIRST)
        self.change("import fcntl\nprint('one')\n")
        _, _, second = self.critic(FIXED)
        self.change("import fcntl\nprint('one!')\n")
        _, prompt, third = self.critic("Checks:\n- runs: yes, ran: prints one!\nCRITIC: {tid} fixes=0 fine")
        self.assertIn("- runs: yes, ran: tool.py prints\n- again: yes, ran: two runs, no crash", prompt)
        self.assertEqual((third["since"], third["carried"], third["step"]), (second["sha"], ["again", "clear"], "pass"))

    def test_a_carried_yes_answered_no_counts_as_no(self):
        self.critic(FIRST)
        self.change("import fcntl\n")
        _, _, rec = self.critic("Checks:\n- runs: no, ran: it prints nothing now\n- again: yes, ran: no crash\n"
                                "CRITIC: {tid} fixes=0 broke it")
        self.assertEqual(rec["answers"]["runs"], "no")
        self.assertNotIn("runs", rec["carried"])
        self.assertEqual((rec["step"], rec["failed"]), ("rethink", ["runs"]), "its first rung")

    def test_a_same_sha_recritique_says_the_tree_didnt_change(self):
        self.critic(FIRST)
        _, prompt, rec = self.critic(FIXED)
        head = self.s.git("rev-parse", "--short", "HEAD")
        self.assertIn(f"Nothing in the tree: the head is {head} again", prompt)
        self.assertIn("Judge the new pack only.", prompt)
        self.assertNotIn("```diff", prompt.split("## What changed since")[1])
        self.assertEqual(rec["since"], head)

    def test_a_first_critique_that_leaves_a_check_out_counts_it_no(self):
        _, _, rec = self.critic("Checks:\n- runs: yes, ran: it prints\n- clear: -\nCRITIC: {tid} fixes=0 fine")
        self.assertEqual(rec["unanswered"], ["again"])
        self.assertEqual(rec["step"], "revise")
        self.assertNotIn("carried", rec)

    def test_an_all_yes_verdict_rechecked_on_its_points_alone_is_a_verdict(self):
        self.critic("Checks:\n- runs: yes, ran: it prints\n- again: yes, ran: twice\n- clear: -\n"
                    "Fix in this ticket:\n1. the help says `tool` -> say `tool.py`\nCRITIC: {tid} fixes=1 help")
        self.change("print('one')\n", "help.txt")
        out, _, rec = self.critic("The help names tool.py now.\nCRITIC: {tid} fixes=0 fixed")
        self.assertEqual((rec["step"], rec["carried"]), ("pass", ["runs", "again", "clear"]))

    def test_only_the_tickets_own_files_count_after_merging_the_upstream(self):
        self.critic(FIRST)
        self.s.git("switch", "-q", "main")
        self.change("other\n", "other.py")
        self.s.git("push", "-q", "origin", "main")
        self.s.git("switch", "-q", "t-001-x")
        self.s.git("fetch", "-q", "origin")
        self.s.git("merge", "-q", "--no-edit", "origin/main")
        self.change("import fcntl\nprint('one')\n")
        _, prompt, _ = self.critic(FIXED)
        delta = prompt.split("## What changed since")[1]
        self.assertIn("tool.py", delta)
        self.assertNotIn("other.py", delta, "the upstream's change is no part of the revision")

    def test_a_changed_scale_or_a_numeric_one_judges_afresh(self):
        self.critic(FIRST)
        self.s.write(".windvane/CRITIC.md", SCALE + "- 9 fast: under a second.\n")
        _, prompt, rec = self.critic(FIXED)
        self.assertNotIn("## What changed since", prompt)
        self.assertIn("## Your earlier critiques of this ticket", prompt)
        self.assertNotIn("carried", rec)
        self.assertIn("again", rec["answers"])
        self.assertIn("runs", rec["unanswered"])

    def test_a_numeric_scale_keeps_its_earlier_critiques_unscored(self):
        self.s.write(".windvane/CRITIC.md", "# What a 10 means\n\nworks: it runs.\n")
        self.critic("Scores: works=6\nIt crashes.\nCRITIC: {tid} works=6 fixes=1 crashes")
        self.change("import fcntl\n")
        _, prompt, rec = self.critic("Scores: works=8\nCRITIC: {tid} works=8 fixes=0 fine")
        earlier = prompt.split("## Your earlier critiques of this ticket, oldest first\n")[1]
        self.assertIn("It crashes.", earlier)
        self.assertNotIn("works=6", earlier, "no anchor on the earlier number")
        self.assertNotIn("## What changed since", prompt)

    def labels(self):
        return [r["label"] for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("role") == "critic"]

    def test_an_answer_naming_no_check_is_reasked_not_carried_over(self):
        self.critic(FIRST)
        self.change("import fcntl\n")
        broke = "Checks:\n- run: no, ran: it prints nothing now\n- again: yes, ran: no crash\nCRITIC: {tid} fixes=0 broke"
        os.environ["FAKE_CRITIC_REASK"] = broke.replace("- run:", "- runs:")
        _, _, rec = self.critic(broke)
        self.assertEqual(self.labels()[-1], "T-001-critic-reask")
        self.assertEqual((rec["answers"]["runs"], rec["step"], rec["reasked"]), ("no", "rethink", True))
        self.assertNotIn("stray", rec)

    def test_an_answer_off_its_bullet_is_reasked_and_still_stray_carries_nothing(self):
        self.critic(FIRST)
        self.change("import fcntl\n")
        loose = "Checks:\nruns: no, ran: it prints nothing now\n- again: yes, ran: no crash\nCRITIC: {tid} fixes=0 broke"
        os.environ["FAKE_CRITIC_REASK"] = loose
        _, reasked, rec = self.critic(loose)
        self.assertIn("these answers count for no check (`runs:`)", reasked, "the re-ask's prompt")
        self.assertEqual((rec["stray"], rec["carried"], rec["unanswered"]), (["runs"], [], ["runs", "said", "short"]))
        self.assertEqual(rec["step"], "rethink", "with nothing carried, the unanswered check is a no")

    def test_a_run_with_no_verdict_or_a_dead_critic_leaves_the_last_verdict_to_judge_from(self):
        _, _, first = self.critic(FIRST)
        self.change("import fcntl\nprint('one')\n")
        os.environ["FAKE_CRITIC_REASK"] = "still no line"
        _, _, none = self.critic("no CRITIC line at all")
        self.assertEqual(none["step"], "none")
        _, prompt, rec = self.critic(FIXED)
        self.assertIn(f"## What changed since your last critique ({first['sha']} → ", prompt)
        self.assertEqual((rec["since"], rec["step"]), (first["sha"], "pass"))

    def test_a_judged_head_that_is_gone_judges_afresh(self):
        self.critic(FIRST)
        recs = read_jsonl(self.p.state / "critic.jsonl")
        recs[-1]["sha"] = "0000000"
        (self.p.state / "critic.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        _, prompt, rec = self.critic(FIXED)
        self.assertNotIn("## What changed since", prompt)
        self.assertIn("## Your earlier critiques of this ticket", prompt)
        self.assertNotIn("carried", rec)
        self.assertEqual(rec["step"], "rethink", "nothing carried: `runs` unanswered is a no")

    def test_stats_counts_the_later_runs_and_the_carried_checks_apart(self):
        self.critic(FIRST)
        self.change("import fcntl\nprint('one')\n")
        self.critic(FIXED)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats.critic_stats(argparse.Namespace(last=10, since=None), self.p)
        line = " ".join(out.getvalue().split()).split("critic runs: ")[1]
        self.assertRegex(line, r"^first critiques 1, [\d.]+ turns and \d+k weighted tokens each; later ones 1, [\d.]+ "
                               r"turns and \d+k each, \d+k per ticket; 2 checks carried over in 1 re-critique")


if __name__ == "__main__":
    unittest.main()
