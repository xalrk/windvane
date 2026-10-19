"""T-090: every push call site test_push_fit.py drives, driven again with a 120-character ticket title and a
120-character error, reason or fix text: each push carries the title and the text whole (T-093: no cut, no "…"),
or whole()'s overflow line in the project log does. test_push_fit's short ticket name and 300-character error
can't see a site that cuts or drops a long title, or one that keeps a long reason out of its push."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_push_fit as fit  # noqa: E402
from windvane import driver  # noqa: E402
from test_plain_error import ENVELOPE, MESSAGE  # noqa: E402

TEXT = ("The API stopped answering mid-turn: the connection was reset by the peer after six hundred seconds of waiting "
        "on a reply")
TITLE = ("Retry overloaded sessions with a longer backoff and say on the phone which ticket waits, why, and until when "
         "it waits on")
FIX = ("Split the ticket into one ticket for the driver and one for its prompts, then run each alone, then merge "
       "them in series.")
# Sites whose push says none of the free text, by design: what they push is fixed words, times, counts and paths
NO_TEXT = {"dollar_budget", "paused", "started_and_stopped:windvane started", "killed", "tmp_full", "held_for_good",
           "secret_aside", "kept_secret", "not_taken_up",  # a secret's push: a kind and a place, never text
           "scale_warned"}  # a scale's id and path


def limit_fix(p, patch, s):
    """set_aside_limit's fix is fixed text today; a long one must still arrive whole."""
    pilot(p).set_aside_limit("T-001", TEXT, FIX)
    return {"Set aside": ()}


def pilot(p, worktree=False):
    """A pilot that knows the queue's titles, as one in a run does."""
    pl = real_pilot(p, worktree)
    pl.queue = driver.Queue(p)
    return pl


real_pilot = fit.pilot


class LongTitle(fit.Sandbox):
    def ticket(self, tid, deps="", touches=None):
        self.write(f".windvane/tickets/{tid}.md", f"# {tid}: {TITLE}\nDepends on: {deps or 'nothing'}.\n")


def sweep():
    """Every site with the long inputs: {name: ([(at, title, body)], words or a traceback, the project log)}."""
    logs, out = [], {}
    real = driver.log

    def log(p, msg):  # the project log, kept past the sandbox's end
        logs.append(msg)
        real(p, msg)
    with mock.patch.object(fit, "LONG", TEXT), mock.patch.object(fit, "Sandbox", LongTitle), \
            mock.patch.object(fit, "pilot", pilot), \
            mock.patch.object(driver, "log", log):
        for site in fit.SITES + [limit_fix]:
            logs.clear()
            got, words = fit.drive(site)
            out[site.__name__] = got, words, "\n".join(logs)
    return out


class PushSweep(unittest.TestCase):
    swept = None

    @classmethod
    def results(cls):
        if cls.swept is None:
            cls.swept = sweep()
        return cls.swept

    def test_the_long_inputs_are_120_characters(self):
        self.assertEqual({len(TEXT), len(TITLE), len(FIX)}, {120})

    def test_every_site_ran(self):
        for name, (got, words, _) in self.results().items():
            with self.subTest(name):
                self.assertIsInstance(words, dict, words)
                self.assertTrue(got, "no push")

    def test_a_push_naming_the_ticket_names_it_whole(self):
        for name, (got, _, log) in self.results().items():
            for _, title, msg in got:
                with self.subTest(name, title=title[:40]):
                    self.assertNotIn("…", title + msg)
                    if "T-001" in title:
                        self.assertIn(f"{TITLE} (T-001)", title)

    def test_a_push_carries_its_long_text_whole_or_the_log_does(self):
        for name, (got, _, log) in self.results().items():
            for _, title, msg in got:
                key = f"{name}:{title.split(':')[0]}"
                if name in NO_TEXT or key in NO_TEXT:
                    continue
                with self.subTest(key):
                    said = " ".join(msg.split())
                    whole = [t for t in (TEXT, FIX) if t.rstrip(".") in said]
                    self.assertTrue(whole or f"the whole text: {TEXT}" in log,
                                    f"neither the push nor the log has the text whole:\n{msg}")

    def test_a_sessions_api_error_is_pushed_by_its_message(self):
        """The sites a session's error reaches: its JSON body would fill the first line instead of the message."""
        with mock.patch.object(fit, "LONG", ENVELOPE):
            for site in (fit.set_aside, fit.failed):
                got, words = fit.drive(site)
                with self.subTest(site.__name__):
                    self.assertIsInstance(words, dict, words)
                    [(_, _, msg)] = got
                    self.assertIn(f"API Error 400: {MESSAGE[:-1]}", msg.split("\n")[0])
                    self.assertNotIn("{", msg)

    def test_a_fix_text_arrives_whole(self):
        [(_, _, msg)] = self.results()["limit_fix"][0]
        self.assertIn(FIX, " ".join(msg.split()))


if __name__ == "__main__":
    unittest.main()
