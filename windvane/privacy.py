"""Secrets and personal data in what windvane commits: tokens, ntfy URLs and this machine's topics, the user's own
private words, personal emails.

`.windvane/` is committed and pushed (the autopilot reads it from the remote), so an answer typed at the desk or a
ticket line a session wrote stays in git history for good. This finds such lines before they leave the machine. It
reports only a kind and a place (`file:line`), never the matched text, so its findings can go in a log line or a
phone push; the secrets it looks for (the topics, the private words) are read from this machine's config at run time
and kept only in memory."""
import getpass
import os
import re
import socket
import subprocess
from pathlib import Path

from .config import DIR

# Domains and addresses that are placeholders or public bot addresses, not anyone's personal email.
SAFE_DOMAINS = re.compile(r"(^|\.)(example\.(com|org|net)|test|example|invalid|localhost|local|lan|internal"
                          r"|home\.arpa)$", re.I)
SAFE_ADDRESS = re.compile(r"no-?reply|^git@", re.I)  # git@host: an SSH remote
# A real address ends in a top-level domain of two letters or more, so a version pin (`pkg@v2026.10.13`, a range
# ending in `18.x`) isn't one; an ssh login to a `.local` or `.lan` host is SAFE_DOMAINS'.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})(?![A-Za-z0-9-])")
NTFY_URL = re.compile(r"ntfy\.[A-Za-z0-9.-]+/[A-Za-z0-9_-]{3,}")
MNT_PATH = re.compile(r"/mnt/[A-Za-z0-9_.-]+")
TOKEN = re.compile(r"\b(ghp_[A-Za-z0-9]{30,}|gh[ousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
                   r"|sk-ant-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})")
COMMON_HOSTS = {"localhost", "fedora", "ubuntu", "debian", "linux", "archlinux", "raspberrypi"}
# The kinds that are a leak wherever they show up. A home path, the username or the hostname are often named honestly
# (a ticket about a path), so the guards on answers and pushes leave them to a full scan.
SURE = ("token", "ntfy-url", "ntfy-topic", "private-term", "email")
NAMES = {"token": "a token", "ntfy-url": "an ntfy URL", "ntfy-topic": "this machine's ntfy topic",
         "private-term": "a word from private-terms.txt", "email": "a personal email address",
         "home-path": "this machine's home path", "username": "this machine's username",
         "hostname": "this machine's hostname", "mnt-path": "a /mnt path"}


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def topics():
    """The real ntfy topics on this machine: the last path part of any URL value and every *TOPIC* value."""
    found = set()
    for f in (config_dir() / "ntfy/config", config_dir() / "windvane/phone.toml"):
        try:
            text = f.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            m = re.match(r"\s*([A-Za-z_][\w.-]*)\s*=\s*(.+?)\s*$", line)
            if not m or line.lstrip().startswith("#"):
                continue
            key, value = m.group(1).lower(), m.group(2).strip("\"'")
            if "url" in key and "/" in value.rstrip("/").split("://")[-1]:
                found.add(value.rstrip("/").rsplit("/", 1)[-1])
            elif "topic" in key:
                found.add(value.rsplit("/", 1)[-1])
    return {t for t in found if len(t) >= 6}


def private_terms():
    """The user's own words to keep out (a name, a client, a street), one per line in private-terms.txt."""
    try:
        lines = (config_dir() / "windvane/private-terms.txt").read_text().splitlines()
    except OSError:
        return set()
    return {t.strip() for t in lines if len(t.strip()) >= 3 and not t.strip().startswith("#")}


def literal(words):
    """One case-insensitive regex, matching whole words (`_` and `-` count as breaks), for a set of literal strings,
    or None."""
    words = sorted(w for w in words if w)
    if not words:
        return None
    return re.compile(r"(?<![A-Za-z0-9])(" + "|".join(map(re.escape, words)) + r")(?![A-Za-z0-9])", re.I)


def username():
    """The login name, or "" where there is none (a container's arbitrary uid): only a full scan asks."""
    try:
        return os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
    except (KeyError, OSError):
        return ""


def rules(kinds=SURE):
    """kind -> regex for `kinds` ("email" needs none: `kinds_in` checks it). The machine's own ones are built here,
    at run time, and only kept in memory."""
    home = os.path.expanduser("~").rstrip("/")
    ci = bool(os.environ.get("CI"))  # a CI machine's user and host (`runner`) are nobody's, and common words
    make = {"home-path": lambda: re.compile(re.escape(home) + r"(?![\w.-])") if home.count("/") >= 2 else None,
            "username": lambda: literal({u} if len(u := username()) >= 4 and not ci else set()),
            "hostname": lambda: literal({h} if len(h := socket.gethostname().split(".")[0]) >= 4
                                        and h.lower() not in COMMON_HOSTS and not ci else set()),
            "mnt-path": lambda: MNT_PATH, "ntfy-url": lambda: NTFY_URL, "ntfy-topic": lambda: literal(topics()),
            "private-term": lambda: literal(private_terms()), "token": lambda: TOKEN, "email": lambda: None}
    built = {k: make[k]() for k in kinds}
    return {k: v for k, v in built.items() if v or k == "email"}


def personal_email(text):
    for m in EMAIL.finditer(text):
        if not SAFE_DOMAINS.search(m.group(1)) and not SAFE_ADDRESS.search(m.group(0)):
            return True
    return False


def kinds_in(text, rules):
    """The kinds of leak in one line of text (the matches themselves are dropped right here)."""
    found = {k for k, rx in rules.items() if rx and rx.search(text)}
    if "email" in rules and personal_email(text):
        found.add("email")
    if "username" in found and "home-path" in found and not rules["username"].search(rules["home-path"].sub("", text)):
        found.discard("username")  # the home path already holds it: one leak, one kind
    return found


def guarded(p):
    """The kinds the guards on `p`'s answers and pushes refuse: SURE, less the project's `scan_allow`."""
    return tuple(k for k in SURE if k not in p.cfg["scan_allow"])


def scan_text(text, kinds=SURE):
    """The kinds of leak in `text`, sorted."""
    r = rules(kinds)
    return sorted({k for line in text.splitlines() for k in kinds_in(line, r)})


def added(repo, revs, path=None):
    """(commit, file, line number, text) for each line the commits `revs` name add (under `path`, when given), from
    each commit's own diff: a leak a later commit took out again is still in the history a push sends. A merge counts
    by its diff to its first parent, so a line typed into a conflict's resolution is seen too; a line found in two
    commits is listed once."""
    out = subprocess.run(["git", "-c", "core.quotepath=off", "log", "-p", "-U0", "--no-color", "--no-ext-diff",
                          "--no-textconv", "--no-renames", "--diff-merges=first-parent", "--format=%x01%h", *revs,
                          "--", *([path] if path else [])],
                         cwd=repo, capture_output=True).stdout.decode("utf-8", "replace")
    lines, seen, sha, name, n, head = [], set(), "", "", 0, False
    for line in out.splitlines():
        if line.startswith("\x01"):
            sha, head = line[1:], False
        elif line.startswith("diff --git "):
            name, head = "", True  # until its first hunk, `+++ ` is the file's header, not an added line
        elif head and line.startswith("+++ "):
            name = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", line)
            n, head = int(m.group(1)) if m else 0, False
        elif line.startswith("+") and name and not head:
            if (name, line) not in seen:
                seen.add((name, line))
                lines.append((sha, name, n, line[1:]))
            n += 1
    return lines


def on(repo, rev, path):
    """Every line of `rev`'s files under `path`, as a set: what's already there doesn't count as new."""
    out = subprocess.run(["git", "grep", "-h", "-I", "-e", "", rev, "--", path], cwd=repo, capture_output=True).stdout
    return set(out.decode("utf-8", "replace").splitlines())


def outgoing(repo, base, head="HEAD", path=".windvane", kinds=SURE):
    """[(kind, "file:line in commit")] for leaks in the lines head's new commits add under `path`. A line already on
    `base` (an old leak a commit only moves, as answering a question moves it in QUESTIONS.md) doesn't count: one
    old leak mustn't stop every later push. Reading `base`'s files only happens when something was found."""
    r = rules(kinds)
    found = [(k, sha, name, n, text) for sha, name, n, text in added(repo, [f"{base}..{head}"], path)
             for k in sorted(kinds_in(text, r))]
    old = on(repo, base, path) if found else set()
    return [(k, f"{name}:{n} in {sha}") for k, sha, name, n, text in found if text not in old]


def history(repo):
    """[(kind, "file:line in commit")] for every kind in the lines any commit on any ref adds: what a repository
    shows once it's public, deleted files included."""
    r = rules(tuple(NAMES))
    return [(k, f"{name}:{n} in {sha}") for sha, name, n, text in added(repo, ["--all"])
            for k in sorted(kinds_in(text, r))]


def said(found, most=3):
    """Findings in words, kinds and places only: "a token at .windvane/QUESTIONS.md:12 in 1a2b3c4"."""
    parts = [f"{NAMES.get(k, k)} at {where}" for k, where in found[:most]]
    return ", ".join(parts) + (f" and {len(found) - most} more" if len(found) > most else "")


def refusal(p, text):
    """Why free text the user typed can't be committed, or "" when it can: the kinds it holds, never the text."""
    kinds = scan_text(text, guarded(p))
    return (f"it holds {' and '.join(NAMES[k] for k in kinds)}, and answers are committed and pushed with {DIR}/"
            if kinds else "")
