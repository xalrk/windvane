"""The scout: ideas from outside the loop's own records.

Every other source of ideas looks inward (the digest, failures, rejected experiments), and a loop that never looks
out runs out of ideas once it has fixed what it can see. A `builtin = "scout"` [[periodic]] entry runs every
`scout_every` tickets finished on main (counted across runs) and once in each plateau's exploration round. Its prompt
gets what changed in the installed Claude Code (`claude --help` and `--version` against what the last scout recorded,
in the state dir's `scout.json`) and the model ids new to the ledger; it alone has the web tools, and it has no shell.

Web text is untrusted: the scout's new tickets are all that may land (`vet` drops its notes on other tickets too), at
most MOST (the ids it's given), each an `Idea: scout` naming its `Source:`. One with a URL anywhere but that line, or
naming a package to install, is dropped with a log line; the rest still pass the experiment gate like any plan's.

A model id the ledger hasn't shown before also queues one ablation round (`models_watch`): the next plan that writes
experiments writes only `ablation` ones, since rules written for an older model are the first thing to test.
"""
import difflib
import re
import subprocess
import time
from pathlib import Path

from .queue import header_line
from .util import load_json, read_jsonl, save_json

MOST = 3  # tickets a scout round may land
TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "WebFetch", "WebSearch"]  # no Bash: nothing it read gets run
WEB = ["WebFetch", "WebSearch"]
HELP_LINES = 150  # a first scout's `claude --help`, cut: later ones get only the diff
DIFF_LINES = 120
URL = re.compile(r"https?://\S+|\bwww\.\S+", re.I)
INSTALL = re.compile(
    r"\b(?:pip3?|pipx|uv|uvx|poetry|npm|pnpm|yarn|bun|cargo|gem|go|brew|apt(?:-get)?|dnf|yum|conda|winget)\s+"
    r"(?:install|add|get|i)\b|\b(?:npx|uvx|pipx\s+run)\s+[\w@]"
    r"|\binstall(?:ing)?\s+(?:the\s+|a\s+)?[\w.@/-]+\s+(?:package|library|module|crate|gem|plugin)\b"
    r"|\badd\s+(?:a\s+|the\s+)?(?:new\s+)?(?:dependency|package)\b", re.I)


def state_file(p):
    return p.state / "scout.json"


def installed():
    """(`claude --version`, `claude --help`) of the claude on PATH, "" for one that fails."""
    def out(*args):
        try:
            r = subprocess.run(["claude", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.strip() if r.returncode == 0 else ""
    return out("--version"), out("--help")


def cli_part(st, version, help_text):
    """The prompt's section on Claude Code: the whole `--help` the first time, then only its diff against the last
    scout's, or one line when nothing changed (no diff to read)."""
    if not help_text:
        return "## Claude Code\n`claude --help` gave nothing here: read the changelog instead."
    if "help" not in st:
        lines = help_text.splitlines()
        cut = f"\n({len(lines) - HELP_LINES} more lines)" if len(lines) > HELP_LINES else ""
        return (f"## Claude Code {version} (the first scout: no earlier record)\n```\n"
                + "\n".join(lines[:HELP_LINES]) + f"\n```{cut}")
    if st["help"] == help_text and st.get("version") == version:
        return f"## Claude Code {version}: `claude --help` unchanged since the last scout ({st.get('at', '?')})"
    diff = list(difflib.unified_diff(st["help"].splitlines(), help_text.splitlines(), "last scout", "now",
                                     lineterm="", n=1))[2:]
    cut = f"\n({len(diff) - DIFF_LINES} more lines)" if len(diff) > DIFF_LINES else ""
    was = st.get("version") or "?"
    return (f"## Claude Code since the last scout ({version if was == version else f'{was} → {version}'})\n```diff\n"
            + ("\n".join(diff[:DIFF_LINES]) or "(only the version changed)") + f"\n```{cut}")


def models_part(st):
    new = st.get("new_models") or []
    return f"## Models new to the ledger since the last scout\n{', '.join(new)}" if new else ""


def start(p, done, explore):
    """A scout round starts: (its prompt's sections, what to record once it ends well). What made it due (`done`,
    tickets finished on main; `explore`, the exploration round it runs in) is recorded now, so a round that fails
    isn't due again at once; the CLI it saw only when it ends well, so a failed round's diff is shown again."""
    st = load_json(state_file(p), {})
    save_json(state_file(p), {**st, "done_at": done, **({"explored": explore["at"]} if explore else {})})
    version, help_text = installed()
    return (f"{cli_part(st, version, help_text)}\n\n{models_part(st)}".strip(),
            {"version": version, "help": help_text})


def recorded(p, seen):
    """A scout round ended well: the CLI it saw is the next one's baseline, and its new models were told."""
    st = load_json(state_file(p), {})
    st.pop("new_models", None)
    save_json(state_file(p), {**st, **(seen if seen.get("help") else {}), "at": time.strftime("%Y-%m-%d")})


def due(p, entry, done, explore):
    """Whether the scout `entry` is due: an exploration round open and not scouted yet, or `scout_every` tickets
    finished on main since it last ran (never ran: at once)."""
    st = load_json(state_file(p), {})
    if explore and explore.get("ids") is None and st.get("explored") != explore.get("at"):
        return True
    return "done_at" not in st or done - st["done_at"] >= entry["scout_every"]


def ledger_models(p):
    """The model ids the ledger's sessions ran on: the id Claude Code reported, or a configured full id."""
    out = set()
    for r in read_jsonl(p.state / "ledger.jsonl"):
        for m in (r.get("model_id"), r.get("model")):
            if isinstance(m, str) and m.startswith("claude-"):
                out.add(m)
    return out


def models_watch(p, log):
    """Model ids new to the ledger: told to the next scout, and the first time one shows, one ablation round queued
    (the next plan that writes experiments). The first ids ever seen are only the baseline."""
    ids = ledger_models(p)
    st = load_json(state_file(p), {})
    if "models" not in st:
        if ids:
            save_json(state_file(p), {**st, "models": sorted(ids)})
        return
    new = sorted(ids - set(st["models"]))
    if not new:
        return
    st["models"] = sorted(set(st["models"]) | set(new))
    st["new_models"] = sorted(set(st.get("new_models", [])) | set(new))
    if not st.get("ablation"):
        st["ablation"] = {"models": new}
        log(f"scout: {', '.join(new)} new in the ledger: the next plan's experiments are one ablation round, "
            "rules written for an older model first")
    save_json(state_file(p), st)


def ablation(p):
    """The queued ablation round ({"models"}), or None."""
    return load_json(state_file(p), {}).get("ablation")


def ablated(p, ids, log):
    """A plan wrote experiments (`ids`) outside an exploration round: that was the ablation round."""
    from .mix import exploring
    st = load_json(state_file(p), {})
    if ids and st.get("ablation") and not exploring(p):
        st["ablated"] = {**st.pop("ablation"), "ids": sorted(ids)}
        save_json(state_file(p), st)
        log(f"scout: the ablation round planned {', '.join(sorted(ids))}")


def refused(text):
    """Why the driver drops a scout ticket ("" when it may land): web text decides nothing that runs or that the
    project depends on, and the source is always on record."""
    head = "\n".join(text.splitlines()[:10])
    from .mix import idea
    if idea(head) != "scout":
        return "it isn't an `Idea: scout` ticket"
    if not header_line(head, "Source").strip():
        return "it names no `Source:`"
    m = URL.search(re.sub(r"(?im)\bSource:.*$", "", text))
    if m:
        return f"it names a URL to fetch ({m.group(0)[:60]})"
    m = INSTALL.search(text)
    if m:
        return f"it names a package to install ({' '.join(m.group(0).split())[:60]})"
    return ""


def vet(p, kept, name, log):
    """The scout's share of `Periodic.periodic_keep`'s result (only its MOST ids could land): the new tickets that
    pass `refused`, with their queue lines; its notes on other tickets dropped (it writes new tickets only)."""
    keep, tickets, (old_q, queue), notes = kept
    drop = []
    for tid in tickets:
        f = next(f for f in keep if p.norm(Path(f).stem) == tid)
        why = refused(keep[f])
        if not why:
            continue
        log(f"{name}: dropped {tid}: {why}")
        drop.append(tid)
        del keep[f]
    for f in notes:
        log(f"{name}: notes on {p.norm(Path(f).stem)} dropped: the scout writes new tickets only")
    if drop:
        bullet = re.compile(rf"^\s*-\s*(?:\*\*)?(?:{'|'.join(map(re.escape, drop))})\b", re.I)
        old = set(old_q.splitlines())
        queue = "\n".join(x for x in queue.splitlines() if x in old or not bullet.match(x))
    return keep, [t for t in tickets if t not in drop], (old_q, queue), {}
