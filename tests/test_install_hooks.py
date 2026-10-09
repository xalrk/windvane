"""T-117: the phone's `stop` and `overview` hooks aren't lost: install.json is changed under a lock, never saved over
when it can't be read, a project's own hooks come back at its run's start, and old hooks are known for any name."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import cli, driver, install, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import REPO, Sandbox  # noqa: E402

# A first `phone_hooks` that has read install.json and waits before saving it, as a slow one would.
SLOW = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from windvane import install
from windvane.config import Project
real = install.save
def save(m):
    Path(sys.argv[3]).touch()
    time.sleep(1.5)
    real(m)
install.save = save
install.phone_hooks(Project(sys.argv[2]))
"""


class InstallHooks(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.ntfy = Path.home() / ".config/ntfy"
        self.ntfy.mkdir(parents=True)

    def project(self, name):
        """Another project on the sandbox's remote, named `name` in its config."""
        root = self.s.tmp / f"p{len(list(self.s.tmp.glob('p*')))}"
        self.s.git("clone", "-q", str(self.s.remote), str(root), cwd=self.s.tmp)
        cfg = root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('name = "sandbox"', f'name = "{name}"'))
        return Project(root)

    def listed(self):
        return [e["path"] for e in install.load()["paths"]]

    def test_two_processes_adding_hooks_at_once_keep_both(self):
        one, two = self.project("one"), self.project("two")
        mark = self.s.tmp / "read"
        slow = subprocess.Popen([sys.executable, "-c", SLOW, str(REPO), str(one.root), str(mark)])
        self.addCleanup(slow.wait, 60)
        until = time.time() + 30
        while not mark.exists() and slow.poll() is None and time.time() < until:
            time.sleep(0.05)
        self.assertTrue(mark.exists(), "the slow phone_hooks never reached its save")
        with contextlib.redirect_stdout(io.StringIO()):
            install.phone_hooks(two)  # while the first holds what it read
        self.assertEqual(slow.wait(60), 0)
        for name in ("one", "two"):
            for kind in ("stop.d", "overview.d"):
                self.assertIn(str(self.ntfy / kind / f"windvane-{name}"), self.listed())

    def test_init_leaves_an_unreadable_install_record_as_it_is_and_says_why(self):
        f = install.manifest_path()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b'{"paths": [{"path": "/x", "kind": "file"}],')  # cut off mid-write
        out = io.StringIO()
        with mock.patch("windvane.cli.shutil.which", lambda n: None), contextlib.redirect_stdout(out):
            cli.cmd_init(argparse.Namespace(dir=str(self.s.root), name=None, check="true", forge=None))
        self.assertEqual(f.read_bytes(), b'{"paths": [{"path": "/x", "kind": "file"}],')
        self.assertIn(f"phone hooks: not written: {f} can't be read", out.getvalue())
        self.assertEqual(out.getvalue().count("can't be read"), 1)
        self.assertFalse((self.ntfy / "stop.d").exists())

    def test_install_and_uninstall_leave_an_unreadable_record_as_it_is(self):
        f = install.manifest_path()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("[]")  # JSON, but not a record
        claude = self.s.tmp / "claude-config"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude)}):
            for step in (install.install, install.uninstall):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    step(yes=True)
                self.assertIn("isn't a windvane install record", out.getvalue())
                self.assertEqual(f.read_text(), "[]")
        self.assertFalse(claude.exists())

    def test_a_run_brings_back_its_own_hooks_tidied_while_its_repo_was_away(self):
        p = Project(self.s.root)
        phone.register(p)
        with contextlib.redirect_stdout(io.StringIO()):
            install.phone_hooks(p)
        stop = self.ntfy / "stop.d/windvane-sandbox"
        before = stop.read_text()
        away = self.s.tmp / "unmounted"
        self.s.root.rename(away)
        install.tidy_phone_hooks()  # another project's run starts meanwhile
        self.assertFalse(stop.exists())
        away.rename(self.s.root)
        pilot = driver.Pilot(p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                          replan=0, goal="", parallel=1))
        pilot.run(now() + timedelta(minutes=5))
        self.assertEqual(stop.read_text(), before)
        self.assertTrue((self.ntfy / "overview.d/windvane-sandbox").exists())
        self.assertIn(str(stop), self.listed())
        self.assertNotIn("phone hooks", (p.state / "autopilot.log").read_text())

    def test_a_run_leaves_a_hook_of_the_users_under_its_name_alone(self):
        p = Project(self.s.root)
        stop = self.ntfy / "stop.d/windvane-sandbox"
        stop.parent.mkdir()
        stop.write_text("#!/bin/sh\necho theirs\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            install.tidy_phone_hooks(p)
        self.assertEqual(out.getvalue(), "")  # a run's start writes nothing to a terminal
        self.assertEqual(stop.read_text(), "#!/bin/sh\necho theirs\n")
        self.assertNotIn(str(stop), self.listed())
        self.assertIn(str(self.ntfy / "overview.d/windvane-sandbox"), self.listed())

    def test_an_old_hook_of_a_project_named_with_dots_and_underscores_is_known(self):
        p = self.project("my_app.v2")
        with contextlib.redirect_stdout(io.StringIO()):
            install.phone_hooks(p)
        install.save({"paths": [], "settings": []})  # written before install.json listed hooks
        hooks = sorted(self.ntfy.glob(f"*.d/windvane-{p.name}"))
        self.assertEqual(len(hooks), 2)
        self.assertTrue(all(install.ours(f) for f in hooks))
        p.root.rename(self.s.tmp / "deleted")
        install.tidy_phone_hooks()
        self.assertFalse(any(f.exists() for f in hooks))  # known as windvane's, so a deleted repo's are swept


if __name__ == "__main__":
    unittest.main()
