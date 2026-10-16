"""A provisional answer's id (`T-003.p2`) names the same question until it is answered, whatever else is answered or
added meanwhile, and `windvane status` and preflight name it the same way."""
import contextlib
import io
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, preflight, questions  # noqa: E402
from helpers import Sandbox  # noqa: E402

BLOCK = "### {t}: {q}\nThe ticket didn't say.\n- **A**: {a}\n- **B**: other\nRecommendation: A, because it's simpler.\n" \
        "Built: A; to change it: config.py\n"
FIRST, SECOND, THIRD = ("T-003", "Red or blue?", "red"), ("T-003", "Lamp or torch?", "lamp"), \
    ("T-003", "Tea or coffee?", "tea")


def qfile(*blocks):
    return "# Questions\n\n## Open\n\n## Provisional\n" + "\n".join(BLOCK.format(t=t, q=q, a=a) for t, q, a in blocks) \
        + "\n## Answered\n"


def merged(text, *blocks):
    """`text` with blocks a later ticket's merge put under ## Provisional."""
    head, sep, tail = text.partition("\n## Answered")
    return head.rstrip("\n") + "\n" + "\n".join(BLOCK.format(t=t, q=q, a=a) for t, q, a in blocks) + "\n" + sep + tail


def above(text, *blocks):
    """`text` with blocks a worker put at the top of ## Provisional."""
    return text.replace("## Provisional\n", "## Provisional\n" + "".join(BLOCK.format(t=t, q=q, a=a) + "\n"
                                                                         for t, q, a in blocks), 1)


def ids(text):
    """The ids `windvane status` prints, and preflight's: they must agree."""
    status = [q["id"] for q in questions.with_ids(text)]
    assert status == [x["id"] for x in preflight.provisionals(text)], (status, preflight.provisionals(text))
    return status


def answer(text, key, letter="A"):
    """`text` with the question `key` names answered, as `windvane answer` files it."""
    x = [x for x in preflight.provisionals(text) if x["id"] == key][0]
    return preflight.file_answers(text, [(x, letter, "")])


def answered(text):
    return [q for _, q, _ in questions.blocks(questions.part(text, "Answered"))]


class StableIds(unittest.TestCase):
    def test_answering_the_first_keeps_the_second_id(self):
        text = qfile(FIRST, SECOND)
        self.assertEqual(ids(text), ["T-003.p1", "T-003.p2"])
        text = answer(text, "T-003.p1")
        self.assertEqual(ids(text), ["T-003.p2"])
        self.assertEqual(preflight.provisionals(text)[0]["question"], "Lamp or torch?")

    def test_answering_the_second_first_then_the_first(self):
        text = answer(qfile(FIRST, SECOND), "T-003.p2")
        self.assertEqual(ids(text), ["T-003.p1"])
        self.assertEqual(answered(text), ["Lamp or torch?"])
        text = answer(text, "T-003.p1")
        self.assertEqual(ids(text), [])
        self.assertEqual(answered(text), ["Lamp or torch?", "Red or blue?"])

    def test_a_question_merged_later_takes_a_new_number(self):
        self.assertEqual(ids(merged(qfile(FIRST, SECOND), THIRD)), ["T-003.p1", "T-003.p2", "T-003.p3"])
        self.assertEqual(ids(merged(answer(qfile(FIRST, SECOND), "T-003.p1"), THIRD)), ["T-003.p2", "T-003.p3"])
        self.assertEqual(ids(merged(answer(qfile(FIRST, SECOND), "T-003.p2"), THIRD)), ["T-003.p1", "T-003.p3"])
        text = answer(answer(qfile(FIRST, SECOND), "T-003.p2"), "T-003.p1")
        self.assertEqual(ids(merged(text, THIRD)), ["T-003.p3"])

    def test_a_question_put_above_the_shown_ones_takes_a_new_number(self):
        shown = {"T-003: Red or blue?": 1, "T-003: Lamp or torch?": 2}
        self.assertEqual(questions.prov_numbers(above(qfile(FIRST, SECOND), THIRD), shown), [3, 1, 2])
        text = answer(qfile(FIRST, SECOND), "T-003.p1")
        self.assertEqual(questions.prov_numbers(above(text, THIRD), {"T-003: Lamp or torch?": 2}), [3, 2])

    def test_the_middle_of_three_answered_leaves_the_others_alone(self):
        text = answer(qfile(FIRST, SECOND, THIRD), "T-003.p2")
        self.assertEqual(ids(text), ["T-003.p1", "T-003.p3"])
        self.assertEqual(answer(text, "T-003.p3").count("Tea or coffee?"), 1)
        self.assertEqual(answered(answer(text, "T-003.p3")), ["Lamp or torch?", "Tea or coffee?"])

    def test_other_tickets_count_on_their_own(self):
        text = answer(qfile(("T-001", "Big or small?", "big"), FIRST, ("T-001", "Up or down?", "up"), SECOND),
                      "T-001.p1")
        self.assertEqual(ids(text), ["T-003.p1", "T-001.p2", "T-003.p2"])

    def test_a_single_provisional_is_named_alike_by_status_and_preflight(self):
        self.assertEqual(ids(qfile(FIRST)), ["T-003.p1"])

    def test_a_reworded_block_still_records_the_id_it_had(self):
        x = preflight.provisionals(qfile(FIRST, SECOND))[1]
        text = preflight.file_answers(qfile(FIRST, ("T-003", "Lamp, or a torch?", "lamp")), [(x, "A", "")])
        self.assertIn(", as T-003.p2)", questions.part(text, "Answered"))
        self.assertEqual(ids(text), ["T-003.p1"])


class AnswerCommand(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-003")
        self.s.write(".windvane/QUESTIONS.md", qfile(FIRST, SECOND))
        self.s.publish("provisional")
        self.addCleanup(os.chdir, os.getcwd())

    def tearDown(self):
        self.s.close()

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def main_questions(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("show", "origin/main:.windvane/QUESTIONS.md")

    def test_the_second_id_still_answers_after_the_first(self):
        self.assertIn("recorded", self.cli("answer", "T-003.p1", "A"))
        self.assertIn("recorded", self.cli("answer", "T-003.p2", "A"))
        text = self.main_questions()
        self.assertEqual(questions.provisional(text), [])
        lamp = [body for _, q, body in questions.blocks(questions.part(text, "Answered")) if q == "Lamp or torch?"]
        self.assertIn("Answer: A (confirmed the provisional answer)", lamp[0])
        self.assertIn(", as T-003.p2)", lamp[0])

    def test_status_names_each_question_alike_while_others_are_answered_or_put_above(self):
        self.assertEqual(self.status(), ["Red or blue? T-003.p1", "Lamp or torch? T-003.p2"])
        self.s.write(".windvane/QUESTIONS.md", above(self.main_questions(), THIRD))
        self.s.publish("a third, above")
        self.assertEqual(self.status(), ["Tea or coffee? T-003.p3", "Red or blue? T-003.p1", "Lamp or torch? T-003.p2"])
        self.cli("answer", "T-003.p2", "A")
        self.assertEqual(self.status(), ["Tea or coffee? T-003.p3", "Red or blue? T-003.p1"])
        self.assertEqual(answered(self.main_questions()), ["Lamp or torch?"])
        self.cli("answer", "T-003.p3", "A")
        self.assertEqual(self.status(), ["Red or blue? T-003.p1"])
        self.assertEqual(answered(self.main_questions()), ["Lamp or torch?", "Tea or coffee?"])

    def status(self):
        """(question, id) of each provisional line `windvane status` prints."""
        return [f"{m.group(1)} {m.group(2)}" for m in re.finditer(r"^provisional: T-003: (.+?) Built .* windvane answer "
                                                                  r"(\S+) <letter>$", self.cli("status"), re.M)]

    def test_the_bare_ticket_id_answers_its_only_one_left(self):
        self.cli("answer", "T-003.p2", "A")
        self.assertIn("recorded", self.cli("answer", "T-003", "A"))
        self.assertEqual(answered(self.main_questions()), ["Lamp or torch?", "Red or blue?"])


if __name__ == "__main__":
    unittest.main()
