"""T-090: an API error reaches the phone, a set-aside reason and the stop reason as its message, not its JSON; the
project log keeps the whole text."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import net  # noqa: E402
from windvane.runner import ending, plain_error  # noqa: E402
from test_set_aside import Base  # noqa: E402

MESSAGE = ("messages.3.content.0: unexpected `tool_use_id` found in `tool_result` blocks: toolu_01. Each `tool_result` "
           "block must have a corresponding `tool_use` block in the previous message.")
ENVELOPE = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"' + MESSAGE
            + '"},"request_id":"req_011"}')


class PlainError(unittest.TestCase):
    def test_the_envelope_becomes_its_code_and_message(self):
        self.assertEqual(plain_error(ENVELOPE), f"API Error 400: {MESSAGE}")

    def test_the_json_with_no_prefix_takes_its_code_from_the_error_type(self):
        self.assertEqual(plain_error('{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'),
                         "API Error 529: Overloaded")
        self.assertEqual(plain_error('{"type":"error","error":{"type":"new_error","message":"Odd"}}'),
                         "API Error: Odd")

    def test_words_around_it_stay(self):
        self.assertEqual(plain_error('error_during_execution: API Error: 500 {"type":"error","error":{"type":'
                                     '"api_error","message":"Internal \\"server\\" error"}} (retried)'),
                         'error_during_execution: API Error 500: Internal "server" error (retried)')

    def test_other_text_comes_back_as_it_is(self):
        for text in ("API Error: 529 Overloaded", "no result event (exit 1): Error: socket hang up",
                     'the reviewer said {"a": 1}', 'API Error: 400 {"type":"error","error":{"message":"cut sh',
                     '{"type":"error","error":{"type":"api_error"}}', "API Error: 400 [1, 2]", "",
                     "API Error: 400 " + '{"a":' * 100000):
            self.assertEqual(plain_error(text), text)

    def test_the_session_hands_back_the_whole_error_for_the_log(self):
        final = {"is_error": True, "result": ENVELOPE}
        self.assertEqual(ending(final, 1, []), ("error", ENVELOPE))


class SaidByMessage(Base):
    tickets = ("T-001", "T-002")

    def test_a_set_aside_for_the_400_envelope_leads_with_its_message(self):
        self.pilot().set_aside("T-001", ENVELOPE)
        [(_, msg)] = self.aside()
        first = msg.split("\n")[0]
        self.assertTrue(first.startswith(f"Sessions died 4×: API Error 400: {MESSAGE[:-1]}"), first)
        self.assertNotIn("{", msg)
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn(f"({ENVELOPE})", log)

    def test_the_status_reason_and_a_failed_push_say_the_message_too(self):
        pilot = self.pilot()
        pilot.set_aside("T-001", ENVELOPE)
        self.assertEqual(pilot.aside["T-001"], f"its sessions kept dying: API Error 400: {MESSAGE}")
        with mock.patch.object(net, "online", lambda: True):
            pilot.fail("T-002", ENVELOPE)
        [msg] = [m for t, m in self.pushes if t.startswith("Failed")]
        self.assertEqual(msg, f"API Error 400: {MESSAGE[:-1]}. Branch kept, run goes on.")
        self.assertIn(f"T-002: FAILED: {ENVELOPE}", (self.p.state / "autopilot.log").read_text())

    def test_a_long_error_that_isnt_json_is_bounded_in_the_reason_and_the_summary(self):
        page = ("<html><body>" + " ".join(f"Bad gateway, try {i} of the upstream." for i in range(200))
                + "</body></html>")
        self.assertGreater(len(page), 6000)
        self.assertEqual(plain_error(page), page)
        pilot = self.pilot()
        pilot.set_aside("T-001", page)
        reason = pilot.aside["T-001"]
        self.assertLessEqual(len(reason), len("its sessions kept dying: ") + 300 + len(" (the rest is in the log)"))
        self.assertTrue(reason.endswith(" (the rest is in the log)"), reason)
        pilot.crashes = self.p.ap["retries"]  # one more death stops the run, with the error as its reason
        with mock.patch.object(net, "online", lambda: True):
            self.assertFalse(pilot.crashed("T-002", page))
        pilot.stop_reason = pilot.halt
        self.assertLess(len(pilot.summary().encode()), 1000)
        self.assertIn(f"the whole text: {page}", (self.p.state / "autopilot.log").read_text())


if __name__ == "__main__":
    unittest.main()
