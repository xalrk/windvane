"""The service's listener waits its turn while a run's stream holds the reply topic, a listener that fell back to
polling goes back to a stream once one gets through, and a run's stream that raises is started again."""
import fcntl
import json
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
from test_phone_backoff import Base, Server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def soon(cond, most=10):
    end = time.time() + most
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class HeldBack(Server):
    """The scripted ntfy behind a proxy that buffers long requests: a stream opens and brings nothing (polls get
    through) until `through` is set; then a stream starts with ntfy's `open` event and stays up on keepalives."""

    def __init__(self):
        super().__init__()
        self.through = False
        base, fake = self.httpd.RequestHandlerClass, self

        class Proxy(base):
            def do_GET(self):
                if "poll=1" in self.path:
                    return base.do_GET(self)
                fake.urls.append(self.path)
                self.send_response(200)
                self.end_headers()
                event = "open"
                while not fake.closing:
                    if fake.through:
                        try:
                            self.wfile.write(json.dumps({"event": event}).encode() + b"\n")
                            self.wfile.flush()
                        except OSError:  # the listener gave up on a stream held back earlier
                            return
                        event = "keepalive"
                    time.sleep(0.05)

        self.httpd.RequestHandlerClass = Proxy


class Lock(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))
        self.held = open(phone.state_dir() / "listen.lock", "w")
        self.addCleanup(self.held.close)
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_the_listener_waits_for_the_lock_and_takes_over_when_it_is_released(self):
        stop = threading.Event()
        self.addCleanup(stop.set)
        out = []
        t = threading.Thread(target=lambda: out.append(phone.listen(until=stop.is_set)), daemon=True)
        t.start()
        time.sleep(1.5)
        self.assertTrue(t.is_alive(), "it waits, it doesn't exit")
        self.assertEqual(self.ntfy.urls, [])
        self.assertEqual(len(self.log()), 1, self.log())
        self.assertIn("another reader (a run's own stream) holds the reply topic; listening once it lets go",
                      self.log()[0])
        self.held.close()  # the run's stream ends
        self.assertTrue(soon(lambda: self.ntfy.urls), "it opens a stream once the lock is free")
        self.assertNotIn("poll=1", self.ntfy.urls[0])
        self.assertIn("the reply topic is free; listening", self.log()[-1])
        self.ntfy.publish(self.c["reply_topic"], "status")
        self.assertTrue(soon(lambda: any(m.get("title") == "Status" for m in self.ntfy.msgs)), self.ntfy.msgs)
        stop.set()
        self.ntfy.closing = True
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertNotIn("already listening", "\n".join(self.log()))

    def test_the_service_command_waits_and_a_sigterm_ends_it_at_once(self):
        proc = subprocess.Popen([sys.executable, "-m", "windvane", "phone", "listen"], cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(2)
        self.assertIsNone(proc.poll(), "it waits instead of exiting for systemd to start it again")
        self.held.close()
        self.assertTrue(soon(lambda: self.ntfy.urls), "it takes over once the lock is free")
        t = time.time()
        proc.send_signal(signal.SIGTERM)
        proc.wait(5)
        self.assertLess(time.time() - t, 2)
        err = proc.stderr.read()
        proc.stderr.close()
        self.assertIn("holds the reply topic; listening once it lets go", err)
        self.assertNotIn("already listening", err)

    def test_a_run_s_stream_and_once_still_give_up_at_once(self):
        self.assertIs(phone.listen(once=True), False)
        t = time.time()
        self.assertIs(phone.listen(until=lambda: False, heard=lambda: None), False)
        self.assertLess(time.time() - t, 0.5)
        self.assertEqual(self.ntfy.urls, [])
        self.assertEqual(self.log(), [])


class BackToStream(Base):
    def test_a_polling_listener_returns_to_a_stream_once_one_gets_through(self):
        self.ntfy.close()
        self.ntfy = HeldBack()
        self.addCleanup(self.ntfy.close)
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))
        phone.save_conf({**self.c, "server": self.ntfy.url})
        polls = []
        stop = threading.Event()
        self.addCleanup(stop.set)

        def rest(s, until=lambda: False):
            polls.append(s)
            time.sleep(0.02)

        streams = lambda: [u for u in self.ntfy.urls if "poll=1" not in u]  # noqa: E731
        with mock.patch.object(phone, "rest", rest), mock.patch.object(phone, "STREAM_WAIT", 0.3), \
                mock.patch.object(phone, "RESTREAM", 0.5):
            t = threading.Thread(target=phone.listen, kwargs={"until": stop.is_set}, daemon=True)
            t.start()
            self.assertTrue(soon(lambda: len(streams()) >= 2), "a stream is tried again while polling")
            self.assertTrue(polls, "it polled in between")
            self.assertEqual(len(self.log()), 1, "a retry still held back adds no line")
            self.ntfy.through = True
            self.assertTrue(soon(lambda: "gets through again" in "\n".join(self.log())), self.log())
            n = len(polls)
            time.sleep(1)
            self.assertEqual(len(polls), n, "no polls once the stream is back")
            stop.set()
            self.ntfy.closing = True
            t.join(5)
        self.assertIn("brought nothing", self.log()[0])
        self.assertIn("the server's stream gets through again; polling stopped", self.log()[1])
        self.assertEqual(len(self.log()), 2, self.log())


class RunStreamRestarts(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(phone.end_run)
        self.addCleanup(setattr, phone, "_stream_broke", False)

    def test_a_run_s_stream_that_raises_is_started_again_by_the_next_pump_and_told_once(self):
        calls = []

        def listen(until, since, heard):
            calls.append(1)
            if len(calls) <= 2:
                raise RuntimeError("boom")
            while not until():
                time.sleep(0.05)

        with mock.patch.object(phone, "listen", listen), mock.patch.object(phone, "PUMP_EVERY", 0.1):
            phone.start_run(self.p)
            for n in (2, 3):
                self.assertTrue(soon(lambda: (phone.pump(), len(calls) >= n)[1]), calls)
            self.assertTrue(phone._stream.is_alive(), "the third stream runs")
            phone.end_run()
        self.assertEqual(len(calls), 3)
        lines = [x for x in self.log() if "the run's stream stopped" in x]
        self.assertEqual(len(lines), 1, self.log())
        self.assertIn("the run's stream stopped (RuntimeError); it starts again within 0 s", lines[0])


if __name__ == "__main__":
    unittest.main()
