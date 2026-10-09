"""A stranger's first hour on a machine that has never seen windvane: install.sh into an empty home, then doctor,
install, init, new and a run that merges the first ticket, each step the one the step before named. Offline: the
package builds from this checkout with the in-tree backend, and `claude` is the Sandbox's fake.
tools/clean_machine_check.sh runs this same test in a throwaway container."""
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FAKE_CLAUDE, REPO, tmpdir  # noqa: E402
from test_install import HELP  # noqa: E402

# In front of the Sandbox's fake: answers doctor's `claude --help` and `claude auth status`, and leaves the evidence
# pack a worker's prompt asks for (the driver then runs the critic itself), as a real worker would.
CLAUDE = f'''#!/usr/bin/env python3
import os, re, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    sys.exit(print('{{"loggedIn": true}}'))
if args[:1] == ["--help"]:
    sys.exit(print("{HELP} --allowedTools --disallowed-tools"))
if args[:1] == ["--version"]:
    sys.exit(print("2.1.0 (Claude Code)"))
m = re.search(r"evidence pack goes in (\\S+)/ ", args[args.index("-p") + 1])
if m:
    os.makedirs(m.group(1), exist_ok=True)
    open(os.path.join(m.group(1), "notes.md"), "w").write("User-facing: yes\\nIt says hello.\\n")
os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(__file__), "claude-session"), *args])
'''
# The critic's reply on the scale init's CRITIC.md starts with (its example ladder, until the user writes their own)
CRITIQUE = ("Checks:\n- shown: yes, the transcript\n- safe: yes, seen\n- failures: yes, seen\n- class: no\n"
            "Beyond this ticket:\n- more\nCRITIC: 8/10 {tid} fixes=0 says hello")

OPTIONAL = {"claude", "windvane", "gh", "tmux", "notify", "notify-ask"}  # the clean machine has none of these


def next_steps(text):
    """The commands a message tells the user to run next: `next: <cmd>` lines and backticked `windvane ...`."""
    nexts = "\n".join(re.findall(r"^next: (.*)", text, re.M))
    return re.findall(r"\S*windvane \w+", nexts) + re.findall(r"`(\S*windvane [^`]+)`", text)


class CleanMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self, "windvane-clean-")
        self.home = self.tmp / "home"
        self.home.mkdir()
        fakes = self.tmp / "fakes"
        fakes.mkdir()
        (fakes / "claude").write_text(CLAUDE)
        (fakes / "claude-session").write_text(FAKE_CLAUDE)
        (fakes / "claude").chmod(0o755)
        (fakes / "python3").symlink_to(sys.executable)
        # Nothing but the system's tools and `claude`: no XDG dirs, no git identity, and none of the optional tools
        # this machine may have (gh, tmux, ntfy's), left out of a copy of its bin dirs. The Python running the suite
        # is `python3`, as tools/check_pythons.sh makes it for each version.
        system = self.tmp / "system-bin"
        system.mkdir()
        for d in ("/usr/local/bin", "/usr/bin", "/bin"):
            for exe in Path(d).glob("*") if os.path.isdir(d) else ():
                if exe.name not in OPTIONAL and not (system / exe.name).exists() and os.access(exe, os.X_OK):
                    (system / exe.name).symlink_to(exe)
        self.env = {"HOME": str(self.home), "PATH": f"{fakes}:{system}",
                    "LANG": "C.UTF-8", "TMPDIR": str(self.tmp), "PIP_NO_INDEX": "1", "WINDVANE_SOURCE": str(REPO),
                    "WINDVANE_SELF_UPDATE": "0", "FAKE_LOG": str(self.tmp / "calls.jsonl"),
                    "FAKE_CRITIC_TEXT": CRITIQUE}

    def sh(self, cmd, cwd=None, ok=True):
        """`cmd` as the user types it, in their shell; asserts it worked."""
        r = subprocess.run(["sh", "-c", cmd], cwd=cwd or self.home, env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=600)
        if ok:
            self.assertEqual(r.returncode, 0, f"$ {cmd}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
        return r.stdout + r.stderr

    def test_install_doctor_init_new_run(self):
        out = self.sh(f"sh {shlex.quote(str(REPO / 'install.sh'))}")
        bin_dir = self.home / ".local/bin"
        self.assertTrue((bin_dir / "windvane").exists(), out)
        # ~/.local/bin isn't on a clean PATH: install.sh says how to put it there, and its next steps work before that
        add = re.search(r"^ +(export PATH=.*)$", out, re.M)
        self.assertTrue(add, out)
        steps = next_steps(out)
        self.assertEqual([s.split()[-1] for s in steps], ["doctor", "install"], out)
        out = self.sh(steps[0], ok=False)
        self.assertIn("MISSING   `windvane` on PATH", out)
        self.assertIn(add.group(1), out)  # doctor's fix is the same line, not a tool this machine lacks

        self.env["PATH"] = self.sh(f"{add.group(1)}; echo $PATH").strip()
        out = self.sh("windvane doctor")
        self.assertIn("all set", out)
        for line in out.splitlines():  # each fix is a command the user can run, or something to install
            if "->" in line:
                fix = line.split("-> ")[1]
                self.assertRegex(fix, r"^(windvane \w+|install \S+)", line)
        self.assertNotIn("pipx", out)
        self.sh("windvane install --yes")
        self.assertIn("ok        the mention hook", self.sh("windvane doctor"))

        # A new user's project: their git identity, a repo and a remote to push to
        self.sh("git config --global user.name 'New User' && git config --global user.email new@example.com"
                " && git config --global init.defaultBranch main")
        remote, proj = self.tmp / "remote.git", self.home / "hello"
        self.sh(f"git init -q --bare {remote} && git init -q {proj}")
        self.sh(f"echo hi > README && git add README && git commit -qm init && git remote add origin {remote}"
                " && git push -q -u origin main", cwd=proj)
        out = self.sh("windvane init --check 'test -f README'", cwd=proj)
        self.assertIn("forge: git", out)
        self.assertIn("commit .windvane/ and push it to origin/main", out)

        out = self.sh("windvane new 'Say hello'", cwd=proj)
        ticket = Path(out.strip())
        self.assertEqual(ticket, proj / ".windvane/tickets/T-001.md")
        self.assertIn("# T-001: Say hello", ticket.read_text())
        self.sh("git add .windvane && git commit -qm 'windvane setup and first ticket' && git push -q", cwd=proj)
        self.assertIn("T-001", self.sh("windvane queue", cwd=proj))

        out = self.sh("windvane run --for 20m", cwd=proj, ok=False)  # too short for a ticket: it says so, not "Why: deadline"
        self.assertIn("would do nothing: give it longer (e.g. --for 2h)", out)
        out = self.sh("windvane run --for 2h", cwd=proj)
        self.assertIn("critic 8/10", out)
        self.assertIn("Why: queue empty. Merged: Say hello (T-001).", out)
        self.sh("git pull -q", cwd=proj)
        log = self.sh("git log --format=%s origin/main", cwd=proj)
        self.assertIn("Merge T-001: Say hello", log, out)
        self.assertTrue((proj / "T-001.txt").exists())
        self.assertIn("T-001: merged", self.sh("windvane status", cwd=proj))


if __name__ == "__main__":
    unittest.main()
