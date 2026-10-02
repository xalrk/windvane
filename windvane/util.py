"""Small shared helpers: git, the log, phone pushes, JSON state files."""
import json
import subprocess
from datetime import datetime
from pathlib import Path


def run(cmd, cwd, timeout=None, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)


def sh(cwd, *cmd):
    """stdout of a command, stripped; "" when it fails."""
    r = run(list(cmd), cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def git(cwd, *args):
    return run(["git", *args], cwd)


def gout(cwd, *args):
    return sh(cwd, "git", *args)


def now():
    return datetime.now()


def hhmm(t):
    return t.strftime("%H:%M") if t.date() == now().date() else t.strftime("%a %H:%M")


def log(p, msg, echo=True):
    line = f"{now():%Y-%m-%d %H:%M} {msg}"
    if echo:
        print(line, flush=True)
    p.state.mkdir(parents=True, exist_ok=True)
    with (p.state / "autopilot.log").open("a") as f:
        f.write(line + "\n")


def push(title, msg, prio=3, tags="", buttons=()):
    """A phone push (censored first), when there's a phone: see phone.py. Silently nothing without one."""
    from . import phone
    phone.push(title, msg, prio, tags, buttons)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def append_jsonl(path, entry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"at": f"{now():%Y-%m-%d %H:%M}", **entry}) + "\n")


def read_jsonl(path):
    out = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def last_match(regex, text):
    m = None
    for m in regex.finditer(text or ""):
        pass
    return m
