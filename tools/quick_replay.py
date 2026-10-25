"""Replays tools/test_pool.py's quick pick over a project's past failed checks, to tune it on evidence: for each
failed check in the state dir's ledger, the ticket's newest commit before the check, its diff against where it left
the main branch then, the test modules as they were in that commit, and the history the pick would have had then
(the reruns log's and check logs' earlier lines). A check counts as caught when the pick holds one of the modules
that failed there for real (`test_pool.real_failures`): the quick pass would have failed it in a minute. Prints a
line a check, then the catch rate, what caught them and the picks' size in module seconds per job.
Standard library only; it reads the state dir and runs git, nothing else.
Usage: quick_replay.py --state DIR [--reruns FILE] [--times FILE] [--jobs K] [--main origin/main] [--repo DIR]
"""
import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_pool  # noqa: E402


def git(repo, *args):
    r = subprocess.run(["git", "-c", "core.quotepath=off", *args], cwd=repo, capture_output=True)
    return r.stdout if r.returncode == 0 else b""


def failed_checks(state):
    """(start time, ticket, log) of every failed check the ledger can tie to a ticket and a log still kept."""
    starts, ends = {}, {}
    for line in (state / "ledger.jsonl").read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "check-start" and row.get("log"):
            starts[row.get("start")] = row
        elif row.get("event") == "check" and "start" in row and not row.get("reused"):
            ends[row["start"]] = row
    out = []
    for start, row in sorted(starts.items()):
        end = ends.get(start)
        log = state / "checks" / row["log"]
        if end and end.get("passed") is False and not end.get("not_run") and log.exists():
            out.append((start, row["ticket"], log))
    return out


def ticket_commits(repo, main):
    """ticket -> [(commit time, sha)] of its own commits, oldest first: those whose subject names it (`T-123: ...`),
    those its merge into `main` brought (`Merge T-123: ...`), and those of a branch named after it not merged yet."""
    out = {}
    def add(ticket, lines):
        for line in lines:
            ct, _, sha = line.partition(" ")
            if sha:
                out.setdefault(ticket, set()).add((int(ct), sha))
    def ticket_of(text):
        word = text.split(":", 1)[0].split(" ")[-1]
        return word.upper() if re.fullmatch(r"[A-Za-z]+-\d+", word) else None
    for line in git(repo, "log", "--all", "--no-merges", "--format=%ct %H %s").decode(errors="replace").splitlines():
        ct, sha, subject = (line.split(" ", 2) + [""])[:3]
        if t := ticket_of(subject):
            add(t, [f"{ct} {sha}"])
    for line in git(repo, "log", "--first-parent", "--merges", "--format=%H %s", main).decode(errors="replace").splitlines():
        sha, _, subject = line.partition(" ")
        if subject.startswith("Merge ") and (t := ticket_of(subject)):
            add(t, git(repo, "log", "--no-merges", "--format=%ct %H", f"{sha}^1..{sha}^2").decode().splitlines())
    for ref in git(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/origin/").decode().splitlines():
        m = re.match(r"([a-z]+-\d+)-", ref.rsplit("/", 1)[-1])
        if m:
            add(m.group(1).upper(), git(repo, "log", "--no-merges", "--format=%ct %H", f"{main}..{ref}").decode().splitlines())
    return {t: sorted(v) for t, v in out.items()}


def tests_at(repo, sha, folder, dest):
    """The test folder as it was in `sha`, written under `dest`."""
    data = git(repo, "archive", "--format=tar", sha, folder)
    with tarfile.open(fileobj=BytesIO(data)) as tar:
        tar.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tar.extractall(dest)
    return Path(dest) / folder


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--reruns", type=Path)
    p.add_argument("--times", type=Path)
    p.add_argument("--jobs", type=int, default=16)
    p.add_argument("--main", default="origin/main")
    p.add_argument("--repo", type=Path, default=Path("."))
    p.add_argument("--tests", default="tests")
    a = p.parse_args(argv)
    times = json.loads(a.times.read_text()) if a.times else {}
    commits = ticket_commits(a.repo, a.main)
    logs = sorted((a.state / "checks").glob("*.log"), key=lambda f: f.name)
    caught, total, sizes, by = 0, 0, [], {}
    for start, ticket, log in failed_checks(a.state):
        real = test_pool.real_failures(log.read_text(errors="replace"))
        mine = [sha for ct, sha in commits.get(ticket, []) if ct <= start]
        if not real or not mine:
            continue
        sha = mine[-1]
        main_then = git(a.repo, "rev-list", "-1", "--first-parent", f"--before={int(start)}", a.main).decode().strip()
        base = git(a.repo, "merge-base", sha, main_then).decode().strip() if main_then else ""
        if not base:
            continue
        changed = git(a.repo, "diff", "--name-only", base, sha).decode(errors="replace").splitlines()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
        earlier = [f for f in logs if f.name[:15] < stamp]
        failures = test_pool.history(a.reruns, earlier, before=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(start)))
        with tempfile.TemporaryDirectory() as tmp:
            tests = tests_at(a.repo, sha, a.tests, tmp)
            chosen = test_pool.pick(tests, changed, failures, times, a.jobs * test_pool.QUICK_WALL)
        picked = dict(chosen)
        hit = sorted(real & set(picked))
        total += 1
        caught += bool(hit)
        size = sum(times.get(m, 10) for m in picked) / a.jobs
        sizes.append(size)
        for m in hit[:1]:
            why = picked[m].split(" ", 1)[0] if not picked[m].startswith("named") else "named"
            by[why] = by.get(why, 0) + 1
        print(f"{ticket} {log.name[:15]} {'caught' if hit else 'MISSED'} real={','.join(sorted(real))} "
              f"by={hit[0] + ' (' + picked[hit[0]] + ')' if hit else '-'} picked={len(picked)} ~{size:.0f}s/job "
              f"changed={len(changed)}")
    if total:
        sizes.sort()
        print(f"caught {caught} of {total} failed checks ({100 * caught / total:.0f}%); first catch by: "
              + ", ".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1]))
              + f"; picks' module time per job: median {sizes[len(sizes) // 2]:.0f}s, most {sizes[-1]:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
