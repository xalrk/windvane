"""T-185: two writers of answers.json, shown.json or preflight.json can't lose each other's changes. Each was a load,
edit, save with no lock, so of two `windvane answer`s at once (or one beside the driver dropping settled answers) only
the last save survived, and the run built the recommended option in place of the lost answer."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import edit_json, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
N = 50
# A writer process: waits for the go file, so all writers run their loops at the same moment, then makes N edits:
# `answer T` gives T-0, T-1, ...; `settle` drops a settled answer; `note X` adds ids; `cli PROJECT T` runs
# `windvane answer T.q1 A` ... `T.qN A` through the CLI; `hold` takes answers.json's lock and keeps it
WRITER = """
import contextlib, io, sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from windvane import cli, questions, util
p, go, who, arg = SimpleNamespace(state=Path(sys.argv[2])), Path(sys.argv[3]), sys.argv[4], sys.argv[5:]
if who == "hold":
    util.edit_json(p.state / "answers.json", lambda a: (print("held", flush=True), time.sleep(60)), {})
while not go.exists():
    time.sleep(0.001)
for i in range(1, %d + 1):
    if who == "settle":
        questions.drop_answers(p, ["T-999"])
    elif who == "note":
        questions.note(p, lambda s: s["ids"].update({f"{arg[0]}-{i}": i}))
    elif who == "cli":
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["-C", arg[0], "answer", f"{arg[1]}.q{i}", "A"])
    else:
        questions.give_answer(p, f"{arg[0]}-{i}", "A")
""" % N


def writer(state, go, *who):
    return subprocess.Popen([sys.executable, "-c", WRITER, str(ROOT), str(state), str(go), *who],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def finish(test, procs):
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        test.assertEqual(proc.returncode, 0, err[-2000:])


class Writers(unittest.TestCase):
    """The helpers themselves, raced from processes and threads."""
    def setUp(self):
        d = tempfile.TemporaryDirectory(prefix="windvane-test-")
        self.addCleanup(d.cleanup)
        self.state = Path(d.name)
        self.p = SimpleNamespace(state=self.state)

    def race(self, *who):
        """Run one writer process per tuple in `who` side by side."""
        go = self.state / "go"
        procs = [writer(self.state, go, *w) for w in who]
        time.sleep(0.3)  # each is started and waiting
        go.touch()
        finish(self, procs)

    def test_two_answer_writers_keep_every_answer(self):
        self.race(("answer", "T-001"), ("answer", "T-002"))
        a = questions.desk_answers(self.p)
        self.assertEqual(len(a), 2 * N, f"lost {2 * N - len(a)} answers")
        self.assertEqual({v["option"] for v in a.values()}, {"A"})

    def test_settling_answers_keeps_one_given_meanwhile(self):
        self.race(("answer", "T-001"), ("settle",))
        self.assertEqual(len(questions.desk_answers(self.p)), N, "an answer given while the driver settled was lost")

    def test_settling_drops_only_the_settled(self):
        for t in ("T-001", "T-002"):
            questions.give_answer(self.p, t, "B")
        questions.drop_answers(self.p, ["T-001", "T-003"])
        self.assertEqual(list(questions.desk_answers(self.p)), ["T-002"])

    def test_two_shown_writers_keep_every_id(self):
        self.race(("note", "a"), ("note", "b"))
        ids = questions.shown(self.p)["ids"]
        self.assertEqual(len(ids), 2 * N, f"lost {2 * N - len(ids)} ids")

    def test_two_threads_keep_every_id(self):
        """The driver's parallel slots are threads of one process: each edit opens the lock file anew, so they
        exclude each other too."""
        def loop(who):
            for i in range(N):
                questions.note(self.p, lambda s: s["ids"].update({f"{who}-{i}": i}))
        threads = [threading.Thread(target=loop, args=(w,)) for w in "ab"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(len(questions.shown(self.p)["ids"]), 2 * N)

    def test_a_killed_writer_leaves_no_lock_behind(self):
        holder = writer(self.state, self.state / "go", "hold")
        self.addCleanup(holder.stderr.close)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(lambda: holder.poll() is None and holder.kill())
        self.assertEqual(holder.stdout.readline().strip(), "held")
        blocked = threading.Thread(target=questions.give_answer, args=(self.p, "T-001", "A"), daemon=True)
        blocked.start()
        blocked.join(0.5)
        self.assertTrue(blocked.is_alive(), "the lock didn't hold another writer off")
        holder.send_signal(signal.SIGKILL)  # mid-edit: no cleanup runs
        holder.wait(10)
        blocked.join(10)
        self.assertFalse(blocked.is_alive(), "a dead writer's lock still held the next one off")
        questions.give_answer(self.p, "T-002", "B")
        self.assertEqual(sorted(questions.desk_answers(self.p)), ["T-001", "T-002"])

    def test_files_keep_their_format(self):
        """The lock is a sibling file; the records stay the plain JSON they were, defaults and all."""
        questions.give_answer(self.p, "T-001", "A", "why")
        questions.note(self.p, lambda s: s["pushed"].append("k"))
        answer = json.loads((self.state / "answers.json").read_text())["T-001"]
        self.assertEqual((answer["option"], answer["text"]), ("A", "why"))
        self.assertEqual(json.loads((self.state / "shown.json").read_text()),
                         {"asked": {}, "pushed": ["k"], "preflight": [], "built": {}, "ids": {}})
        self.assertTrue((self.state / "answers.json.lock").exists())
        self.assertEqual(edit_json(self.state / "x.json", lambda d: d.setdefault("n", 1), {}), 1)


def waiting(tid, i, asked=None):
    """A question of state.json, as the driver keeps it."""
    return {"ticket": tid, "q": f"Question {i}?", "opts": {"A": "yes", "B": "no"}, "rec": "A", "must_wait": False,
            "branch": f"{tid.lower()}-x", "asked": asked}


class ThroughTheCommand(unittest.TestCase):
    """`windvane answer` and the driver's poll, raced in a sandbox project."""
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.go = self.p.state / "go"
        qs = {f"{t}.q{i}": waiting(t, i) for t in ("T-001", "T-002") for i in range(1, N + 1)}
        (self.p.state / "state.json").write_text(json.dumps({"questions": qs}))
        (self.p.state / "preflight.json").write_text(json.dumps({"questions": [
            {"id": k, "ticket": q["ticket"], "kind": "open", "question": q["q"]} for k, q in qs.items()]}))

    def test_two_answer_commands_at_once_keep_both_answers(self):
        procs = [subprocess.Popen(["windvane", "answer", key, "A"], cwd=self.s.root, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) for key in ("T-001.q1", "T-001.q2")]
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            self.assertIn("answer recorded", out, err)
        self.assertEqual(sorted(questions.desk_answers(self.p)), ["T-001.q1", "T-001.q2"])

    def test_answer_commands_racing_keep_every_answer_and_forget_every_question(self):
        procs = [writer(self.p.state, self.go, "cli", str(self.s.root), t) for t in ("T-001", "T-002")]
        time.sleep(0.5)
        self.go.touch()
        finish(self, procs)
        a = questions.desk_answers(self.p)
        self.assertEqual(len(a), 2 * N, f"lost {2 * N - len(a)} answers")
        left = json.loads((self.p.state / "preflight.json").read_text())["questions"]
        self.assertEqual(left, [], f"{len(left)} answered questions would be offered again")

    def test_answers_given_while_the_driver_settles_others_are_kept(self):
        """The driver keeps answering and settling T-001.q1 while the user answers T-002's questions one by one."""
        sent = lambda *a, **k: True  # noqa: E731
        with mock.patch.object(driver, "push", sent), mock.patch.object(phone, "ask", sent):
            pl = driver.Pilot(self.p, args(answer_wait=60))
            pl.deadline = now() + timedelta(hours=2)
            pl.queue = Queue(self.p)
            pl.log = lambda *a, **k: None
            later = {f"T-002.q{i}": waiting("T-002", i) for i in range(1, N + 1)}  # not sent yet: the poll skips them
            pl.questions = {**later, "T-001.q1": waiting("T-001", 1, time.time())}
            pl.save()  # state.json, which `windvane answer` reads the waiting questions from
            proc = writer(self.p.state, self.go, "cli", str(self.s.root), "T-002")
            time.sleep(0.5)
            self.go.touch()
            settled = 0
            while proc.poll() is None:
                pl.questions["T-001.q1"] = waiting("T-001", 1, time.time())
                questions.give_answer(self.p, "T-001.q1", "B")
                pl.poll_questions()
                settled += "T-001" in pl.feedback
                pl.feedback.clear()
            finish(self, [proc])
        self.assertGreater(settled, 0)
        a = questions.desk_answers(self.p)
        self.assertNotIn("T-001.q1", a)
        self.assertEqual(len(a), N, f"lost {N - len(a)} answers given while the driver settled {settled}")


if __name__ == "__main__":
    unittest.main()
