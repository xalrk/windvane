import argparse
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

SCALE = "# What a 10 means\n\n## works\n- 5 runs: the promise happens.\n- 8 again: and on a second pass.\n"
PASSES = "Checks:\n- runs: yes, ran: it runs\n- again: yes, ran: twice\nFine.\nCRITIC: {tid} fixes=0 fine"
READS = "Checks:\n- runs: yes, read: the diff\n- again: yes, read: no state kept\nFine.\nCRITIC: {tid} fixes=0 read"
ASKS = "The lock may outlive a crash, which only running it shows.\nCRITIC: {tid} trial run it twice, killed midway"


class Depth(unittest.TestCase):
    """T-261, through `windvane critic` and the Sandbox's fake `claude`: the driver picks a light read or the full
    trial from the change's size, its risky files and whether anything sent the ticket back; a pinned depth wins; and
    a light read that asks for the trial gets exactly one."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true").replace('depth = "full"', 'depth = "auto"'))
        s.write(".windvane/CRITIC.md", SCALE)
        s.ticket("T-001")
        s.write("src/core.py", "x = 1\n")
        s.publish("critic on, auto depth")
        s.git("switch", "-q", "-c", "t-001-x")
        self.s, self.p = s, Project(s.root)
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\n`python3 tool.py` shows it.\n")
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)
        for k, v in (("FAKE_CRITIC_TEXT", PASSES), ("FAKE_CRITIC_LIGHT", READS)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def change(self, lines, path="tool.py"):
        self.s.write(path, "".join(f"print({i})\n" for i in range(lines)))
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", f"{lines} lines")

    def depth(self):
        return critic.depth(self.p, "T-001", str(self.s.root), "HEAD")

    def critic(self):
        """`windvane critic T-001`: (its output, the critic sessions it ran, its last critic.jsonl line)."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit:
                pass
        calls = [c for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]
        return out.getvalue(), calls, read_jsonl(self.p.state / "critic.jsonl")[-1]

    def labels(self):
        return [r["label"] for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("role") == "critic"]

    def test_a_small_change_gets_a_light_read(self):
        self.change(10)
        self.assertEqual(self.depth(), ("light", "10 lines changed, no [critic] risky file, never sent back"))

    def test_the_autopilots_own_files_dont_count_as_lines(self):
        self.change(5)
        self.s.write(".windvane/NOTES.md", "".join(f"note {i}\n" for i in range(400)))
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "notes")
        self.assertEqual(self.depth()[0], "light")

    def test_a_change_over_light_lines_gets_the_trial(self):
        self.change(251)
        self.assertEqual(self.depth(), ("full", "251 lines changed, over [critic] light_lines = 250"))
        self.p.critic["light_lines"] = 300
        self.assertEqual(self.depth()[0], "light")

    def test_a_risky_file_gets_the_trial_however_small(self):
        self.change(1, "src/core.py")
        self.assertEqual(self.depth()[0], "light", "no risky globs set")
        self.p.critic["risky"] = ["src/"]
        self.assertEqual(self.depth(), ("full", "touches src/core.py"))
        self.p.critic["risky"] = ["src/*.py", "tool.py"]
        self.change(1)
        self.assertEqual(self.depth(), ("full", "touches src/core.py and 1 more [critic] risky files"))

    def test_a_bounced_ticket_is_always_full(self):
        self.change(3)
        for by, said in (("check", "check"), ("reviewer", "check, reviewer")):
            critic.sent_back(self.p, "T-001", by)
            self.assertEqual(self.depth(), ("full", f"sent back before, by {said}"))
        critic.sent_back(self.p, "T-002", "check")
        self.assertEqual(critic.bounced(self.p, "T-002"), ["check"], "each ticket's own")
        self.assertEqual(critic.bounced(self.p, "T-001"), ["check", "reviewer"])

    def test_an_earlier_critic_send_back_makes_the_next_critique_full(self):
        self.change(3)
        os.environ["FAKE_CRITIC_LIGHT"] = READS.replace("- again: yes, read: no state kept", "- again: no, read: a lock")
        _, _, first = self.critic()
        self.assertEqual((first["depth"], first["step"]), ("light", "revise"))
        self.change(4)
        _, calls, again = self.critic()
        self.assertEqual((again["depth"], again["step"]), ("full", "pass"))
        self.assertEqual(again["depth_why"], "sent back before, by critic")
        self.assertIn("## Depth: full (sent back before, by critic)", calls[-1]["prompt"])

    def test_another_tickets_verdict_doesnt_count(self):
        self.change(3)
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-002", "sha": "abc", "step": "revise", "score": 2})
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-001", "step": "none", "why": "no line"})
        self.assertEqual(self.depth()[0], "light", "a no-verdict line sent nothing back")

    def test_a_pinned_depth_wins(self):
        self.change(400)
        self.p.critic["risky"] = ["tool.py"]
        critic.sent_back(self.p, "T-001", "reviewer")
        self.p.critic["depth"] = "light"
        self.assertEqual(self.depth(), ("light", "set by [critic] depth"))
        self.p.critic["depth"] = "full"
        self.change(1)
        self.p.critic["risky"] = []
        self.assertEqual(self.depth(), ("full", "set by [critic] depth"))

    def test_a_light_read_reads_the_diff_with_no_copy_and_few_turns(self):
        self.change(3)
        out, calls, rec = self.critic()
        self.assertEqual(len(calls), 1)
        self.assertEqual((rec["depth"], rec["step"]), ("light", "pass"))
        self.assertNotIn("trial", rec, "a light read runs nothing, so it has no trial outcome")
        prompt = calls[0]["prompt"]
        self.assertIn("## The change\n", prompt)
        self.assertIn("+print(2)", prompt, "the whole diff, not just what users read")
        self.assertIn("## Depth: light (3 lines changed", prompt)
        self.assertNotIn("## Your copy", prompt)
        self.assertIn("Bash", calls[0]["deny"])
        self.assertEqual(calls[0]["max_turns"], str(critic.LIGHT_TURNS))
        self.assertEqual(Path(calls[0]["cwd"]).resolve(), self.s.root.resolve(), "no throwaway copy made")
        self.assertEqual(self.labels(), ["T-001-critic-light"])
        self.assertIn("CRITIC STEP: pass", out)

    def test_a_full_trial_says_its_depth_and_why(self):
        self.change(300)
        _, calls, rec = self.critic()
        self.assertEqual((rec["depth"], rec["trial"]), ("full", "ran"))
        self.assertIn("## Depth: full (300 lines changed, over [critic] light_lines = 250)", calls[0]["prompt"])
        self.assertIn("## Your copy", calls[0]["prompt"])
        self.assertIsNone(calls[0]["max_turns"])
        self.assertEqual(self.labels(), ["T-001-critic"])

    def test_a_light_read_asking_for_the_trial_gets_exactly_one(self):
        self.change(3)
        os.environ["FAKE_CRITIC_LIGHT"] = ASKS
        out, calls, rec = self.critic()
        self.assertEqual(self.labels(), ["T-001-critic-light", "T-001-critic"])
        self.assertEqual((rec["depth"], rec["step"], rec["trial"]), ("full", "pass", "ran"))
        self.assertEqual(rec["asked_trial"], "run it twice, killed midway")
        self.assertIn("## Depth: full (the light read asked for it: run it twice, killed midway)", calls[1]["prompt"])
        self.assertIn("## Your copy", calls[1]["prompt"])
        self.assertEqual(len(read_jsonl(self.p.state / "critic.jsonl")), 1, "one verdict for the one call")

    def test_a_trial_asking_for_the_trial_again_is_no_second_one(self):
        self.change(3)
        os.environ["FAKE_CRITIC_LIGHT"] = ASKS
        os.environ["FAKE_CRITIC_TEXT"] = ASKS  # the trial too, and its re-ask
        out, calls, rec = self.critic()
        self.assertEqual(self.labels().count("T-001-critic"), 1)
        self.assertEqual(self.labels(), ["T-001-critic-light", "T-001-critic", "T-001-critic-reask"])
        self.assertEqual((rec["step"], rec["depth"]), ("none", "full"))
        self.assertIn("CRITIC STEP: none", out)

    def test_a_trial_that_gives_no_verdict_is_still_owed_on_the_rerun(self):
        self.change(3)
        os.environ["FAKE_CRITIC_LIGHT"] = ASKS
        os.environ["FAKE_CRITIC_TEXT"] = ASKS
        out, _, rec = self.critic()
        self.assertEqual(rec["step"], "none")
        self.assertIn("run `windvane critic T-001` again", out)
        os.environ["FAKE_CRITIC_TEXT"] = PASSES  # the rerun, as that line says, and the trial passes
        _, calls, rec = self.critic()
        self.assertEqual(self.labels()[3:], ["T-001-critic"], "straight to the trial: no second light read")
        self.assertEqual((rec["depth"], rec["step"], rec["trial"]), ("full", "pass", "ran"))
        self.assertIn(f"## Depth: full (sent back before, by {critic.ASKED})", calls[-1]["prompt"])

    def test_a_light_read_out_of_turns_gets_the_trial(self):
        self.change(3)
        os.environ["FAKE_MAX_TURNS"] = "# windvane critic"
        os.environ["FAKE_MAX_TURNS_IN"] = "## Depth: light"
        self.addCleanup(os.environ.pop, "FAKE_MAX_TURNS", None)
        self.addCleanup(os.environ.pop, "FAKE_MAX_TURNS_IN", None)
        _, calls, rec = self.critic()
        self.assertEqual(self.labels(), ["T-001-critic-light", "T-001-critic"])
        self.assertEqual((rec["depth"], rec["step"]), ("full", "pass"))
        self.assertEqual(rec["asked_trial"], f"it ran out of its {critic.LIGHT_TURNS} turns")

    def test_a_light_read_for_another_ticket_is_no_trial_ask(self):
        self.assertIsNone(critic.asked_trial(ASKS.replace("{tid}", "T-009"), "T-001", self.p.norm))
        self.assertIsNone(critic.asked_trial(READS.replace("{tid}", "T-001"), "T-001", self.p.norm))
        self.assertEqual(critic.asked_trial("CRITIC: T-001 trial", "T-001", self.p.norm), "no reason given")

    def test_the_log_line_names_the_depth(self):
        self.change(3)
        self.critic()
        d = driver.Pilot.__new__(driver.Pilot)  # only its critic gate, on the verdict just given
        d.p, d.scores, lines = self.p, {}, []
        d.log = lines.append
        self.assertTrue(d.critic_gate("T-001", read_jsonl(self.p.state / "critic.jsonl")[-1]["sha"]))
        self.assertRegex(lines[-1], r"^T-001: critic \S+/10 \(works=\S+\) \(pass, light\): read$")


class DepthConfig(unittest.TestCase):
    def test_a_bad_depth_or_lines_is_refused_at_load(self):
        for extra, said in (('depth = "deep"\n', "[critic] depth"), ("light_lines = -1\n", "[critic] light_lines"),
                            ("light_lines = true\n", "[critic] light_lines"), ('risky = "src/"\n', "[critic] risky")):
            s = Sandbox()
            self.addCleanup(s.close)
            cfg = s.root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace('depth = "full"\n', extra if extra.startswith("depth") else
                                                   'depth = "full"\n' + extra))
            with self.assertRaises(SystemExit) as e:
                Project(s.root)
            self.assertIn(said, str(e.exception.code))


class DepthStats(unittest.TestCase):
    """What `stats --critic` shows by depth: critic tokens per ticket and the reviewer's misses at each."""

    def test_cost_and_misses_by_depth(self):
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        st = p.state
        st.mkdir(parents=True, exist_ok=True)
        recs = [{"at": "2026-10-20 09:00", "ticket": "T-001", "sha": "a", "step": "pass", "score": 8, "depth": "light"},
                {"at": "2026-10-20 10:00", "ticket": "T-002", "sha": "b", "step": "pass", "score": 8, "depth": "full"},
                {"at": "2026-10-20 11:00", "ticket": "T-003", "sha": "c", "step": "revise", "score": 4,
                 "depth": "full"},
                {"at": "2026-10-20 11:30", "ticket": "T-003", "sha": "d", "step": "pass", "score": 8, "depth": "full"},
                {"at": "2026-10-20 12:00", "ticket": "T-004", "sha": "e", "step": "pass", "score": 8, "depth": "full",
                 "asked_trial": "a lock"}]  # started light, and its read asked for the trial
        by = {}
        for r in recs:
            by.setdefault(r["ticket"], []).append(r)
        for tid, label, cache in (("T-001", "T-001-critic-light", 40000), ("T-003", "T-003-critic", 100000),
                                  ("T-003", "T-003-critic", 100000), ("T-004", "T-004-critic-light", 20000),
                                  ("T-004", "T-004-critic", 100000), ("T-002", "T-002-critic", 90000)):
            append_jsonl(st / "ledger.jsonl", {"at": "2026-10-20 13:00", "role": "critic", "label": label,
                                               "ticket": tid, "cache_read": cache * 10})
        (st / "autopilot.log").write_text("2026-10-20 09:20 T-001: review changes: a crash on retry\n"
                                          "2026-10-20 12:10 T-003: review approve: fine\n"
                                          "2026-10-20 12:20 T-004: review approve: fine\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats.depths(p, by, "")
        self.assertEqual(" ".join(out.getvalue().split()),
                         "by depth (a ticket by the depth it started at): light 2 tickets, 80k weighted critic "
                         "tokens each, 1 asked for the trial; full 2 tickets, 145k each. Reviews after a pass that "
                         "asked for changes: light 1 of 1, full 0 of 2")

    def test_nothing_before_depths_are_recorded(self):
        s = Sandbox()
        self.addCleanup(s.close)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats.depths(Project(s.root), {"T-001": [{"ticket": "T-001", "step": "pass"}]}, "")
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
