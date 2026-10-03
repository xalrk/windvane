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

from .util import append_jsonl, load_json, save_json

NEST = "WINDVANE_SESSION"  # names the session a command runs in, so a session started inside it can register
TOOL_MS = "600000"
POLL = 5  # seconds between a quiet session's stop checks (small file reads, no tokens): how soon `stop --now` acts


def env(extra=None):
    """The sessions' environment: AUTOPILOT=1 keeps the user's ntfy hook quiet (the driver pushes its own). The Bash
    tool's default timeout is 2 minutes; the rules and `windvane await` assume a tool call may run 10."""
    return {**os.environ, "AUTOPILOT": "1", "BASH_DEFAULT_TIMEOUT_MS": TOOL_MS, "BASH_MAX_TIMEOUT_MS": TOOL_MS,
            **(extra or {})}


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


def kill_nested(p, session):
    """Stop the sessions started inside `session`. Claude Code runs each Bash command in a session of its own, so
    killing a worker's process group misses a critic it started: each one registers its group here instead."""
    d = p.state / "nested" / session
    for f in d.glob("*") if d.is_dir() else ():
        try:
            os.killpg(int(f.name), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, ValueError):
            pass
        f.unlink(missing_ok=True)
    try:
        d.rmdir()
    except OSError:  # gone, or a nested session registered just now (its own deadline and caps still stop it)
        pass


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
        """A tiny Haiku call, only to read the current usage."""
        d = p.state / "probe"
        d.mkdir(parents=True, exist_ok=True)
        try:
            out = subprocess.run(["claude", "-p", "ok", "--model", "haiku", "--output-format", "stream-json",
                                  "--verbose", "--no-session-persistence", "--setting-sources", "local",
                                  "--strict-mcp-config"], cwd=d, capture_output=True, text=True, timeout=180,
                                 env=env()).stdout
        except subprocess.TimeoutExpired:
            return
        for line in out.splitlines():
            if '"rate_limit_event"' in line:
                self.update(json.loads(line).get("rate_limit_info", {}))


# The built-in tools each windvane role starts with (`--tools`). Every tool's description is re-read on every turn,
# so the full set (25 tools, ~10k tokens) doubled a worker's first-turn context. A role not named here (a project's
# [[periodic]] session) keeps the CLI's default set and the project's skills.
TOOLS = {"worker": ["Bash", "Read", "Edit", "Write"], "fix": ["Bash", "Read", "Edit", "Write"],
         "planner": ["Bash", "Read", "Edit", "Write"], "reviewer": ["Bash", "Read"],
         "critic": ["Read", "Glob", "Grep"], "preflight": ["Read", "Glob", "Grep"]}
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


def settings_json(allow=(), deny=()):
    """The session's permission rules. A project's `[permissions]` rules come after windvane's own; deny wins over
    allow in Claude Code, so a project's allow can't lift a deny that keeps a session off `main`."""
    return json.dumps({"permissions": {"allow": list(allow), "deny": list(deny)}})


def ending(final, code, noise):
    """How a session ended, as (kind, error text): "ok"; "error" when the result event says the API or the CLI failed
    (overload, 5xx, not logged in, max turns); "no result" when the process died before one (network drop, crash).
    `noise` is the stream's last non-JSON lines, where a crash says why."""
    if not final:
        why = " ".join(" ".join(noise).split())[-300:]
        return "no result", f"no result event (exit {code})" + (f": {why}" if why else "")
    sub = final.get("subtype") or "success"
    if final.get("is_error") or sub != "success":
        why = " ".join(str(final.get("result") or final.get("errors") or "").split())
        why = re.sub(r'\{.*"message":\s*"([^"]*)".*\}', r"\1", why)[:300] or f"exit {code}"  # the API's JSON body
        return "error", why if sub == "success" else f"{sub}: {why}"  # it reaches the phone: plain words only
    return "ok", None


def run_claude(p, role, label, prompt, system, cwd, allow=(), deny=(), usage=None, stop_if=None, ticket="",
               limits=None):
    """Run one headless session to its end. `stop_if()` returns a reason to kill it (deadline, usage cap);
    `limits` (kill time, 5-hour cap, weekly cap) passes the same rule on to sessions started inside this one.
    Returns (final text, why it was stopped or None, why it failed or None): a failure is the session's own
    (API error, crash), never the ticket's, so callers can retry it."""
    if p.perm_problems:  # the run and `windvane periodic` stop before this; a desk `plan` or `critic` stops here
        raise SystemExit(f"windvane: bad permission rules in config.toml: {'; '.join(p.perm_problems)}")
    own = p.perms.get(role, {})
    allow, deny = [*allow, *own.get("allow", ())], [*deny, *own.get("deny", ())]
    model, effort = p.models.get(role, "opus"), p.models.get(f"{role}_effort", "high")
    raw = p.state / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", prompt, "--model", model, "--effort", effort, "--permission-mode", "auto",
           "--output-format", "stream-json", "--verbose", "--setting-sources", "project,local",
           *lean_flags(p, role), "--settings", settings_json(allow, deny), "--append-system-prompt", system]
    started, parent = time.time(), os.environ.get(NEST)
    extra = {NEST: raw.stem}
    if limits:
        extra.update(WINDVANE_STOP_AT=str(limits[0].timestamp()), WINDVANE_CAPS=f"{limits[1]:g},{limits[2]:g}")
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, env=env(extra))
    mark = p.state / "nested" / parent / str(proc.pid) if parent else None
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
    code = proc.wait()
    kill_nested(p, raw.stem)  # killed with it, or left running by a Bash call that timed out
    if mark:
        mark.unlink(missing_ok=True)
    end, error = ("killed", None) if killed else ending(final, code, noise)
    u, turns = final.get("usage"), final.get("num_turns")
    estimated = not u and bool(seen)
    if estimated:  # killed or crashed before its result event: add up what the stream showed
        u = {k: sum(m.get(k) or 0 for m in seen.values()) for k in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
        turns = len(seen)
    u = u or {}
    append_jsonl(p.state / "ledger.jsonl", {
        "role": role, "label": label, "ticket": ticket, "model": model, "effort": effort,
        "minutes": round((time.time() - started) / 60, 1), "turns": turns, "estimated": estimated,
        "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
        "base": first_turn(seen), "usd": final.get("total_cost_usd"), "killed": killed, "end": end, "error": error,
        "exit": code, "stream": raw.name})
    return result, killed, error
