"""Network outages: a dropped connection is nobody's fault and fixes itself, so the run waits it out instead of failing
tickets, spending crash retries or dying of a git timeout. `online()` says whether the API and the git remote can be
reached; `wait()` pauses until they can; `git`/`sh`/`call` run the commands that talk to the network (fetch, push,
ls-remote, gh) without ever raising, and wait and try again when one fails with the network down."""
import os
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit

from .util import log, now, push, run

TIMEOUT = 300  # seconds a network command may take: a stalled connection fails instead of hanging the run
PROBE_S = 4  # seconds each host may take to resolve and answer
LONG = 10  # minutes of outage after which its end is pushed (nothing can be pushed while it lasts)

probe = None  # tests: a function standing in for the real probe
waiter = None  # set by the driver: waits for the network, False when a stop or the deadline ended the wait
hosts = []  # (host, port) of the git remote, set by the driver; the API host is always probed
lock = threading.Lock()  # parallel slots wait one at a time: the outage is logged and pushed once


def api_host():
    """Where the sessions' API traffic goes: the HTTPS proxy when one is set, else the API (or its configured URL)."""
    for var in ("HTTPS_PROXY", "https_proxy", "ANTHROPIC_BASE_URL"):
        if os.environ.get(var):
            u = urlsplit(os.environ[var] if "//" in os.environ[var] else "//" + os.environ[var])
            if u.hostname:
                return u.hostname, u.port or (80 if u.scheme == "http" else 443)
    return "api.anthropic.com", 443


def remote_host(url):
    """(host, port) a git remote URL connects to, or None for a local path or an ssh alias (a name without a dot,
    which only ~/.ssh/config resolves: a DNS probe of it would always say down)."""
    m = re.match(r"^(?:[\w.-]+@)?([\w.-]+):(?!//)", url)  # scp-like ssh: git@host:owner/repo
    host, port = (m.group(1), 22) if m and "/" not in m.group(1) else (None, None)
    if host is None and "://" in url:
        u = urlsplit(url)
        if u.scheme == "file":
            return None
        host, port = u.hostname, u.port or {"ssh": 22, "git": 9418, "http": 80}.get(u.scheme, 443)
    return (host, port) if host and "." in host else None


def reach(host, port):
    try:
        with socket.create_connection((host, port), timeout=PROBE_S):
            return True
    except OSError:  # no DNS, no route, refused, timed out
        return False


def online():
    """Whether the API and the git remote resolve and answer, a few seconds each."""
    if probe:
        return probe()
    return all(reach(h, port) for h, port in [api_host(), *hosts])


def wait(p, until, stopped, sleep=time.sleep):
    """Wait for the network: probe after 1, 2, 4, then every 5 minutes. True once it's back; False when `stopped()`
    or the deadline (`until()`, live) ends the wait first. Logs once when it went down and once when it's back, and
    pushes then when the wait was long: the user may have had a phone full of silence."""
    with lock:
        if online():
            return True
        since, waited, k = now(), 0, 0
        log(p, f"network down since {since:%H:%M}: waiting for it")
        while True:
            for _ in range(min(5, 2 ** k)):
                if stopped() or now() >= until():
                    log(p, f"network still down after {waited} min; stopped waiting")
                    return False
                sleep(60)
                waited += 1
            k += 1
            if online():
                break
        log(p, f"network back after {waited} min; carrying on")
        if waited >= LONG:
            push(f"{p.name}: network back", f"It was down from {since:%H:%M} for {waited} min. The run waited and "
                 "carried on; nothing was failed for it.", 3, "signal_strength")
        return True


def down_wait():
    """With the network down, wait for it (when a driver set a waiter): True to try again."""
    return bool(waiter) and not online() and waiter()


def call(cmd, cwd, timeout=TIMEOUT, env=None):
    """Run a network command: a timeout or a missing `gh` is a failed result, never an exception. A failure while
    the network is down waits for it and tries again."""
    while True:
        try:
            r = run(cmd, cwd, timeout, env)
        except subprocess.TimeoutExpired:
            r = subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 127, "", str(e))
        if r.returncode == 0 or not down_wait():
            return r


def git(cwd, *args):
    return call(["git", *args], cwd)


def sh(cwd, *cmd):
    """stdout of a network command, stripped; "" when it fails (as util.sh)."""
    r = call(list(cmd), cwd)
    return r.stdout.strip() if r.returncode == 0 else ""
