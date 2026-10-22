"""A server of your own that demands a token (`auth-default-access: deny-all`): every request and every button
carries it, it is kept like the topics and never shown, and a refusal is told once in plain words."""
import contextlib
import io
import json
import os
import stat
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, doctor, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_phone_backoff import Server  # noqa: E402

TOKEN = "tk_0123456789abcdefghijklmnopqrs"  # an ntfy access token's shape


class Locked(Server):
    """The scripted ntfy of test_phone_backoff, set to deny-all: a request without `Bearer <token>` gets 401 (no
    token) or 403 (another one) and reaches nothing. `seen`: (method, path, Authorization) of every request;
    `bodies`: every accepted push as sent."""

    def __init__(self, token=TOKEN):
        super().__init__()
        self.token, self.seen, self.bodies = token, [], []
        base, fake = self.httpd.RequestHandlerClass, self

        class Guard(base):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.allowed():
                    if self.path == "/":
                        fake.bodies.append(json.loads(body))
                    self.rfile = io.BytesIO(body)
                    self.headers.replace_header("Content-Length", str(len(body)))
                    base.do_POST(self)

            def do_GET(self):
                if self.allowed():
                    base.do_GET(self)

            def allowed(self):
                got = self.headers.get("Authorization")
                fake.seen.append((self.command, self.path, got))
                if got == f"Bearer {fake.token}":
                    return True
                self.send_response(403 if got else 401)
                self.end_headers()
                self.wfile.write(b'{"code":40301,"http":403,"error":"forbidden"}')
                return False

        self.httpd.RequestHandlerClass = Guard


class Base(unittest.TestCase):
    token = TOKEN

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.ntfy = Locked()
        self.addCleanup(self.ntfy.close)
        self.p = Project(self.s.root)
        self.c = {"enabled": True, "server": self.ntfy.url, "topic": phone.new_topic(),
                  "reply_topic": phone.new_topic(), **({"token": self.token} if self.token else {})}
        phone.save_conf(self.c)
        phone.mark_now()
        phone.git_names.cache_clear()
        self.waits = []
        for k, v in (("_last_pump", 0.0), ("_hold_until", 0.0), ("_held", False), ("_refused", 0)):
            self.addCleanup(setattr, phone, k, getattr(phone, k))
            setattr(phone, k, v)
        patch = mock.patch.object(phone, "rest", lambda s, until=lambda: False: self.waits.append(round(s)))
        patch.start()
        self.addCleanup(patch.stop)
        phone.use(self.p)
        self.addCleanup(phone.use, None)

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text() if f.exists() else ""

    def assert_never_shown(self, *texts):
        for text in (self.log(), json.dumps([m for m in self.ntfy.msgs]), *texts):
            self.assertNotIn(TOKEN, text)


class WithToken(Base):
    def test_the_push_and_its_buttons_carry_the_token(self):
        self.assertTrue(phone.push("Done: T-001", "merged", buttons=("Status=status", "Stop=stop")))
        self.assertEqual(self.ntfy.seen, [("POST", "/", f"Bearer {TOKEN}")])
        acts = self.ntfy.bodies[-1]["actions"]
        self.assertEqual([a["headers"] for a in acts], [{"Authorization": f"Bearer {TOKEN}"}] * 2)
        self.assertEqual(self.ntfy.msgs[-1]["message"], "merged")

    def test_a_tap_reaches_the_reply_topic_and_the_stream_reads_it_with_the_token(self):
        phone.push("Done: T-001", "merged", buttons=("Status=status",))
        act = self.ntfy.bodies[-1]["actions"][0]
        tap = urllib.request.Request(act["url"], data=act["body"].encode(), headers=act["headers"], method="POST")
        with urllib.request.urlopen(tap, timeout=5):  # what the phone does on a tap
            pass
        self.ntfy.gets = ["deliver"]
        with mock.patch.object(phone, "running_lines", lambda: "Sandbox: autopilot running"):
            self.assertEqual(phone.listen(once=True, since=0), 1)
        self.assertEqual(self.ntfy.msgs[-1]["title"], "Status")
        gets = [s for s in self.ntfy.seen if s[0] == "GET"]
        self.assertTrue(gets)
        self.assertTrue(all(auth == f"Bearer {TOKEN}" for _, _, auth in self.ntfy.seen), self.ntfy.seen)

    def test_a_held_stream_carries_the_token_too(self):
        self.ntfy.publish(self.c["reply_topic"], "help")
        self.ntfy.gets = ["deliver"]
        heard = []
        phone.listen(until=lambda: bool(heard), since=0, heard=lambda: heard.append(1))
        self.assertEqual(heard, [1])
        self.assertNotIn("poll=1", self.ntfy.seen[0][1])
        self.assertTrue(all(auth == f"Bearer {TOKEN}" for _, _, auth in self.ntfy.seen), self.ntfy.seen)

    def test_the_token_is_censored_and_never_logged(self):
        self.assertTrue(phone.push(f"token {TOKEN}", f"the server said {TOKEN} was fine"))
        self.assertNotIn(TOKEN, json.dumps(self.ntfy.msgs[-1]))
        self.assertIn(TOKEN[:2] + "***", self.ntfy.msgs[-1]["message"])
        self.assert_never_shown()

    def test_doctor_says_own_server_and_token_set_without_either(self):
        rows = doctor.checks(self.p)
        what = [w for _, w, _ in rows if "phone" in w]
        self.assertEqual(what, ["phone pushes and questions (ntfy, own server, token set)"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            doctor.main(self.p)
        self.assertNotIn(self.ntfy.url, out.getvalue())
        self.assert_never_shown(out.getvalue())


class WithoutToken(Base):
    token = None

    def test_nothing_reaches_a_deny_all_server_and_the_refusal_is_told_once(self):
        self.assertFalse(phone.push("Done: T-001", "merged", buttons=("Status=status",)))
        self.assertFalse(phone.push("Done: T-002", "merged"))
        self.assertFalse(phone.listen(once=True))
        self.assertEqual(self.ntfy.msgs, [])
        self.assertTrue(all(auth is None for _, _, auth in self.ntfy.seen))
        self.assertNotIn("headers", json.dumps(self.ntfy.bodies))
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("windvane phone: the server asks for a token (401): windvane phone setup --token TOKEN",
                      lines[0])


class WrongToken(Base):
    token = "tk_somebodyelsestoken000000000000"

    def test_a_403_logs_one_line_and_a_good_push_ends_the_spell(self):
        self.assertFalse(phone.push("Done: T-001", "merged"))
        self.assertFalse(phone.push("Done: T-002", "merged"))
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("windvane phone: the server refused the token (403): windvane phone setup --token TOKEN",
                      lines[0])
        self.assertNotIn(self.token, self.log())
        self.ntfy.token = self.token  # the server takes it again (or the user set the right one)
        self.assertTrue(phone.push("Done: T-003", "merged"))
        self.assertEqual(phone._refused, 0)

    def test_a_refused_stream_says_so_once_and_backs_off(self):
        phone.listen(until=lambda: len(self.waits) >= 3)
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("the server refused the token (403): windvane phone setup --token TOKEN; reconnecting, next "
                      "try in 10 s", lines[0])
        self.assertEqual(self.waits, [10, 20, 40])


class Setup(Base):
    token = None

    def setup_cli(self, *args, stdin=""):
        self.addCleanup(os.chdir, os.getcwd())  # -C moves into the sandbox, which goes away
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO(stdin)):
            cli.main(["-C", str(self.s.root), "phone", "setup", *args, "--no-service"])
        return out.getvalue()

    def test_setup_with_a_token_saves_it_private_and_sends_with_it(self):
        phone.conf_path().unlink()
        out = self.setup_cli("--server", self.ntfy.url, "--token", TOKEN)
        c = phone.conf()
        self.assertEqual(c["token"], TOKEN)
        self.assertEqual(stat.S_IMODE(phone.conf_path().stat().st_mode), 0o600)
        self.assertIn("test push: sent", out)
        self.assertIn("add yours first (Android: Settings, Manage users)", out)
        self.assertEqual(self.ntfy.msgs[-1]["title"], "windvane: phone works")
        self.assert_never_shown(out)

        with contextlib.redirect_stdout(io.StringIO()) as rotated:
            phone.rotate()
        self.assertEqual(phone.conf()["token"], TOKEN, "new topics, the same token")
        self.assertIn("test push: sent", rotated.getvalue())
        self.assert_never_shown(rotated.getvalue())

    def test_token_alone_adds_one_and_keeps_the_topics(self):
        out = self.setup_cli("--token", "-", stdin=TOKEN + "\n")
        c = phone.conf()
        self.assertEqual((c["token"], c["topic"], c["reply_topic"], c["server"]),
                         (TOKEN, self.c["topic"], self.c["reply_topic"], self.ntfy.url))
        self.assertIn("test push: sent", out)
        self.assert_never_shown(out)
        self.setup_cli("--token", "")
        self.assertNotIn("token", phone.conf())
        self.assertNotIn("token", phone.conf_path().read_text())

    def test_a_refused_test_push_says_to_set_the_token(self):
        out = self.setup_cli()
        self.assertIn("test push: FAILED (the server asks for a token (401): windvane phone setup --token TOKEN)",
                      out)
        out = self.setup_cli("--token", "tk_wrong0000000000000000000000000")
        self.assertIn("test push: FAILED (the server refused the token (403): windvane phone setup --token TOKEN)",
                      out)


if __name__ == "__main__":
    unittest.main()
