"""The built-in phone (phone.py) against a local fake ntfy server: pushes, questions, answers and commands."""
import contextlib
import io
import json
import os
import re
import stat
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import Usage  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

EMAIL = "jane.roe" + "@" + "example.com"


class FakeNtfy:
    """Enough of ntfy: publish as JSON to / or as a body to /<topic>; read /<t1,t2>/json?since=...[&poll=1], a
    stream that stays open a moment for new messages (like ntfy's keepalive stream) unless polled."""

    def __init__(self):
        self.msgs, self.cond = [], threading.Condition()
        self.reading = threading.Event()  # a reader asked for messages: a listener started has taken its start time
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/":
                    fake.publish(**json.loads(body))
                else:
                    fake.publish(self.path.strip("/"), body.decode(), title=self.headers.get("Title"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                topics = url.path.strip("/").split("/")[0].split(",")
                q = urllib.parse.parse_qs(url.query)
                since, poll = q.get("since", ["all"])[0], "poll" in q
                fake.reading.set()
                self.send_response(200)
                self.end_headers()
                with fake.cond:
                    ids = [m["id"] for m in fake.msgs]
                    if since == "all":
                        start = 0
                    elif since.isdigit():
                        start = next((i for i, m in enumerate(fake.msgs) if m["time"] >= int(since)), len(ids))
                    else:
                        start = ids.index(since) + 1 if since in ids else len(ids)
                end = time.time() + (0 if poll else 0.5)
                while True:
                    with fake.cond:
                        new, start = fake.msgs[start:], len(fake.msgs)
                    for m in new:
                        if m["topic"] in topics:
                            self.wfile.write((json.dumps({"event": "message", **m}) + "\n").encode())
                            self.wfile.flush()
                    if time.time() >= end:
                        return
                    with fake.cond:
                        fake.cond.wait(0.1)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def publish(self, topic, message="", **kw):
        with self.cond:
            self.msgs.append({"id": f"m{len(self.msgs)}", "time": int(time.time()), "topic": topic,
                              "message": message, **{k: v for k, v in kw.items() if v is not None}})
            self.cond.notify_all()

    def on(self, topic):
        with self.cond:
            return [m for m in self.msgs if m["topic"] == topic]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def post(url, body):
    """What the phone does: a button posts its command to the reply topic; typing does the same."""
    urllib.request.urlopen(urllib.request.Request(url, data=body.encode(), method="POST"), timeout=5).close()


class PhoneTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.ntfy = FakeNtfy()
        self.addCleanup(self.ntfy.close)
        self.p = Project(self.s.root)
        self.c = {"enabled": True, "server": self.ntfy.url, "topic": phone.new_topic(),
                  "reply_topic": phone.new_topic()}
        phone.save_conf(self.c)
        phone.mark_now()  # as `phone setup` does: else `listen` marks its own start, missing a reply a second older
        phone.git_names.cache_clear()  # git's name comes from this sandbox's HOME
        for k, v in (("PUMP_EVERY", 0), ("RETRY", 0.1), ("_last_pump", 0.0)):
            self.addCleanup(setattr, phone, k, getattr(phone, k))
            setattr(phone, k, v)

    def pushes(self):
        return self.ntfy.on(self.c["topic"])

    def phone_sends(self, text):
        post(f"{self.ntfy.url}/{self.c['reply_topic']}", text)

    def press(self, push, label):
        a = next(a for a in push["actions"] if a["label"] == label)
        post(a["url"], a["body"])


class Pushes(PhoneTest):
    def test_a_push_carries_its_buttons_to_the_reply_topic(self):
        self.assertTrue(phone.push("Done: T-001", "merged", 4, "tada", ("Status=status", "Stop=stop")))
        m = self.pushes()[-1]
        self.assertEqual((m["title"], m["message"], m["priority"], m["tags"]), ("Done: T-001", "merged", 4, ["tada"]))
        self.assertEqual([(a["label"], a["body"], a["method"]) for a in m["actions"]],
                         [("Status", "status", "POST"), ("Stop", "stop", "POST")])
        self.assertTrue(all(a["url"] == f"{self.ntfy.url}/{self.c['reply_topic']}" for a in m["actions"]))

    def test_emails_tokens_and_the_topics_are_censored(self):
        token = "ghp_" + "Ab3" * 12
        phone.push(f"From {EMAIL}", f"mail {EMAIL}, token {token}, reply to {self.c['reply_topic']}")
        m = self.pushes()[-1]
        text = m["title"] + m["message"]
        for secret in (EMAIL, token, self.c["reply_topic"], self.c["topic"]):
            self.assertNotIn(secret, text)
        self.assertIn("ja***@***", m["message"])

    def test_a_failing_censor_sends_nothing(self):
        with mock.patch.object(phone, "censor", side_effect=RuntimeError("broken")):
            self.assertFalse(phone.push("t", "hello"))
        self.assertEqual(self.pushes(), [])

    def test_off_or_never_set_up_sends_nothing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            phone.off()
        self.assertFalse(phone.push("t", "hello"))
        self.assertFalse(phone.ask("sandbox-t-001", "Red or blue?", ["red", "blue"]))
        phone.conf_path().unlink()
        self.assertFalse(phone.push("t", "hello"))
        self.assertEqual(self.pushes(), [])

    def test_the_users_own_scripts_come_first(self):
        record = self.s.tmp / "scripts.txt"
        for name in ("notify", "notify-ask"):
            f = self.s.bin / name
            f.write_text(f'#!/bin/sh\necho "{name} $*" >> {record}\n'
                         '[ "$1" = --get ] && printf "ANSWER A\\tred\\n"\nexit 0\n')
            f.chmod(0o755)
        phone.push("Done", "merged", 3, "tada", ("Status=status",))
        phone.ask("sandbox-t-001", "Red or blue?", ["red", "blue"])
        self.assertEqual(phone.answer("sandbox-t-001"), "ANSWER A\tred")
        phone.pump()  # their ntfy-listen reads the replies: windvane doesn't
        calls = record.read_text()
        self.assertIn("notify -t Done -p 3 -g tada -a Status=status merged", calls)
        self.assertIn("notify-ask sandbox-t-001 Red or blue? red blue", calls)
        self.assertIn("notify-ask --get sandbox-t-001", calls)
        self.assertEqual(self.pushes(), [])


class Setup(PhoneTest):
    def test_setup_makes_private_random_topics_and_sends_a_test_push(self):
        phone.conf_path().unlink()
        self.addCleanup(os.chdir, os.getcwd())  # -C moves into the sandbox, which goes away
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "phone", "setup", "--server", self.ntfy.url, "--no-service"])
        c = phone.conf()
        self.assertTrue(phone.on(c))
        self.assertEqual(stat.S_IMODE(phone.conf_path().stat().st_mode), 0o600)
        for t in (c["topic"], c["reply_topic"]):
            self.assertRegex(t, r"^[A-Za-z0-9_-]{32}$")
            user = os.environ.get("USER", "")  # unset in a bare CI shell: a stand-in letter would match by chance
            self.assertFalse(re.search(r"(?i)claude|ntfy|sandbox" + (f"|{re.escape(user)}" if user else ""), t))
        self.assertNotEqual(c["topic"], c["reply_topic"])
        self.assertIn("test push: sent", out.getvalue())
        self.assertIn(c["topic"], out.getvalue())  # at the desk only, to subscribe
        self.assertNotIn(c["reply_topic"], out.getvalue())  # the phone never needs to type it
        self.assertEqual(self.ntfy.on(c["topic"])[-1]["title"], "windvane: phone works")
        self.assertEqual(phone.projects(), {"sandbox": str(self.s.root)})

        with contextlib.redirect_stdout(io.StringIO()):
            phone.rotate()
        new = phone.conf()
        self.assertTrue(new["topic"] != c["topic"] and new["reply_topic"] != c["reply_topic"])
        self.assertEqual(self.ntfy.on(new["topic"])[-1]["title"], "windvane: new topic works")

    def test_the_listener_service_is_installed_and_removed(self):
        calls = []
        with mock.patch.object(phone, "systemctl", side_effect=lambda *a: calls.append(a) or mock.Mock(returncode=0)), \
                mock.patch.object(phone.shutil, "which", side_effect=lambda n: "/bin/x" if n == "systemctl" else None), \
                contextlib.redirect_stdout(io.StringIO()):
            phone.install_service()
            self.assertIn("phone listen", phone.unit_path().read_text())
            phone.off()
        self.assertFalse(phone.unit_path().exists())
        self.assertIn(("enable", "--now", phone.SERVICE), calls)
        self.assertIn(("disable", "--now", phone.SERVICE), calls)
        self.assertFalse(phone.conf()["enabled"])


class Replies(PhoneTest):
    def ask(self, qid="sandbox-t-001", opts=("red", "blue", "green")):
        self.assertTrue(phone.ask(qid, "Red or blue?", list(opts)))
        return self.pushes()[-1]

    def test_a_button_answers_the_question(self):
        m = self.ask()
        self.assertEqual([a["label"] for a in m["actions"]], ["A", "B", "Answer on computer"])
        self.assertIn("For C, send the letter", m["message"])
        self.press(m, "B")
        self.assertEqual(phone.answer("sandbox-t-001"), "")
        phone.listen(once=True)
        self.assertEqual(phone.answer("sandbox-t-001"), "ANSWER B\tblue")
        self.assertEqual(self.pushes()[-1]["title"], "Answer recorded")

    def test_a_bare_letter_answers_the_newest_and_later_waits(self):
        self.ask("sandbox-t-001")
        os.utime(phone.state_dir() / "questions/sandbox-t-001.json", (1, 1))
        self.ask("sandbox-t-002")
        self.phone_sends("c")
        self.phone_sends("later sandbox-t-001")
        self.assertEqual(phone.listen(once=True), 2)  # it read both (False: the lock was taken or the read failed)
        self.assertEqual(phone.answer("sandbox-t-002"), "ANSWER C\tgreen")
        self.assertEqual(phone.answer("sandbox-t-001"), "LATER")

    def test_only_exact_commands_count_and_own_pushes_are_never_commands(self):
        phone.push("stop", "stop")  # windvane's own push (it has a title) on the push topic
        self.ntfy.publish(self.c["topic"], "please stop everything")  # chat typed on the push topic
        self.phone_sends("answer sandbox-t-001 B; rm -rf ~")
        before = len(self.pushes())
        phone.listen(once=True)
        new = self.pushes()[before:]
        self.assertEqual([m["title"] for m in new], ["Commands"])  # only the reply topic's junk gets the help
        self.assertFalse((phone.state_dir() / "answers").exists())

    def test_before_the_start_answers_count_but_commands_dont(self):
        m = self.ask()
        phone.mark_now()
        self.press(m, "B")
        self.phone_sends("status")
        before = len(self.pushes())
        phone.listen(once=True, since=time.time() + 5)  # both were sent before this run started
        self.assertEqual(phone.answer("sandbox-t-001"), "ANSWER B\tblue")
        self.assertEqual([m["title"] for m in self.pushes()[before:]], ["Answer recorded"])

    def test_the_stream_listener_answers_status(self):
        phone.register(self.p)
        done = threading.Event()
        t = threading.Thread(target=phone.listen, kwargs={"until": done.is_set}, daemon=True)
        t.start()
        self.addCleanup(t.join, 10)
        self.addCleanup(done.set)
        self.assertTrue(self.ntfy.reading.wait(30), "the listener never opened its stream")
        self.phone_sends("status")  # after `listen` took its start: sent sooner, it would be a stale command
        for _ in range(100):
            if any(m.get("title") == "Status" for m in self.pushes()):
                break
            time.sleep(0.1)
        status = next(m for m in self.pushes() if m.get("title") == "Status")
        self.assertIn("Sandbox: autopilot off", status["message"])


class WithTheDriver(PhoneTest):
    def setUp(self):
        super().setUp()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.publish()

    def worker_prompts(self):
        calls = [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["prompt"] for c in calls if "worker" in c["system"]]

    def test_an_answer_from_the_phone_is_built(self):
        os.environ["FAKE_WORKER"] = "question"
        self.addCleanup(setattr, driver, "NAP", driver.NAP)
        driver.NAP = 0.2
        pilot = driver.Pilot(self.p, args(answer_wait=60))
        done, napping, naps = threading.Event(), threading.Event(), []
        self.addCleanup(done.set)
        nap = pilot.nap

        def timed_nap(until, answers=False):  # each wait for an answer, and whether it ended before its minute
            napping.set() if answers else None
            nap(until, answers)
            naps.append((answers, now() < until))
            napping.clear()
        pilot.nap = timed_nap

        def phone_user():  # taps B on each question once the run is waiting for it
            pressed = set()
            while not done.is_set():
                for m in self.pushes():
                    if m.get("title", "").startswith("Question") and m["id"] not in pressed and napping.is_set():
                        pressed.add(m["id"])
                        self.press(m, "B")
                time.sleep(0.1)
        threading.Thread(target=phone_user, daemon=True).start()
        pilot.run(now() + timedelta(hours=1))
        waits = [early for answers, early in naps if answers]
        self.assertTrue(waits and all(waits), naps)  # the waiting run wakes for the answer, not after its minute
        self.assertIn("option B (blue)", self.worker_prompts()[1])
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertTrue(any(m.get("title") == "Answer recorded" for m in self.pushes()))

    def test_a_tap_on_usage_doesnt_end_a_usage_pause(self):
        """The pause runs until an hour from now; a tap on Usage once it's paused gets its reply while the pause goes
        on, and only a stop ends it."""
        self.addCleanup(setattr, driver, "NAP", driver.NAP)
        driver.NAP = 0.2
        pilot = driver.Pilot(self.p, args())
        pilot.deadline = now() + timedelta(hours=3)
        pilot.usage.five, pilot.usage.five_reset = 90, time.time() + 3600
        with mock.patch.object(Usage, "probe") as probe:
            pause = threading.Thread(target=lambda: self.ok.append(pilot.wait_for_reset()), daemon=True)
            self.ok = []
            pause.start()
            self.until(lambda: any(m.get("title", "").endswith(": paused") for m in self.pushes()))
            self.phone_sends("usage")
            self.until(lambda: any(m.get("title") == "Claude usage" for m in self.pushes()))  # the tap is answered
            pause.join(1)  # a few naps' time to end, were the tap to end the pause
            self.assertTrue(pause.is_alive(), "the tap ended the pause")
            probe.assert_not_called()  # not on the tap
            (self.p.state / "STOP").write_text("")
            pause.join(30)
        self.assertEqual(self.ok, [True])
        self.assertEqual(sum(m.get("title", "").endswith(": paused") for m in self.pushes()), 1)
        probe.assert_called_once()  # when the pause ends

    def until(self, cond, wait=30):
        """Polls `cond` until it holds: a loaded machine is slower, never a reason to fail."""
        end = time.time() + wait
        while not cond():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def test_a_stop_sent_between_runs_doesnt_stop_the_next(self):
        phone.mark_now()
        self.phone_sends("stop")  # tapped after the last run ended, with no listener to take it
        time.sleep(1.1)  # ntfy's times are in whole seconds
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertFalse(any(m.get("title") == "Stop" for m in self.pushes()))

    def test_a_question_whose_push_failed_is_sent_again(self):
        os.environ["FAKE_WORKER"] = "question"
        pilot = driver.Pilot(self.p, args(answer_wait=60))
        real, fails = phone.send, []

        def flaky(c, title, *a, **k):
            if title.startswith("Question") and not fails:
                fails.append(title)
                return False
            return real(c, title, *a, **k)
        orig = pilot.poll_questions

        def press_then_poll():
            for m in self.pushes():
                if m.get("title", "").startswith("Question") and not m.get("pressed"):
                    m["pressed"] = True
                    self.press(m, "B")
            orig()
        pilot.poll_questions = press_then_poll
        pilot.nap = lambda until, answers=False: None
        with mock.patch.object(phone, "send", side_effect=flaky):
            pilot.run(now() + timedelta(hours=1))
        log = (self.p.state / "autopilot.log").read_text()
        self.assertEqual(log.count("the phone push failed"), 1)
        asked = [m for m in self.pushes() if m.get("title", "").startswith("Question") and "T-001" in m["message"]]
        self.assertEqual(len(asked), 1)
        self.assertIn("option B (blue)", self.worker_prompts()[1])

    def test_stop_from_the_phone_stops_the_run(self):
        pilot = driver.Pilot(self.p, args())
        orig = pilot.pick

        def stop_then_pick(free):
            if not any(m.get("title") == "Stop" for m in self.pushes()):
                self.phone_sends("stop")
                self.until(lambda: phone.pump() or (self.p.state / "STOP").exists())
            return orig(free)
        pilot.pick = stop_then_pick
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])  # the ticket picked in that pass ends; T-002 never starts
        self.assertEqual(len(self.worker_prompts()), 1)
        self.assertIn("Why: asked to stop", (self.p.state / "autopilot.log").read_text())
        stop = next(m for m in self.pushes() if m.get("title") == "Stop")
        self.assertIn("Sandbox: autopilot stops after the current ticket", stop["message"])


if __name__ == "__main__":
    unittest.main()
