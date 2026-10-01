"""`windvane watch`: follow the autopilot's sessions live from their streams in `runs/` (what each says, the
commands it runs and their output) and the driver's log. Local only: costs no tokens."""
import json
import re
import sys
import time
import unicodedata

tty = sys.stdout.isatty()
c = lambda code, s: f"\033[{code}m{s}\033[0m" if tty else s


def clip(text, n):
    rows = str(text).rstrip("\n").split("\n")
    return "\n".join(rows[:n]) + (f"\n… (+{len(rows) - n} lines)" if len(rows) > n else "")


# Markdown, rendered the way Claude Code's terminal does: line by line, so every rendered line closes its own
# colours and clipping between lines can never leave one on. Without colour the markers are dropped all the same.
SGR = {"b": "1", "dim": "2", "i": "3", "s": "9", "h": "36", "code": "94"}  # later keys win on a clash
CODE = re.compile(r"(`+)(.+?)\1")
ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
HOLD = re.compile("[\ue000-\uefff]")  # placeholders for code spans and escapes, so no marker inside them counts
SPAN = re.compile(
    r"\[(?P<lt>[^\]]+)\]\((?P<url>[^)\s]+)\)"
    r"|\*\*(?=\S)(?P<b1>.+?)(?<=\S)\*\*"
    r"|(?<!\w)__(?=\S)(?P<b2>.+?)(?<=\S)__(?!\w|\.\w)"
    r"|(?<![\w*])\*(?=[^\s*])(?P<i1>.+?)(?<=[^\s*])\*(?![\w*])"  # not intra-word: 2*3*4 stays as typed
    r"|(?<!\w)_(?=[^\s_])(?P<i2>.+?)(?<=[^\s_])_(?!\w|\.\w)"  # snake_case_names, __init__.py stay
    r"|~~(?=\S)(?P<s>.+?)(?<=\S)~~")
FENCE = re.compile(r"\s*(`{3,}|~{3,})\s*([^\s`]*)")
HEADING = re.compile(r" {0,3}#{1,6}\s+(.*?)(\s+#+)?\s*$")
RULE = re.compile(r" {0,3}([-*_])(\s*\1){2,}\s*$")
BULLET = re.compile(r"(\s*)[-*+]\s+(.*)")
NUMBERED = re.compile(r"(\s*)(\d+[.)])\s+(.*)")
QUOTE = re.compile(r"\s*>\s?(.*)")
TABLE_RULE = re.compile(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")


def inline(text, st=frozenset()):
    """Text as (piece, styles) segments. Code spans and backslash escapes are held out first, so `*.py` and
    `\\*` never open emphasis, while **a `b` c** still wraps the code span."""
    held = []

    def hold(literal, code):
        held.append((literal, code))
        return chr(0xE000 + len(held) - 1)
    text = CODE.sub(lambda m: hold(m.group(2).strip() or m.group(2), True), text)
    return spans(ESC.sub(lambda m: hold(m.group(1), False), text), st, held)


def spans(text, st, held):
    out, at = [], 0
    for m in SPAN.finditer(text):
        out += unhold(text[at:m.start()], st, held)
        if m.group("lt"):
            out += spans(m.group("lt"), st, held)
            if m.group("url") != m.group("lt"):
                out.append((f" ({m.group('url')})", st | {"dim"}))
        else:
            key = "b" if m.group("b1") or m.group("b2") else "i" if m.group("i1") or m.group("i2") else "s"
            out += spans(next(g for g in m.group("b1", "b2", "i1", "i2", "s") if g), st | {key}, held)
        at = m.end()
    return out + unhold(text[at:], st, held)


def unhold(text, st, held):
    out, at = [], 0
    for m in HOLD.finditer(text):
        i = ord(m.group()) - 0xE000
        if i < len(held):
            out += [(text[at:m.start()], st), (held[i][0], st | {"code"} if held[i][1] else st)]
            at = m.end()
    return out + [(text[at:], st)]


def paint(segs, color):
    """Segments as one line; with colour, every change of style starts from a reset and the line ends reset."""
    if not color:
        return "".join(t for t, _ in segs)
    out, cur = [], frozenset()
    for t, st in segs:
        if t and st != cur:
            out.append(("\033[0m" if cur else "") + (f"\033[{';'.join(SGR[k] for k in SGR if k in st)}m" if st else ""))
            cur = st
        out.append(t)
    return "".join(out) + ("\033[0m" if cur else "")


def width(s):
    """Columns a string takes in a terminal (wide CJK/emoji count two, combining marks none)."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def cells(row):
    """A pipe-table row's cells; a `|` inside a code span or escaped as `\\|` doesn't split."""
    s = row.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") and not s.endswith("\\|") else s
    out, cur, tick, i = [], "", False, 0
    while i < len(s):
        if s[i] == "\\" and s[i + 1:i + 2] == "|":
            cur, i = cur + "\\|", i + 2
            continue
        tick = not tick if s[i] == "`" else tick
        if s[i] == "|" and not tick:
            out.append(cur.strip())
            cur = ""
        else:
            cur += s[i]
        i += 1
    return out + [cur.strip()]


def table(rows, rule, color):
    """Rows padded to aligned columns (`:--:` centres, `--:` right-aligns), the header bold, a rule under it."""
    aligns = [("^" if a.startswith(":") else ">") if a.endswith(":") else "<" for a in cells(rule)]
    grid = [[inline(x, frozenset({"b"}) if r == 0 else frozenset()) for x in cells(row)] for r, row in enumerate(rows)]
    n = max(len(r) for r in grid)
    grid = [r + [[]] * (n - len(r)) for r in grid]
    widths = [max(width("".join(t for t, _ in r[k])) for r in grid) for k in range(n)]
    aligns += ["<"] * n
    bar = (" │ ", frozenset({"dim"}))
    out = []
    for r, row in enumerate(grid):
        line = []
        for k, segs in enumerate(row):
            gap = widths[k] - width("".join(t for t, _ in segs))
            left = {"<": 0, ">": gap, "^": gap // 2}[aligns[k]]
            pad = [(" " * (gap - left), frozenset())] if k < n - 1 else []
            line += ([bar] if k else []) + [(" " * left, frozenset())] + segs + pad
        out.append(paint(line, color))
        if r == 0:
            out.append(paint([("─┼─".join("─" * w for w in widths), frozenset({"dim"}))], color))
    return out


def markdown(text, color):
    """Assistant text as terminal lines: headings, lists, quotes, rules, fenced code and tables by line, the rest
    inline. Code blocks get a dim gutter and their language, with nothing inside them read as Markdown."""
    lines, out, i, fence = text.split("\n"), [], 0, None
    dim = frozenset({"dim"})
    while i < len(lines):
        line = lines[i].rstrip()
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                fence = None
            else:
                out.append(paint([("  │ ", dim), (line, frozenset())], color))
        elif m:
            fence = m.group(1)
            out.append(paint([("  ┌ " + m.group(2) if m.group(2) else "  ┌", dim)], color))
        elif "|" in line and i + 1 < len(lines) and "|" in lines[i + 1] and TABLE_RULE.match(lines[i + 1]):
            rows, rule, i = [line], lines[i + 1], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out += table(rows, rule, color)
            continue
        elif m := HEADING.match(line):
            out.append(paint(inline(m.group(1), frozenset({"b", "h"})), color))
        elif RULE.match(line):
            out.append(paint([("─" * 40, dim)], color))
        elif m := BULLET.match(line):
            out.append(paint([("  " + m.group(1) + "• ", frozenset())] + inline(m.group(2)), color))
        elif m := NUMBERED.match(line):
            out.append(paint([("  " + m.group(1) + m.group(2) + " ", frozenset())] + inline(m.group(3)), color))
        elif m := QUOTE.match(line):
            out.append(paint([("▎ ", dim)] + inline(m.group(1), frozenset({"i"})), color))
        else:
            out.append(paint(inline(line), color))
        i += 1
    return out


def clip_rendered(lines, n, color):
    """The first n rendered lines; a clipped message ends with a reset, so no colour can run on."""
    if len(lines) <= n:
        return "\n".join(lines)
    return "\n".join(lines[:n]) + ("\033[0m" if color else "") + f"\n… (+{len(lines) - n} lines)"


def show(ev, out_lines):
    t = ev.get("type")
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                print(c("1", "› ") + clip_rendered(markdown(b["text"].strip(), tty), 12, tty))
            elif b.get("type") == "tool_use":
                i = b.get("input", {})
                arg = i.get("command") or i.get("file_path") or i.get("pattern") or json.dumps(i)[:120]
                print(c("36", f"  {b['name']}: ") + clip(arg, 3))
    elif t == "user" and out_lines:
        for b in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
            if b.get("type") == "tool_result":
                content = b.get("content")
                if isinstance(content, list):
                    content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                if content and str(content).strip():
                    print(c("2", "    " + clip(content, out_lines).replace("\n", "\n    ")))
    elif t == "result":
        print(c("32", f"■ done: {ev.get('num_turns')} turns, {ev.get('duration_ms', 0) / 60000:.0f} min"))


def main(p, n=30, out_lines=6):
    sys.stdout.reconfigure(line_buffering=True)  # piped into less or a file, it still streams
    runs, logf = p.state / "runs", p.state / "autopilot.log"
    current, pos, log_pos = None, 0, logf.stat().st_size if logf.exists() else 0
    first = True
    try:
        while True:
            files = sorted(runs.glob("*.jsonl"), key=lambda f: f.stat().st_mtime) if runs.exists() else []
            if files and files[-1] != current:
                current, pos = files[-1], 0
                print(c("35", f"\n══ {current.name}"))
                if first and n:
                    lines = current.read_text().splitlines()
                    for line in lines[-n:]:
                        try:
                            show(json.loads(line), out_lines)
                        except ValueError:
                            pass
                    pos = current.stat().st_size
                elif first:
                    pos = current.stat().st_size
                first = False
            if current:
                with current.open() as f:
                    f.seek(pos)
                    chunk = f.read()
                    done = chunk.rfind("\n") + 1
                    for line in chunk[:done].splitlines():
                        try:
                            show(json.loads(line), out_lines)
                        except ValueError:
                            pass
                    pos += len(chunk[:done].encode())
            if logf.exists() and logf.stat().st_size > log_pos:
                with logf.open() as f:
                    f.seek(log_pos)
                    for line in f.read().splitlines():
                        print(c("33", f"[driver] {line}"))
                    log_pos = f.tell()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
