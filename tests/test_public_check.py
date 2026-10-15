"""`windvane public-check`: what a repository would expose if it went public. A throwaway repo gets seeded leaks in
its history, a fake `gh` stands in for GitHub's pull requests and settings, and the output names kinds and places,
never the text.

The leaks are built from pieces at run time, so this file itself stays clean for the working-tree scan."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli  # noqa: E402
from helpers import Sandbox  # noqa: E402

TOKEN = "ghp_" + "Kx4" * 12
OTHER = "AKIA" + "QZ7" * 5 + "X"
EMAIL = "someone@" + "fastmail.com"
NOREPLY = "1+bot@users." + "noreply.github.com"
# GitHub as gh shows it, from a JSON state file; "fail" makes every call fail as gh does offline.
FAKE_GH = r'''import json, sys
STATE = sys.argv.pop(1)
a = sys.argv[1:]
st = json.load(open(STATE))
if st.get("fail"):
    sys.exit("error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com")
if a[:2] == ["pr", "list"]:
    print(json.dumps(st.get("prs", [])))
elif a[:3] == ["api", "--paginate", "repos/{owner}/{repo}/pulls/comments"]:
    for c in st.get("review_comments", []):
        print(json.dumps([str(c["pr"]), c["body"]]))
elif a[:2] == ["api", "repos/{owner}/{repo}"]:
    print(json.dumps(st.get("repo", {"full_name": "me/proj"})))
elif a[:1] == ["api"] and "/actions/" in a[1]:
    print(st.get(a[1].split("/actions/")[1].split("?")[0], 0))
elif a[:2] == ["api", "user/emails"]:
    if "emails" not in st:
        sys.exit("HTTP 404: Not Found (https://api.github.com/user/emails)")
    print(json.dumps(st["emails"]))
'''
SECURE = {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}


class PublicCheck(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())  # `-C` moves into the sandbox, which goes
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.gh = self.s.tmp / "gh.json"
        self.state({"repo": {"full_name": "me/proj", "security_and_analysis": SECURE},
                    "emails": [{"email": NOREPLY, "primary": True, "visibility": "private"}]})
        fake = self.s.bin / "gh"
        fake.write_text(f"#!/bin/sh\nexec python3 - {self.gh} \"$@\" <<'EOF'\n{FAKE_GH}EOF\n")
        fake.chmod(0o755)

    def state(self, st):
        self.gh.write_text(json.dumps(st))

    def edit(self, **change):
        st = json.loads(self.gh.read_text())
        st.update(change)
        self.state(st)

    def commit(self, msg, **env):
        self.s.git("add", "-A")
        subprocess.run(["git", "commit", "-qm", msg], cwd=self.s.root, check=True, env={**os.environ, **env})
        return self.s.git("rev-parse", "--short", "HEAD")

    def check(self, *args, cmd=("public-check",)):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main(["-C", str(self.s.root), *cmd, *args])
        return code, out.getvalue()

    def test_a_clean_repo_exits_0_and_says_what_it_read(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("History: 1 commit(s) on every branch and tag", out)
        self.assertIn("ok       nothing found", out)
        self.assertEqual(out.count("--github"), 1, out)
        self.assertIn("windvane public-check: what proj would expose", out)
        self.assertNotIn(str(self.s.tmp), out)  # a path on this machine is a leak of its own
        self.assertNotIn("What to do next", out)
        code, out = self.check("--github")
        self.assertEqual(code, 0, out)
        for ok in ("secret scanning is on", "push protection is on", "no Actions runs", "no Actions artifacts",
                   "your account keeps its email private"):
            self.assertIn(f"ok       {ok}", out)

    def test_a_token_deleted_three_commits_later_and_a_removed_env_file_are_found_with_their_commits(self):
        self.s.write("src/app.cfg", f"key = {TOKEN}\n")
        self.s.write(".env", "PASSWORD=hunter2\n")
        added = self.commit("add config")
        for i in range(2):
            self.s.write("notes.txt", f"note {i}\n")
            self.commit(f"note {i}")
        self.s.write("src/app.cfg", "key = from the environment\n")
        (self.s.root / ".env").unlink()
        self.commit("take the key out")
        self.s.git("switch", "-q", "-c", "side")  # another branch: --all reads it too
        self.s.write("side.txt", f"aws {OTHER}\n")
        side = self.commit("side work")
        self.s.git("switch", "-q", "main")
        self.assertNotIn(TOKEN, (self.s.root / "src/app.cfg").read_text())
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(f"a token: 2 place(s): side.txt:1 in {side}, src/app.cfg:1 in {added}", out)
        self.assertIn(f"a file named like a secret: 1 place(s): .env in {added}", out)
        self.assertIn("1. Rotate first", out)
        self.assertIn("git filter-repo --sensitive-data-removal", out)
        self.assertIn("windvane never rewrites history itself", out)
        for secret in (TOKEN, OTHER, "hunter2"):
            self.assertNotIn(secret, out)

    def test_a_personal_author_email_is_found_and_a_noreply_one_isnt(self):
        self.s.write("a.txt", "a\n")
        self.commit("by a bot", GIT_AUTHOR_EMAIL=NOREPLY, GIT_COMMITTER_EMAIL=NOREPLY)
        self.assertEqual(self.check()[0], 0)
        self.s.write("b.txt", "b\n")
        sha = self.commit("by a person", GIT_AUTHOR_EMAIL=EMAIL)
        self.s.write("c.txt", "c\n")
        self.commit(f"mail {EMAIL.split('@')[0]} at the address in the log")  # no address: not an email
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(f"a personal email as commit author or committer: 1 commit(s): {sha}\n", out)
        self.assertNotIn(EMAIL, out)

    def test_a_file_name_git_quotes_is_named_as_it_is(self):
        self.s.write('my "notes"\tdraft.txt', f"{TOKEN}\n")
        sha = self.commit("notes")
        self.assertIn(f'a token: 1 place(s): my "notes"\tdraft.txt:1 in {sha}', self.check()[1])

    def test_a_secret_in_a_commit_message_is_found(self):
        self.s.write("a.txt", "a\n")
        sha = self.commit(f"deploy\n\nused {TOKEN} for now")
        code, out = self.check()
        self.assertIn(f"a token: 1 place(s): message of {sha}", out)
        self.assertNotIn(TOKEN, out)

    def test_a_secret_in_a_pr_comment_is_found_with_github_and_not_without(self):
        self.edit(prs=[{"number": 7, "title": "Add login", "body": "Adds it.",
                        "comments": [{"body": f"works with\n{TOKEN}"}], "reviews": [{"body": "LGTM"}]},
                       {"number": 8, "title": "Docs", "body": f"by {EMAIL}", "comments": [], "reviews": []}],
                  review_comments=[{"pr": 8, "body": f"try {OTHER}"}])
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertNotIn("PR #", out)
        code, out = self.check("--github")
        self.assertEqual(code, 1, out)
        self.assertIn("a token: 2 place(s): PR #7 comment, PR #8 review comment", out)
        self.assertIn("a personal email address: 1 place(s): PR #8 body", out)
        for secret in (TOKEN, OTHER, EMAIL):
            self.assertNotIn(secret, out)

    def test_a_pull_request_target_workflow_and_disabled_push_protection_are_reported_with_their_fix(self):
        self.s.write(".github/workflows/label.yml", "on:\n  pull_request_target:\njobs: {}\n")
        self.s.write(".github/workflows/test.yml",
                     "on: [pull_request]\njobs:\n  t:\n    env:\n      K: ${{ secrets.NPM_TOKEN }}\n")
        self.s.write(".github/workflows/ok.yml",
                     "on: [pull_request]  # pull_request_target in a comment\njobs:\n  t:\n"
                     "    env:\n      T: ${{ secrets.GITHUB_TOKEN }}\n")
        self.commit("workflows")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("WARNING  .github/workflows/label.yml runs on pull_request_target: a fork's PR runs it with this "
                      "repo's secrets and write access  -> use `pull_request` instead", out)
        self.assertIn("WARNING  .github/workflows/test.yml runs on pull_request and uses secrets  -> fork PRs get no "
                      "secrets", out)
        self.assertNotIn("ok.yml", out)
        self.edit(repo={"full_name": "me/proj", "security_and_analysis": {
            "secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "disabled"}}},
                  runs=12, artifacts=3)
        code, out = self.check("--github")
        self.assertIn("WARNING  push protection is off  -> turn it on at "
                      "https://github.com/me/proj/settings/security_analysis", out)
        self.assertIn("WARNING  12 Actions runs (with their logs) turn public with the repo  -> read them", out)
        self.assertIn("WARNING  3 Actions artifacts turn public", out)

    def test_settings_gh_cant_read_are_skipped_with_where_to_look_and_settings_warn_without_failing(self):
        self.edit(repo={"full_name": "me/proj"}, runs=4)
        del_emails = json.loads(self.gh.read_text())
        del_emails.pop("emails")
        self.state(del_emails)
        code, out = self.check("--github")
        self.assertEqual(code, 0, out)  # what can't be read is no finding, nor what GitHub's settings warn of
        self.assertIn("WARNING  4 Actions runs (with their logs) turn public with the repo", out)
        self.assertIn("SKIPPED  couldn't see secret scanning and push protection", out)
        self.assertIn("SKIPPED  couldn't read whether your account blocks pushes that expose its email  -> check "
                      "\"Block command line pushes that expose my email\" at https://github.com/settings/emails", out)

    def test_gh_offline_says_couldnt_check_and_exits_2(self):
        self.edit(fail=True)
        code, out = self.check("--github")
        self.assertEqual(code, 2, out)
        self.assertIn("couldn't check them (gh failed: error connecting to api.github.com)",
                      out)
        self.assertIn("SKIPPED  couldn't check the repository's settings (gh failed:", out)
        self.assertIn("The scan didn't finish", out)

    def test_exit_2_outside_a_repo_or_when_git_cant_read_its_history(self):
        elsewhere = self.s.tmp / "not-a-repo"
        elsewhere.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(cli.main(["-C", str(elsewhere), "public-check"]), 2)
        self.assertIn("run it in a git repository", out.getvalue())
        head = self.s.git("rev-parse", "HEAD")
        obj = self.s.root / ".git/objects" / head[:2] / head[2:]
        obj.chmod(0o644)
        obj.write_bytes(b"broken")
        code, out = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("History: couldn't scan it: git log failed:", out)

    def test_doctor_leaks_is_public_check_without_github(self):
        self.s.write("a.txt", f"{TOKEN}\n")
        self.commit("oops")
        self.assertEqual(self.check(cmd=("doctor", "--leaks")), self.check())


if __name__ == "__main__":
    unittest.main()
