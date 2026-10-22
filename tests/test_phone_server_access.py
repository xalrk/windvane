"""A server of your own grants windvane's token only its two topics: setup prints the exact `ntfy access` lines for
them (and what an iPhone needs from the server) at the terminal, and never pushes or logs a topic."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
import test_phone_server as server  # noqa: E402


UNGRANTED = ("the server hasn't let the token's user use windvane's topics (403): run the two `ntfy access` lines "
             "`windvane phone setup` prints")
RETRY = "Once they have run on the server, `windvane phone setup` again sends the test push."


class Access(server.Base):
    token = None
    setup_cli = server.Setup.setup_cli

    def grants(self, c):
        return [f"  ntfy access <user> {c['topic']} rw", f"  ntfy access <user> {c['reply_topic']} rw"]

    def assert_topics_unsent(self, c):
        """No topic in what a push says (a push goes *to* its topic, and its buttons post to the reply one) or logs."""
        sent = json.dumps([(m.get("title"), m.get("message")) for m in self.ntfy.msgs]) + self.log()
        self.assertTrue(self.ntfy.msgs or self.log(), "something was pushed or logged to look in")
        for topic in (c["topic"], c["reply_topic"]):
            self.assertNotIn(topic, sent)

    def test_setup_with_a_token_prints_the_grants_for_its_own_topics_and_the_iphone_hint(self):
        phone.conf_path().unlink()
        out = self.setup_cli("--server", self.ntfy.url, "--token", server.TOKEN)
        c = phone.conf()
        lines = out.splitlines()
        for grant in self.grants(c):
            self.assertIn(grant, lines)
        self.assertNotIn('"*"', out)
        self.assertEqual(sum("ntfy access" in line for line in lines), 2, out)
        self.assertIn("On an iPhone, pushes come only if the server's server.yml sets upstream-base-url", out)
        self.assertLess(out.index("ntfy access"), out.index("test push:"), "granted before the test push is judged")
        self.assertIn("test push: sent", out)
        self.assert_topics_unsent(c)
        self.assert_never_shown(out)

    def test_a_test_push_the_server_refuses_says_to_grant_and_run_setup_again(self):
        self.ntfy.token = "tk_notgrantedyet00000000000000000"  # the topics aren't the token's yet
        out = self.setup_cli("--token", server.TOKEN)
        self.assertIn(f"test push: FAILED ({UNGRANTED})\n{RETRY}\n", out)
        self.assertNotIn("--token", out, "the token is fine: nothing says to change it")
        self.assert_topics_unsent(phone.conf())

    def test_rotate_refused_for_its_new_topics_says_to_grant_them(self):
        self.setup_cli("--token", server.TOKEN)
        self.ntfy.token = "tk_notgrantedyet00000000000000000"  # the new topics aren't granted yet
        with contextlib.redirect_stdout(io.StringIO()) as rotated:
            phone.rotate()
        out = rotated.getvalue()
        self.assertIn(f"test push: FAILED ({UNGRANTED})\n{RETRY}\n", out)
        self.assertLess(out.index("ntfy access"), out.index("test push:"))

    def test_a_token_the_server_doesnt_know_still_says_to_change_it(self):
        self.assertEqual(phone.refused(401, {"token": server.TOKEN}),
                         "the server refused the token (401): windvane phone setup --token -")
        self.assertEqual(phone.refused(403, {}), "the server asks for a token (403): windvane phone setup --token -")

    def test_rotate_prints_the_grants_for_the_new_topics(self):
        self.setup_cli("--token", server.TOKEN)
        with contextlib.redirect_stdout(io.StringIO()) as rotated:
            phone.rotate()
        c = phone.conf()
        self.assertNotEqual(c["topic"], self.c["topic"])
        for grant in self.grants(c):
            self.assertIn(grant, rotated.getvalue().splitlines())
        self.assertNotIn(self.c["topic"], rotated.getvalue())
        self.assert_topics_unsent(c)

    def test_no_token_no_grants_and_no_iphone_line(self):
        out = self.setup_cli()  # the deny-all fake refuses it: no token, so nothing to grant
        self.assertNotIn("ntfy access", out)
        self.assertNotIn("upstream-base-url", out)


if __name__ == "__main__":
    unittest.main()
