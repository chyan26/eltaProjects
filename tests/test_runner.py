import os
import sys
import unittest
from unittest import mock

from highlight_builder import runner


class RunProcessTest(unittest.TestCase):
    def test_child_output_is_read_as_utf8_even_when_the_console_is_not(self):
        # The tool scripts print Chinese text and accented names; a non-UTF-8 console must not change that.
        lines = []
        env = {"PYTHONIOENCODING": "ascii"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("PYTHONUTF8", None)
            runner.run_process([sys.executable, "-c", "print('Jos\\u00e9 \\u6e2c\\u8a66')"], "s1", lines.append)
        self.assertEqual(lines, ["José 測試"])


if __name__ == "__main__":
    unittest.main()
