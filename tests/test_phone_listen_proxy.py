"""A proxy that turns up after the listener's stream worked is still caught, a try at the stream while polling
doesn't hold an answer back for a whole STREAM_WAIT, and a waiting listener's line follows the lock's holder."""
import fcntl
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import phone  # noqa: E402
from test_phone_backoff import Base  # noqa: E402
from test_phone_listen_lock import HeldBack, soon  # noqa: E402


class LateProxy(Base):
    def test_two_silent_streams_after_one_that_delivered_fall_back_to_polling_with_one_line(self):
        self.ntfy.publish(self.c["reply_topic"], "status")
        self.ntfy.gets = ["deliver", "silent", "silent"]
        with mock.patch.object(phone, "STREAM_WAIT", 0.3):
            self.listen_until(3)
        self.assertTrue(all("poll=1" not in u for u in self.ntfy.urls[:3]), self.ntfy.urls)
        self.assertTrue(self.ntfy.urls[3:] and all("poll=1" in u for u in self.ntfy.urls[3:]), self.ntfy.urls)
        self.assertEqual(self.waits, [phone.RETRY, phone.PUMP_EVERY, phone.PUMP_EVERY])
        self.assertEqual(sum("brought nothing" in line for line in self.log()), 1, self.log())
        self.assertIn("the stream dropped (TimeoutError); reconnecting, next try in 10 s", self.log()[0])

    def test_one_silent_stream_between_working_ones_is_a_hiccup(self):
        self.ntfy.publish(self.c["reply_topic"], "status")
        self.ntfy.gets = ["deliver", "silent", "deliver", "silent", "drop"]
        with mock.patch.object(phone, "STREAM_WAIT", 0.3):
            self.listen_until(3)
        self.assertFalse([u for u in self.ntfy.urls if "poll=1" in u], self.ntfy.urls)
        self.assertNotIn("brought nothing", "\n".join(self.log()))


class TryWhilePolling(Base):
    def test_an_answer_sent_during_a_try_is_handled_within_a_poll_and_the_try_s_wait(self):
        self.ntfy.close()
        self.ntfy = HeldBack()
        self.addCleanup(self.ntfy.close)
        self.addCleanup(lambda: setattr(self.ntfy, "closing", True))
        self.ntfy.gets = ["deliver"] * 1000  # polls get everything past `since`
        phone.save_conf({**self.c, "server": self.ntfy.url})
        stop = threading.Event()
        self.addCleanup(stop.set)
        streams = lambda: [u for u in self.ntfy.urls if "poll=1" not in u]  # noqa: E731
        status = lambda: any(m.get("title") == "Status" for m in self.ntfy.msgs)  # noqa: E731
        with mock.patch.object(phone, "rest", lambda s, until=lambda: False: time.sleep(s)), \
                mock.patch.object(phone, "STREAM_WAIT", 3), mock.patch.object(phone, "TRY_WAIT", 0.4), \
                mock.patch.object(phone, "PUMP_EVERY", 0.2), mock.patch.object(phone, "RESTREAM", 0.3):
            t = threading.Thread(target=phone.listen, kwargs={"until": stop.is_set}, daemon=True)
            t.start()
            self.assertTrue(soon(lambda: "brought nothing" in "\n".join(self.log())), "it falls back to polling")
            n = len(streams())
            self.assertTrue(soon(lambda: len(streams()) > n, 3), "it tries a stream again")
            sent = time.time()
            self.ntfy.publish(self.c["reply_topic"], "status")
            self.assertTrue(soon(status, 5))
            took = time.time() - sent
            stop.set()
            self.ntfy.closing = True
            t.join(5)
        self.assertLess(took, 0.2 + 0.4 + 0.4, "one PUMP_EVERY plus TRY_WAIT (and slack), not STREAM_WAIT's 3 s")
        self.assertEqual(sum("brought nothing" in line for line in self.log()), 1, self.log())


class Holder(Base):
    def setUp(self):
        super().setUp()
        self.held = open(phone.state_dir() / "listen.lock", "w")
        self.addCleanup(self.held.close)
        fcntl.flock(self.held, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def hold_as(self, name):
        self.held.seek(0)
        self.held.truncate()
        self.held.write(name)
        self.held.flush()

    def test_a_waiting_listener_names_each_new_holder_once(self):
        self.hold_as("4242 run\n")
        stop = threading.Event()
        self.addCleanup(stop.set)
        lock = open(phone.state_dir() / "listen.lock", "a+")
        self.addCleanup(lock.close)
        t = threading.Thread(target=phone.take, args=(lock, "listen", stop.is_set), daemon=True)
        t.start()
        self.assertTrue(soon(lambda: self.log(), 3))
        self.hold_as("")  # a third reader between truncating the file and writing its name
        time.sleep(1.2)
        self.hold_as("4343 listen\n")
        self.assertTrue(soon(lambda: len(self.log()) == 2, 3), self.log())
        time.sleep(1.2)
        self.assertEqual(len(self.log()), 2, "the same holder isn't named again")
        self.held.close()
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertIn("a run reads the phone's replies on its own stream", self.log()[0])
        self.assertIn("another `windvane phone listen` (pid 4343) already reads the phone's replies", self.log()[1])
        self.assertIn("the reply topic is free; listening", self.log()[2])
        self.assertEqual(len(self.log()), 3, self.log())


if __name__ == "__main__":
    unittest.main()
