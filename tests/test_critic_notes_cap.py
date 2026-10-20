import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import REPO, Sandbox  # noqa: E402


class NotesCap(unittest.TestCase):
    """Through `windvane critic` and the Sandbox's fake `claude`: notes over the cap start no critic session and
    say why; notes at the cap reach the critic whole."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.ticket("T-001")
        s.publish("critic on")
        s.git("switch", "-q", "-c", "t-001-x")
        self.s, self.p = s, Project(s.root)
        self.notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        self.notes.parent.mkdir(parents=True)
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)

    def critic(self, size):
        """`windvane critic T-001` on a pack of `size` characters ending in a line of its own: (its exit, its output,
        the prompts of the critic sessions it ran)."""
        last = "the last line, quoted in full\n"
        head = "User-facing: yes\n"
        body = "".join(f"line {i:04} of a long verbatim quote\n" for i in range(size // 32))
        n = size - len(head) - len(last) - 1
        body = body[:n].ljust(n, "x") + "\n"
        self.notes.write_text(head + body + last)
        self.assertEqual(len(self.notes.read_text()), size)
        out, code = io.StringIO(), 0
        with redirect_stdout(out), redirect_stderr(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit as e:
                code = e.code
                if isinstance(code, str):  # sys.exit("message") prints it to stderr and exits 1
                    print(code, file=sys.stderr)
        calls = [c["prompt"] for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]
        return code, out.getvalue().strip(), calls

    def test_a_pack_over_the_cap_starts_no_critic_and_says_its_size(self):
        code, out, calls = self.critic(5061)
        self.assertEqual(calls, [], "no critic session for a pack it would cut")
        self.assertTrue(code, "it exits non-zero")
        self.assertEqual(out, f"CRITIC STEP: none. notes.md is 5061 characters; the critic reads at most "
                              f"{critic.NOTES_CAP}: shorten it and run `windvane critic T-001` again.")
        self.assertEqual(read_jsonl(self.p.state / "critic.jsonl"), [], "no revision, nor a no-verdict line")

    def test_a_pack_under_the_cap_reaches_the_critic_whole(self):
        code, out, calls = self.critic(3900)
        self.assertIn(code, (0, None), out)
        self.assertEqual(len(calls), 1)
        self.assertIn(self.notes.read_text(), calls[0], "the whole file, its last line too")
        self.assertIn("the last line, quoted in full\n", calls[0])
        self.assertTrue(out.splitlines()[-1].startswith("CRITIC STEP: pass"), out)

    def test_a_pack_at_the_cap_still_goes(self):
        _, _, calls = self.critic(critic.NOTES_CAP)
        self.assertEqual(len(calls), 1)

    def test_the_worker_rules_give_the_cap(self):
        self.assertIn(f"at most 25 lines and {critic.NOTES_CAP} characters", (REPO / "prompts/WORKER.md").read_text())


if __name__ == "__main__":
    unittest.main()
