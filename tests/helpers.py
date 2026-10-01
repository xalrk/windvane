"""Test helpers: a throwaway project repo with a bare remote, and an environment that can't reach the real phone
or the real Claude (PATH holds only a fake `claude` and the system's bin dirs)."""
import os
import subprocess
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

FAKE_CLAUDE = r'''#!/usr/bin/env python3
# A stand-in for `claude -p ... --output-format stream-json`: plays the role its system prompt names.
import json, os, re, subprocess, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
emit = lambda ev: print(json.dumps(ev), flush=True)
emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": 0.1, "resetsAt": 0}, "seven_day": {"utilization": 0.2}}}})
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"system": system[:40], "prompt": prompt}) + "\n")
sh = lambda *c: subprocess.run(c, capture_output=True, text=True).stdout.strip()
text = "ok"
if system.startswith("# windvane worker run"):
    tid = re.search(r"Ticket (\S+)\.", prompt).group(1)
    behaviour = os.environ.get("FAKE_WORKER", "ready")
    branch = tid.lower() + "-work"
    if "In progress on branch" in prompt:
        sh("git", "switch", branch)
    else:
        sh("git", "switch", "-c", branch, "origin/main")
    with open(f"{tid}.txt", "a") as f:
        f.write("built\n")
    if behaviour == "question" and "answered" not in prompt and "didn't answer" not in prompt:
        q = open(".windvane/QUESTIONS.md").read().replace("## Open\n", f"## Open\n### {tid}: Red or blue?\nContext.\n- **A**: red\n- **B**: blue\nRecommendation: A, because.\n", 1)
        open(".windvane/QUESTIONS.md", "w").write(q)
        sh("git", "add", "-A"); sh("git", "commit", "-qm", "question"); sh("git", "push", "-q", "-u", "origin", "HEAD")
        text = f"RESULT: BLOCKED {tid} Red or blue?"
    else:
        sh("git", "add", "-A"); sh("git", "commit", "-qm", f"{tid}: build")
        sh("git", "push", "-q", "-u", "origin", "HEAD")
        text = f"done\nRESULT: READY {tid} {sh('git', 'rev-parse', 'HEAD')}"
elif system.startswith("# windvane review run"):
    tid = re.search(r"Review (\S+) at head", prompt).group(1)
    verdict = os.environ.get("FAKE_REVIEW", "APPROVE")
    text = f"1. a point\nREVIEW: {verdict} {tid} looks fine"
elif system.startswith("# windvane critic"):
    tid = re.search(r"Critique (\S+)\.", prompt).group(1)
    text = f"Score: 9/10\nGood.\nBeyond this ticket:\n- make it faster\nCRITIC: 9/10 {tid} fixes=0 good"
elif system.startswith("# windvane planner run"):
    ids = re.search(r"## Ids for new tickets.*\n(.*)", prompt).group(1).split(", ")
    os.makedirs(".windvane/tickets", exist_ok=True)
    open(f".windvane/tickets/{ids[0]}.md", "w").write(f"# {ids[0]}: planned\nDepends on: nothing.\n")
    with open(".windvane/QUEUE.md", "a") as f:  # a tracked file, modified: its status line starts with a space
        f.write(f"- {ids[0]} planned\n")
    if os.environ.get("FAKE_PLAN_OUTSIDE"):
        open("stray.txt", "w").write("x")
    text = "PLAN: new=1 noted=0 one ticket"
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
emit({"type": "result", "result": text, "num_turns": 3, "total_cost_usd": 0.01,
      "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}})
'''


class Sandbox:
    """A project with a bare `origin`, `.windvane/` set up for forge git, and an isolated environment."""

    def __init__(self, config_extra=""):
        self.tmp = Path(tempfile.mkdtemp(prefix="windvane-test-"))
        self.remote = self.tmp / "remote.git"
        self.root = self.tmp / "proj"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        (self.bin / "windvane").symlink_to(REPO / "bin/windvane")
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp / "home"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "FAKE_LOG": str(self.tmp / "calls.jsonl")}
        self.old_env = dict(os.environ)
        os.environ.clear()
        os.environ.update({**self.old_env, **self.env})
        import shutil
        assert shutil.which("claude") == str(fake) and not shutil.which("notify"), "tests must not reach the real tools"
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.tmp)
        self.root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.write(".windvane/config.toml", textwrap.dedent(f"""\
            name = "sandbox"
            forge = "git"
            check = "test -f README"
            [critic]
            enabled = false
            """) + config_extra)
        self.write(".windvane/QUESTIONS.md", "# Q\n\n## Open\n\n## Provisional\n\n## Answered\n")
        self.write(".windvane/QUEUE.md", "# Queue\n")
        self.write("README", "hi\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "-q", "-u", "origin", "main")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True,
                              check=True).stdout.strip()

    def write(self, rel, text):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)

    def ticket(self, tid, deps=""):
        self.write(f".windvane/tickets/{tid}.md", f"# {tid}: do {tid}\nDepends on: {deps or 'nothing'}.\n")

    def publish(self, msg="tickets"):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        self.git("push", "-q", "origin", "main")

    def close(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        subprocess.run(["rm", "-rf", str(self.tmp)])
