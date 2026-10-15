"""Headless Claude sessions: one per worker, critic, reviewer and planner run. Each run's stream is kept in
`runs/`, and its tokens and turns go to `ledger.jsonl` (`windvane stats`). Usage limits come from the stream's
rate_limit_event, so they count the user's other sessions too."""
import json
import os
import re
import selectors
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import actions, scratch
from .config import HOME, author, github_token
from .util import append_jsonl, load_json, save_json, started_at

TICKET = "WINDVANE_TICKET"  # the ticket a session works on, so a `windvane check` it runs is counted for it
NEST = "WINDVANE_SESSION"  # names the session a command runs in, so a session started inside it can register
ROLE = "WINDVANE_ROLE"  # the session's role: `windvane request` records it, and files only from a worker (or the desk)
NO_REQUEST = "Bash(windvane request *)"  # sessions other than workers (and the desk) don't file windvane requests
TOOL_MS = "600000"
GRACE = 3  # seconds a nested session gets to end on SIGTERM before SIGKILL: short, the driver waits on it
NO_AUTO = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"  # set, Claude Code turns `--permission-mode auto` off
LOGIN = ".credentials.json"  # Claude Code's login, in its config folder (macOS keeps it in the Keychain instead)
LOGIN_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")  # a login in the environment
# Bash commands run outside the sandbox: windvane's own (a `bg` job must outlive the call, a critic's session must log
# in) and git (`git add -A` fails on the sandbox's mount points, an SSH push can't take its proxy)
OUTSIDE = ("windvane *", "git *")
IN_SANDBOX = "SANDBOX_RUNTIME"  # set by Claude Code in a sandboxed Bash command's environment
POLL = 5  # seconds between a quiet session's stop checks (small file reads, no tokens): how soon `stop --now` acts


# Variable names that look like credentials: a session gets none of them from the driver's environment, but for what
# Claude Code logs in with (ANTHROPIC_*, CLAUDE_*, and AWS_* when it runs on Bedrock), gh's token and `[env] keep`.
CREDENTIAL = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|_KEY$|^AWS_", re.I)
GH_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# git reaching github.com over https through gh, so a push uses gh's token (an SSH key reaches all the user's repos)
GH_GIT = (("credential.https://github.com.helper", ""), ("credential.https://github.com.helper", "!gh auth git-credential"),
          ("url.https://github.com/.insteadOf", "git@github.com:"),
          ("url.https://github.com/.insteadOf", "ssh://git@github.com/"))


def env(extra=None, p=None):
    """The sessions' environment: the driver's, scrubbed of credentials (`scrub`), with project `p`'s GitHub token
    and commit identity. AUTOPILOT=1 keeps the user's ntfy hook quiet (the driver pushes its own). The Bash tool's
    default timeout is 2 minutes; the rules and `windvane await` assume a tool call may run 10. Never
    CLAUDE_CODE_SUBPROCESS_ENV_SCRUB (NO_AUTO), even from the user's shell: with it Claude Code forces the default
    permission mode over `--permission-mode auto`, and a headless session can then write nothing."""
    out = scrub(os.environ, p.cfg["env"]["keep"] if p else ())
    out.pop(NO_AUTO, None)
    if p:
        out = {**github_env(p, out), **identity(p)}
    extra = extra or {}
    out.update({"AUTOPILOT": "1", "BASH_DEFAULT_TIMEOUT_MS": TOOL_MS, "BASH_MAX_TIMEOUT_MS": TOOL_MS},
               **{k: v for k, v in extra.items() if not k.startswith("GIT_CONFIG_") and k != NO_AUTO})
    return with_git_config(out, git_config(extra))


def scrub(environ, keep=()):
    """`environ` without the variables whose names look like credentials (CREDENTIAL): tokens the user's shell
    exports for other work must not reach a session that reads untrusted text and can run commands and push."""
    bedrock = environ.get("CLAUDE_CODE_USE_BEDROCK")
    return {k: v for k, v in environ.items()
            if not CREDENTIAL.search(k) or k in keep or k in GH_TOKENS or re.match(r"(ANTHROPIC|CLAUDE)_", k)
            or bedrock and k.startswith("AWS_")}


def github_env(p, environ):
    """`environ` with `[github] token_file`'s token as gh's only one, and git's github.com pushes going through gh, so
    sessions and the driver reach this one repo, not every repo the user's own login can. Unchanged without one."""
    token = github_token(p)
    if not token:
        return environ
    return with_git_config({**{k: v for k, v in environ.items() if k not in GH_TOKENS}, "GH_TOKEN": token}, GH_GIT)


def scope_github(p):
    """Give this process (the driver, a desk command) the project's GitHub token, for its own gh and git calls."""
    scoped = github_env(p, dict(os.environ))
    if scoped != os.environ:
        os.environ.clear()
        os.environ.update(scoped)


def identity(p):
    """The configured `author` as git's commit identity: a worker's commits carry it, not the machine's global one.
    Set in the environment, not the repo's config, which the user's checkout shares."""
    name, email = author(p)
    return {f"GIT_{who}_{what}": v for who in ("AUTHOR", "COMMITTER") for what, v in (("NAME", name), ("EMAIL", email))
            } if email else {}


def git_config(environ):
    """The (key, value) settings git reads from `environ`'s GIT_CONFIG_COUNT and GIT_CONFIG_KEY_n/VALUE_n."""
    n = environ.get("GIT_CONFIG_COUNT", "")
    n = int(n) if n.isdigit() else 0  # git itself refuses a bad count; a session's own commands will say so
    return [(environ[f"GIT_CONFIG_KEY_{i}"], environ.get(f"GIT_CONFIG_VALUE_{i}", ""))
            for i in range(n) if f"GIT_CONFIG_KEY_{i}" in environ]


def with_git_config(environ, pairs):
    """`environ` with the git settings `pairs` after the ones it has, unless it has them all already."""
    have = git_config(environ)
    if all(pair in have for pair in pairs):
        return environ
    out = {k: v for k, v in environ.items() if not k.startswith("GIT_CONFIG_")}
    for i, (k, v) in enumerate([*have, *pairs]):
        out[f"GIT_CONFIG_KEY_{i}"], out[f"GIT_CONFIG_VALUE_{i}"] = k, v
    out["GIT_CONFIG_COUNT"] = str(len(have) + len(pairs))
    return out


def over_caps(u, cap, week_cap):
    """Why usage `u` must stop a session, or None."""
    if u.five is not None and u.five >= cap:
        return "5-hour cap"
    if u.week is not None and u.week >= week_cap:
        return "weekly cap"
    return "usage limit" if u.rejected else None


def inherited_stop(p, usage):
    """The stop rule of a session started inside another (`windvane critic` from a worker): the outer one's deadline
    and caps, from the environment the driver set, against usage.json, which every session's stream keeps fresh."""
    at, caps = os.environ.get("WINDVANE_STOP_AT"), os.environ.get("WINDVANE_CAPS")
    if not (at and caps):
        return None
    cap, week_cap = map(float, caps.split(","))

    def stop_if():
        if time.time() >= float(at):
            return "time"
        fresh = Usage(p)
        fresh.rejected = usage.rejected
        return over_caps(fresh, cap, week_cap)
    return stop_if


def owner_line(pid):
    """What a session's `session` file holds: its pid, then the pid and start time of the process that owns it (the
    driver, or a desk `plan`), so the sweep can tell a dead driver's worker from a desk session that's still wanted."""
    me = os.getpid()
    return f"{pid} {me} {started_at(me) or '-'}"


def owner_alive(owner):
    """Whether the process named by a `session` file's owner fields ([pid, start]) still runs: the same pid, started
    at the same time (a reused number is someone else). A start it can't read either side counts as alive."""
    try:
        pid, start = int(owner[0]), float(owner[1]) if owner[1:2] not in ([], ["-"]) else None
    except ValueError:
        return True  # unreadable: spared, as before owners were recorded
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # another user's: a reused number, which the start time shows
        pass
    t = started_at(pid)
    return t is None or start is None or abs(t - start) <= 2


def signal_group(pgid, sig, mark=None, strict=False):
    """Send `sig` to a process group; False when it's gone. With `mark` (the file written as it started), a leader
    started after it is someone else reusing the number (after a reboot, say), and is left alone. `strict` leaves it
    alone too when its start time can't be read: numbers from a dead driver may well be a stranger's now."""
    if mark:
        t = started_at(pgid)
        if t is None and strict:
            return False
        try:
            if t is not None and t > mark.stat().st_mtime + 2:  # /proc's boot time and ps's are in whole seconds
                return False
        except OSError:
            pass
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_nested(p, session, strict=False, lead=()):
    """Stop the sessions started inside `session`: SIGTERM, then SIGKILL for groups alive after GRACE. Claude Code
    runs each Bash command in a session of its own, so killing a worker's process group misses a critic it started:
    each one registers its group here instead. `lead` adds the session's own group (the sweep's orphaned worker). How
    many groups were still running (`strict`: see signal_group)."""
    d = p.state / "nested" / session
    marks = list(d.glob("*")) if d.is_dir() else []
    groups = [(int(f.name), f) for f in marks if f.name.isdigit()] + [(g, d / "session") for g in lead]
    alive = [g for g, f in groups if signal_group(g, signal.SIGTERM, f, strict)]
    n, end = len(alive), time.time() + GRACE
    while alive and time.time() < end:
        time.sleep(0.1)
        alive = [g for g in alive if signal_group(g, 0)]
    for g in alive:
        signal_group(g, signal.SIGKILL)
    for f in marks:
        f.unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:  # gone, or a nested session registered just now (its own deadline and caps still stop it)
        pass
    return n


def sweep_nested(p):
    """Kill what sessions of a driver that died left running (`nested/<session>/<group>`), and their dirs, under the
    run's lock. A live session whose owner lives too (a desk `plan` beside the run) keeps its own; one whose owner is
    gone (a dead driver's worker that outlived it) is killed with its own. A `session` file from before owners were
    recorded holds only the pid, and a live one is spared. Strict, as these numbers may predate a reboot. How many
    groups were killed; a sweep that kills any leaves a `swept` row in the ledger (`windvane stats`)."""
    n = 0
    for d in sorted((p.state / "nested").glob("*")):
        own, lead = d / "session", []
        try:
            pid, *owner = own.read_text().split() or [""]
            if pid.isdigit() and signal_group(int(pid), 0, own):
                if not owner or owner_alive(owner):
                    continue
                lead = [int(pid)]
        except OSError:
            pass
        n += kill_nested(p, d.name, strict=True, lead=lead)
    if n:
        append_jsonl(p.state / "ledger.jsonl", {"event": "swept", "role": "driver", "groups": n,
                                                "at": f"{datetime.now():%Y-%m-%d %H:%M}"})
    return n


PROBE_S = 180  # a usage probe's time box: a hung one is killed, its row says `killed: time`


class Usage:
    """Latest 5-hour and weekly utilisation, in percent."""

    def __init__(self, p):
        self.path = p.state / "usage.json"
        s = load_json(self.path, {})
        self.five, self.week, self.five_reset, self.at = s.get("five"), s.get("week"), s.get("five_reset"), s.get("at", 0)
        self.rejected = False

    def update(self, info):
        wins = info.get("unifiedWindows") or {}
        if "five_hour" in wins:
            self.five = round(100 * wins["five_hour"]["utilization"], 1)
            self.five_reset = wins["five_hour"].get("resetsAt")
        if "seven_day" in wins:
            self.week = round(100 * wins["seven_day"]["utilization"], 1)
        self.rejected = info.get("status") not in (None, "allowed", "allowed_warning")
        self.at = time.time()
        save_json(self.path, {"five": self.five, "week": self.week, "five_reset": self.five_reset, "at": self.at})

    def text(self):
        f = lambda v: "?" if v is None else f"{v:g}"
        return f"5h {f(self.five)}% · week {f(self.week)}%"

    def probe(self, p):
        """A tiny Haiku call, only to read the current usage. It still spends tokens, so its result goes to the
        ledger as a `probe` row, the fields a session's row has, and `stats` shows what the probes cost."""
        d = p.state / "probe"
        d.mkdir(parents=True, exist_ok=True)
        started, killed = time.time(), None
        try:
            proc = subprocess.run(["claude", "-p", "ok", "--model", "haiku", "--output-format", "stream-json",
                                   "--verbose", "--no-session-persistence", "--setting-sources", "local",
                                   "--strict-mcp-config"], cwd=d, capture_output=True, timeout=PROBE_S, env=env())
            out, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as e:  # killed: what it streamed before still cost
            out, code, killed = e.stdout or b"", None, "time"
        final, seen = {}, {}
        for line in out.decode(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "rate_limit_event":
                self.update(ev.get("rate_limit_info", {}))
            elif ev.get("type") == "assistant":
                msg = ev.get("message") or {}
                seen[msg.get("id") or len(seen)] = msg.get("usage") or {}
            elif ev.get("type") == "result":
                final = ev
        if final or seen:  # nothing streamed, nothing spent
            end, error = ("killed", None) if killed else ending(final, code, [])
            append_jsonl(p.state / "ledger.jsonl", {
                "role": "probe", "label": "probe", "ticket": "", "model": "haiku", "effort": None,
                "minutes": round((time.time() - started) / 60, 1), **spent(final, seen), "base": first_turn(seen),
                "usd": final.get("total_cost_usd"), "killed": killed, "end": end, "error": error,
                "exit": code, "stream": None})


# The built-in tools each windvane role starts with (`--tools`). Every tool's description is re-read on every turn,
# so the full set (25 tools, ~10k tokens) doubled a worker's first-turn context. A role not named here (a project's
# [[periodic]] session) keeps the CLI's default set and the project's skills.
TOOLS = {"worker": ["Bash", "Read", "Edit", "Write"], "fix": ["Bash", "Read", "Edit", "Write"],
         "planner": ["Bash", "Read", "Edit", "Write"], "reviewer": ["Bash", "Read"],
         "critic": ["Read", "Glob", "Grep"], "critic:project": ["Bash", "Read", "Glob", "Grep"],
         "preflight": ["Read", "Glob", "Grep"]}
NO_MCP = json.dumps({"mcpServers": {}})
# The flags every session's command line uses that an older `claude` may not know: `windvane doctor` checks them,
# since a CLI that lacks one fails every session at launch.
CLI_FLAGS = ("--effort", "--setting-sources", "--strict-mcp-config", "--mcp-config", "--tools",
             "--disable-slash-commands", "--append-system-prompt")


def lean_flags(p, role):
    """The command-line flags that keep a session's base context small: no MCP servers (none but the empty config),
    and for windvane's own roles only the tools their rules use and no skills. The user's hooks stay out through
    `--setting-sources project,local`. Subagents, when the config turns them on, come with the Agent tool."""
    flags = ["--strict-mcp-config", "--mcp-config", NO_MCP]
    if role in TOOLS:
        tools = TOOLS[role] + (["Agent"] if p.ap.get("subagents") and "Edit" in TOOLS[role] else [])
        flags += ["--tools", ",".join(tools), "--disable-slash-commands"]
    return flags


def first_turn(seen):
    """A session's base context: everything its first API call read (system prompt, tools, CLAUDE.md files, the
    prompt), cached or not. What the session reads per turn beyond this is its own growth."""
    u = next(iter(seen.values()), {})
    return sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))


def spent(final, seen):
    """A ledger row's turns and tokens, from the result event; a session killed or crashed before it gets what its
    stream showed (`estimated`)."""
    u, turns = final.get("usage"), final.get("num_turns")
    estimated = not u and bool(seen)
    if estimated:
        u = {k: sum(m.get(k) or 0 for m in seen.values()) for k in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        turns = len(seen)
    u = u or {}
    return {"turns": turns, "estimated": estimated, "input": u.get("input_tokens", 0),
            "output": u.get("output_tokens", 0), "cache_read": u.get("cache_read_input_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0)}


def login_file():
    """Claude Code's login file: in CLAUDE_CONFIG_DIR when set, as Claude Code reads it, else in ~/.claude."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser().resolve() / LOGIN


def login_rules():
    """The deny rules that keep Claude Code's file tools (Read, Edit, and Grep and Glob through Read) off the login
    file. Claude Code itself still reads it: permission rules govern the session's tool calls only."""
    login = login_file()
    return [f"Read(/{login})", f"Edit(/{login})"]  # `//` makes the path absolute in a permission rule


def sandbox():
    """Claude Code's Bash sandbox, there to keep commands off the login file and the login variables. Writes and the
    network stay as open as without it (every host, the home folder), so a project's tools keep working; a command
    that still fails in it can be retried outside it, which auto mode's classifier reviews. Where the sandbox can't
    start (no bubblewrap on Linux), Claude Code runs commands without it."""
    return {"enabled": True, "excludedCommands": list(OUTSIDE),
            "filesystem": {"allowWrite": ["~/", "/tmp", "/var/tmp"]},
            "credentials": {"files": [{"path": str(login_file()), "mode": "deny"}],
                            "envVars": [{"name": n, "mode": "deny"} for n in LOGIN_VARS]},
            "network": {"allowedDomains": ["*"], "allowLocalBinding": True, "allowAllUnixSockets": True}}


def settings_json(allow=(), deny=()):
    """The session's settings: permission rules and the sandbox. A project's `[permissions]` rules come after
    windvane's own (the login file's first); deny wins over allow in Claude Code, so a project's allow can't lift a
    deny that keeps a session off `main` or the login file."""
    return json.dumps({"permissions": {"allow": list(allow), "deny": [*login_rules(), *deny]}, "sandbox": sandbox()})


def outside_sandbox(what):
    """Stop a command that can't work inside Claude Code's Bash sandbox (a session there can't log in, a job dies
    with the call), saying how to run it outside: on its own, as OUTSIDE lets it."""
    if os.environ.get(IN_SANDBOX):
        raise SystemExit(f"windvane: {what} can't run inside the Bash sandbox: run the `windvane` command on its own "
                         "(no pipe, redirect, `cd` or `&&`), so it runs outside it")


OWN_LIMITS = {"error_max_turns": "max turns", "error_max_budget_usd": "dollar budget"}  # as the phone says them


# The API's error types and their HTTP codes, for an error body that comes with no code before it
API_STATUS = {"invalid_request_error": 400, "authentication_error": 401, "permission_error": 403,
              "not_found_error": 404, "request_too_large": 413, "rate_limit_error": 429, "api_error": 500,
              "overloaded_error": 529}


def plain_error(text):
    """A session's error as a push or a status line says it: `API Error: 400 {"type":"error","error":{"message":
    "…"}}`, or the same JSON with no prefix, becomes `API Error 400: …`, so the phone shows the API's message, not
    its JSON. Any other text comes back as it is. The log keeps the whole text: only what's said is shortened."""
    text = str(text)
    start = text.find("{")
    try:
        body, end = json.JSONDecoder().raw_decode(text, start) if start >= 0 else (None, 0)
    except (ValueError, RecursionError):  # not JSON, or nested past Python's limit
        return text
    inner = body.get("error") if isinstance(body, dict) else None
    inner = inner if isinstance(inner, dict) else body if isinstance(body, dict) else {}
    message = inner.get("message")
    prefix = re.search(r"API Error:?\s*(\d{3})?\s*$", text[:start])
    if not isinstance(message, str) or not message.strip() or not (prefix or body.get("type") == "error"):
        return text
    code = (prefix and prefix.group(1)) or API_STATUS.get(inner.get("type"))
    head = text[:prefix.start()] if prefix else text[:start]
    return f"{head}API Error{f' {code}' if code else ''}: {' '.join(message.split())}{text[end:]}"


def ending(final, code, noise):
    """How a session ended, as (kind, error text): "ok"; "error" when the result event says the API or the CLI failed
    (overload, 5xx, not logged in); "budget" when the session spent its own budget (max turns, max dollars): the
    ticket's doing, not a crash to retry; "no result" when the process died before one (network drop, crash).
    `noise` is the stream's last non-JSON lines, where a crash says why."""
    if not final:
        why = " ".join(" ".join(noise).split())[-300:]
        return "no result", f"no result event (exit {code})" + (f": {why}" if why else "")
    sub = final.get("subtype") or "success"
    if sub.startswith("error_max_"):
        return "budget", OWN_LIMITS.get(sub, sub.removeprefix("error_").replace("_", " "))
    if final.get("is_error") or sub != "success":
        why = " ".join(str(final.get("result") or final.get("errors") or "").split()) or f"exit {code}"
        return "error", why if sub == "success" else f"{sub}: {why}"  # whole, for the log: plain_error() is for pushes
    return "ok", None


def run_claude(p, role, label, prompt, system, cwd, allow=(), deny=(), usage=None, stop_if=None, ticket="",
               limits=None, extra_env=None, resume=None, session=None):
    """Run one headless session to its end. `resume` continues an earlier session by its id, so a follow-up turn
    reads that context from the cache instead of paying for it fresh; `session`, a dict, gets this one's id. `stop_if()` returns a reason to kill it (deadline, usage cap);
    `limits` (kill time, 5-hour cap, weekly cap) passes the same rule on to sessions started inside this one.
    Returns (final text, why it was stopped or None, why it failed or None): a failure is the session's own
    (API error, crash), never the ticket's, so callers can retry it; a session out of its own turns or budget comes
    back stopped ("max turns")."""
    outside_sandbox("a session")
    if p.perm_problems:  # the run and `windvane periodic` stop before this; a desk `plan` or `critic` stops here
        raise SystemExit(f"windvane: bad permission rules in config.toml: {'; '.join(p.perm_problems)}")
    own = p.perms.get(role, {})
    allow, deny = [*allow, *own.get("allow", ())], [*deny, *own.get("deny", ())]
    base = role.split(":")[0]  # `critic:project` runs on the critic's model unless the config names its own
    model = p.models.get(role, p.models.get(base, "opus"))
    effort = p.models.get(f"{role}_effort", p.models.get(f"{base}_effort", "high"))
    raw = p.state / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", prompt, "--model", model, "--effort", effort, "--permission-mode", "auto",
           "--output-format", "stream-json", "--verbose", "--setting-sources", "project,local",
           *lean_flags(p, role), "--settings", settings_json(allow, deny), "--append-system-prompt", system,
           *(["--resume", resume] if resume else [])]
    started, parent = time.time(), os.environ.get(NEST)
    extra = {NEST: raw.stem, TICKET: ticket, ROLE: role, "TMPDIR": str(scratch.session_dir(p, ticket or label)),
             **(extra_env or {})}
    if limits:
        extra.update(WINDVANE_STOP_AT=str(limits[0].timestamp()), WINDVANE_CAPS=f"{limits[1]:g},{limits[2]:g}")
    own = p.state / "nested" / raw.stem / "session"  # alive, so a run's start never sweeps what it started
    own.parent.mkdir(parents=True, exist_ok=True)
    # `windvane request` from outside its tree: who it is (requests.py), written first: the session may ask at once
    (own.parent / "role").write_text(role)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, env=env(extra, p))
    mark = p.state / "nested" / parent / str(proc.pid) if parent else None
    own.write_text(owner_line(proc.pid))
    if mark:
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    result, killed, final, noise, seen = "", None, {}, [], {}
    try:
        with raw.open("w") as out:
            while True:
                if sel.select(timeout=POLL):
                    line = proc.stdout.readline()
                    if not line:
                        break
                    out.write(line)
                    out.flush()
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        noise = (noise + [line.strip()])[-5:] if line.strip() else noise
                        continue
                    if ev.get("type") == "rate_limit_event" and usage:
                        usage.update(ev.get("rate_limit_info", {}))
                    elif ev.get("type") == "assistant":
                        msg = ev.get("message") or {}
                        seen[msg.get("id") or len(seen)] = msg.get("usage") or {}
                    elif ev.get("type") == "result":
                        result, final = ev.get("result") or "", ev
                if killed is None and stop_if:
                    killed = stop_if()
                    if killed:
                        os.killpg(proc.pid, signal.SIGTERM)
                        kill_nested(p, raw.stem)
                        try:
                            proc.wait(60)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
    except BaseException:  # the driver failed reading it: never leave it working unwatched in the slot's tree
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_nested(p, raw.stem)
        proc.wait()
        if mark:
            mark.unlink(missing_ok=True)
        raise
    finally:  # an unclosed pipe warns when collected, onto whatever stderr is then (a test's capture)
        sel.close()
        proc.stdout.close()
    code = proc.wait()
    kill_nested(p, raw.stem)  # killed with it, or left running by a Bash call that timed out
    if not ticket:  # a ticket's scratch outlives its sessions (the driver's check, the next run) until it ends
        scratch.drop(p, label)
    if mark:
        mark.unlink(missing_ok=True)
    end, error = ("killed", None) if killed else ending(final, code, noise)
    if end == "budget":  # stopped by its own limit: the caller treats it as a killed run, never retries it
        killed, error = error, None
    if session is not None:
        session["id"] = final.get("session_id")
    append_jsonl(p.state / "ledger.jsonl", {
        "role": role, "label": label, "ticket": ticket, "model": model, "effort": effort,
        "minutes": round((time.time() - started) / 60, 1), **spent(final, seen),
        "base": first_turn(seen), "usd": final.get("total_cost_usd"), "killed": killed, "end": end, "error": error,
        "exit": code, "stream": raw.name})
    actions.refresh(p)  # the next worker start reads a fresh total from the cache, never waits on a count
    return result, killed, error


def rules(p, name):
    text = (HOME / "prompts" / name).read_text()
    for k, v in {"{upstream}": p.upstream, "{remote}": p.remote, "{main}": p.main}.items():
        text = text.replace(k, v)
    return text
