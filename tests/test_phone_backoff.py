"""The phone stays inside the ntfy server's per-visitor budget: dropped streams back off, a 429 holds the pushes for
its Retry-After, and a run with no listener service holds one stream instead of polling every PUMP_EVERY seconds."""
import contextlib
import fcntl
import io
import json
import os
import re
import sys
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import timedelta
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

LIMIT = json.dumps({"code": 42901, "http": 429, "error": "limit reached: too many requests"}).encode()


class Server:
    """A scripted ntfy. Each GET takes the next of `gets` ("drop": closed with no answer, "429:<s>": refused with
    that Retry-After, "silent": headers and then nothing, "deliver": all it has past `since`, then closed); when
    they run out it holds a stream open, sending each new message until `close` (a poll gets what's there and
    ends). Each POST takes the next of `posts` (an HTTP status; 200 when they run out)."""

    def __init__(self):
        self.gets, self.posts, self.urls, self.msgs = [], [], [], []
        self.cond, self.closing = threading.Condition(), False
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                code = fake.posts.pop(0) if fake.posts else 200
                if code != 200:
                    self.send_response(code)
                    self.send_header("Retry-After", "30")
                    self.end_headers()
                    self.wfile.write(LIMIT)
                    return
                m = json.loads(body) if self.path == "/" else {"topic": self.path.strip("/"), "message": body.decode()}
                fake.publish(m["topic"], m.get("message", ""), title=m.get("title"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                fake.urls.append(self.path)
                step = fake.gets.pop(0) if fake.gets else "hold"
                if step == "drop":
                    self.close_connection = True
                    return
                if step.startswith("429:"):
                    self.send_response(429)
                    self.send_header("Retry-After", step[4:])
                    self.end_headers()
                    self.wfile.write(LIMIT)
                    return
                self.send_response(200)
                self.end_headers()
                if step == "silent":
                    while not fake.closing:
                        time.sleep(0.05)
                    return
                url = urllib.parse.urlparse(self.path)
                topics = url.path.strip("/").split("/")[0].split(",")
                since = urllib.parse.parse_qs(url.query).get("since", [""])[0]
                with fake.cond:
                    ids = [m["id"] for m in fake.msgs]
                    start = ids.index(since) + 1 if since in ids else 0 if step == "deliver" else len(ids)
                while True:
                    with fake.cond:
                        new, start = fake.msgs[start:], len(fake.msgs)
                    for m in new:
                        if m["topic"] in topics:
                            self.wfile.write((json.dumps({"event": "message", **m}) + "\n").encode())
                            self.wfile.flush()
                    if step == "deliver" or "poll=1" in self.path or fake.closing:
                        return
                    with fake.cond:
                        fake.cond.wait(0.05)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def publish(self, topic, message, title=None):
        with self.cond:
            self.msgs.append({"id": f"m{len(self.msgs)}", "time": int(time.time()), "topic": topic,
                              "message": message, **({"title": title} if title else {})})
            self.cond.notify_all()

    def close(self):
        self.closing = True
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.ntfy = Server()
        self.addCleanup(self.ntfy.close)
        self.p = Project(self.s.root)
        self.c = {"enabled": True, "server": self.ntfy.url, "topic": phone.new_topic(),
                  "reply_topic": phone.new_topic()}
        phone.save_conf(self.c)
        phone.mark_now()
        phone.git_names.cache_clear()
        self.waits = []
        for k, v in (("_last_pump", 0.0), ("_hold_until", 0.0), ("_held", False), ("_heard", 0)):
            self.addCleanup(setattr, phone, k, getattr(phone, k))
            setattr(phone, k, v)
        patch = mock.patch.object(phone, "rest", lambda s, until=lambda: False: self.waits.append(round(s)))
        patch.start()
        self.addCleanup(patch.stop)
        phone.use(self.p)
        self.addCleanup(phone.use, None)

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text().splitlines() if f.exists() else []

    def listen_until(self, waits):
        phone.listen(until=lambda: len(self.waits) >= waits)


class Reconnects(Base):
    def test_three_drops_in_a_row_wait_10_20_and_40_s_with_one_line(self):
        self.ntfy.gets = ["drop"] * 3
        self.listen_until(3)
        self.assertEqual(self.waits, [10, 20, 40])
        self.assertEqual(len(self.ntfy.urls), 3)
        self.assertEqual(len(self.log()), 1, self.log())
        self.assertIn("the stream dropped (RemoteDisconnected); reconnecting, next try in 10 s", self.log()[0])

    def test_the_wait_stops_doubling_at_five_minutes(self):
        self.ntfy.gets = ["drop"] * 8
        self.listen_until(8)
        self.assertEqual(self.waits, [10, 20, 40, 80, 160, 300, 300, 300])

    def test_a_stream_that_delivers_starts_the_count_again_and_a_new_spell(self):
        self.ntfy.publish(self.c["reply_topic"], "status")
        self.ntfy.gets = ["drop", "drop", "deliver", "drop"]
        self.listen_until(3)
        self.assertEqual(self.waits, [10, 20, 10])
        self.assertEqual(len(self.log()), 2, "one line for each spell of drops")

    def test_a_429_waits_its_retry_after(self):
        self.ntfy.gets = ["429:45", "drop"]
        self.listen_until(2)
        self.assertEqual(self.waits, [45, 20])
        self.assertIn("refused the stream (429, limit reached: too many requests); reconnecting, next try in 45 s",
                      self.log()[0])

    def test_streams_that_bring_nothing_fall_back_to_polling(self):
        """A proxy that holds a stream back: the stream opens and stays silent. Polls get through, at PUMP_EVERY."""
        self.ntfy.gets = ["silent", "deliver", "deliver"]
        with mock.patch.object(phone, "STREAM_WAIT", 0.3):
            self.listen_until(2)
        self.assertNotIn("poll=1", self.ntfy.urls[0])
        self.assertTrue(all("poll=1" in u for u in self.ntfy.urls[1:]), self.ntfy.urls)
        self.assertEqual(self.waits, [phone.PUMP_EVERY] * 2)
        self.assertIn("the server's stream opened but brought nothing in 0 s: check for a proxy or VPN that buffers "
                      "long requests (`env | grep -i proxy`, a company gateway). Asking every 15 s instead, and "
                      "trying a stream again every 30 min", self.log()[0])


class Pushes(Base):
    def test_a_429_holds_the_next_push_for_its_retry_after_and_is_logged_once(self):
        self.ntfy.posts = [429, 429]
        self.assertFalse(phone.push("Done: T-001", "merged"))
        self.assertFalse(phone.push("Done: T-002", "merged"))
        self.assertTrue(phone.push("Done: T-003", "merged"))
        self.assertEqual(self.waits, [30, 30])
        self.assertEqual([m["message"] for m in self.ntfy.msgs], ["merged"])
        self.assertEqual(len(self.log()), 1, self.log())
        self.assertIn("refused a push (429, limit reached: too many requests); the next push waits 30 s. A server of "
                      "your own lifts its limits: windvane phone setup --server URL", self.log()[0])

    def test_a_hold_past_the_pushs_wait_fails_it_without_asking_the_server(self):
        self.ntfy.posts = [429]
        with mock.patch.object(phone, "retry_after", lambda e, default: 3600):
            self.assertFalse(phone.push("Done: T-001", "merged"))
        self.assertFalse(phone.push("Done: T-002", "merged"))
        self.assertEqual(self.waits, [])
        self.assertEqual(self.ntfy.msgs, [])
        self.assertRegex(self.log()[0], r"pushes until \d\d:\d\d are skipped, and questions asked again after\.")

    def test_a_push_that_goes_out_ends_the_spell(self):
        self.ntfy.posts = [429]
        phone.push("Done: T-001", "merged")
        phone.push("Done: T-002", "merged")
        self.ntfy.posts = [429]
        phone.push("Done: T-003", "merged")
        self.assertEqual(len(self.log()), 2, self.log())


class RunStream(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(phone.end_run)  # the server's streams end first (cleanups run last first), then the run's
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))

    def pump_until(self, cond, most=5):
        end = time.time() + most
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_a_run_holds_one_stream_and_an_answer_reaches_the_waiting_question(self):
        with mock.patch.object(phone, "PUMP_EVERY", 0.1):
            phone.start_run(self.p)
            self.assertIs(phone.ask("t-001", "Which cache?", ["New", "Old"]), True)
            self.assertTrue(self.pump_until(lambda: self.ntfy.urls))
            time.sleep(0.2)  # the stream has taken its start
            self.ntfy.publish(self.c["reply_topic"], "answer t-001 B")
            self.assertTrue(self.pump_until(lambda: phone.pump() > 0), "the waiting run woke up for the answer")
            self.assertEqual(phone.answer("t-001"), "ANSWER B\tOld")
            time.sleep(0.5)  # five PUMP_EVERYs: a poll each would show
            phone.pump()
        self.assertEqual(len(self.ntfy.urls), 1, self.ntfy.urls)
        self.assertNotIn("poll=1", self.ntfy.urls[0])
        self.ntfy.closing = True
        phone.end_run()
        self.assertFalse(phone._stream.is_alive(), "the stream ends with its run")

    def test_a_run_leaves_the_replies_to_a_listener_that_holds_the_lock(self):
        lock = open(phone.state_dir() / "listen.lock", "w")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(phone, "PUMP_EVERY", 0.1):
            phone.start_run(self.p)
            self.pump_until(lambda: phone.pump() and False, most=0.5)
        self.assertEqual(self.ntfy.urls, [])



class ARun(Base):
    """The run itself, in the sandbox: no listener service, a worker's question goes to the phone, the phone
    answers B, and the run builds B having made one request to the reply topic."""

    def test_a_run_holds_one_stream_and_wakes_for_the_phones_answer(self):
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.publish()
        for k, v in (("NAP", 0.2),):
            self.addCleanup(setattr, driver, k, getattr(driver, k))
            setattr(driver, k, v)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, FAKE_WORKER="question"))
        stack.enter_context(mock.patch.object(phone, "PUMP_EVERY", 0.1))  # a poll each 0.1 s would show
        done = threading.Event()
        self.addCleanup(done.set)
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))
        stuck = threading.Timer(60, lambda: (self.p.state / "STOP").write_text(""))  # no answer: end, don't hang
        stuck.daemon = True
        stuck.start()
        self.addCleanup(stuck.cancel)

        def phone_user():  # what tapping B does: the button posts "answer <id> B" to the reply topic
            answered = set()
            while not done.is_set():
                for m in list(self.ntfy.msgs):
                    q = re.match(r"Question \((.+)\)", m.get("title", ""))
                    if q and m["id"] not in answered:
                        answered.add(m["id"])
                        self.ntfy.publish(self.c["reply_topic"], f"answer {q.group(1)} B")
                time.sleep(0.1)
        threading.Thread(target=phone_user, daemon=True).start()
        pilot = driver.Pilot(self.p, args(answer_wait=60))
        t = time.time()
        pilot.run(now() + timedelta(hours=1))
        took = time.time() - t
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertIn("option B (blue)", [c["prompt"] for c in calls if "worker" in c["system"]][1])
        self.assertLess(took, 50, "the run woke for the answer, not after its wait")
        self.assertEqual(len(self.ntfy.urls), 1, self.ntfy.urls)
        self.assertNotIn("poll=1", self.ntfy.urls[0])
        self.assertEqual(self.waits, [], "the stream never dropped")


if __name__ == "__main__":
    unittest.main()
