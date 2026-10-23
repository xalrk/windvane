"""A rotate on a server of your own keeps the old topics in phone.toml until the user says their grants were taken
back, so a lost terminal can't leave them granted with nothing to name them; and setup checks the reply topic's read
grant beside the test push, naming the one `ntfy access` line still to run."""
import contextlib
import io
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
import test_phone_server as server  # noqa: E402


class Killed(Exception):
    """The terminal closing, or a Ctrl-C, right after the rotate's write."""


class Old(server.Base):
    setup_cli = server.Setup.setup_cli

    def setUp(self):
        super().setUp()
        self.ntfy.username = "ann"
        self.old = self.c["topic"], self.c["reply_topic"]
        self.resets = [f"  ntfy access --reset ann {t}" for t in self.old]

    def refuse(self, *topics):
        """`ntfy access --reset` has run for `topics`: the server answers 403 to the token's reads on them."""
        allowed = self.ntfy.httpd.RequestHandlerClass.allowed

        def reset(handler):
            if handler.command == "GET" and handler.path.split("/")[1] in topics:
                handler.send_response(403)
                handler.end_headers()
                handler.wfile.write(b'{"code":40301,"http":403,"error":"forbidden"}')
                return False
            return allowed(handler)

        patch = mock.patch.object(self.ntfy.httpd.RequestHandlerClass, "allowed", reset)
        patch.start()
        self.addCleanup(patch.stop)

    def killed_rotate(self):
        with mock.patch.object(phone, "access_help", side_effect=Killed), \
                contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(Killed):
            phone.rotate()
        return out.getvalue()

    def assert_no_token(self, out):
        for line in out.splitlines():
            self.assertNotIn(server.TOKEN, line)
        self.assert_never_shown(out)

    def test_a_killed_rotate_keeps_the_old_topics_until_setup_hears_they_were_revoked(self):
        out = self.killed_rotate()
        self.assertNotIn("--reset", out, "killed before the lines were printed")
        c = phone.conf()
        self.assertEqual(c["revoke"], list(self.old))
        self.assertNotIn(c["topic"], self.old, "the new topics were written in the same go")
        self.assertEqual(stat.S_IMODE(phone.conf_path().stat().st_mode), 0o600)

        out = self.setup_cli()
        lines = out.splitlines()
        self.assertEqual([line for line in lines if "--reset" in line], self.resets)
        self.assertIn("`windvane phone setup --revoked`", out)
        self.assert_no_token(out)
        out = self.setup_cli()
        self.assertEqual([line for line in out.splitlines() if "--reset" in line], self.resets, "every setup, again")

        self.refuse(*self.old)
        out = self.setup_cli("--revoked")
        self.assertNotIn("--reset", out)
        self.assertNotIn("revoke", phone.conf())
        self.assertNotIn("revoke", phone.conf_path().read_text())
        self.assertNotIn("--reset", self.setup_cli(), "gone for good")
        self.assert_no_token(out)
        self.assertNotIn(self.old[0], self.log(), "the topics stay out of the log")

    def test_two_rotates_before_the_revoke_name_all_four_old_topics(self):
        self.killed_rotate()
        second = phone.conf()["topic"], phone.conf()["reply_topic"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            phone.rotate()
        resets = [line for line in out.getvalue().splitlines() if "--reset" in line]
        self.assertEqual(resets, self.resets + [f"  ntfy access --reset ann {t}" for t in second])
        self.assertEqual(phone.conf()["revoke"], [*self.old, *second])

    def test_without_a_token_nothing_is_recorded(self):
        phone.save_conf({k: v for k, v in self.c.items() if k != "token"})
        self.ntfy.token = None  # an open server: no grants to take back
        with contextlib.redirect_stdout(io.StringIO()):
            phone.rotate()
        self.assertNotIn("revoke", phone.conf())


class ReplyGrant(server.Base):
    setup_cli = server.Setup.setup_cli

    def setUp(self):
        super().setUp()
        self.ntfy.username = "ann"
        allowed = self.ntfy.httpd.RequestHandlerClass.allowed
        reply, fake = self.c["reply_topic"], self

        def no_reads_on_reply(handler):  # `ntfy access ann <reply topic> rw` hasn't run yet
            if handler.command == "GET" and handler.path.startswith(f"/{reply}/") and fake.shut:
                fake.ntfy.seen.append((handler.command, handler.path, handler.headers.get("Authorization")))
                handler.send_response(403)
                handler.end_headers()
                handler.wfile.write(b'{"code":40301,"http":403,"error":"forbidden"}')
                return False
            return allowed(handler)

        self.shut = True
        patch = mock.patch.object(self.ntfy.httpd.RequestHandlerClass, "allowed", no_reads_on_reply)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_reply_topic_refusing_reads_names_its_one_access_line_in_the_failed_line(self):
        out = self.setup_cli()
        tail = out.split("test push: ", 1)[1].splitlines()
        self.assertEqual(len(tail), 1, out)
        failed = tail[0]
        self.assertTrue(failed.startswith("sent; reply topic: FAILED ("), "the push went through and says so")
        self.assertEqual(failed.count("FAILED"), 1, failed)
        line = f"`ntfy access ann {self.c['reply_topic']} rw`"
        self.assertEqual(failed.count(line), 1, failed)
        self.assertNotIn(self.c["topic"], failed, "the push topic is granted: not named")
        self.assertIn("read it (403)", failed)
        polls = [p for m, p, _ in self.ntfy.seen if m == "GET" and p.startswith(f"/{self.c['reply_topic']}/")]
        self.assertEqual(len(polls), 1, self.ntfy.seen)
        self.assertIn("poll=1", polls[0])
        self.assert_never_shown(out)
        for row in out.splitlines():
            self.assertNotIn(server.TOKEN, row)

        self.shut = False  # the line ran on the server
        self.assertIn("test push: sent", self.setup_cli())

    def test_a_push_topic_refused_alone_names_its_line(self):
        self.shut = False
        topic = self.c["topic"]
        with mock.patch.object(phone, "send", lambda *a, **k: setattr(phone, "_refused", 403) or False):
            out = self.setup_cli()
        failed = out.split("test push: ", 1)[1]
        self.assertIn(f"`ntfy access ann {topic} rw`", failed)
        self.assertNotIn(self.c["reply_topic"], failed)


if __name__ == "__main__":
    unittest.main()
