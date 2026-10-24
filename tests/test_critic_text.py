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

SCALE = "# What a 10 means\n\n## clear\n- 10 said: every new or changed line says what happened.\n"
HEAD = "## What users read, as changed\n"


class CriticReadsText(unittest.TestCase):
    """Through `windvane critic` and the Sandbox's fake `claude`: the critic's prompt holds the diff of the files users
    read (Markdown outside .windvane/, and `[critic] text`), so it judges the text there and the pack quotes none."""

    def setUp(self):
        s = Sandbox('text = ["src/messages/*.json"]\n')  # the Sandbox's config ends in [critic]
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", SCALE)
        s.write("docs/x.md", "# X\n\nOld intro.\n\nKeep this.\n")
        s.write("CHANGELOG.md", "# Changelog\n\n## Unreleased\n")
        s.write("src/messages/en.json", '{"done": "Done."}\n')
        s.write("src/main.py", "print('hi')\n")
        s.ticket("T-001")
        s.publish("critic on")
        s.git("switch", "-q", "-c", "t-001-x")
        self.s, self.p = s, Project(s.root)
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\nThe docs, the changelog and the done message say what the sync does now.\n")
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)

    def change(self, files):
        for rel, text in files.items():
            self.s.write(rel, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "change")

    def critic(self, reads="said sync now keeps"):
        """`windvane critic T-001`, its fake critic saying `said: yes` only when the prompt's diff adds `reads`:
        (its last line, the critic's prompt)."""
        os.environ["FAKE_CRITIC_READS"] = reads
        self.addCleanup(os.environ.pop, "FAKE_CRITIC_READS", None)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
        calls = [c["prompt"] for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]
        self.assertEqual(len(calls), 1)
        return out.getvalue().strip().splitlines()[-1], calls[0]

    def section(self, prompt):
        self.assertIn(HEAD, prompt)
        return prompt.split(HEAD, 1)[1].split("\n## ")[0]

    def test_docs_changelog_and_text_globs_reach_the_critic_and_it_judges_them(self):
        self.change({"docs/x.md": "# X\n\nThe sync now keeps your edits.\n\nKeep this.\n",
                     "CHANGELOG.md": "# Changelog\n\n## Unreleased\n- The sync now keeps your edits.\n",
                     "src/messages/en.json": '{"done": "Synced: your edits are kept."}\n',
                     "src/main.py": "print('the source line')\n",
                     ".windvane/QUEUE.md": "# Queue\n- the queue line\n",
                     ".windvane/NOTES.md": "the notes line\n"})
        last, prompt = self.critic()
        diff = self.section(prompt)
        for line in ("-Old intro.", "+The sync now keeps your edits.", "+- The sync now keeps your edits.",
                     '+{"done": "Synced: your edits are kept."}'):
            self.assertIn(line, diff)
        self.assertNotIn("Keep this.", diff.split("+The sync now keeps your edits.")[0], "one line of context")
        for gone in ("the source line", "the queue line", "the notes line", ".windvane/"):
            self.assertNotIn(gone, diff, "source and the project's state stay out")
        self.assertNotIn("sync now keeps", self.notes(), "the pack quotes nothing")
        self.assertTrue(last.startswith("CRITIC STEP: pass"), last)
        self.assertEqual(read_jsonl(self.p.state / "critic.jsonl")[-1]["score"], 10)

    def test_without_the_text_in_the_diff_the_same_pack_fails_the_check(self):
        self.change({"src/main.py": "print('the sync now keeps your edits')\n"})
        last, prompt = self.critic()
        self.assertNotIn(HEAD, prompt, "no section when no file users read changed")
        self.assertRegex(last, r"^CRITIC STEP: (revise|rethink)")

    def test_an_oversized_diff_is_cut_to_the_cap_biggest_hunks_first_and_says_so(self):
        far = "\n" * 6  # far enough apart to be hunks of their own
        old = far.join(f"para {i}" for i in range(30)) + "\n"
        self.s.write("docs/big.md", old)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "big doc")
        self.s.git("push", "-q", "origin", "HEAD:main")  # on main before the change
        new = old.replace("para 3\n", "para 3, the sync now keeps your edits\n")
        new = new.replace("para 20\n", "para 20 " + "x" * (critic.TEXT_CAP + 500) + "\n")
        self.change({"docs/big.md": new, "CHANGELOG.md": "# Changelog\n\n## Unreleased\n- A small line.\n"})
        last, prompt = self.critic()
        diff = self.section(prompt)
        body = diff.split("\n```", 1)[0]
        self.assertLessEqual(len(body), critic.TEXT_CAP + 20, "the diff holds no more than the cap")
        self.assertNotIn("x" * 100, diff, "the giant hunk is the one cut")
        self.assertIn("+para 3, the sync now keeps your edits", diff, "the small hunks stay whole")
        self.assertIn("+- A small line.", diff)
        self.assertIn(f"Cut to {critic.TEXT_CAP:,} characters: 1 hunk (", diff)
        self.assertIn("the biggest, of docs/big.md (1). Read those files for the rest.", diff)
        self.assertTrue(last.startswith("CRITIC STEP: pass"), last)

    def test_a_cut_hunk_in_a_file_git_quotes_is_named_by_its_path(self):
        odd = 'docs/say "hi"\tnow.md'  # git's headers quote this name: the cut note mustn't read it from them
        self.change({odd: "y" * (critic.TEXT_CAP + 10) + "\n",
                     "CHANGELOG.md": "# Changelog\n\n## Unreleased\n- The sync now keeps your edits.\n"})
        last, prompt = self.critic()
        diff = self.section(prompt)
        self.assertIn(f"the biggest, of {odd} (1). Read those files for the rest.", diff)
        self.assertIn("+- The sync now keeps your edits.", diff)
        self.assertTrue(last.startswith("CRITIC STEP: pass"), last)

    def test_code_fences_in_the_docs_dont_close_the_section(self):
        self.change({"docs/x.md": "# X\n\n```\nThe sync now keeps your edits.\n```\n\nKeep this.\n"})
        last, prompt = self.critic()
        diff = self.section(prompt)
        self.assertTrue(diff.startswith("````diff\n"), diff[:20])
        self.assertTrue(diff.rstrip().endswith("\n````"), diff[-20:])
        self.assertIn("+```\n+The sync now keeps your edits.\n+```", diff)
        self.assertTrue(last.startswith("CRITIC STEP: pass"), last)

    def test_a_bad_text_setting_is_refused_at_load(self):
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace('text = ["src/messages/*.json"]', 'text = "src/messages/*.json"'))
        with self.assertRaises(SystemExit) as e:
            Project(self.s.root)
        self.assertIn("[critic] text", str(e.exception.code))

    def notes(self):
        return (critic.pack_dir(self.p, "T-001") / "notes.md").read_text()

    def test_the_rules_say_where_the_text_is(self):
        self.assertIn("What users read, as changed", (REPO / "prompts/CRITIC.md").read_text())
        self.assertIn("only what the critic can't\n     run itself or read in the diff", (REPO / "prompts/WORKER.md").read_text())


if __name__ == "__main__":
    unittest.main()
