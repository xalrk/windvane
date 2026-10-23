"""phone.toml's read-edit-write is one locked step, so a rotate and a setup at once can't write back a file read before
the other's write and lose the old topics; and `setup --revoked` drops only the old topics the server now refuses."""
import contextlib
import io
import random
import subprocess
import sys
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
import test_phone_server as server  # noqa: E402
import test_phone_rotate_old as rotate_old  # noqa: E402


def at_once(*calls):
    """Run `calls` in threads started together; re-raise the first error any of them hit."""
    start, errors = threading.Barrier(len(calls)), []

    def run(call):
        start.wait()
        try:
            call()
        except BaseException as e:  # noqa: BLE001: SystemExit too
            errors.append(e)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    if errors:
        raise errors[0]


class Lock(server.Base):
    def slow_writes(self):
        """A slow disk: each write waits a moment first, so another command's read can land before it."""
        save = phone.save_conf
        patch = mock.patch.object(phone, "save_conf", lambda c: time.sleep(random.uniform(0, 0.02)) or save(c))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_rotate_and_a_setup_at_once_never_lose_the_old_topics(self):
        self.slow_writes()
        rotated = []
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(25):
                c = phone.conf()
                rotated += [c["topic"], c["reply_topic"]]
                at_once(phone.rotate, lambda: phone.setup(service=False, token=server.TOKEN))
                c = tomllib.loads(phone.conf_path().read_text())  # parses: no half-written file moved in
                self.assertEqual(c["revoke"], rotated)
                self.assertEqual(c["token"], server.TOKEN)
                self.assertNotIn(c["topic"], rotated)
        self.assertEqual(sorted(p.name for p in phone.conf_path().parent.iterdir()), ["phone.toml", "phone.toml.lock"])

    def test_two_writes_at_once_each_use_their_own_temp_file(self):
        def writes():
            for _ in range(50):
                phone.save_conf(self.c)

        at_once(writes, writes)
        self.assertEqual(tomllib.loads(phone.conf_path().read_text())["topic"], self.c["topic"])
        self.assertEqual([p.name for p in phone.conf_path().parent.glob("*.tmp")], [])

    def test_a_rotate_in_another_process_waits_for_the_lock(self):
        before = phone.conf_path().read_text()
        with phone.conf_lock():
            rotate = subprocess.Popen(["windvane", "phone", "rotate"], stdout=subprocess.DEVNULL)
            self.addCleanup(rotate.kill)
            with self.assertRaises(subprocess.TimeoutExpired):
                rotate.wait(1.5)
            self.assertEqual(phone.conf_path().read_text(), before)
        self.assertEqual(rotate.wait(30), 0)
        self.assertEqual(phone.conf()["revoke"], [self.c["topic"], self.c["reply_topic"]])


class Revoked(server.Base):
    setup_cli, refuse = server.Setup.setup_cli, rotate_old.Old.refuse

    def setUp(self):
        super().setUp()
        self.ntfy.username = "ann"
        self.old = self.c["topic"], self.c["reply_topic"]

    def rotate(self):
        with contextlib.redirect_stdout(io.StringIO()):
            phone.rotate()

    def test_only_the_topic_the_server_refuses_is_dropped(self):
        self.rotate()
        gone, kept = self.old
        self.refuse(gone)
        out = self.setup_cli("--revoked")
        self.assertEqual(phone.conf()["revoke"], [kept])
        line, = [row for row in out.splitlines() if "--reset" in row]
        self.assertTrue(line.startswith("old topics: the server still lets ann read 1 of them"), line)
        self.assertIn(f"`ntfy access --reset ann {kept}`", line)
        self.assertNotIn(gone, out)
        self.assertIn("old topics: 1 taken back on the server (403), dropped from phone.toml", out)
        self.assertIn("test push: sent", out)

        self.refuse(kept)  # its line ran too
        out = self.setup_cli("--revoked")
        self.assertNotIn("revoke", phone.conf())
        self.assertNotIn("--reset", out)

    def test_an_unreachable_server_keeps_every_old_topic(self):
        self.rotate()
        self.ntfy.close()
        out = self.setup_cli("--revoked")
        self.assertEqual(phone.conf()["revoke"], list(self.old))
        self.assertIn("old topics: 2 kept in phone.toml, as the server couldn't say they were taken back "
                      "(no answer): `windvane phone setup --revoked` checks again", out)
        self.assertIn("test push: FAILED", out)

    def test_a_server_that_never_answers_a_poll_still_lets_setup_end(self):
        self.rotate()
        allowed, release = self.ntfy.httpd.RequestHandlerClass.allowed, threading.Event()
        self.addCleanup(release.set)

        def hangs(handler):  # takes the connection, then says nothing
            if handler.command == "GET" and "poll=1" in handler.path:
                release.wait(60)
                return False
            return allowed(handler)

        patch = mock.patch.object(self.ntfy.httpd.RequestHandlerClass, "allowed", hangs)
        patch.start()
        self.addCleanup(patch.stop)
        began = time.monotonic()
        out = self.setup_cli("--revoked")
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(phone.conf()["revoke"], list(self.old))
        self.assertIn("(no answer)", out)
        self.assertIn("test push: sent", out)


if __name__ == "__main__":
    unittest.main()
