"""T-102: the phone's `status` (ntfy-listen's status.d hook) lists only the autopilots that are on, while `overview`
keeps one hook per project; hooks of a deleted repo go, and an older install's per-project status hooks are replaced."""
import contextlib
import fcntl
import io
import json
import shlex
import shutil
import subprocess
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import driver, install, phone  # noqa: E402
from windvane.config import Project, exe  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402

NONE = "windvane: no autopilot running (overview lists every project)"


def run_hook(f):
    r = subprocess.run([str(f)], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr


class PhoneStatus(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.ntfy = Path.home() / ".config/ntfy"
        self.ntfy.mkdir(parents=True)
        self.roots = {"sandbox": self.s.root}
        for name in ("idle", "gone"):  # two more projects on the same remote
            root = self.s.tmp / name
            shutil.copytree(self.s.root, root, symlinks=True)
            cfg = root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace('name = "sandbox"', f'name = "{name}"'))
            self.roots[name] = root
        self.p = {n: Project(r) for n, r in self.roots.items()}
        for p in self.p.values():
            phone.register(p)
            install.phone_hooks(p)

    def start(self, name):
        """Hold the project's driver lock, as a running autopilot does."""
        f = self.p[name].state / "lock"
        f.parent.mkdir(parents=True, exist_ok=True)
        fh = open(f, "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX)

    def hooks(self, kind):
        return sorted(f.name for f in (self.ntfy / kind).iterdir())

    def test_status_lists_only_the_running_project_and_skips_a_deleted_one(self):
        self.start("sandbox")
        shutil.rmtree(self.roots["gone"])
        self.assertEqual(self.hooks("status.d"), ["windvane"])
        code, out, err = run_hook(self.ntfy / "status.d/windvane")
        self.assertEqual((code, out.splitlines()), (0, ["Sandbox: autopilot running"]), err)
        self.assertNotIn("Traceback", err)

    def test_with_none_running_status_says_so_in_one_line(self):
        code, out, err = run_hook(self.ntfy / "status.d/windvane")
        self.assertEqual((code, out), (0, NONE), err)

    def test_a_deleted_repos_hooks_go_and_every_overview_hook_left_answers(self):
        shutil.rmtree(self.roots["gone"])
        code, out, err = run_hook(self.ntfy / "overview.d/windvane-gone")  # before the sweep: a line, not a traceback
        self.assertNotIn("Traceback", out + err)
        self.assertIn("doesn't exist", err)
        m = install.load()  # one of them written before install.json listed hooks
        m["paths"] = [e for e in m["paths"] if not e["path"].endswith("overview.d/windvane-gone")]
        install.save(m)
        install.tidy_phone_hooks()  # what a run's start does
        self.assertEqual(self.hooks("overview.d"), ["windvane-idle", "windvane-sandbox"])
        self.assertEqual(self.hooks("stop.d"), ["windvane-idle", "windvane-sandbox"])
        for f in (self.ntfy / "overview.d").iterdir():
            code, out, err = run_hook(f)
            self.assertEqual(code, 0, err)
            self.assertNotIn("Traceback", out + err)
            self.assertIn("tickets done", out)
        listed = [e["path"] for e in install.load()["paths"]]
        self.assertFalse([x for x in listed if x.endswith("-gone")])

    def test_upgrading_replaces_the_old_per_project_status_hooks_and_keeps_the_users_own(self):
        m = install.load()
        status = self.ntfy / "status.d"
        (status / "windvane").unlink()  # the install as it was before T-102: a status hook per project
        m["paths"] = [e for e in m["paths"] if e["path"] != str(status / "windvane")]
        for name, root in self.roots.items():
            f = status / f"windvane-{name}"
            f.write_text(f"#!/usr/bin/env bash\n# windvane {name}: `status` from the phone (ntfy-listen)\n"
                         f"exec {' '.join(exe())} -C {root} status --line\n")
            f.chmod(0o755)
            if name != "gone":  # written before install.json listed hooks: known by its form
                m["paths"].append({"path": str(f), "kind": "file"})
        install.save(m)
        before = [run_hook(f)[1] for f in sorted(status.iterdir())]  # the old answer: a line for every project
        self.assertIn("Idle: autopilot off", "\n".join(before))
        for mine in ("mine", "windvane-mine"):  # the user's own scripts, not in install.json
            (status / mine).write_text("#!/bin/sh\necho mine\n")
        install.tidy_phone_hooks()
        self.assertEqual(self.hooks("status.d"), ["mine", "windvane", "windvane-mine"])
        self.assertEqual(run_hook(status / "windvane")[1], NONE)
        install.tidy_phone_hooks()  # nothing left to change: install.json stays as it is
        listed = [e["path"] for e in install.load()["paths"]]
        self.assertEqual(listed.count(str(status / "windvane")), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            install.uninstall(yes=True)  # uninstall still takes back exactly what windvane wrote
        self.assertEqual(self.hooks("status.d"), ["mine", "windvane-mine"])
        self.assertFalse((self.ntfy / "overview.d").exists())  # its own directory too, once empty

    def test_a_hook_another_run_swept_first_is_no_error(self):
        shutil.rmtree(self.roots["gone"])
        real = install.hook_root_exists

        def raced(f):  # another run's start removes the hook between the look and the unlink
            gone = real(f)
            if not gone:
                f.unlink()
            return gone
        with mock.patch.object(install, "hook_root_exists", raced):
            install.tidy_phone_hooks()
        self.assertEqual(self.hooks("overview.d"), ["windvane-idle", "windvane-sandbox"])

    def test_a_run_starts_even_when_the_hooks_cant_be_tidied(self):
        shutil.rmtree(self.roots["gone"])
        (self.ntfy / "overview.d").chmod(0o555)  # unwritable: the gone repo's hook can't be removed
        self.addCleanup((self.ntfy / "overview.d").chmod, 0o755)
        pilot = driver.Pilot(self.p["sandbox"], Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0,
                                                          answer_wait=10, replan=0, goal="", parallel=1))
        pilot.run(now() + timedelta(minutes=5))
        self.assertEqual(pilot.stop_reason, "queue empty")
        self.assertIn("phone hooks: not tidied", (self.p["sandbox"].state / "autopilot.log").read_text())

    def test_an_unreadable_install_record_is_neither_swept_by_nor_saved_over(self):
        shutil.rmtree(self.roots["gone"])
        install.manifest_path().write_text("{not json")
        install.tidy_phone_hooks()
        self.assertEqual(install.manifest_path().read_text(), "{not json")
        self.assertIn("windvane-gone", self.hooks("overview.d"))

    def test_a_status_hook_the_user_wrote_under_windvanes_name_is_left_alone(self):
        f = self.ntfy / "status.d/windvane"
        m = install.load()
        m["paths"] = [e for e in m["paths"] if e["path"] != str(f)]
        install.save(m)
        f.write_text("#!/bin/sh\necho theirs\n")
        with contextlib.redirect_stdout(io.StringIO()) as said:
            install.phone_hooks(self.p["idle"])
        self.assertIn("isn't windvane's, so it's left alone", said.getvalue())
        self.assertEqual(f.read_text(), "#!/bin/sh\necho theirs\n")
        self.assertNotIn(str(f), json.dumps(install.load()))


    def test_init_refreshes_and_records_a_hook_claude_wrote_before_install_json_listed_it(self):
        f = self.ntfy / "stop.d/windvane-idle"
        m = install.load()
        m["paths"] = [e for e in m["paths"] if e["path"] != str(f)]
        install.save(m)
        current = f.read_text()
        f.write_text(current.replace(shlex.join(exe()), "/old/path/windvane"))  # an old hook: a stale exe path
        with contextlib.redirect_stdout(io.StringIO()) as said:
            install.phone_hooks(self.p["idle"])
        self.assertNotIn("left alone", said.getvalue())
        self.assertEqual(f.read_text(), current)
        self.assertIn(str(f), [e["path"] for e in install.load()["paths"]])


if __name__ == "__main__":
    unittest.main()
