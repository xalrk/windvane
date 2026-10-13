"""Test helpers: a throwaway project repo with a bare remote, and an environment that can't reach the real phone
or the real Claude (PATH holds only a fake `claude` and the system's bin dirs)."""
import faulthandler
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import weakref
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROC = Path("/proc")  # running() reads it; a test points it at an empty dir to take the macOS branch
# A test still in its sandbox after this long is stuck (the slowest takes seconds): it prints every thread's stack and
# ends the run, which names the hang instead of letting it use up a CI job's time limit in silence.
HANG_S = 300

FAKE_CLAUDE = r'''#!/usr/bin/env python3
# A stand-in for `claude -p ... --output-format stream-json`: plays the role its system prompt names.
import json, os, re, signal, subprocess, sys
signal.signal(signal.SIGTERM, signal.SIG_DFL)  # dies on SIGTERM like `claude`, even under a shell that ignores it
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
emit = lambda ev: print(json.dumps(ev), flush=True)
emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": float(os.environ.get("FAKE_FIVE", "0.1")), "resetsAt": 0}, "seven_day": {"utilization": 0.2}}}})
sh = lambda *c: subprocess.run(c, capture_output=True, text=True).stdout.strip()
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        deny = json.loads(args[args.index("--settings") + 1])["permissions"]["deny"] if "--settings" in args else []
        flags = [a for a in args if a not in (prompt, system)]
        f.write(json.dumps({"system": system[:40], "prompt": prompt, "deny": deny, "flags": flags,
                            "tool_ms": os.environ.get("BASH_DEFAULT_TIMEOUT_MS"),
                            "ticket_env": os.environ.get("WINDVANE_TICKET"),
                            "tmpdir": os.environ.get("TMPDIR"),
                            # the gh the Bash tool would run: after the session's env file, as Claude Code does
                            "gh": os.environ.get("CLAUDE_ENV_FILE") and sh(  # a bash only when there's a file
                                "bash", "-c", '. "$CLAUDE_ENV_FILE"; command -v gh')})
                + "\n")
# FAKE_FAIL: what each session does in turn (ok, error, crash, quiet = no RESULT line); a trailing * repeats the last
steps, step = os.environ.get("FAKE_FAIL", "").split(","), ""
if steps[0] and system:
    count = log + ".n"
    n = int(open(count).read()) if os.path.exists(count) else 0
    open(count, "w").write(str(n + 1))
    step = steps[n] if n < len(steps) else steps[-1] if steps[-1].endswith("*") else ""
    step = step.rstrip("*")
hang = os.environ.get("FAKE_HANG")
if hang and system.startswith(os.environ.get("FAKE_HANG_ROLE", "")):
    # streams two API messages (the first as two events), then works on until it is killed
    use = lambda i, o: {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": 1000}
    for mid, u in (("m1", use(10, 5)), ("m1", use(10, 5)), ("m2", use(20, 7))):
        emit({"type": "assistant", "message": {"id": mid, "usage": u, "content": [{"type": "text", "text": "."}]}})
    open(hang + ".pid", "w").write(str(os.getpid()))
    if os.environ.get("FAKE_HANG_LEFTOVER") and system.startswith("# windvane worker run"):
        open("half-done.txt", "w").write("half\n")  # uncommitted work a killed run leaves in its worktree
    open(hang, "w").write("streamed")
    import time
    time.sleep(60)
if os.environ.get("FAKE_ERROR_FOR") and re.search(os.environ["FAKE_ERROR_FOR"], prompt.split("\n")[0]):
    step = "error"  # FAKE_ERROR_FOR: a regex on the prompt's first line ("Ticket T-001.", "Critique T-001.")
if os.environ.get("FAKE_MAX_TURNS") and system.startswith(os.environ["FAKE_MAX_TURNS"]):
    emit({"type": "result", "subtype": os.environ.get("FAKE_LIMIT", "error_max_turns"), "is_error": True, "num_turns": 5,
          "result": ""})  # FAKE_LIMIT: which of its own limits it hit
    sys.exit(1)
if step == "error":
    emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
          "result": os.environ.get("FAKE_ERROR_TEXT")  # FAKE_ERROR_TEXT: the error's own words, else an overload
          or 'API Error: 529 {"type":"overloaded_error","message":"Overloaded"}'})
    sys.exit(1)
if step == "crash":
    print("Error: socket hang up", file=sys.stderr, flush=True)
    sys.exit(1)
text = "ok"
if system.startswith("# windvane worker run"):
    tid = re.search(r"Ticket (\S+)\.", prompt).group(1)
    behaviour = os.environ.get("FAKE_WORKER", "ready")
    import time
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"system": "span", "prompt": f"{tid} start {time.time()}"}) + "\n")
    pid_file = os.environ.get("FAKE_CRITIC_PID")
    if pid_file:  # `windvane critic` from the Bash tool, which runs each command in a session of its own
        c = subprocess.Popen(["windvane", "critic", tid], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        while not os.path.exists(pid_file):
            if c.poll() is not None:  # `windvane critic` failed: no critic to wait for, so fail fast, never hang
                sys.exit(f"windvane critic exited {c.returncode} before its critic started")
            time.sleep(0.05)
        emit({"type": "system", "subtype": "critic started"})
        c.wait()
    time.sleep(float(os.environ.get("FAKE_WORK_S", "0")))
    branch = tid.lower() + "-work"
    if "In progress on branch" in prompt:
        sh("git", "switch", branch)
    else:
        sh("git", "switch", "-c", branch, "origin/main")
        # FAKE_BARRIER=n: the first n new branches all fork the same main, however late a loaded machine starts one
        barrier = int(os.environ.get("FAKE_BARRIER", "0"))
        if barrier and log:
            open(log + ".forked", "a").write(tid + "\n")
            end = time.time() + 60
            while len(open(log + ".forked").read().split()) < barrier and time.time() < end:
                time.sleep(0.05)
    resolve_only = os.environ.get("FAKE_RESOLVE_ONLY") and "conflicts with" in prompt  # merges main, nothing more
    if not resolve_only:
        with open(f"{tid}.txt", "a") as f:
            f.write("built\n")
    if os.environ.get("FAKE_DECISION") and not resolve_only:  # appends the next decision and refers to it
        log_f = "docs/DECISIONS.md"
        n = max(int(m) for m in re.findall(r"^\| D-(\d+) \|", open(log_f).read(), re.M)) + 1
        open(log_f, "a").write(f"| D-{n} | {tid} |\n")
        open(f"{tid}.txt", "a").write(f"see D-{n}\n")
    if os.environ.get("FAKE_CONFLICT"):  # every ticket rewrites one file: a merge after another's conflicts
        if "conflicts with" in prompt:
            sh("git", "merge", "-q", "-X", "ours", "-m", "merge main", "origin/main")
        open("shared.txt", "w").write(tid + "\n")
    notes = os.environ.get("FAKE_NOTES")  # append: a section of its own in NOTES.md; edit: also rewrites its line 1
    if notes and "conflicts with" not in prompt:
        nt = open("NOTES.md").read()
        if notes == "edit":
            nt = f"# Notes, as {tid} sees them\n" + nt.split("\n", 1)[1]
        open("NOTES.md", "w").write(nt + f"\n## {tid}\nFound things.\n")
    elif notes == "edit":
        sh("git", "merge", "-q", "-X", "ours", "-m", "merge main", "origin/main")
    if behaviour in ("question", "questions") and "answered" not in prompt and "didn't answer" not in prompt:
        more = (f"### {tid}: Big or small?\n- **A**: big\n- **B**: small\nRecommendation: A, because.\n"
                * (behaviour == "questions"))  # questions: two at once
        q = open(".windvane/QUESTIONS.md").read().replace("## Open\n", f"## Open\n### {tid}: Red or blue?\nContext.\n- **A**: red\n- **B**: blue\nRecommendation: A, because.\n{more}", 1)
        open(".windvane/QUESTIONS.md", "w").write(q)
        sh("git", "add", "-A"); sh("git", "commit", "-qm", "question"); sh("git", "push", "-q", "-u", "origin", "HEAD")
        text = f"RESULT: BLOCKED {tid} Red or blue?"
    else:
        if behaviour == "question" and "didn't answer" in prompt:  # its recommendation, moved to ## Provisional
            q = open(".windvane/QUESTIONS.md").read()
            block = re.search(rf"^### {tid}: .*?(?=^### |^## )", q, re.M | re.S)
            if block:
                q = q.replace(block.group(0), "", 1).replace("## Provisional\n", "## Provisional\n" + block.group(0)
                                                             + "Built: A; to change it: colour.txt\n", 1)
                open(".windvane/QUESTIONS.md", "w").write(q)
        if not resolve_only:
            sh("git", "add", "-A"); sh("git", "commit", "-qm", f"{tid}: build")
        sh("git", "push", "-q", "-u", "origin", "HEAD")
        text = f"done\nRESULT: READY {tid} {sh('git', 'rev-parse', 'HEAD')}"
        if step == "quiet":
            text = "done, all pushed"
elif system.startswith("# windvane review run"):
    tid = re.search(r"Review (\S+) at head", prompt).group(1)
    verdict = os.environ.get("FAKE_REVIEW", "APPROVE")
    text = f"1. a point\nREVIEW: {verdict} {tid} looks fine"
elif system.startswith("# windvane critic") and prompt.startswith("Critique the project"):
    dim = re.search(r"the (\S+) dimension only", prompt).group(1)
    checks = re.findall(r"^- [\d.]+ (\S+):", prompt.split("## Open tickets")[0], re.M)
    together = True
    if os.environ.get("FAKE_PROJECT_BARRIER"):  # n sessions must all run at once, or each answers no
        import glob, time
        open(f"{log}.proj-{dim}", "w").close()
        until = time.time() + 15
        while len(glob.glob(log + ".proj-*")) < int(os.environ["FAKE_PROJECT_BARRIER"]) and time.time() < until:
            time.sleep(0.05)
        together = time.time() < until
    if os.environ.get("FAKE_PROJECT_EDIT"):  # tries to change the tree and the repo
        open("README", "a").write("edited by the critic\n")
        open("stray-critic.txt", "w").write("x\n")
        sh("git", "commit", "-qam", "the critic commits")
    no = os.environ.get("FAKE_PROJECT_NO", "").split(",")  # the check ids it answers no
    text = "Checks:\n" + "".join(f"- {c}: " + ("no, README:1 says so\n" if c in no or not together else "yes, seen\n")
                                 for c in checks)
    beyond = os.environ.get(f"FAKE_PROJECT_BEYOND_{dim}", f"- improve {dim} (new)").replace("|", "\n")
    text += f"Fine.\nBeyond:\n{beyond}\nFor the user: decide {dim}\nCRITIC: PROJECT fixes=0 {dim} ok"
elif system.startswith("# windvane critic"):
    m = re.search(r"Critique (\S+)\.", prompt)  # a re-ask in the same session: its ticket from the env
    tid = m.group(1) if m else os.environ["WINDVANE_TICKET"]
    if os.environ.get("FAKE_CRITIC_PID"):  # a slow critic, to be killed with its worker
        import time
        open(os.environ["FAKE_CRITIC_PID"], "w").write(str(os.getpid()))
        time.sleep(60)
    score, fixes = os.environ.get("FAKE_CRITIC", "9 0").split()  # FAKE_CRITIC: "<score> <fixes>"
    fix = f"Fix in this ticket:\n- {tid} x\n" if fixes != "0" else ""
    text = (f"Score: {score}/10\nGood.\n{fix}Beyond this ticket:\n- make it faster\n"
            f"CRITIC: {score}/10 {tid} fixes={fixes} good")
    if os.environ.get("FAKE_CRITIC_TEXT"):  # a whole critique, `{tid}` filled in
        text = os.environ["FAKE_CRITIC_TEXT"].replace("{tid}", tid)
    if "--resume" in args and os.environ.get("FAKE_CRITIC_REASK"):  # its reply to the driver's re-ask
        text = os.environ["FAKE_CRITIC_REASK"].replace("{tid}", tid)
elif system.startswith("# windvane planner run"):
    ids = re.search(r"## Ids for new tickets.*\n(.*)", prompt).group(1).split(", ")
    os.makedirs(".windvane/tickets", exist_ok=True)
    open(f".windvane/tickets/{ids[0]}.md", "w").write(f"# {ids[0]}: planned\nDepends on: nothing.\n")
    with open(".windvane/QUEUE.md", "a") as f:  # a tracked file, modified: its status line starts with a space
        f.write(f"- {ids[0]} planned\n")
    if os.environ.get("FAKE_PLAN_MOVE_MAIN"):  # someone merges to main while the planner works
        c = sh("git", "commit-tree", "origin/main^{tree}", "-p", "origin/main", "-m", "moved")
        sh("git", "push", "-q", "origin", f"{c}:refs/heads/main")
    if os.environ.get("FAKE_PLAN_OUTSIDE"):
        open("stray.txt", "w").write("x")
    text = "PLAN: new=1 noted=0 one ticket"
elif system.startswith("# windvane periodic run"):  # the project's rules file says what to do
    ids = re.search(r"## Ids for new tickets.*\n(.*)", prompt).group(1).split(", ")
    if "WRITE_TICKET" in system:
        open(f".windvane/tickets/{ids[0]}.md", "w").write(f"# {ids[0]}: found\nDepends on: nothing.\n")
        q = open(".windvane/QUEUE.md").read()
        open(".windvane/QUEUE.md", "w").write(q.replace("# Queue\n", "") + f"- {ids[0]} found\n")  # drops a line
    if "WRITE_STRAY_ID" in system:  # a ticket under an id it wasn't given
        open(".windvane/tickets/T-099.md", "w").write("# T-099: stray\nDepends on: nothing.\n")
    if "WRITE_REPORT" in system:
        open("report.txt", "w").write("the jump is broken\n")
    for how, tid in re.findall(r"(APPEND_NOTE|EDIT_NOTE|DELETE_NOTE)=(\S+)", system):  # on an existing ticket
        path = f".windvane/tickets/{tid}.md"
        lines = open(path).read().splitlines()
        if how == "DELETE_NOTE":
            os.remove(path)
            continue
        at = 1 if how == "EDIT_NOTE" else len(lines)
        open(path, "w").write("\n".join(lines[:at] + ["", "## From the playtest", "jump fails at x=3"] + lines[at:]))
    if "EDIT_RULES" in system:  # edits the next session's rules in its tree: they must still come from main
        open("tools/TRIAGE.md", "w").write("# Triage\nNO_LINE\n")
    verdicts = ""  # a triage of windvane requests: each fenced request's WANT=<verdict>, or a body that fools it
    for rid, body in re.findall(r"^<request-\w+ (R-\d+)>\n(.*?)^</request-\w+ \1>", prompt, re.M | re.S):
        want = (re.findall(r"WANT=(\w+)", body) or ["obey" if "ignore previous instructions" in body else ""])[0]
        if want in ("accept", "obey"):  # its bullet at the top of the queue: the entry's `land` must move it
            tid = ids.pop(0)
            open(f".windvane/tickets/{tid}.md", "w").write(f"# {tid}: asked in {rid}\nDepends on: nothing.\n")
            q = open(".windvane/QUEUE.md").read()
            open(".windvane/QUEUE.md", "w").write(q.replace("# Queue\n", f"# Queue\n- {tid} asked in {rid}\n"))
            verdicts += f"REQUEST: {rid} accept {tid}\n"
        if want == "obey":  # fooled: changes the config and tries a merge, as the body said
            open(".windvane/config.toml", "a").write('land = "top"\n')
            sh("git", "merge", "-q", "origin/t-001-work")
        elif want in ("reject", "question"):
            verdicts += f"REQUEST: {rid} {want} " + ("not windvane's doing" if want == "reject" else "A or B?") + "\n"
    open("README", "a").write("stray\n")
    score = re.search(r"SCORE=(\d+)", system)
    extra = int((re.findall(r"REPLY_LINES=(\d+)", system) or [0])[0])  # a long reply
    text = "played\n" + "".join(f"note {i}\n" for i in range(extra))
    text += "covered: the jump by T-001\nnot ticketed: the fog (by design)\n" if "NOTE=" in system else ""
    text += verdicts
    text += ("" if "NO_LINE" in system else "PERIODIC: " + (
        "see report.txt" if "WRITE_REPORT" in system else "wrote a ticket") + (f" score={score.group(1)}" if score else ""))
elif system.startswith("# windvane fix run"):  # FAKE_FIX: push (fixes it), red (pushes, still red), giveup
    n = re.search(r"Desk PR #(\d+)", prompt).group(1)
    branch = re.search(r"HEAD:(\S+?)`", prompt).group(1)
    how = os.environ.get("FAKE_FIX", "push")
    if how == "giveup":
        text = f"FIX: GAVE UP #{n} needs a product call"
    else:
        if "conflicts with" in prompt:
            sh("git", "merge", "-q", "-X", "ours", "-m", "merge main", "origin/main")
        if how == "push" and os.path.exists("BROKEN"):
            sh("git", "rm", "-q", "BROKEN")
        sh("git", "commit", "-q", "--allow-empty", "-m", "fix")
        sh("git", "push", "-q", "origin", f"HEAD:{branch}")
        text = f"FIX: PUSHED #{n} fixed it"
elif system.startswith("# windvane preflight"):  # FAKE_PREFLIGHT: the questions it finds
    found = os.environ.get("FAKE_PREFLIGHT", "")
    text = found + "\nPREFLIGHT: " + ("1 questions" if found else "NONE")
emit({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": text}], "usage": {
    "input_tokens": 3, "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 6000}}})  # the first turn
emit({"type": "result", "result": text, "num_turns": 3, "total_cost_usd": 0.01, "session_id": "fake-session",
      "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}})
'''


def running(pid):
    """Whether process `pid` still runs: gone or ended (a zombie waiting to be reaped) is not running. /proc where
    there is one, else `ps` (macOS)."""
    if (PROC / "self/stat").exists():
        try:
            return not (PROC / str(pid) / "stat").read_text().split(") ")[-1].startswith("Z")
        except OSError:
            return False
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def tmpdir(test, prefix="windvane-"):
    """A temp dir removed when `test` ends, passed or failed: every check runs the suite on four Pythons, and a dir
    leaked per run once took all of /tmp's inodes."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    test.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


class Sandbox:
    """A project with a bare `origin`, `.windvane/` set up for forge git, and an isolated environment."""

    def __init__(self, config_extra=""):
        self.tmp = Path(tempfile.mkdtemp(prefix="windvane-test-"))
        # removed by close(), or when it's collected or the run ends: a setUp that fails after this never calls close
        self.rm = weakref.finalize(self, shutil.rmtree, str(self.tmp), True)
        self.old_env = dict(os.environ)
        faulthandler.dump_traceback_later(HANG_S, exit=True)
        try:
            self.setup(config_extra)
        except BaseException:  # undone, or the next test runs in a half-made sandbox's environment
            self.close()
            raise

    def setup(self, config_extra):
        (self.tmp / "tmp").mkdir()  # the TMPDIR of everything it runs
        self.remote = self.tmp / "remote.git"
        self.root = self.tmp / "proj"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        # `python3` is the Python running the tests: bin/windvane and the fake run it from PATH, and /usr/bin's can be
        # too old for windvane (macOS ships 3.9)
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "windvane").symlink_to(REPO / "bin/windvane")
        self.env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.tmp / "home"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "WINDVANE_SELF_UPDATE": "0", "TMPDIR": str(self.tmp / "tmp"),
                    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "FAKE_LOG": str(self.tmp / "calls.jsonl")}
        os.environ.clear()
        os.environ.update({**self.old_env, **self.env})
        for k in ("WINDVANE_SESSION", "WINDVANE_TICKET", "WINDVANE_STOP_AT", "WINDVANE_CAPS", "WINDVANE_ROLE",
                  "WINDVANE_SUPERVISED", "WINDVANE_RESTARTED"):  # when the tests run inside a session
            os.environ.pop(k, None)
        for k in [k for k in os.environ if k.startswith("GIT_CONFIG_")]:  # a worker's [skip ci] hook, for one
            os.environ.pop(k)
        from windvane import net, scratch
        net.probe, net.waiter = (lambda: True), None  # never probe the real network; a test fakes an outage
        self.fullness, scratch.fullness = scratch.fullness, lambda path=None: (10, 10)  # nor the real disk (a full one pushes)
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

    def ticket(self, tid, deps="", touches=None):
        self.write(f".windvane/tickets/{tid}.md", f"# {tid}: do {tid}\nDepends on: {deps or 'nothing'}.\n"
                   + (f"Touches: {touches}.\n" if touches else ""))

    def publish(self, msg="tickets"):
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        self.git("push", "-q", "origin", "main")

    def close(self):
        faulthandler.cancel_dump_traceback_later()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.rm()
        from windvane import net, phone, scratch
        net.probe, net.waiter = None, None
        scratch.fullness = getattr(self, "fullness", scratch.fullness)
        phone.use(None)  # the next test's pushes name its own project
