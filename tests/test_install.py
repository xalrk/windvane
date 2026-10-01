import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from windvane.cli import add_keyword_hook  # noqa: E402

RUNTIME = tempfile.mkdtemp()


def hook(prompt, session=""):
    out = subprocess.run([sys.executable, str(REPO / "hooks/mention_hook.py")],
                         input=json.dumps({"prompt": prompt, "session_id": session}), capture_output=True, text=True,
                         env={"XDG_RUNTIME_DIR": RUNTIME, "PATH": "/usr/bin:/bin"}).stdout
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None


class KeywordHook(unittest.TestCase):
    def test_fires_on_the_keywords_only(self):
        for p in ("yo dawg heres my idea, lets build it with windvane", "run autopilot overnight",
                  "how's the Autopilot doing", "use windvane on this", "auto-pilot status?"):
            self.assertIn("windvane", hook(p) or "", p)
        for p in ("fix the pilot light", "autopilots? no: autopiloting is a word", "build it with claude"):
            self.assertIsNone(hook(p), p)

    def test_once_per_session(self):
        self.assertIsNotNone(hook("windvane status?", "s-1"))
        self.assertIsNone(hook("[notice] windvane merged T-003", "s-1"))
        self.assertIsNotNone(hook("windvane status?", "s-2"))

    def test_settings_keep_other_hooks_and_get_it_once(self):
        f = Path(tempfile.mkdtemp()) / "settings.json"
        f.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                "command": "x"}]}]}}))
        self.assertTrue(add_keyword_hook(f))
        self.assertFalse(add_keyword_hook(f))
        data = json.loads(f.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
        self.assertEqual(data["hooks"]["Stop"][0]["hooks"][0]["command"], "x")


if __name__ == "__main__":
    unittest.main()
