"""Headless Claude sessions: one per worker, critic, reviewer and planner run. Each run's stream is kept in
`runs/`, and its tokens and turns go to `ledger.jsonl` (`windvane stats`). Usage limits come from the stream's
rate_limit_event, so they count the user's other sessions too."""
import json
import os
import selectors
import signal
import subprocess
import time
from datetime import datetime

from .util import append_jsonl, load_json, save_json



def env():
    """The sessions' environment: AUTOPILOT=1 keeps the user's ntfy hook quiet (the driver pushes its own)."""
    return {**os.environ, "AUTOPILOT": "1"}


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


def settings_json(allow=(), deny=()):
    return json.dumps({"permissions": {"allow": list(allow), "deny": list(deny)}})


def run_claude(p, role, label, prompt, system, cwd, allow=(), deny=(), usage=None, stop_if=None, ticket=""):
    """Run one headless session to its end. `stop_if()` returns a reason to kill it (deadline, usage cap).
    Returns (final text, why it was stopped or None)."""
    model, effort = p.models.get(role, "opus"), p.models.get(f"{role}_effort", "high")
    raw = p.state / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{label}.jsonl"
    raw.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", prompt, "--model", model, "--effort", effort, "--permission-mode", "auto",
           "--output-format", "stream-json", "--verbose", "--setting-sources", "project,local",
           "--strict-mcp-config", "--settings", settings_json(allow, deny), "--append-system-prompt", system]
    started = time.time()
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, env=env())
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    result, killed, final = "", None, {}
    with raw.open("w") as out:
        while True:
            if sel.select(timeout=30):
                line = proc.stdout.readline()
                if not line:
                    break
                out.write(line)
                out.flush()
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "rate_limit_event" and usage:
                    usage.update(ev.get("rate_limit_info", {}))
                elif ev.get("type") == "result":
                    result, final = ev.get("result") or "", ev
            if killed is None and stop_if:
                killed = stop_if()
                if killed:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(60)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    u = final.get("usage") or {}
    append_jsonl(p.state / "ledger.jsonl", {
        "role": role, "label": label, "ticket": ticket, "model": model, "effort": effort,
        "minutes": round((time.time() - started) / 60, 1), "turns": final.get("num_turns"),
        "input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
        "usd": final.get("total_cost_usd"), "killed": killed, "stream": raw.name})
    return result, killed
