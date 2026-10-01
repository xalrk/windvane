"""`windvane watch`: follow the autopilot's sessions live from their streams in `runs/` (what each says, the
commands it runs and their output) and the driver's log. Local only: costs no tokens."""
import json
import sys
import time

tty = sys.stdout.isatty()
c = lambda code, s: f"\033[{code}m{s}\033[0m" if tty else s


def clip(text, n):
    rows = str(text).rstrip("\n").split("\n")
    return "\n".join(rows[:n]) + (f"\n… (+{len(rows) - n} lines)" if len(rows) > n else "")


def show(ev, out_lines):
    t = ev.get("type")
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                print(c("1", "› ") + clip(b["text"].strip(), 12))
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
