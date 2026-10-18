"""T-185: two writers of answers.json or shown.json can't lose each other's changes. Each was a load, edit, save with
no lock, so of two `windvane answer`s at once (or one beside the driver dropping settled answers) only the last save
survived, and the run built the recommended option in place of the lost answer."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import questions  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
N = 50
# A writer process: waits for the go file, so all writers run their loops at the same moment, then makes N edits
WRITER = """
import sys, time
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from windvane import questions
p, go, who = SimpleNamespace(state=Path(sys.argv[2])), Path(sys.argv[3]), sys.argv[4]
while not go.exists():
    time.sleep(0.001)
for i in range(%d):
    if who == "settle":
        questions.drop_answers(p, ["T-999"])
    elif who.startswith("note"):
        questions.note(p, lambda s: s["ids"].update({f"{who}-{i}": i}))
    else:
        questions.give_answer(p, f"{who}-{i}", "A")
""" % N


class Writers(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(prefix="windvane-test-")
        self.addCleanup(d.cleanup)
        self.state = Path(d.name)
        self.p = SimpleNamespace(state=self.state)

    def race(self, *who):
        """Run one writer process per name in `who` side by side."""
        go = self.state / "go"
        procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(ROOT), str(self.state), str(go), w],
                                  stderr=subprocess.PIPE, text=True) for w in who]
        time.sleep(0.3)  # each is started and waiting
        go.touch()
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, 0, err[-2000:])

    def test_two_answer_writers_keep_every_answer(self):
        self.race("T-001", "T-002")
        a = questions.desk_answers(self.p)
        self.assertEqual(len(a), 2 * N, f"lost {2 * N - len(a)} answers")
        self.assertEqual({v["option"] for v in a.values()}, {"A"})

    def test_settling_answers_keeps_one_given_meanwhile(self):
        self.race("T-001", "settle")
        self.assertEqual(len(questions.desk_answers(self.p)), N, "an answer given while the driver settled was lost")

    def test_settling_drops_only_the_settled(self):
        for t in ("T-001", "T-002"):
            questions.give_answer(self.p, t, "B")
        questions.drop_answers(self.p, ["T-001", "T-003"])
        self.assertEqual(list(questions.desk_answers(self.p)), ["T-002"])

    def test_two_shown_writers_keep_every_id(self):
        self.race("note1", "note2")
        ids = questions.shown(self.p)["ids"]
        self.assertEqual(len(ids), 2 * N, f"lost {2 * N - len(ids)} ids")

    def test_files_keep_their_format(self):
        """The lock is a sibling file; the records stay the plain JSON they were, defaults and all."""
        questions.give_answer(self.p, "T-001", "A", "why")
        questions.note(self.p, lambda s: s["pushed"].append("k"))
        answer = json.loads((self.state / "answers.json").read_text())["T-001"]
        self.assertEqual((answer["option"], answer["text"]), ("A", "why"))
        self.assertEqual(json.loads((self.state / "shown.json").read_text()),
                         {"asked": {}, "pushed": ["k"], "preflight": [], "built": {}, "ids": {}})
        self.assertTrue((self.state / "answers.json.lock").exists())


if __name__ == "__main__":
    unittest.main()
