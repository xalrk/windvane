#!/usr/bin/env python3
"""Render every assistant message in a state dir's session logs the way `windvane watch` does, at a few widths, and
report what a renderer change broke: lines wider than the width, crashes, Markdown markers left in the text, and a
`---` rule wider than every other line. Each message is checked whole and as `watch` prints it, clipped; a finding
only the clip has is marked `clipped`. After the logs come a few built-in messages (`synthetic:<n>`) with what the
logs may lack, such as rules beside tables; --no-synthetic leaves them out.

  watch-sweep.py STATE_DIR [--widths 60,80,120] [--show] [--no-synthetic]

It reads `STATE_DIR/runs/*.jsonl` and never runs `claude`. Findings are printed as `file:line`, the width and the
kind; the rendered line itself only with --show, since it may hold paths or names. Code (spans and fenced blocks)
is skipped for markers: a marker there is what was typed.

Exit status: 0 clean, 1 findings, 2 no logs to read.
"""
import argparse
import json
import re
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import watch  # noqa: E402

ANSI = re.compile(r"\033\[[0-9;]*m")
CODE_TEXT = re.compile(r"\033\[(?:[0-9;]*;)?94m[^\033]*")  # a code span's text runs to the next escape
FENCED = "\033[2m  │ \033[0m"  # how a line inside a fenced block starts, with colour
MARKERS = [("bold **", re.compile(r"\*\*")), ("strike ~~", re.compile(r"~~")), ("backtick", re.compile(r"`")),
           ("link ](", re.compile(r"\]\(")), ("heading #", re.compile(r"^\s{0,3}#{1,6} ")),
           ("table rule", re.compile(r"^\s*\|?\s*:?-{3,}:?\s*\|"))]
PROSE = "Some **bold** prose with `code` that runs on long enough to wrap at every width the sweep tries. " * 2
NARROW = "| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 22 |"
WIDE = "\n".join(["| " + " | ".join(f"Column {k}" for k in range(8)) + " |", "|" + "---|" * 8]
                 + ["| " + " | ".join(f"row {r} cell {k}" for k in range(8)) + " |" for r in range(3)])
SYNTHETIC = [  # rules beside what they're sized from; the session logs had none
    PROSE + "\n\nIn short:\n\n---\n\n" + NARROW + "\n\n---\n\n" + PROSE,
    NARROW + "\n\n---\n\n" + NARROW.replace("alpha", "a much longer name"),
    WIDE + "\n\n---\n\n" + PROSE,
    "---\n\n" + PROSE + "\n\n" + NARROW + "\n\n---",
    PROSE + "\n---\n" + NARROW + "\n---\nDone.",  # no blank lines: each rule still stops at the other
]


def messages(state):
    """(place, text) for every assistant text block in the session logs, oldest log first."""
    for f in sorted((state / "runs").glob("*.jsonl")):
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict) or ev.get("type") != "assistant":
                continue
            for b in ev.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    yield f"{f.name}:{n}", b["text"].strip()


def check(text, cols):
    """(kind, rendered line) for each problem in one message at one width, whole and as `watch` clips it."""
    found = []
    for color in (False, True):
        try:
            parts = watch.blocks(text, color, cols)
            whole = [line for b in parts for line, _ in b]
            clipped = watch.clip_rendered(parts, watch.TEXT_LINES, color).split("\n")
        except Exception:
            return [("crash", traceback.format_exc().strip().splitlines()[-1])]
        seen = problems(whole, color, cols) + rule_past_text(parts)
        found += seen + [("clipped " + kind, line) for kind, line in problems(clipped, color, cols) if (kind, line)
                         not in seen]
    return list(dict.fromkeys(found))  # once each, though both colourings may find it


def problems(lines, color, cols):
    """(kind, plain line) for each rendered line wider than `cols` or, coloured, with a marker left in its prose."""
    found = []
    for line in lines:
        plain = ANSI.sub("", line)
        if watch.width(plain) > cols:
            found.append((f"too wide ({watch.width(plain)})", plain))
        if color and not line.startswith(FENCED):
            prose = ANSI.sub("", CODE_TEXT.sub("", line))
            found += [(f"marker {name}", plain) for name, rx in MARKERS if rx.search(prose)]
    return found


def rule_past_text(parts):
    """A finding for each `---` rule (a block of one line of nothing but ─) wider than every line of the block just
    above it and just below it, past blank lines and up to another rule: it runs on past what it parts. A table's or
    records' own rules are inside their block, so they're not checked."""
    plain = [[ANSI.sub("", line) for line, _ in b] for b in parts]
    is_rule = [len(b) == 1 and set(b[0]) == {"─"} for b in plain]
    found = []
    for i, b in enumerate(plain):
        if not is_rule[i]:
            continue
        near = []
        for step in (-1, 1):
            j = i + step
            while 0 <= j < len(plain) and plain[j] == [""]:
                j += step
            while 0 <= j < len(plain) and plain[j] != [""] and not is_rule[j]:
                near += [watch.width(line) for line in plain[j]]
                j += step
        if near and watch.width(b[0]) > max(near):
            found.append((f"rule past text ({watch.width(b[0])})", b[0]))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("state", type=Path)
    ap.add_argument("--widths", default="60,80,120")
    ap.add_argument("--show", action="store_true", help="print the offending rendered line too")
    ap.add_argument("--synthetic", action=argparse.BooleanOptionalAction, default=True,
                    help="check the built-in messages too (default: on)")
    a = ap.parse_args(argv)
    widths = [int(w) for w in a.widths.split(",")]
    msgs = list(messages(a.state))
    if not msgs:
        print(f"no assistant messages in {a.state / 'runs'}")
        return 2
    extra = [(f"synthetic:{n}", text) for n, text in enumerate(SYNTHETIC, 1)] if a.synthetic else []
    counts = {}
    for place, text in msgs + extra:
        for cols in widths:
            for kind, line in check(text, cols):
                key = kind.split(" (")[0]
                counts[key] = counts.get(key, 0) + 1
                print(f"{place} @{cols}: {kind}" + (f"\n    {line}" if a.show else ""))
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "clean"
    more = f" + {len(extra)} synthetic" if extra else ""
    print(f"{len(msgs)} messages{more} at widths {','.join(map(str, widths))}: {summary}")
    return 1 if counts else 0


if __name__ == "__main__":
    sys.exit(main())
