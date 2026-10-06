"""T-098: free text is cut by one rule, in the bytes ntfy counts. ntfy takes 4,096 bytes a message, but the bound
counted characters: 2,000 three-byte characters, or two long pieces in one push, could still go over and arrive as an
attachment. Each cut says where it fell, and a push over the limit keeps its lines that fit."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import phone  # noqa: E402


def sent_through_push(msg):
    """What the built-in push hands ntfy for `msg` (no `notify` on PATH, the phone on)."""
    out = []
    with mock.patch.object(phone.shutil, "which", lambda name: None), \
            mock.patch.object(phone, "conf", lambda: {"enabled": True, "topic": "t", "reply_topic": "r"}), \
            mock.patch.object(phone, "send", lambda c, title, m, *a: out.append(m) or True):
        phone.push("Failed: T-001", msg)
    return out[0]


class Bytes(unittest.TestCase):
    def assert_clean(self, text, most):
        """Within `most` bytes, and whole characters: it encodes back to what it decoded from."""
        raw = text.encode()
        self.assertLessEqual(len(raw), most)
        self.assertEqual(raw.decode(), text)
        self.assertNotIn("�", text)

    def test_2000_three_byte_characters_stay_in_the_bound(self):
        free = "漢" * 2000
        self.assertEqual(len(free.encode()), 6000)
        out = phone.whole("", free, log=[].append)
        self.assert_clean(out, phone.MOST)
        self.assertTrue(out.startswith("漢" * 400) and out.endswith(phone.REST), out[-40:])
        self.assert_clean(sent_through_push(out), phone.NTFY)

    def test_two_1500_character_pieces_in_one_push_stay_under_ntfys_limit(self):
        for ch in ("a", "é", "漢"):
            body = (phone.whole("Sessions died 4×: ", ch * 1500, ".", log=[].append) + "\n"
                    + phone.whole("The error: ", ch * 1500, ".", log=[].append) + "\nBranch kept, run goes on.")
            self.assert_clean(sent_through_push(body), phone.NTFY - 1)

    def test_the_short_cut_ends_on_a_character_with_an_ellipsis_without_a_log(self):
        out = phone.cut("é" * 400)
        self.assert_clean(out, phone.REASON)
        self.assertTrue(out.endswith("é…"), out[-5:])
        self.assertEqual(phone.cut("  short reason  "), "short reason")

    def test_the_short_cut_with_a_log_says_the_rest_is_there(self):
        lines = []
        text = "word " * 100
        out = phone.cut(text, log=lines.append)
        self.assert_clean(out, phone.REASON)
        self.assertTrue(out.endswith("word" + phone.REST), out[-40:])
        self.assertEqual(lines, [f"cut at a word end; the whole text: {text.strip()}"])


class Wordings(unittest.TestCase):
    def logged(self, free):
        lines = []
        phone.whole("", free, log=lines.append)
        self.assertEqual(len(lines), 1, lines)
        return lines[0].split(";")[0]

    def test_a_sentence_end(self):
        self.assertEqual(self.logged(" ".join(f"Step {i} failed." for i in range(500))), "cut at a sentence end")

    def test_a_word_end(self):
        self.assertEqual(self.logged("word " * 1000), "cut at a word end")

    def test_mid_word(self):
        self.assertEqual(self.logged("x" * 5000), "cut mid-word")


class Guard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = Path(self.dir.name)
        self.addCleanup(phone.use, None)
        phone.use(SimpleNamespace(display="Acme", state=self.state))

    def test_a_message_over_4096_bytes_keeps_its_lines_that_fit_and_is_logged_once(self):
        msg = "\n".join(f"Line {i}: " + "ü" * 40 for i in range(100))
        self.assertGreater(len(msg.encode()), phone.NTFY)
        out = sent_through_push(msg)
        self.assertLessEqual(len(out.encode()), phone.NTFY)
        kept = out.removesuffix(phone.REST)
        self.assertTrue(msg.startswith(kept + "\n"), kept[-60:])  # whole lines only
        log = (self.state / "autopilot.log").read_text()
        self.assertEqual(log.count(msg), 1)
        self.assertIn("push cut at a line end to fit ntfy", log)

    def test_a_message_that_fits_is_sent_as_it_is(self):
        msg = "ü" * 2000
        self.assertEqual(sent_through_push(msg), msg)
        self.assertFalse((self.state / "autopilot.log").exists())


if __name__ == "__main__":
    unittest.main()
