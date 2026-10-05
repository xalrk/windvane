"""A session's output pipe is closed when its run ends: left open, it warns whenever it's collected, onto whatever
stderr is then, which fails a later test that captures stderr."""
import gc
import sys
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Pipes(unittest.TestCase):
    def test_a_run_leaves_no_unclosed_pipe(self):
        s = Sandbox()
        self.addCleanup(s.close)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            runner.run_claude(Project(s.root), "worker", "pipe-run", "hi", "# windvane test", s.root)
            gc.collect()
        self.assertEqual([str(w.message) for w in caught if w.category is ResourceWarning], [])


if __name__ == "__main__":
    unittest.main()
