"""T-099: the old-git held-back tests (`test_held_old_git`) again with a real git before 2.38, such as Ubuntu 22.04's
2.34, in place of an emulated one: WINDVANE_OLD_GIT=/path/to/git, or `git-2.34` on the PATH. Skipped without one."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_held_old_git  # noqa: E402  (the module, so its own class isn't collected here twice)

OLD_GIT = os.environ.get("WINDVANE_OLD_GIT") or shutil.which("git-2.34")


def version(git):
    """(major, minor) of `git`, or None when it doesn't run."""
    try:
        out = subprocess.run([git, "--version"], capture_output=True, text=True).stdout
    except OSError:
        return None
    m = re.search(r"(\d+)\.(\d+)", out)
    return m and (int(m[1]), int(m[2]))


@unittest.skipUnless(OLD_GIT, "no real old git: set WINDVANE_OLD_GIT=/path/to/git (before 2.38), or put git-2.34 "
                              "on the PATH")
class HeldRealOldGit(test_held_old_git.HeldOldGit):
    @classmethod
    def setUpClass(cls):
        found = version(OLD_GIT)
        if not found or found >= (2, 38):  # a wrong path fails loudly rather than testing today's git
            raise AssertionError(f"WINDVANE_OLD_GIT={OLD_GIT} is {found or 'not a git'}: it must be a git before 2.38")
        super().setUpClass()

    def old_git(self):
        return f'#!/bin/sh\nexec {shlex.quote(os.path.abspath(OLD_GIT))} "$@"\n'


if __name__ == "__main__":
    unittest.main()
