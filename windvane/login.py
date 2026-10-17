"""An expired Claude login: every session dies at once, nothing a retry fixes, and it's over the moment the user logs
in again. Like a network outage (net.py) it is nobody's fault, so the run spends no crash retry on it and sets no
ticket aside: it pushes once, asking for /login, and waits without starting sessions until the login looks back.

Back is told without spending tokens: `claude auth status` turning to logged in, or the login file changing (a /login
rewrites it). Where neither can show it (`auth status` never saying logged out, a login kept in the macOS Keychain),
the session is tried again every `BLIND` minutes: one that dies of the login costs nothing. Runs on one machine share
the login, so the expiry is pushed once per machine (a marker in the state root), and the first run to see the login
back (or, after a blind try, a session that works) clears it and says the runs go on."""
import json
import os
import re
import subprocess
import threading
import time

from .config import user_dir
from .util import log, now, push

# What `claude -p` says when its login is gone: the API's 401, or the CLI's own words before it reaches the API
ERROR = re.compile(r"authentication_error|API Error:?\s*401\b|failed to authenticate|not logged in|please run /login"
                   r"|oauth (?:session|token) (?:has )?(?:expired|been revoked|revoked)|invalid api key", re.I)
POLL = 60  # seconds between looks at the login while waiting
BLIND = 15  # minutes after which a session is tried again with no sign the login is back
STALE = 12 * 3600  # seconds after which the machine's marker no longer counts as said: a later run pushes again
TIMEOUT = 30  # seconds `claude auth status` may take

status = None  # tests: a function standing in for `claude auth status`, returning logged in or not (or None)
lock = threading.Lock()  # parallel slots wait one at a time: the expiry is logged once per run
waits = []  # one entry per slot waiting now (or queued on `lock`): the driver starts no session meanwhile
back_at = 0.0  # when this process last saw the login look back: a session started before it is rerun at once
waiter = None  # set by the driver: waits for the login after a session (label, error) died of it, False when a stop
#                or the deadline ended the wait; without one (a desk command) a login death is an error as any


def expired(error):
    """Whether a session's error says the Claude login is gone."""
    return bool(error and ERROR.search(str(error)))


def marker():
    """The machine's mark that an expiry was pushed: in the state root, shared by every project's runs."""
    return user_dir("XDG_STATE_HOME", ".local/state") / "login-expired"


def logged_in(cwd):
    """`claude auth status`: True or False, None when it can't tell (an old CLI, a timeout). Logged in needs both its
    exit code and its JSON to say so."""
    if status:
        return status()
    from .runner import env
    try:
        r = subprocess.run(["claude", "auth", "status", "--json"], cwd=cwd, capture_output=True, text=True,
                           timeout=TIMEOUT, env=env())
        return r.returncode == 0 and json.loads(r.stdout).get("loggedIn") is True
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return None


def file_sig():
    """The login file's (mtime, size), or None without one: a /login rewrites it."""
    from .runner import login_file
    try:
        st = login_file().stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def said_once(since):
    """Whether this expiry was already pushed on this machine; marks it said when not."""
    m = marker()
    m.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(m, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - m.stat().st_mtime < STALE:
                return True
        except OSError:
            pass
        m.write_text(f"{since:%Y-%m-%d %H:%M}\n")  # an old mark: this is a new expiry
        return False
    with os.fdopen(fd, "w") as f:
        f.write(f"{since:%Y-%m-%d %H:%M}\n")
    return False


def ok():
    """The login works again (a wait saw it back, or a session ended well): the first run on the machine to see it
    after an expiry pushes that the runs go on."""
    try:
        marker().unlink()
    except OSError:
        return
    push("Claude login back", "The waiting runs go on by themselves.", 3, "key")


def waiting():
    """Whether a slot of this run is waiting for the login: no session may start until it ends."""
    return bool(waits)


def down_wait(label, error):
    """After a session died of the login, wait for it (when a driver set a waiter): True to run it again."""
    return bool(waiter) and waiter(label, error)


def gate():
    """Hold back a session about to start while a slot waits for the login (a slot already past the driver's own
    check when the wait began): it starts once the wait ends."""
    with lock:
        pass


def wait(p, label, error, started, until, stopped, sleep=time.sleep):
    """Wait for the login to come back after `label` died of it (`error`), a session started at `started` (epoch
    seconds). True to rerun it; False when `stopped()` or the deadline (`until()`, live) ended the wait first."""
    global back_at
    waits.append(label)  # list appends and removes are atomic under the GIL: no lock for the count
    try:
        with lock:
            if back_at > started:  # another slot's wait ended after this session began
                log(p, f"  {label} died of the Claude login; it's back since, so rerun at once, no retry spent")
                return True
            since = now()
            log(p, f"  {label} died of the Claude login ({error}); waiting for /login, no retry spent")
            if not said_once(since):
                push("Claude login expired", "Claude's login expired: run `claude` and /login. The run waits and goes "
                     "on by itself.", 4, "key")
            sig, was_out, waited = file_sig(), False, 0
            while True:
                if stopped() or now() >= until():
                    log(p, f"  Claude login still gone after {waited} min; stopped waiting")
                    return False
                sleep(POLL)
                waited += 1
                state = logged_in(p.state)
                if file_sig() != sig or (state and was_out):
                    why = "the Claude login looks back"
                    ok()
                    break
                was_out = was_out or state is False
                if waited % BLIND == 0 and not was_out:  # `auth status` can't tell: it never said logged out
                    why = f"no sign of the Claude login after {waited} min; trying again"
                    break
            log(p, f"  {why}: rerun {label}, no retry spent")
            back_at = time.time()
            return True
    finally:
        waits.remove(label)
