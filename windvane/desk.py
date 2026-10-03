"""The user's own PRs from the desk, merged by the autopilot between tickets (forge github).

Merging them by hand while a run is on races its tickets, and their CI has to run somewhere. So an open PR labelled
`desk_label` by the repo's owner is a candidate: between tickets, with no slot busy, the driver tests its merge with
main locally (GitHub's own answer lags), runs the project's check on its exact head, and merges a green one by that
head only. A conflict, a red check or a refused merge gets a short fix session (prompts/FIX.md) that pushes to the
PR's branch, up to `desk_fixes` per PR; the next pass checks the new head. Each head is handled once, and state.json
keeps which, so a restart doesn't check it again.
"""
import json
import re
import time
from datetime import timedelta

from . import net
from .jobs import tail
from .util import git, gout, hhmm, last_match, now, push

FIX_RE = re.compile(r"FIX: (PUSHED|GAVE UP) #(\d+)[ \t]*(.*)")
DESK_EVERY = 120  # seconds between looks at the open PRs while none needs the driver
FIX_HOURS = 1  # one fix session at most


class Desk:
    """The Pilot's desk-PR part: `self.desk` is {PR number: {"heads", "fixes", "fixed", "stuck"}} in state.json;
    `heads` are the heads handled, which the driver doesn't touch again."""

    def desk_init(self, saved):
        self.desk = saved
        self.desk_prs, self.desk_looked, self.desk_owner, self.desk_merged = [], 0, None, []

    def desk_due(self):
        """The labelled PRs whose head isn't handled yet, lowest number first. Lists the open PRs at most every
        DESK_EVERY seconds; what it found stays due in between, so a running slot can't make it be forgotten."""
        p = self.p
        if p.cfg["forge"] != "github" or not p.ap["desk_label"]:
            return []
        if p.ci_dir == p.work_dir and any(self.owner.values()):
            return []  # isolate = false: the checkout holds a ticket's unfinished work
        if time.time() - self.desk_looked >= DESK_EVERY:
            self.desk_looked = time.time()
            self.desk_prs = self.desk_list()
        return [pr for pr in self.desk_prs if pr["headRefOid"] not in self.desk.get(str(pr["number"]), {}).get("heads", [])]

    def desk_done(self, d, *heads):
        """Mark heads handled: the one checked, and the one `gh` listed if it lags behind the fetched branch."""
        d["heads"] = list(dict.fromkeys([*d.get("heads", []), *filter(None, (*heads, d.get("listed")))]))[-10:]
        self.save()

    def desk_list(self):
        p, root = self.p, self.p.main_root()
        if self.desk_owner is None:
            try:
                self.desk_owner = json.loads(net.sh(root, "gh", "repo", "view", "--json", "owner"))["owner"]["login"]
            except (ValueError, KeyError, TypeError):
                return []  # no gh, or not logged in: looked at again after DESK_EVERY
        try:
            prs = json.loads(net.sh(root, "gh", "pr", "list", "--state", "open", "--limit", "100", "--json",
                                "number,title,author,headRefName,headRefOid,isCrossRepository,isDraft,labels"))
        except ValueError:
            return []
        open_ = {str(pr["number"]) for pr in prs}
        for n in [n for n in self.desk if n not in open_]:  # merged or closed: forget it
            del self.desk[n]
        label = p.ap["desk_label"]
        return sorted((pr for pr in prs if (pr.get("author") or {}).get("login") == self.desk_owner
                       and label in {lb.get("name") for lb in pr.get("labels") or []}
                       and not pr.get("isCrossRepository") and not pr.get("isDraft")), key=lambda pr: pr["number"])

    def desk_one(self, pr):
        """Check, merge or fix one desk PR at its current head."""
        p, cd = self.p, self.cd
        n, branch = str(pr["number"]), pr["headRefName"]
        d = self.desk.setdefault(n, {"fixes": 0})
        if d.get("stuck"):  # a head the user pushed after a give-up or the last try: a fresh candidate
            d.update(fixes=0, fixed=False, stuck=False)
        d["listed"] = pr["headRefOid"]
        self.desk_looked = 0  # whatever happens, the next pass looks at the PRs again
        self.fetch()
        head = gout(cd, "rev-parse", "--verify", "-q", f"{p.remote}/{branch}^{{commit}}")
        if not head:
            self.log(f"desk PR #{n}: its branch {branch} isn't on {p.remote}; left alone")
            return self.desk_done(d)
        self.log(f"desk PR #{n}: {pr['title'][:60]} at {head[:7]}")
        files = self.desk_conflicts(cd, head)
        if files is not None and self.desk_renumber(n, branch, head, files):
            files, head = None, gout(cd, "rev-parse", "HEAD")
        if files is not None:
            return self.desk_problem(pr, d, head, f"conflicts with {p.main}",
                                     f"`git merge-tree` finds conflicts in: {', '.join(files) or '(unnamed)'}")
        ok, summary, logf = self.check_at(head)
        if not ok:
            return self.desk_problem(pr, d, head, "check fails", f"{summary}\n```\n{tail(logf, 40)}\n```")
        self.desk_comment(n, f"**windvane check: PASSED** at {head[:7]}: {summary}. Merging.")
        r = net.call(["gh", "pr", "merge", n, "--merge", "--match-head-commit", head], p.main_root())
        if r.returncode:
            now_head = self.desk_head(n)
            if now_head and now_head != head:
                self.log(f"desk PR #{n}: its head moved to {now_head[:7]} during the check; next pass")
                return self.save()
            why = " ".join((r.stderr or r.stdout).split())[:300]
            return self.desk_problem(pr, d, head, "merge refused", f"`gh pr merge` said: {why}")
        self.fetch()
        if git(p.main_root(), "merge-base", "--is-ancestor", head, p.upstream).returncode:
            self.desk_done(d, head)
            return self.desk_leave(n, d, "merge didn't land", f"`gh pr merge` succeeded, but {p.upstream} doesn't "
                                   f"contain {head[:7]}.")
        if net.git(p.main_root(), "push", "-q", f"--force-with-lease={branch}:{head}", p.remote, "--delete",
                   branch).returncode and net.sh(p.main_root(), "git", "ls-remote", "--heads", p.remote, branch):
            self.log(f"desk PR #{n}: kept {p.remote}/{branch}: it moved past the merged head")
        self.desk_done(d, head)
        self.desk_merged.append(n)
        self.log(f"desk PR #{n}: merged {head[:7]}")
        push(f"{'Fixed: d' if d.get('fixed') else 'D'}esk PR #{n} merged", f"{pr['title'][:150]}\n{summary}", 2,
             "white_check_mark")

    def desk_renumber(self, n, branch, head, files):
        """A conflict only in appended entries of `[[append_only]]` files: resolved like a ticket's and pushed to the
        PR's branch, so the PR merges at that head. False when it isn't one, or the push is refused."""
        from .driver import clean_detach, said
        p, cd = self.p, self.cd
        if not files or not set(files) <= {r["file"] for r in p.append_only} or not clean_detach(p, cd, p.upstream):
            return False
        ok, renumbered = self.merge_main(head, f"Merge {branch} into {p.main}")
        if not ok or renumbered is None or net.git(cd, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{head}",
                                                   p.remote, f"HEAD:refs/heads/{branch}").returncode:
            return False
        self.log(f"desk PR #{n}: conflicted with {p.main} only in appended entries: {said(renumbered)}")
        self.desk_comment(n, f"**windvane: resolved the conflict with {p.main}** in appended entries: "
                             f"{said(renumbered)}.")
        return True

    def desk_conflicts(self, cd, head):
        """The files `head` conflicts in when merged with main ([] if git names none), or None for a clean merge
        (or a git too old for `merge-tree --write-tree`: the merge itself says so later)."""
        r = git(cd, "merge-tree", "--write-tree", "--name-only", "--no-messages", self.p.upstream, head)
        if r.returncode != 1:
            return None
        return [f for f in r.stdout.splitlines()[1:] if f.strip()]

    def desk_head(self, n):
        try:
            return json.loads(net.sh(self.p.main_root(), "gh", "pr", "view", n, "--json", "headRefOid"))["headRefOid"]
        except (ValueError, KeyError, TypeError):
            return ""

    def desk_comment(self, n, body):
        net.sh(self.p.main_root(), "gh", "pr", "comment", n, "--body", body)

    def desk_problem(self, pr, d, head, problem, details):
        """Comment the problem, then a fix session while the PR has tries left; else leave it for the desk."""
        n = str(pr["number"])
        self.log(f"desk PR #{n}: {problem} at {head[:7]}")
        self.desk_comment(n, f"**windvane: {problem}** at {head[:7]}\n\n{details[-4000:]}")
        if d["fixes"] >= self.p.ap["desk_fixes"]:
            self.desk_done(d, head)
            return self.desk_leave(n, d, problem, f"Still so after {d['fixes']} fix run{'s' * (d['fixes'] != 1)}.")
        self.desk_fix(pr, d, head, problem, details)

    def desk_leave(self, n, d, problem, why):
        """Stop trying this PR until the user pushes a new head; said once, as nothing runs again until then."""
        d["stuck"] = True
        self.save()
        self.log(f"desk PR #{n}: {problem}; left for the desk ({why})")
        push(f"Desk PR #{n}: {problem}, left for the desk", f"{why}\nPush a new head to have it tried again.", 3,
             "warning")

    def desk_fix(self, pr, d, head, problem, details):
        from .driver import clean_detach, rules, worker_perms
        p, cd = self.p, self.cd
        n, branch = str(pr["number"]), pr["headRefName"]
        clean_detach(p, cd, head)
        d["fixes"] += 1
        limit = now() + timedelta(hours=FIX_HOURS)
        prompt = "\n\n".join(filter(None, [
            f"Desk PR #{n} ({pr['title']}), branch `{branch}`, head {head}: {problem}. Follow the fix rules in your "
            "system prompt and end with the FIX line.",
            f"## What's wrong\n{details[-6000:]}",
            f"Your tree is detached at {head}. Push with `git push {p.remote} HEAD:{branch}`.",
            f"Check command (`windvane check` runs it): `{p.cfg['check']}`." if p.cfg["check"] else "",
            f"Fix {d['fixes']} of {p.ap['desk_fixes']} for this PR. Time limit: {hhmm(limit)}; check with `date`.",
        ]))
        self.log(f"desk PR #{n}: fix run {d['fixes']}/{p.ap['desk_fixes']}")
        allow, deny = worker_perms(p)
        deny = [*deny, "Bash(gh pr edit *)", "Bash(gh pr close *)"]  # the PR is the user's
        while True:
            text, killed, error = self.claude("fix", f"desk-{n}", prompt, rules(p, "FIX.md"), cd, limit, allow,
                                              deny)
            if not error or not self.crashed(f"desk-{n}", error):
                break
        if killed or error:  # cut off by a stop, the deadline or the caps: the try doesn't count; next run again
            d["fixes"] -= 1
            return self.save()
        m = last_match(FIX_RE, text)
        self.fetch()
        after = gout(cd, "rev-parse", "--verify", "-q", f"{p.remote}/{branch}^{{commit}}")
        if m and m.group(1) == "PUSHED" and after and after != head:
            d["fixed"] = True
            self.log(f"desk PR #{n}: fix pushed {after[:7]}: {m.group(3)}")
            self.desk_comment(n, f"**windvane fix** pushed {after[:7]}: {m.group(3)}")
            return self.desk_done(d, head)
        why = (m.group(3) if m and m.group(1) == "GAVE UP" else
               "it said PUSHED, but the branch didn't move" if m else "the fix run ended without a FIX line")
        self.desk_comment(n, f"**windvane fix gave up**: {why}")
        self.desk_done(d, head, after)  # what it pushed before giving up isn't the user's new head
        self.desk_leave(n, d, problem, f"The fix run gave up: {why}")
