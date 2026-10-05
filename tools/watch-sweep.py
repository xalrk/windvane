#!/usr/bin/env python3
"""Render every assistant message in a state dir's session logs the way `windvane watch` does, at a few widths, and
report what a renderer change broke: lines wider than the width, crashes, and Markdown markers left in the text.

  watch-sweep.py STATE_DIR [--widths 60,80,120] [--show]

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
    """(kind, rendered line) for each problem in one message at one width."""
    found = []
    for color in (False, True):
        try:
            lines = watch.markdown(text, color, cols)
        except Exception:
            return [("crash", traceback.format_exc().strip().splitlines()[-1])]
        for line in lines:
            plain = ANSI.sub("", line)
            if watch.width(plain) > cols:
                found.append((f"too wide ({watch.width(plain)})", plain))
            if color and not line.startswith(FENCED):
                prose = ANSI.sub("", CODE_TEXT.sub("", line))
                found += [(f"marker {name}", plain) for name, rx in MARKERS if rx.search(prose)]
    return list(dict.fromkeys(found))  # once each, though both colourings may find it


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("state", type=Path)
    ap.add_argument("--widths", default="60,80,120")
    ap.add_argument("--show", action="store_true", help="print the offending rendered line too")
    a = ap.parse_args(argv)
    widths = [int(w) for w in a.widths.split(",")]
    msgs = list(messages(a.state))
    if not msgs:
        print(f"no assistant messages in {a.state / 'runs'}")
        return 2
    counts = {}
    for place, text in msgs:
        for cols in widths:
            for kind, line in check(text, cols):
                key = kind.split(" (")[0]
                counts[key] = counts.get(key, 0) + 1
                print(f"{place} @{cols}: {kind}" + (f"\n    {line}" if a.show else ""))
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "clean"
    print(f"{len(msgs)} messages at widths {','.join(map(str, widths))}: {summary}")
    return 1 if counts else 0


if __name__ == "__main__":
    sys.exit(main())
