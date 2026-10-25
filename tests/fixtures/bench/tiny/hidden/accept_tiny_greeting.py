import unittest
from pathlib import Path


class Greeting(unittest.TestCase):
    def test_built(self):
        """T-001: `T-001.txt` says `built`."""
        self.assertEqual(Path("T-001.txt").read_text().strip(), "built")

    def test_hello(self):
        """T-001: `hello.txt` says `hello`."""
        self.assertEqual(Path("hello.txt").read_text().strip(), "hello")
