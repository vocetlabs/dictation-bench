"""Tests for bench.py's pure parts: the smoke gate and the app configs. No macOS."""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import harness as hn  # noqa: E402


class SmokeTests(unittest.TestCase):
    def test_the_gate_needs_a_settled_dictation_with_most_words_right(self):
        ok = hn.Result("ok", bench.SMOKE_TEXT)
        self.assertTrue(bench.smoke_ok(ok)[0])
        one_off = hn.Result("ok", bench.SMOKE_TEXT.replace("finance", "fiance"))
        self.assertTrue(bench.smoke_ok(one_off)[0])
        self.assertFalse(bench.smoke_ok(hn.Result("ok", "please send"))[0])
        self.assertFalse(bench.smoke_ok(hn.Result("partial", bench.SMOKE_TEXT))[0])
        self.assertFalse(bench.smoke_ok(hn.Result("err_ready"))[0])

    def test_unknown_apps_are_named(self):
        with self.assertRaisesRegex(SystemExit, "handy"):
            bench.app_config("nosuchapp")


if __name__ == "__main__":
    unittest.main()
