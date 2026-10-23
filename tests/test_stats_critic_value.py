import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

SCALE = """# Scale
## works
- 2 shown: is the promise shown?
- 5 safe: no known crash?
- 8 quoted: are the docs quoted word for word?
- 10 saving: does it save something measured?
"""
YES = {"shown": "yes", "safe": "yes", "quoted": "yes", "saving": "no"}


def verdict(at, tid, sha, step, failed=()):
    return {"at": at, "ticket": tid, "sha": sha, "score": 8.0 if step == "pass" else 2.0, "dims": {"works": 8.0},
            "step": step, "failed": list(failed), "answers": {**YES, **{cid: "no" for cid in failed}}}


def turn(mid, usage, command=None):
    content = [{"type": "text", "text": "..."}]
    content += [{"type": "tool_use", "id": f"u-{mid}", "name": "Bash", "input": {"command": command}}] * bool(command)
    return {"type": "assistant", "message": {"id": mid, "content": content, "usage": usage}}


def result(mid, step):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"u-{mid}",
                                                     "content": f"...\n\nCRITIC STEP: {step}. Go on."}]}}


def fresh(n):
    return {"input_tokens": n, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


class CriticValue(unittest.TestCase):
    """`stats --critic` says what the critic's rounds bought: a pack-only round on T-001, a tree-changing round on
    T-002 split over two worker runs, and a reviewer bounce after T-002's pass."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.critic["enabled"] = True
        self.s.write(".windvane/CRITIC.md", SCALE)
        st = self.p.state
        for r in [verdict("2026-10-20 09:00", "T-001", "a1", "revise", ["quoted"]),
                  verdict("2026-10-20 09:10", "T-001", "a1", "pass"),  # same head: only the pack changed
                  verdict("2026-10-20 10:00", "T-002", "b1", "revise", ["quoted", "safe"]),
                  verdict("2026-10-20 10:30", "T-002", "b2", "pass"),  # a new head: the tree changed
                  verdict("2026-10-21 11:30", "T-002", "b3", "pass"),  # after the review's fix
                  verdict("2026-10-21 12:00", "T-003", "c1", "pass"),
                  {"at": "2026-10-21 12:00", "ticket": "PLAN"}]:
            append_jsonl(st / "critic.jsonl", r)
        (st / "autopilot.log").write_text(
            "2026-10-20 09:20 T-001: review approve: fine\n"
            "2026-10-20 10:40 T-002: review changes: the retry clock isn't cleared on the error path\n"
            "2026-10-21 11:40 T-002: review approve: the clock is cleared now\n"
            "2026-10-21 12:10 T-003: review approve: fine\n")
        call = "cd /work && windvane critic {}"
        self.stream("T-001", 0, [turn("m1", fresh(500), call.format("T-001")), result("m1", "revise"),
                                 turn("m2", fresh(1000)), turn("m2", fresh(1000)),  # one turn, two content events
                                 turn("m3", fresh(1000), call.format("T-001")), result("m3", "pass"),
                                 turn("m4", fresh(700))])
        # T-002's round starts in a run cut off before it asked again, and ends in the next run
        self.stream("T-002", 400, [turn("n1", fresh(500), call.format("T-002")), result("n1", "revise"),
                                   turn("n2", fresh(3000))])
        self.stream("T-002", 0, [turn("o1", fresh(2000), "windvane critic T-002"), result("o1", "pass")])
        self.stream("T-003", 0, [turn("q1", fresh(9000), call.format("T-003")), result("q1", "pass")])

    def stream(self, tid, output, events):
        name = f"{tid}-{len(list((self.p.state / 'runs').glob('*')))}.jsonl"
        (self.p.state / "runs").mkdir(parents=True, exist_ok=True)
        (self.p.state / "runs" / name).write_text("".join(json.dumps(e) + "\n" for e in events))
        append_jsonl(self.p.state / "ledger.jsonl", {"at": "2026-10-20 13:00", "role": "worker", "label": tid,
                                                     "ticket": tid, "output": output, "stream": name})

    def out(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats.critic_stats(argparse.Namespace(last=10, since=None), self.p)
        return out.getvalue()

    def test_each_count(self):
        text = self.out()
        flat = " ".join(text.split())
        self.assertIn("what it bought: 1 of 3 tickets passed first time, 2 went back once; 2 revisions: "
                      "1 changed the tree, 1 only the pack", flat)
        rows = [line.split() for line in text.splitlines()[text.splitlines().index(
            "  check        yes      sent back: alone  with others  tree  pack only") + 1:][:4]]
        self.assertEqual(rows, [["shown", "6/6", "0", "0", "0", "0"], ["safe", "5/6", "0", "1", "1", "0"],
                                ["quoted", "4/6", "1", "1", "1", "1"], ["saving", "0/6", "0", "0", "0", "0"]])
        # T-001: m2 (counted once) and m3, 1,000 fresh tokens each and no output; T-002: n2 (3,000 plus half of its
        # run's 400 output tokens at 5x) and o1 (2,000)
        self.assertIn("revision rounds in the worker streams: 2 rounds, 4 turns, 8k weighted tokens (tree 1: 2 turns, "
                      "6k; pack only 1: 2 turns, 2k); a round's output tokens are its run's, shared out by turns", flat)
        self.assertIn("critic misses: 1 of 4 reviews after a critic pass asked for changes:\n"
                      "  T-002    2026-10-20 10:40  the retry clock isn't cleared on the error path\n", text)
        self.assertIn("decided nothing: always yes: shown 6/6; never yes over the pass mark: saving 0/6", flat)

    def test_since_drops_older_verdicts_and_reviews(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats.critic_stats(argparse.Namespace(last=10, since="2026-10-21"), self.p)
        flat = " ".join(out.getvalue().split())
        self.assertIn("what it bought: 2 of 2 tickets passed first time", flat)
        self.assertNotIn("revision rounds", flat)
        self.assertIn("critic misses: none of 2 reviews after a critic pass asked for changes", flat)

    def test_rounds_that_dont_line_up_are_counted_not_split(self):
        (self.p.state / "runs" / "T-001-0.jsonl").write_text("")  # T-001's stream lost: no round to pair
        flat = " ".join(self.out().split())
        self.assertIn("1 round, 2 turns, 6k weighted tokens (tree 1: 2 turns, 6k)", flat)
        (self.p.state / "runs" / "T-003-3.jsonl").write_text(
            json.dumps(result("q0", "revise")) + "\n" + json.dumps(turn("q1", fresh(9000))) + "\n")
        flat = " ".join(self.out().split())
        self.assertIn("2 rounds, 3 turns, 15k weighted tokens (tree 1: 2 turns, 6k; 1 not matched to a verdict)", flat)


if __name__ == "__main__":
    unittest.main()
