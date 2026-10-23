"""What the stream reopened after a try that got through does next: a 429 waits its Retry-After and streams again, a
drop reconnects with the backoff (not a new RESTREAM wait), and a proxy back in the way falls back to polling after
one silent stream and says so again. Run as the service, the proxy line says where its NO_PROXY goes."""
import json
import os
import socket
import struct
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
from test_phone_listen_lock import soon  # noqa: E402


class Scripted(Server):
    """The scripted ntfy behind a proxy: each stream (polls always get through) takes the next of `streams`:
    "silent" (headers, then held back), "through" (ntfy's `open` event, then a keepalive every 0.1 s), "drop" (the
    `open` event, then the connection reset) or "429:<s>". `seen`: (time, path) of every GET."""

    def __init__(self, streams):
        super().__init__()
        self.streams, self.seen = list(streams), []
        base, fake = self.httpd.RequestHandlerClass, self

        class Proxy(base):
            def do_GET(self):
                fake.seen.append((time.time(), self.path))
                if "poll=1" in self.path:
                    return base.do_GET(self)
                step = fake.streams.pop(0) if fake.streams else "through"
                if step.startswith("429:"):
                    fake.gets = [step]
                    return base.do_GET(self)
                self.send_response(200)
                self.end_headers()
                event = "open"
                while not fake.closing:
                    if step != "silent":
                        try:
                            self.wfile.write(json.dumps({"event": event}).encode() + b"\n")
                            self.wfile.flush()
                        except OSError:  # the listener closed this stream
                            return
                        if step == "drop":
                            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                            self.connection.close()
                            return
                        event = "keepalive"
                    time.sleep(0.1)

        self.httpd.RequestHandlerClass = Proxy

    def streamed(self):
        return [(t, u) for t, u in self.seen if "poll=1" not in u]

    def polls_after(self, t):
        return [u for when, u in self.seen if when > t and "poll=1" in u]


class AfterATry(Base):
    def reopen(self, *then, until):
        """A listener behind a proxy falls back to polling, a try gets through, and the reopened stream meets `then`;
        the listener runs until `until(fake)`. RESTREAM is 0.2 s until the try got through, then 60 s, so a wait for
        it shows as a stall."""
        self.ntfy.close()
        self.ntfy = Scripted(["silent", "through", *then])
        self.addCleanup(self.ntfy.close)
        phone.save_conf({**self.c, "server": self.ntfy.url})
        stop = threading.Event()
        self.addCleanup(stop.set)
        with mock.patch.object(phone, "rest", lambda s, until=lambda: False: time.sleep(s)), \
                mock.patch.object(phone, "STREAM_WAIT", 0.5), mock.patch.object(phone, "TRY_WAIT", 0.3), \
                mock.patch.object(phone, "PUMP_EVERY", 0.1), mock.patch.object(phone, "RESTREAM", 0.2), \
                mock.patch.object(phone, "RETRY", 0.2):
            t = threading.Thread(target=phone.listen, kwargs={"until": stop.is_set}, daemon=True)
            t.start()
            self.assertTrue(soon(lambda: "gets through again" in "\n".join(self.log()), 5), self.log())
            phone.RESTREAM = 60
            self.tried = time.time()
            done = soon(lambda: until(self.ntfy), 6)
            stop.set()
            self.ntfy.closing = True
            t.join(5)
        self.assertTrue(done, (self.ntfy.seen, self.log()))
        return self.ntfy.streamed()

    def test_a_429_waits_its_retry_after_and_streams_again_without_polling(self):
        streams = self.reopen("429:2", until=lambda f: len(f.streamed()) >= 4)
        gap = streams[3][0] - streams[2][0]
        self.assertGreater(gap, 1.9)
        self.assertLess(gap, 3)
        self.assertEqual(self.ntfy.polls_after(self.tried), [])
        self.assertIn("the server refused the stream (429, limit reached: too many requests); reconnecting, next try "
                      "in 2 s", "\n".join(self.log()))

    def test_a_dropped_stream_reconnects_with_the_backoff_not_a_restream_wait(self):
        streams = self.reopen("drop", until=lambda f: len(f.streamed()) >= 4)
        self.assertLess(streams[3][0] - streams[2][0], 1.5)
        self.assertEqual(self.ntfy.polls_after(self.tried), [])
        self.assertIn("the stream dropped (ConnectionResetError); reconnecting, next try in 0 s", "\n".join(self.log()))

    def test_a_reopened_stream_that_goes_silent_polls_after_it_and_says_the_proxy_line_again(self):
        streams = self.reopen("silent", until=lambda f: len(f.polls_after(self.tried)) >= 2)
        self.assertEqual(len(streams), 3, "the silent stream, the try, the reopened one; then only polls")
        lines = self.log()
        self.assertEqual(sum("brought nothing" in x for x in lines), 2, lines)
        self.assertNotIn("reconnecting", "\n".join(lines), "one silent stream is enough after a try")


class ProxyLineAsService(Base):
    def proxy_line(self, env):
        self.ntfy.gets = ["silent", "deliver"]
        with mock.patch.object(phone, "STREAM_WAIT", 0.3), mock.patch.dict(os.environ, env):
            os.environ.pop("INVOCATION_ID", None) if not env else None
            self.listen_until(1)
        return next(x for x in self.log() if "brought nothing" in x)

    def test_the_service_s_line_says_where_no_proxy_goes(self):
        line = self.proxy_line({"INVOCATION_ID": "0123456789abcdef"})
        self.assertIn("NO_PROXY=127.0.0.1 lets it bypass that, set for the service with "
                      "`systemctl --user edit windvane-phone` (docs/troubleshooting.md", line)
        self.assertLess(len(line.encode()), phone.MOST)

    def test_run_by_hand_it_stays_as_it_was(self):
        line = self.proxy_line({})
        self.assertIn("NO_PROXY=127.0.0.1 lets it bypass that (docs/troubleshooting.md", line)
        self.assertNotIn("systemctl", line)


if __name__ == "__main__":
    unittest.main()
