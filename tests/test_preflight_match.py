"""Answering a provisional question finds its block under ## Provisional even when a worker reworded it."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import preflight, questions  # noqa: E402

BLOCK = "### {t}: {q}\nThe ticket didn't say.\n- **A**: {a}\n- **B**: other\nRecommendation: A, because it's simpler.\n" \
        "Built: A; to change it: config.py\n"


def qfile(*blocks):
    return "# Questions\n\n## Open\n\n## Provisional\n" + "\n".join(BLOCK.format(t=t, q=q, a=a) for t, q, a in blocks) \
        + "\n## Answered\n"


def saved(text, tid, n=1):
    """The question as preflight saved it from an older QUESTIONS.md, the user's answer with it."""
    return [x for x in preflight.provisionals(text) if x["id"] == f"{tid}.p{n}"][0]


class ProvisionalMatch(unittest.TestCase):
    def test_a_reworded_question_is_still_moved_and_not_asked_again(self):
        x = saved(qfile(("T-003", "Red or blue?", "red")), "T-003")
        now = qfile(("T-001", "Big or small?", "big"), ("T-003", "Which colour, red or blue?", "red"))
        text = preflight.file_answers(now, [(x, "B", "")])
        self.assertEqual([q["ticket"] for q in preflight.provisionals(text)], ["T-001"])
        self.assertEqual([b[:2] for b in questions.blocks(questions.part(text, "Answered"))],
                         [("T-003", "Red or blue?")])

    def test_the_question_id_picks_among_a_tickets_blocks(self):
        old = qfile(("T-003", "Red or blue?", "red"), ("T-003", "Round or square?", "round"))
        x, y = saved(old, "T-003", 1), saved(old, "T-003", 2)
        now = old.replace("Round or square?", "Which shape?").replace("Red or blue?", "Which colour?")
        text = preflight.file_answers(now, [(y, "A", "")])
        self.assertEqual([q["question"] for q in preflight.provisionals(text)], ["Which colour?"])
        text = preflight.file_answers(now, [(x, "A", ""), (y, "A", "")])  # both at once: neither shifts the other
        self.assertEqual(preflight.provisionals(text), [])

    def test_the_exact_text_comes_first(self):
        old = qfile(("T-003", "Red or blue?", "red"), ("T-003", "Round or square?", "round"))
        x = saved(old, "T-003", 2)
        now = qfile(("T-003", "Round or square?", "round"), ("T-003", "Red or blue?", "red"))  # reordered
        text = preflight.file_answers(now, [(x, "A", "")])
        self.assertEqual([q["question"] for q in preflight.provisionals(text)], ["Red or blue?"])

    def test_no_block_of_another_ticket_is_taken(self):
        x = saved(qfile(("T-003", "Red or blue?", "red")), "T-003")
        now = qfile(("T-004", "Red or blue?", "red"))
        text = preflight.file_answers(now, [(x, "A", "")])
        self.assertEqual([q["ticket"] for q in preflight.provisionals(text)], ["T-004"])


if __name__ == "__main__":
    unittest.main()
