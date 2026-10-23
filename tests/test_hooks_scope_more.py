"""A session's hooks where test_hooks_scope doesn't look: the checkout itself with isolate off, two slots building
theirs at once, and the submodules in a worktree, whose pushes are scanned against their own remote. All through
test_actions_budget's fake `gh`, never the real one."""
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.worktree import ensure_worktree, guard_pushes, hooks_folder, submodule_folder  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_actions_budget import FAKE_GH  # noqa: E402

TOKEN = "ghp_" + "Wq4" * 12


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(actions.asked.clear)
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")

    def configure(self, line):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(line + "\n" + cfg.read_text())
        return Project(self.s.root)

    def git(self, env, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)

    def commit(self, env, repo, name, text):
        Path(repo, name).write_text(text)
        self.git(env, repo, "add", name)
        r = self.git(env, repo, "commit", "-qm", name)
        self.assertEqual(r.returncode, 0, r.stderr)

    def own_hook(self, hooks, name):
        """A hook in `hooks` that logs its name and runs, for proof the repo's own hooks still run."""
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> {self.s.tmp}/own.log\n")
        hook.chmod(0o755)

    def ran(self):
        log = self.s.tmp / "own.log"
        return log.read_text().split() if log.exists() else []


class IsolateOff(Base):
    def test_the_session_env_gives_the_checkout_windvanes_hooks_and_plain_git_there_keeps_its_own(self):
        p = self.configure("isolate = false")
        self.assertEqual(p.work_dir, p.main_root())
        self.own_hook(self.s.root / ".git/hooks", "pre-push")
        config = (self.s.root / ".git/config").read_text()
        env = runner.env(guard_pushes(p, p.work_dir), p)
        self.assertEqual(self.git(env, self.s.root, "config", "core.hooksPath").stdout.strip(),
                         str(hooks_folder(p, p.work_dir)))
        self.assertEqual(self.git(None, self.s.root, "config", "core.hooksPath").stdout, "")
        self.commit(env, self.s.root, "b.txt", TOKEN + "\n")
        r = self.git(env, self.s.root, "push", "-q", "origin", "HEAD:refs/heads/t-001-work")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)
        self.assertEqual(self.ran(), [])  # refused before the checkout's own pre-push
        r = self.git(None, self.s.root, "push", "-q", "origin", "HEAD:refs/heads/desk")  # the user's own push
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ran(), ["pre-push"])
        self.assertEqual((self.s.root / ".git/config").read_text(), config)


class TwoSlots(Base):
    def test_two_slots_building_their_hooks_at_once_each_end_with_a_working_pre_push(self):
        p = Project(self.s.root)
        slots = [self.s.tmp / "work-1", self.s.tmp / "work-2"]
        for wt in slots:
            ensure_worktree(p, wt, setup=False)
        envs, errors, start = {}, [], threading.Barrier(len(slots))

        def build(wt):
            try:
                for _ in range(5):  # each rebuild removes and remakes the folder: overlap them
                    start.wait()
                    envs[wt] = guard_pushes(p, wt)
            except Exception as e:  # noqa: BLE001 - reported below, in the test's own thread
                errors.append(e)

        threads = [threading.Thread(target=build, args=(wt,)) for wt in slots]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        for i, wt in enumerate(slots):
            env = runner.env(envs[wt], p)
            self.assertEqual(self.git(env, wt, "config", "core.hooksPath").stdout.strip(), str(hooks_folder(p, wt)))
            self.commit(env, wt, f"s{i}.txt", TOKEN + "\n")
            r = self.git(env, wt, "push", "-q", "origin", f"HEAD:refs/heads/t-00{i}-work")
            self.assertNotEqual(r.returncode, 0, wt.name)
            self.assertIn(f"a token at s{i}.txt:1", r.stderr)
        self.assertNotEqual(hooks_folder(p, slots[0]), hooks_folder(p, slots[1]))


class Submodule(Base):
    """A submodule added in a slot worktree. Its remote has a clean `main` and a branch `old` with a token on it."""

    def setUp(self):
        super().setUp()
        self.sub_remote = self.s.tmp / "sub.git"
        seed = self.s.tmp / "seed"
        self.git(None, self.s.tmp, "init", "-q", "--bare", "-b", "main", str(self.sub_remote))
        self.git(None, self.s.tmp, "init", "-q", "-b", "main", str(seed))
        self.commit(None, seed, "lib.txt", "lib\n")
        self.git(None, seed, "push", "-q", str(self.sub_remote), "main")
        self.commit(None, seed, "old.txt", TOKEN + "\n")  # public already: not this session's to answer for
        self.git(None, seed, "push", "-q", str(self.sub_remote), "HEAD:refs/heads/old")

    def session(self, budget=False):
        self.p = self.configure("actions_minutes = 100") if budget else Project(self.s.root)
        self.wt = self.s.tmp / "work-1"
        ensure_worktree(self.p, self.wt, setup=False)
        push_env = guard_pushes(self.p, self.wt)
        git_env, _ = actions.worker_git(self.p, self.wt)
        self.assertEqual(bool(git_env), budget)
        env = runner.env({**push_env, **git_env}, self.p)
        r = self.git(env, self.wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(self.sub_remote),
                     "sub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.sub = self.wt / "sub"
        gitdir = Path(self.git(env, self.sub, "rev-parse", "--absolute-git-dir").stdout.strip())
        self.assertEqual(gitdir.parent.name, "modules")  # under the worktree's own git dir
        return env, gitdir

    def test_a_submodules_push_is_scanned_against_its_own_remote_and_its_own_hooks_still_run(self):
        env, gitdir = self.session()
        self.assertEqual(self.git(env, self.sub, "config", "core.hooksPath").stdout.strip(),
                         str(submodule_folder(self.p, self.wt)))
        self.assertEqual(self.git(None, self.sub, "config", "core.hooksPath").stdout, "")  # outside the session
        self.assertEqual(self.git(env, self.wt, "config", "core.hooksPath").stdout.strip(),
                         str(hooks_folder(self.p, self.wt)))
        for name in ("pre-commit", "pre-push"):
            self.own_hook(gitdir / "hooks", name)
        self.git(env, self.sub, "checkout", "-q", "-b", "topic", "origin/old")
        self.commit(env, self.sub, "new.txt", "clean\n")
        r = self.git(env, self.sub, "push", "-q", "origin", "topic")
        self.assertEqual(r.returncode, 0, r.stderr)  # the token its remote already has doesn't count
        self.assertEqual(self.ran(), ["pre-commit", "pre-push"])
        self.commit(env, self.sub, "b.txt", TOKEN + "\n")
        r = self.git(env, self.sub, "push", "-q", "origin", "topic")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)
        self.assertNotIn("old.txt", r.stderr)
        self.assertEqual(self.ran(), ["pre-commit", "pre-push", "pre-commit"])  # refused before its own pre-push

    def test_a_push_to_its_remotes_url_counts_what_that_remote_has_and_one_to_a_new_remote_counts_all_it_sends(self):
        env, _ = self.session()
        self.git(env, self.sub, "checkout", "-q", "-b", "topic", "origin/old")
        self.commit(env, self.sub, "new.txt", "clean\n")
        r = self.git(env, self.sub, "push", "-q", str(self.sub_remote), "topic")  # by URL, not by name
        self.assertEqual(r.returncode, 0, r.stderr)
        mirror = self.s.tmp / "mirror.git"
        self.git(None, self.s.tmp, "init", "-q", "--bare", str(mirror))
        self.git(env, self.sub, "remote", "add", "mirror", str(mirror))  # nothing fetched: no refs of it here
        for where in (str(mirror), "mirror"):
            r = self.git(env, self.sub, "push", "-q", where, "topic")
            self.assertNotEqual(r.returncode, 0, where)  # the token on `old` would be public there too
            self.assertIn("a token at old.txt:1", r.stderr)
        r = self.git(env, self.sub, "push", "-q", "mirror", "origin/main:refs/heads/main")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_nested_submodules_push_is_scanned_too(self):
        inner = self.s.tmp / "inner.git"
        self.git(None, self.s.tmp, "init", "-q", "--bare", "-b", "main", str(inner))
        seed = self.s.tmp / "seed"
        self.git(None, seed, "push", "-q", str(inner), "main")
        self.git(None, seed, "checkout", "-q", "main")
        r = self.git(None, seed, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "inner")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.git(None, seed, "commit", "-qm", "inner")
        self.git(None, seed, "push", "-q", str(self.sub_remote), "main")
        env, _ = self.session()
        r = self.git(env, self.sub, "-c", "protocol.file.allow=always", "submodule", "update", "-q", "--init")
        self.assertEqual(r.returncode, 0, r.stderr)
        nested = self.sub / "inner"
        self.assertEqual(self.git(env, nested, "config", "core.hooksPath").stdout.strip(),
                         str(submodule_folder(self.p, self.wt)))
        self.commit(env, nested, "b.txt", TOKEN.replace("Wq4", "Zr7") + "\n")  # inner.git has TOKEN already
        r = self.git(env, nested, "push", "-q", "origin", "HEAD:refs/heads/topic")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)

    def test_a_submodule_with_a_core_hooks_path_of_its_own_still_runs_those_hooks(self):
        env, _ = self.session()
        self.git(None, self.sub, "config", "core.hooksPath", ".githooks")  # the submodule's own config
        self.own_hook(self.sub / ".githooks", "commit-msg")
        self.commit(env, self.sub, "a.txt", "a\n")
        self.assertEqual(self.ran(), ["commit-msg"])
        self.assertEqual(self.git(env, self.sub, "config", "core.hooksPath").stdout.strip(),
                         str(submodule_folder(self.p, self.wt)))  # the session's include still comes last

    def test_at_the_cap_a_submodules_push_is_still_scanned_and_its_commits_get_no_skip_ci(self):
        env, _ = self.session(budget=True)
        self.commit(env, self.sub, "b.txt", TOKEN + "\n")
        self.assertEqual(self.git(env, self.sub, "log", "-1", "--format=%s").stdout.strip(), "b.txt")
        r = self.git(env, self.sub, "push", "-q", "origin", "HEAD:refs/heads/topic")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)
        self.commit(env, self.wt, "a.txt", "a\n")  # the worktree's own commits still get the tag
        self.assertEqual(self.git(env, self.wt, "log", "-1", "--format=%s").stdout.strip(), f"a.txt {actions.SKIP}")


if __name__ == "__main__":
    unittest.main()
