"""A try at the stream that gets through is opened again with STREAM_WAIT, so a keepalive a little late doesn't drop
it; a listener started again behind a proxy falls back to polling after one silent stream; the proxy line names the
fix and the troubleshooting entry it points to exists."""
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
from test_docs import prose, slug  # noqa: E402
from test_phone_backoff import Base, Server  # noqa: E402
from test_phone_listen_lock import soon  # noqa: E402

DOCS = Path(__file__).resolve().parents[1] / "docs"


class SlowKeepalive(Server):
    """The scripted ntfy behind a proxy that buffers long requests until `through` is set; then a stream starts with
    ntfy's `open` event and sends a keepalive every `every` seconds (polls always get through). `opened`: the streams
    that got their `open` event."""

    def __init__(self, every):
        super().__init__()
        self.through, self.opened = False, []
        base, fake = self.httpd.RequestHandlerClass, self

        class Proxy(base):
            def do_GET(self):
                if "poll=1" in self.path:
                    return base.do_GET(self)
                fake.urls.append(self.path)
                mine = len(fake.urls)
                self.send_response(200)
                self.end_headers()
                event, sent = "open", 0.0
                while not fake.closing:
                    newest = mine == len(fake.urls)  # the listener has one stream; older ones it gave up on
                    if fake.through and newest and time.time() - sent >= (0 if event == "open" else every):
                        try:
                            self.wfile.write(json.dumps({"event": event}).encode() + b"\n")
                            self.wfile.flush()
                        except OSError:  # the listener closed this stream
                            return
                        if event == "open":
                            fake.opened.append(self.path)
                        event, sent = "keepalive", time.time()
                    time.sleep(0.02)

        self.httpd.RequestHandlerClass = Proxy


class TryThatGetsThrough(Base):
    def test_a_keepalive_later_than_try_wait_keeps_the_stream(self):
        self.ntfy.close()
        self.ntfy = SlowKeepalive(every=0.45)  # past TRY_WAIT's 0.3 s, well inside STREAM_WAIT's 1.5 s
        self.addCleanup(self.ntfy.close)
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))
        phone.save_conf({**self.c, "server": self.ntfy.url})
        stop = threading.Event()
        self.addCleanup(stop.set)
        with mock.patch.object(phone, "rest", lambda s, until=lambda: False: time.sleep(s)), \
                mock.patch.object(phone, "STREAM_WAIT", 1.5), mock.patch.object(phone, "TRY_WAIT", 0.3), \
                mock.patch.object(phone, "PUMP_EVERY", 0.1), mock.patch.object(phone, "RESTREAM", 0.2):
            t = threading.Thread(target=phone.listen, kwargs={"until": stop.is_set}, daemon=True)
            t.start()
            self.assertTrue(soon(lambda: "brought nothing" in "\n".join(self.log()), 5), "it falls back to polling")
            self.ntfy.through = True
            self.assertTrue(soon(lambda: "gets through again" in "\n".join(self.log()), 5), self.log())
            time.sleep(2.5)  # five keepalives, each 0.15 s later than a try waits
            stop.set()
            self.ntfy.closing = True
            t.join(5)
        self.assertEqual(len(self.ntfy.opened), 2, "the try, then one stream opened again at once")
        self.assertEqual(self.ntfy.opened[0], self.ntfy.opened[1], "from the same last id: nothing is lost")
        self.assertNotIn("dropped", "\n".join(self.log()))


class Restarted(Base):
    def test_a_listener_started_again_behind_a_proxy_polls_after_one_silent_stream(self):
        """Streams worked for the listener before, in another process; this one has no stream of its own yet."""
        self.ntfy.publish(self.c["reply_topic"], "status")
        self.ntfy.gets = ["deliver", "drop"]
        self.listen_until(1)
        self.assertEqual(self.waits, [phone.RETRY])
        self.waits.clear()
        self.ntfy.gets, before = ["silent", "deliver"], len(self.ntfy.urls)
        with mock.patch.object(phone, "STREAM_WAIT", 0.3):
            self.listen_until(1)
        urls = self.ntfy.urls[before:]
        self.assertNotIn("poll=1", urls[0])
        self.assertTrue(urls[1:] and all("poll=1" in u for u in urls[1:]), urls)
        self.assertEqual(self.waits, [phone.PUMP_EVERY])
        self.assertEqual(sum("brought nothing" in line for line in self.log()), 1, self.log())


class ProxyLine(Base):
    def test_it_names_no_proxy_with_the_server_s_host_and_an_entry_that_exists(self):
        self.ntfy.gets = ["silent", "deliver"]
        with mock.patch.object(phone, "STREAM_WAIT", 0.3):
            self.listen_until(1)
        line = next(x for x in self.log() if "brought nothing" in x)
        self.assertIn("NO_PROXY=127.0.0.1", line)
        page, entry = re.search(r'\((docs/[\w.]+), "([^"]+)"\)', line).groups()
        heads = re.findall(r"^#+ (.+)$", prose((DOCS.parent / page).read_text()), re.M)
        self.assertTrue([h for h in heads if entry in h], f"{page} has no heading with {entry!r}")


class PhoneDocLink(unittest.TestCase):
    def test_phone_md_links_the_entry_not_just_the_page(self):
        target = re.search(r"\]\(troubleshooting\.md#([^)]+)\)", (DOCS / "phone.md").read_text()).group(1)
        heads = re.findall(r"^#+ (.+)$", prose((DOCS / "troubleshooting.md").read_text()), re.M)
        self.assertIn(target, [slug(h) for h in heads])
        self.assertIn("brought nothing", next(h for h in heads if slug(h) == target))


if __name__ == "__main__":
    unittest.main()
