"""The grants a server of your own needs, as lines to paste: they name the token's user when the server says who it
is, a rotate takes the old topics back from that user first, a refused test push says its fix in one line, and a
listener started before the grant lands picks up on its own once it does."""
import contextlib
import io
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
import test_phone_server as server  # noqa: E402
from test_phone_listen_lock import soon  # noqa: E402

UNGRANTED = "tk_notgrantedyet00000000000000000"  # what the fake takes until the topics are granted


class Grants(server.Base):
    setup_cli = server.Setup.setup_cli

    def rotated(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            phone.rotate()
        return out.getvalue()

    def assert_no_token(self, out):
        for line in out.splitlines():
            self.assertNotIn(server.TOKEN, line)
        self.assert_never_shown(out)

    def test_the_server_naming_the_token_s_user_fills_it_in(self):
        self.ntfy.username = "ann"
        out = self.setup_cli()
        c = phone.conf()
        lines = out.splitlines()
        self.assertIn(f"  ntfy access ann {c['topic']} rw", lines)
        self.assertIn(f"  ntfy access ann {c['reply_topic']} rw", lines)
        self.assertNotIn("<user>", out)
        self.assertIn(("GET", "/v1/account", f"Bearer {server.TOKEN}"), self.ntfy.seen, "asked with the token")
        self.assert_no_token(out)

    def test_a_server_that_cant_say_keeps_user_and_says_to_put_the_name_there(self):
        for name in (None, "*"):  # an older server (404), an anonymous account
            with self.subTest(name=name):
                self.ntfy.username = name
                out = self.setup_cli()
                c = phone.conf()
                self.assertIn(f"  ntfy access <user> {c['topic']} rw", out.splitlines())
                self.assertIn("(<user> below: the server didn't say its name)", out)

    def test_an_account_lookup_that_fails_is_none(self):
        self.ntfy.username = "ann"
        self.assertEqual(phone.account(self.c), "ann")
        self.assertIsNone(phone.account({**self.c, "token": UNGRANTED}), "refused")
        self.ntfy.close()
        self.assertIsNone(phone.account(self.c), "nothing listening")

    def test_a_rotate_takes_the_old_topics_back_before_granting_the_new(self):
        self.ntfy.username = "ann"
        out = self.rotated()
        c = phone.conf()
        lines = out.splitlines()
        resets = [f"  ntfy access --reset ann {self.c['topic']}", f"  ntfy access --reset ann {self.c['reply_topic']}"]
        grants = [f"  ntfy access ann {c['topic']} rw", f"  ntfy access ann {c['reply_topic']} rw"]
        at = [lines.index(line) for line in resets + grants]
        self.assertEqual(at, sorted(at), "the old topics are reset first")
        self.assertEqual(sum("--reset" in line for line in lines), 2, out)
        self.assert_no_token(out)
        self.assertNotIn(self.c["topic"], self.log(), "printed at the terminal only")

    def test_a_rotate_without_a_token_has_nothing_to_reset(self):
        phone.save_conf({k: v for k, v in self.c.items() if k != "token"})
        self.ntfy.token = None  # an open server
        self.assertNotIn("ntfy access", self.rotated())

    def test_a_refused_test_push_says_its_fix_in_one_line(self):
        self.ntfy.token = UNGRANTED
        for run in (self.setup_cli, self.rotated):
            with self.subTest(run=run.__name__):
                phone._refused = 0
                after = run().split("test push: ", 1)[1].splitlines()
                self.assertEqual(len(after), 1, after)
                self.assertIn("(403): once the `ntfy access` lines above have run on the server, `windvane phone "
                              "setup` sends it again", after[0])


class ListenBeforeTheGrant(server.Base):
    def test_a_listener_logs_the_403_once_then_answers_once_granted(self):
        self.ntfy.token = UNGRANTED  # rotate restarted the listener before the new topics were granted
        stop = threading.Event()
        self.addCleanup(stop.set)
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))
        status = lambda: any(m.get("title") == "Status" for m in self.ntfy.msgs)  # noqa: E731
        with mock.patch.object(phone, "rest", lambda s, until=lambda: False: time.sleep(0.05)):
            t = threading.Thread(target=phone.listen, kwargs={"until": stop.is_set}, daemon=True)
            t.start()
            self.assertTrue(soon(lambda: sum(a == "GET" for a, _, _ in self.ntfy.seen) >= 3), "it keeps trying")
            n = len(self.ntfy.seen)
            self.ntfy.token = server.TOKEN  # `ntfy access` ran on the server
            self.assertTrue(soon(lambda: any(m == "GET" for m, _, _ in self.ntfy.seen[n:])), "it tries again by itself")
            time.sleep(0.2)  # the stream is open
            self.ntfy.publish(self.c["reply_topic"], "status")
            self.assertTrue(soon(status, 5), "the reply is handled")
            self.assertTrue(t.is_alive(), "the same listener, never restarted")
            stop.set()
            self.ntfy.closing = True
            t.join(5)
        refusals = [line for line in self.log().splitlines() if "(403)" in line]
        self.assertEqual(len(refusals), 1, self.log())
        self.assertNotIn(server.TOKEN, self.log())


if __name__ == "__main__":
    unittest.main()
