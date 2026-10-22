"""`windvane doctor` and `init` inside Windows' WSL: they say they're there, and warn about what WSL makes easy to get
wrong (a repo on a Windows drive, a Windows program on PATH, no systemd), each with its fix."""
import argparse
import contextlib
import io
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import Sandbox, tmpdir
from test_doctor import GITHUB
from test_install import REPO, fake_bin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from windvane import cli, doctor, phone, util  # noqa: E402

WSL2 = "Linux version 5.15.167.4-microsoft-standard-WSL2 (root@1b5f4b4e2c3a) (gcc (GCC) 11.2.0) #1 SMP\n"
WSL1 = "Linux version 4.4.0-22621-Microsoft (gcc version 5.4.0 (GCC) ) #2506-Microsoft\n"
LINUX = "Linux version 6.8.0-45-generic (buildd@lcy02-amd64-075) (x86_64-linux-gnu-gcc-13 (Ubuntu 13.2.0)) #45-Ubuntu\n"
MNT = "/mnt/"
C, D = MNT + "c", MNT + "d"  # the Windows drives C: and D: as WSL mounts them
WINDOWS_CLAUDE = f"{C}/Users/me/AppData/Roaming/npm/claude"


class InWsl(unittest.TestCase):
    """test_doctor's fakes on PATH, with /proc/version, systemd and the Windows PATH faked on top."""

    def setUp(self):
        self.tmp = tmpdir(self, "windvane-doctor-wsl-")
        bindir = fake_bin(self.tmp / "bin")
        (bindir / "windvane").symlink_to(REPO / "bin/windvane")
        (bindir / "git").symlink_to(shutil.which("git"))
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        env = {"HOME": str(self.tmp / "home"), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"), "PATH": str(bindir),
               "XDG_CONFIG_HOME": str(self.tmp / "xdg"), "XDG_STATE_HOME": str(self.tmp / "state")}
        for patch in (mock.patch.object(util, "PROC", self.proc), mock.patch.object(phone, "SYSTEMD", self.tmp / "no"),
                      mock.patch.dict(os.environ, env)):
            patch.start()
            self.addCleanup(patch.stop)

    def kernel(self, version):
        (self.proc / "version").write_text(version)

    def doctor(self, windows=()):
        """doctor's output with the tools in `windows` resolving to their Windows builds on the C: drive."""
        real = shutil.which
        with mock.patch.object(doctor.shutil, "which",
                               lambda n, *a, **k: f"{C}/Program Files/{n}" if n in windows else real(n, *a, **k)):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                doctor.main(GITHUB)
        return out.getvalue()

    def test_it_says_wsl2_in_its_first_lines(self):
        self.kernel(WSL2)
        self.assertIn("Linux (WSL2)", "\n".join(self.doctor().splitlines()[:3]))

    def test_wsl1_is_told_to_convert(self):
        self.kernel(WSL1)
        self.assertRegex(self.doctor(), r"WARNING +Linux \(WSL 1\).*-> in Windows: wsl -l -v names it, then "
                                        r"wsl --set-version <name> 2")

    def test_a_windows_claude_on_path_gets_its_warning_and_fix(self):
        self.kernel(WSL2)
        with mock.patch.object(doctor.shutil, "which", lambda n, *a, **k: WINDOWS_CLAUDE if n == "claude" else None):
            rows = doctor.wsl_warnings()
        self.assertEqual(rows, [("warn", f"`claude` on PATH is a Windows program ({WINDOWS_CLAUDE}) that can't see "
                                 "Linux paths",
                                 "install Claude Code inside WSL: curl -fsSL https://claude.ai/install.sh | bash")])
        self.assertRegex(self.doctor(windows=("git", "node")),
                         rf"WARNING +`node` on PATH is a Windows program \({C}/Program Files/node\).*"
                         r"-> install it inside WSL \(sudo apt install nodejs\)")

    def test_a_project_on_a_windows_drive_gets_its_warning_and_fix(self):
        self.kernel(WSL2)
        p = SimpleNamespace(root=Path(f"{D}/code/app"), state=self.tmp / "state/app")
        self.assertEqual(doctor.wsl_warnings(p), [
            ("warn", f"the project {D}/code/app is on the Windows drive {D}: git and the checks are slow there "
             "and its file locks aren't reliable",
             "git clone <the URL from git remote -v> ~/code/app, then use that copy")])

    def test_the_state_dir_and_projects_dir_on_a_windows_drive_are_named(self):
        self.kernel(WSL2)
        (self.tmp / "xdg/windvane").mkdir(parents=True)
        (self.tmp / "xdg/windvane/machine.toml").write_text(f'projects_dir = "{C}/Users/me/code"\n')
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": f"{C}/state"}):
            out = self.doctor()
        self.assertRegex(out, rf"WARNING +the state dir {C}/state/windvane is on the Windows drive {C}.*"
                              r"-> unset XDG_STATE_HOME \(and drop it from your shell's startup file\)")
        self.assertRegex(out, rf'WARNING +projects_dir {C}/Users/me/code is on .*-> set projects_dir = "~/code" in '
                              r'\S*/xdg/windvane/machine.toml\n')

    def test_plain_linux_gets_none_of_it(self):
        self.kernel(LINUX)
        (self.tmp / "xdg/windvane").mkdir(parents=True)
        (self.tmp / "xdg/windvane/machine.toml").write_text(f'projects_dir = "{C}/code"\n')
        out = self.doctor(windows=("claude",))
        self.assertNotRegex(out, r"Linux|WSL|Windows|wsl\.conf")  # nothing new on plain Linux
        self.assertEqual(doctor.wsl_warnings(SimpleNamespace(root=Path(f"{C}/code/app"), state=self.tmp)), [])

    def test_the_systemd_line_names_wsl_conf_only_in_wsl(self):
        self.kernel(WSL2)
        self.assertRegex(self.doctor(), r"optional +systemd .*-> put `\[boot\]` and `systemd=true` in /etc/wsl.conf, "
                                        r"run `wsl --shutdown` in Windows")
        self.kernel(LINUX)
        out = self.doctor()
        self.assertRegex(out, r"optional +systemd .*-> keep `windvane phone listen` running")
        self.assertNotIn("wsl.conf", out)

    def test_systemd_up_is_quiet_and_systemctl_alone_is_not_up(self):
        self.kernel(WSL2)
        with mock.patch.object(phone.shutil, "which", lambda n: "/usr/bin/systemctl"):
            self.assertFalse(phone.has_systemd())  # WSL ships systemctl, but boots without systemd
            with mock.patch.object(phone, "SYSTEMD", self.tmp):
                self.assertTrue(phone.has_systemd())
                self.assertNotIn("systemd", self.doctor())  # a row only when it's missing


class PhoneSetupInWsl(unittest.TestCase):
    def test_setup_without_systemd_names_wsl_conf(self):
        proc = tmpdir(self, "windvane-proc-")
        (proc / "version").write_text(WSL2)
        out = io.StringIO()
        with mock.patch.object(util, "PROC", proc), mock.patch.object(phone, "SYSTEMD", proc / "no"), \
                mock.patch.object(phone, "systemctl") as systemctl, contextlib.redirect_stdout(out):
            self.assertFalse(phone.install_service())
        systemctl.assert_not_called()
        self.assertIn("No systemd here: put `[boot]` and `systemd=true` in /etc/wsl.conf", out.getvalue())


class InitInWsl(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.proc = tmpdir(self, "windvane-proc-")

    def init(self, version):
        (self.proc / "version").write_text(version)
        out = io.StringIO()
        # The sandbox's repo stands in for one on D:; util.windows_drive itself is tested above through doctor.
        with mock.patch.object(util, "PROC", self.proc), mock.patch.object(util, "windows_drive", lambda p: "d"), \
                mock.patch("windvane.cli.shutil.which", lambda n: None), contextlib.redirect_stdout(out):
            cli.cmd_init(argparse.Namespace(dir=str(self.s.root), name=None, check="true", forge=None))
        return out.getvalue()

    def test_init_on_a_windows_drive_warns_once(self):
        out = self.init(WSL2)
        url = util.gout(self.s.root, "remote", "get-url", "origin")
        self.assertTrue(url)
        self.assertEqual(out.count("WARNING"), 1, out)
        self.assertIn(f"WARNING: the project {self.s.root} is on the Windows drive {D}: git and the checks are slow "
                      f"there and its file locks aren't reliable: git clone {url} ~/code/{self.s.root.name}, then use "
                      "that copy\n", out)

    def test_init_off_wsl_says_nothing_of_drives(self):
        self.assertNotIn("Windows drive", self.init(LINUX))


class WindowsDrive(unittest.TestCase):
    def test_only_a_drive_letter_under_mnt_counts(self):
        self.assertEqual([util.windows_drive(p) for p in (C, MNT + "D/x", MNT + "wsl/x", "/home/me" + C, MNT[:-1])],
                         ["c", "d", "", "", ""])


if __name__ == "__main__":
    unittest.main()
