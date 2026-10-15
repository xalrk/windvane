"""`windvane public-check`: what a repository would expose if it went public, before it does.

Making a repo public publishes its whole history, not only its tree: every line any commit on any branch or tag
added, files deleted long ago, commit messages and the emails commits carry, and with them on GitHub its pull
requests, their comments and reviews, and its Actions logs and artifacts. A rewrite afterwards doesn't reach forks,
clones or GitHub's cached `refs/pull/` refs, so a leaked secret is rotated first and removed second.

This reads and changes nothing. History is read in one `git log -p --all` pass. Like `privacy`, it reports a kind and
a place (commit, PR number, `file:line`) and never the matched text, so its output is safe to paste anywhere."""
import io
import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import privacy
from .doctor import run_quiet

NAMES = {**privacy.NAMES, "identity": "a personal email as commit author or committer"}
MOST = 20  # places listed per kind; the rest are counted
REMOVE_DOCS = ("https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/"
               "removing-sensitive-data-from-a-repository")
# A workflow file's trigger names and the secrets it uses (GITHUB_TOKEN is GitHub's own, read-only for a fork's PR).
PR_TARGET = re.compile(r"\bpull_request_target\b")
PR = re.compile(r"\bpull_request\b(?!_)")
SECRETS = re.compile(r"\$\{\{\s*secrets\.(?!GITHUB_TOKEN\b)\w+", re.I)


class ScanFailed(Exception):
    """A part of the scan couldn't run: the result would read as clean when it isn't."""


def history(root):
    """(findings, commits) for every commit on every ref, in one `git log -p` pass: [(kind, place)] for the lines each
    adds (by its diff to its first parent, as `privacy.added` reads it), the files it adds named like secrets, its
    message, and its author's and committer's emails. A line added on two branches is listed once."""
    cmd = ["git", "-c", "core.quotepath=off", "log", "--all", "-p", "-U0", "--no-color", "--no-ext-diff",
           "--no-textconv", "--no-renames", "--diff-merges=first-parent", "--format=%x01%h%x00%ae%x00%ce%n%B%x02"]
    try:  # streamed: a big repo's whole history needn't fit in memory; stderr to a file, so it can't fill a pipe
        with tempfile.TemporaryFile() as errf, \
                subprocess.Popen(cmd, cwd=root, stdout=subprocess.PIPE, stderr=errf) as p, \
                io.TextIOWrapper(p.stdout, encoding="utf-8", errors="replace", newline="\n") as out:
            found, commits = parse(out)
            p.wait()
            errf.seek(0)
            err = errf.read().decode("utf-8", "replace").strip()
    except OSError as e:
        raise ScanFailed(f"git couldn't run: {e}")
    if p.returncode and "does not have any commits" not in err:
        raise ScanFailed(f"git log failed: {(err.splitlines() or ['no error given'])[0]}")
    return found, commits


def parse(lines):
    """`history`'s reading of `git log -p` output, line by line."""
    r = privacy.rules(tuple(privacy.NAMES))
    found, seen, commits = [], set(), 0
    sha, name, n, head, message = "", "", 0, False, False

    def line_kinds(text, place):
        for k in sorted(privacy.kinds_in(text, r)):
            found.append((k, place))

    for line in lines:
        line = line.rstrip("\n")
        if line.startswith("\x01"):
            sha, author, committer = (line[1:].split("\x00") + ["", ""])[:3]
            commits, message, name, head = commits + 1, True, "", False
            if privacy.personal_email(author) or privacy.personal_email(committer):
                found.append(("identity", sha))
        elif message:
            text, message = (line[:-1], False) if line.endswith("\x02") else (line, True)
            line_kinds(text, f"message of {sha}")
        elif line.startswith("diff --git "):
            rest = line[len("diff --git "):]  # "a/NAME b/NAME": the same name twice, renames being off
            name, head = diff_name(rest), True
        elif head and line.startswith("new file mode") and privacy.secret_file(name):
            found.append(("secret-file", f"{name} in {sha}"))
        elif head and line.startswith(("+++ ", "--- ")):
            continue
        elif line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", line)
            n, head = int(m.group(1)) if m else 0, False
        elif line.startswith("+") and name and not head:
            if hash((name, line)) not in seen:  # a hash, not the line: a big history's lines needn't all fit
                seen.add(hash((name, line)))
                line_kinds(line[1:], f"{name}:{n} in {sha}")
            n += 1
    return list(dict.fromkeys(found)), commits  # a message's two lines of one kind are one finding


def diff_name(rest):
    """The file a `diff --git a/NAME b/NAME` header names (renames are off, so both are the same), unquoting the C-style
    quotes git puts around a name with a quote mark, backslash, tab or newline in it."""
    if not rest.startswith('"'):
        return rest[2:2 + (len(rest) - 5) // 2]
    m = re.match(r'"((?:[^"\\]|\\.)*)"', rest)
    raw = m.group(1) if m else rest
    escapes = {"t": "\t", "n": "\n", '"': '"', "\\": "\\"}
    raw = re.sub(r'\\([0-7]{3}|.)', lambda e: chr(int(e.group(1), 8)) if len(e.group(1)) == 3
                 else escapes.get(e.group(1), e.group(1)), raw)
    return raw[2:]


def gh(root, *args, timeout=60):
    """gh's stdout, or ScanFailed with a short reason (its error's first line, censored) when it can't answer."""
    r = run_quiet(["gh", *args], timeout=timeout, cwd=root)
    if r is None:
        raise ScanFailed("gh is missing or timed out")
    if r.returncode and "no git remotes found" in r.stderr:
        raise ScanFailed("this repository has no GitHub remote")
    if r.returncode:
        first = (r.stderr.strip().splitlines() or ["no error given"])[0]
        raise ScanFailed(f"gh failed: {privacy.censor(first)[:120]}")
    return r.stdout


def why(e):
    """A failure in a few words: gh's own, or that its answer didn't read as expected (a ValueError's text is Python's)."""
    return str(e) if isinstance(e, ScanFailed) else "gh's answer couldn't be read"


def pull_requests(root):
    """[(kind, place)] for the titles, bodies, comments and reviews of every pull request, open or closed, and the
    review comments on their lines."""
    r = privacy.rules(tuple(privacy.NAMES))
    prs = json.loads(gh(root, "pr", "list", "--state", "all", "--limit", "100000", "--json",
                        "number,title,body,comments,reviews", timeout=300) or "[]")
    texts = []
    for pr in prs:
        n = pr.get("number")
        texts += [(n, "title", pr.get("title")), (n, "body", pr.get("body"))]
        texts += [(n, "comment", c.get("body")) for c in pr.get("comments") or []]
        texts += [(n, "review", c.get("body")) for c in pr.get("reviews") or []]
    out = gh(root, "api", "--paginate", "repos/{owner}/{repo}/pulls/comments", "-q",
             '.[] | [(.pull_request_url | split("/") | last), .body] | @json', timeout=300)
    for line in out.splitlines():
        if line.strip():
            n, body = json.loads(line)
            texts.append((n, "review comment", body))
    found = set()
    for n, what, text in texts:
        for line in (text or "").splitlines():
            found |= {(k, int(n), what) for k in privacy.kinds_in(line, r)}
    return [(k, f"PR #{n} {what}") for k, n, what in sorted(found)]


def workflows(root):
    """(level, what, fix) for each workflow that a fork's pull request can reach with secrets, as the default branch
    (`origin/HEAD`, else HEAD) has it: GitHub runs `pull_request_target` from there."""
    rev = "origin/HEAD" if subprocess.run(["git", "rev-parse", "-q", "--verify", "origin/HEAD^{commit}"], cwd=root,
                                          capture_output=True).returncode == 0 else "HEAD"
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", rev, "--", ".github/workflows"], cwd=root,
                           capture_output=True, text=True).stdout.split("\n")
    out = []
    for f in [x for x in names if x.endswith((".yml", ".yaml"))]:
        text = subprocess.run(["git", "show", f"{rev}:{f}"], cwd=root, capture_output=True,
                              text=True, errors="replace").stdout
        text = "\n".join(line.split(" #")[0] for line in text.splitlines() if not line.lstrip().startswith("#"))
        if PR_TARGET.search(text):
            out.append(("warn", f"{f} runs on pull_request_target: a fork's PR runs it with this repo's secrets and "
                        "write access", "use `pull_request` instead, or never check out or run the PR's code in it"))
        elif PR.search(text) and SECRETS.search(text):
            out.append(("warn", f"{f} runs on pull_request and uses secrets",
                        "fork PRs get no secrets, so keep the secret steps to `push` or `workflow_dispatch` runs "
                        "(and don't switch to pull_request_target)"))
    return out or [("ok", "no workflow gives a fork's pull request secrets", "")]


def settings(root):
    """(level, what, fix) for the GitHub settings that matter once the repo is public; "skip" when gh can't tell."""
    try:
        repo = json.loads(gh(root, "api", "repos/{owner}/{repo}"))
    except (ScanFailed, ValueError) as e:
        return [("skip", f"couldn't check the repository's settings ({why(e)})", "")]
    slug = repo.get("full_name", "")
    out, sa = [], repo.get("security_and_analysis")
    where = f"https://github.com/{slug}/settings/security_analysis"
    if not sa:
        out.append(("skip", "couldn't see secret scanning and push protection: GitHub shows them to the repo's admins, "
                     "and on a private repo only with Advanced Security", f"check them at {where} once it's public"))
    for key, what in (("secret_scanning", "secret scanning"), ("secret_scanning_push_protection", "push protection")):
        if sa:
            on = (sa.get(key) or {}).get("status") == "enabled"
            out.append(("ok", f"{what} is on", "") if on else
                       ("warn", f"{what} is off", f"turn it on at {where} (free once the repo is public)"))
    for kind, path in (("Actions runs (with their logs)", "runs"), ("Actions artifacts", "artifacts")):
        try:
            count = int(gh(root, "api", f"repos/{{owner}}/{{repo}}/actions/{path}?per_page=1", "-q",
                           ".total_count").strip() or 0)
        except (ScanFailed, ValueError) as e:
            out.append(("skip", f"couldn't count the {kind} ({why(e)})", ""))
            continue
        out.append(("ok", f"no {kind}", "") if not count else
                   ("warn", f"{count} {kind} turn public with the repo",
                    "read them for secrets and delete what shouldn't be public "
                    + ("(`gh run delete <id>`)" if path == "runs" else
                       "(`gh api -X DELETE repos/{owner}/{repo}/actions/artifacts/<id>`)")))
    out.append(email_setting(root))
    return out


def email_setting(root):
    """Whether the account keeps its email private: GitHub's "Block command line pushes that expose my email" can't
    be read through its API, so this points to it either way."""
    page = "https://github.com/settings/emails"
    try:
        emails = json.loads(gh(root, "api", "user/emails"))
        primary = next(e for e in emails if e.get("primary"))
    except (ScanFailed, ValueError, StopIteration, AttributeError):
        return ("skip", "couldn't read whether your account blocks pushes that expose its email",
                f"check \"Block command line pushes that expose my email\" at {page}")
    if primary.get("visibility") == "private":
        return ("ok", "your account keeps its email private", f"make sure pushes that expose it are blocked: {page}")
    return ("warn", "your account's email is not private",
            f"turn on \"Keep my email addresses private\" and \"Block command line pushes that expose my email\" at "
            f"{page}")


def grouped(found):
    """Lines saying each kind's places, MOST at most."""
    by = {}
    for kind, where in found:
        by.setdefault(kind, []).append(where)
    lines = []
    for kind, places in sorted(by.items()):
        more = f" (+{len(places) - MOST} more)" if len(places) > MOST else ""
        what = "commit(s)" if kind == "identity" else "place(s)"
        lines.append(f"  FOUND    {NAMES.get(kind, kind)}: {len(places)} {what}: {', '.join(places[:MOST])}{more}")
    return lines


NEXT = f"""What to do next, in this order:
  1. Rotate first: revoke and replace every token, key or password found, wherever it's used. Removing it from
     history doesn't reach forks, clones or GitHub's cached pull request refs, so a rotated secret is the only safe
     one.
  2. Then remove it from history with git filter-repo (https://github.com/newren/git-filter-repo):
     `git filter-repo --sensitive-data-removal --replace-text <file of the strings>` for lines, `--invert-paths
     --path <file>` for whole files, `--mailmap <file>` for emails; then force-push every branch and tag.
     windvane never rewrites history itself.
  3. Only if a secret can't be rotated, ask GitHub Support to remove the cached pull request refs and views:
     {REMOVE_DOCS}"""


def main(root, github=False):
    """Print the check; 0 when clean, 1 on a leak or a workflow to fix, 2 when the history or (with `github`) the PR
    scan failed. GitHub's settings warn without changing it: a private repo can't turn push protection on, and runs
    already read stay counted."""
    print(f"windvane public-check: what {Path(root).name} would expose if its repository went public")
    failed, found, leaks = False, 0, 0
    try:
        hist, commits = history(root)
        print(f"History: {commits} commit(s) on every branch and tag, deleted files included")
        print("\n".join(grouped(hist)) or "  ok       nothing found")
        leaks += len(hist)
    except ScanFailed as e:
        print(f"History: couldn't scan it: {e}")
        failed = True
    rows = workflows(root)
    found += sum(level == "warn" for level, _, _ in rows)
    if github:
        print("Pull requests: titles, bodies, comments and reviews")
        try:
            prs = pull_requests(root)
            print("\n".join(grouped(prs)) or "  ok       nothing found")
            leaks += len(prs)
        except (ScanFailed, ValueError) as e:
            print(f"  couldn't check them ({why(e)}); run it again once gh can reach the repository")
            failed = True
        rows += settings(root)
    print("Repository settings" + ("" if github else " (only the workflow files)"))
    label = {"ok": "ok      ", "warn": "WARNING ", "skip": "SKIPPED "}
    for level, what, fix in rows:
        print(f"  {label[level]} {what}" + (f"  -> {fix}" if fix else ""))
    if not github:
        print("Run it with --github to check the pull requests and the repository's settings on GitHub too.")
    if leaks:  # a workflow to fix needs no rotating or rewriting: its row says what to do
        print(NEXT)
    if failed:
        print("The scan didn't finish: the result above is incomplete.")
        return 2
    return 1 if found or leaks else 0
