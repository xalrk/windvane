"""Every push and phone question names its project once, by its display name: two projects on one phone stay apart."""
import contextlib
import io
import os
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

# the user's own scripts, faked: `notify` records each title, `notify-ask` each question (and never has an answer)
NOTIFY = '#!/usr/bin/env python3\nimport sys\na = sys.argv[1:]\nopen({!r}, "a").write(a[a.index("-t") + 1] + "\\n")\n'
ASK = '#!/usr/bin/env python3\nimport sys\na = sys.argv[1:]\nif a[0] != "--get":\n    open({!r}, "a").write(a[1] + "\\n")\n'


class Titles(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.environ.pop, "FAKE_WORKER", None)
        self.addCleanup(os.environ.pop, "FAKE_FAIL", None)
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.publish()
        self.titles, self.asks = self.s.tmp / "titles.txt", self.s.tmp / "asks.txt"
        for name, script, out in (("notify", NOTIFY, self.titles), ("notify-ask", ASK, self.asks)):
            f = self.s.bin / name
            f.write_text(script.format(str(out)))
            f.chmod(0o755)

    def display(self, name):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f'display_name = "{name}"\n' + cfg.read_text())
        self.s.publish("display name")

    def run_pilot(self, **env):
        os.environ.update(env)
        driver.Pilot(Project(self.s.root), args(answer_wait=0)).run(now() + timedelta(hours=1))

    def sent(self):
        read = lambda f: f.read_text().splitlines() if f.exists() else []
        return read(self.titles), read(self.asks)

    def cli(self, *argv):
        out = io.StringIO()
        self.addCleanup(os.chdir, os.getcwd())
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue().strip()

    def assert_named_once(self, texts, display):
        self.assertTrue(texts)
        for t in texts:
            self.assertTrue(t.startswith(display), t)
            self.assertEqual(t.count(display), 1, t)
            self.assertNotIn("sandbox", t)

    def test_every_push_of_a_run_names_the_project_once(self):
        q = self.s.root / ".windvane/QUESTIONS.md"  # T-002 was built on a provisional answer
        q.write_text(q.read_text().replace("## Provisional\n", "## Provisional\n### T-002: Big or small?\n"
                                           "- **A**: big\n- **B**: small\nBuilt: A; to change it: size.txt\n"))
        self.s.publish("provisional")
        self.run_pilot(FAKE_WORKER="question")  # asked, no answer: the recommendation is built and merged
        self.s.git("pull", "-q", "--ff-only", "origin", "main")
        self.s.ticket("T-003")
        self.s.publish("one more")
        self.run_pilot(FAKE_WORKER="ready", FAKE_FAIL="quiet*")  # its runs end without a result line
        titles, asks = self.sent()
        self.assert_named_once(titles + asks, "Sandbox")
        for want in ("Sandbox windvane started", "Sandbox windvane stopped", "Sandbox: Done: do T-001 (T-001)",
                     "Sandbox: No answer: do T-001 (T-001)", "Sandbox: Done: do T-002 (T-002)",
                     "Sandbox: Failed: do T-003 (T-003)"):
            self.assertIn(want, titles)
        self.assertTrue(asks[0].startswith("Sandbox: do T-001 (T-001): Red or blue?"), asks)
        self.assertNotIn("autopilot", " ".join(titles))

    def test_display_name_and_the_phones_replies(self):
        self.display("Acme Corp")
        self.s.ticket("T-003")
        self.s.publish()
        self.run_pilot(FAKE_WORKER="ready", FAKE_FAIL="quiet*")
        titles, _ = self.sent()
        self.assert_named_once(titles, "Acme Corp")
        self.assertIn("Acme Corp windvane started", titles)
        self.assertIn("Acme Corp: Failed: do T-003 (T-003)", titles)
        status = self.cli("status", "--line")
        self.assertTrue(status.startswith("Acme Corp: autopilot off"), status)
        self.assertTrue(self.cli("overview").startswith("Acme Corp: "))

    def test_the_default_is_the_name_with_a_capital(self):
        self.assertEqual(Project(self.s.root).display, "Sandbox")
        self.assertTrue(self.cli("status", "--line").startswith("Sandbox: autopilot off"))
        self.assertTrue(self.cli("overview").startswith("Sandbox: "))

    def test_a_project_named_windvane_says_windvane_once(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(re.sub(r'(?m)^name = .*$', 'name = "windvane"', cfg.read_text()))
        self.display("windvane")
        p = Project(self.s.root)
        self.assertEqual((p.name, p.display), ("windvane", "windvane"))
        phone.use(p)
        phone.push("windvane started", "Until 04:10.")
        phone.push("Done: T-041 names", "merged")
        phone.ask("windvane-t-041", "T-041: Red or blue?", ["red", "blue"])
        self.assertEqual(self.sent(), (["windvane started", "windvane: Done: T-041 names"],
                                       ["windvane: T-041: Red or blue?"]))


if __name__ == "__main__":
    unittest.main()
