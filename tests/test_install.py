import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from windvane import cli, install, runner  # noqa: E402
from windvane.install import add_keyword_hook  # noqa: E402
from helpers import Sandbox, tmpdir  # noqa: E402

RUNTIME = tempfile.mkdtemp(prefix="windvane-runtime-")
atexit.register(shutil.rmtree, RUNTIME, True)


def hook(prompt, session=""):
    out = subprocess.run([sys.executable, str(REPO / "hooks/mention_hook.py")],
                         input=json.dumps({"prompt": prompt, "session_id": session}), capture_output=True, text=True,
                         env={"XDG_RUNTIME_DIR": RUNTIME, "PATH": "/usr/bin:/bin"}).stdout
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None


class KeywordHook(unittest.TestCase):
    def test_fires_on_the_keywords_only(self):
        for p in ("yo dawg heres my idea, lets build it with windvane", "yo claude put windvane on my idea",
                  "i wanna make a recipe app, use wind-vane", "autopilot this idea: a budgeting tool", "run autopilot overnight",
                  "how's the Autopilot doing", "use Windvane on this", "auto-pilot status?"):
            self.assertIn("windvane", hook(p) or "", p)
        for p in ("fix the pilot light", "autopilots? no: autopiloting is a word", "build it with claude"):
            self.assertIsNone(hook(p), p)

    def test_once_per_session(self):
        self.assertIsNotNone(hook("windvane status?", "s-1"))
        self.assertIsNone(hook("[notice] windvane merged T-003", "s-1"))
        self.assertIsNotNone(hook("windvane status?", "s-2"))

    def test_settings_keep_other_hooks_and_get_it_once(self):
        f = tmpdir(self) / "settings.json"
        f.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                "command": "x"}]}]}}))
        self.assertTrue(add_keyword_hook(f))
        self.assertFalse(add_keyword_hook(f))
        data = json.loads(f.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "x")


HELP = " ".join(runner.CLI_FLAGS)  # `claude --help`, as far as doctor reads it
FAKES = {"claude": '#!/bin/sh\n[ "$1 $2" = "auth status" ] && echo \'{"loggedIn": true}\'\n'
                   f'[ "$1" = "--help" ] && echo "{HELP} --allowedTools --disallowed-tools"\nexit 0\n',
         "gh": "#!/bin/sh\nexit 0\n", "tmux": "#!/bin/sh\nexit 0\n"}


def fake_bin(d, **override):
    """Fake claude, gh and tmux in `d` (so nothing reaches the real tools); `override` replaces a fake's script."""
    d.mkdir(parents=True, exist_ok=True)
    for name, text in {**FAKES, **override}.items():
        (d / name).write_text(text)
        (d / name).chmod(0o755)
    return d


ODD_SETTINGS = '{"model":"opus",\n   "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}'


class Installed(unittest.TestCase):
    """The package as someone else gets it: install.sh builds it from this checkout into a fresh venv (offline, with
    the standard-library build backend), and its `windvane` runs with no checkout around."""

    def test_venv_install_help_doctor_install_uninstall(self):
        tmp = tmpdir(self, "windvane-venv-")
        home, claude_dir = tmp / "home", tmp / "claude"
        env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp / "data"), "XDG_STATE_HOME": str(tmp / "state"),
               "XDG_CONFIG_HOME": str(tmp / "config"), "CLAUDE_CONFIG_DIR": str(claude_dir), "PIP_NO_INDEX": "1",
               "WINDVANE_SOURCE": str(REPO),
               "PATH": f"{home / '.local/bin'}:{fake_bin(tmp / 'fakes')}:{Path(sys.executable).parent}:/usr/bin:/bin"}

        def run(*args, cwd=tmp):
            return subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd, stdin=subprocess.DEVNULL)

        r = run("sh", str(REPO / "install.sh"))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        exe = home / ".local/bin/windvane"
        self.assertEqual(sorted(p.name for p in exe.parent.iterdir()), ["windvane"])  # one command, no other name
        self.assertEqual(sorted(p.name for p in (tmp / "data").iterdir()), ["windvane"])
        pkg = Path(run(str(tmp / "data/windvane/venv/bin/python"), "-c",
                       "import windvane, os; print(os.path.dirname(windvane.__file__))").stdout.strip())
        self.assertTrue(str(pkg).startswith(str(tmp / "data")), pkg)
        for f in ("prompts/WORKER.md", "templates/config.toml", "skills/windvane/SKILL.md", "hooks/mention_hook.py"):
            self.assertTrue((pkg / f).is_file(), f)

        r = run(str(exe), "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for cmd in ("doctor", "install", "uninstall"):
            self.assertIn(cmd, r.stdout)

        r = run(str(exe), "doctor")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("all set", r.stdout)
        self.assertRegex(r.stdout, r"optional +the mention hook .*-> windvane install")

        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(ODD_SETTINGS)
        r = run(str(exe), "install")  # no terminal and no --yes: asks nothing, changes nothing
        self.assertIn("skipped", r.stdout)
        self.assertEqual((claude_dir / "settings.json").read_text(), ODD_SETTINGS)
        self.assertFalse((claude_dir / "skills").exists())

        r = run(str(exe), "install", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((claude_dir / "skills/windvane").resolve(), (pkg / "skills/windvane").resolve())
        self.assertFalse((home / ".local/bin/windvane").resolve().is_relative_to(REPO))  # no checkout link
        hooks = json.loads((claude_dir / "settings.json").read_text())["hooks"]
        cmd = hooks["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertEqual(Path(cmd).resolve(), (pkg / "hooks/mention_hook.py").resolve())
        out = subprocess.run([cmd], input=json.dumps({"prompt": "build it with windvane"}), capture_output=True,
                             text=True, env=env).stdout
        self.assertIn("windvane", out)
        self.assertIn("ok        the mention hook", run(str(exe), "doctor").stdout)

        r = run(str(exe), "uninstall", "--yes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"rm -r {tmp / 'data/windvane'} ~/.local/bin/windvane", r.stdout)  # install.sh's way out
        self.assertEqual((claude_dir / "settings.json").read_text(), ODD_SETTINGS)
        self.assertEqual(sorted(p.name for p in claude_dir.iterdir()), ["settings.json"])
        self.assertFalse((tmp / "state/windvane/install.json").exists())


class Checkout(unittest.TestCase):
    def test_bin_runs_without_leaking_pythonpath(self):
        """What bin/windvane starts (sessions, checks, bg and heavy jobs) mustn't import the checkout's windvane."""
        cfg = tmpdir(self)
        (cfg / "windvane").mkdir()
        (cfg / "windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\n")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        r = subprocess.run([str(REPO / "bin/windvane"), "heavy", "--", sys.executable, "-c",
                            "import os; print(os.environ.get('PYTHONPATH'))"], capture_output=True, text=True,
                           env={**env, "XDG_CONFIG_HOME": str(cfg), "XDG_RUNTIME_DIR": str(cfg)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "None")


class InstallInProcess(unittest.TestCase):
    """install/uninstall from a checkout, in a throwaway HOME and Claude config dir."""

    def setUp(self):
        self.tmp = tmpdir(self, "windvane-inst-")
        self.claude = self.tmp / "claude"
        env = {"HOME": str(self.tmp / "home"), "XDG_STATE_HOME": str(self.tmp / "state"),
               "CLAUDE_CONFIG_DIR": str(self.claude)}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def quiet(self, fn, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            fn(*args)
        return out.getvalue()

    def test_no_settings_file_before_none_after(self):
        self.quiet(install.install, True)
        self.assertTrue((self.claude / "settings.json").exists())
        self.assertEqual(os.readlink(self.tmp / "home/.local/bin/windvane"), str(REPO / "bin/windvane"))
        self.assertIn("The checkout itself stays", self.quiet(install.uninstall, True))
        self.assertFalse(self.claude.exists())
        self.assertFalse((self.tmp / "home/.local").exists())

    def test_keeps_the_users_own_changes(self):
        self.claude.mkdir()
        f = self.claude / "settings.json"
        f.write_text('{"model": "opus"}')
        self.quiet(install.install, True)
        data = json.loads(f.read_text())
        data["theme"] = "dark"
        data["hooks"]["UserPromptSubmit"].append({"hooks": [{"type": "command", "command": "mine"}]})
        f.write_text(json.dumps(data))
        self.quiet(install.uninstall, True)
        self.assertEqual(json.loads(f.read_text()), {"model": "opus", "theme": "dark", "hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "mine"}]}]}})

    def test_leaves_a_real_skill_dir_and_restores_an_older_link(self):
        (self.claude / "skills/windvane").mkdir(parents=True)
        old = self.tmp / "old-checkout/bin/windvane"
        old.parent.mkdir(parents=True)
        old.write_text("#!/bin/sh\n")
        link = self.tmp / "home/.local/bin/windvane"
        link.parent.mkdir(parents=True)
        link.symlink_to(old)
        out = self.quiet(install.install, True)
        self.assertIn("isn't a link, so it's left alone", out)
        self.assertEqual(os.readlink(link), str(REPO / "bin/windvane"))
        self.quiet(install.uninstall, True)
        self.assertTrue((self.claude / "skills/windvane").is_dir())
        self.assertEqual(os.readlink(link), str(old))

    def test_replaces_a_broken_skill_link_and_doesnt_bring_it_back(self):
        skill = self.claude / "skills/windvane"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(self.tmp / "venv-of-an-older-python/windvane/skills/windvane")
        self.quiet(install.install, True)
        self.assertEqual(skill.resolve(), (REPO / "skills/windvane").resolve())
        self.quiet(install.uninstall, True)
        self.assertFalse(skill.exists() or skill.is_symlink())

    def test_installing_twice_adds_once(self):
        self.quiet(install.install, True)
        self.assertIn("already", self.quiet(install.install, True))
        groups = json.loads((self.claude / "settings.json").read_text())["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(groups), 1)
        self.quiet(install.uninstall, True)
        self.assertFalse(self.claude.exists())
        self.assertIn("nothing to remove", self.quiet(install.uninstall, True))


class TwoInstalls(unittest.TestCase):
    """Two windvane installs on one machine (a checkout and an installed copy): the shared phone `status` hook stays
    with the install that wrote it while its command is there, and `stop`/`overview` name the one that ran `init`."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.ntfy = Path.home() / ".config/ntfy"
        self.ntfy.mkdir(parents=True)
        self.first, self.second = (self.fake(n) for n in ("first", "second"))

    def fake(self, name):
        f = self.s.tmp / name / "windvane"
        f.parent.mkdir()
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
        return str(f)

    def init(self, cmd):
        with mock.patch.object(install, "exe", lambda: [cmd]), contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["init", str(self.s.root)])
        return out.getvalue()

    def runs(self, kind):
        return (self.ntfy / kind).read_text().splitlines()[-1].split()[1]

    def test_init_from_a_second_install_leaves_the_firsts_status_until_its_command_is_gone(self):
        self.init(self.first)
        self.assertEqual(self.runs("status.d/windvane"), self.first)
        said = self.init(self.second)
        self.assertEqual(self.runs("status.d/windvane"), self.first)
        self.assertIn(f"phone status: kept with the windvane that set it up ({self.first})", said)
        self.assertEqual(self.runs("stop.d/windvane-sandbox"), self.second)
        self.assertEqual(self.runs("overview.d/windvane-sandbox"), self.second)
        os.unlink(self.first)
        self.assertNotIn("kept with", self.init(self.second))
        self.assertEqual(self.runs("status.d/windvane"), self.second)

    def test_a_runs_start_brings_back_a_status_hook_thats_missing_or_whose_install_is_gone(self):
        self.init(self.first)
        with mock.patch.object(install, "exe", lambda: [self.second]):
            install.tidy_phone_hooks()  # the first is still there: left alone
            self.assertEqual(self.runs("status.d/windvane"), self.first)
            os.unlink(self.first)
            install.tidy_phone_hooks()
            self.assertEqual(self.runs("status.d/windvane"), self.second)
            (self.ntfy / "status.d/windvane").unlink()
            install.tidy_phone_hooks()
            self.assertEqual(self.runs("status.d/windvane"), self.second)


if __name__ == "__main__":
    unittest.main()
