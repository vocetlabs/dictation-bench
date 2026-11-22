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

    def test_log_counts_are_numbers_from_the_apps_log(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d) / "app.log"
            log.write_text('x cleanup outcome="applied" duration_ms=1\ny cleanup outcome="fallback_timeout"\n'
                           'z cleanup outcome="applied"\n')
            app = hn.App(name="a", version="1", process="p", app_path="/A.app", hotkey="ralt", logs=str(log),
                         log_counts='cleanup outcome="([a-z_]+)"')
            self.assertEqual(bench.log_counts(app), {"applied": 2, "fallback_timeout": 1})
            self.assertEqual(bench.count_diff({"applied": 2, "fallback_timeout": 1}, {"applied": 1}),
                             {"applied": 1, "fallback_timeout": 1})
            self.assertEqual(bench.log_counts(hn.App(name="b", version="1", process="p", app_path="/A.app",
                                                     hotkey="ralt")), {})

    def test_unknown_apps_are_named(self):
        with self.assertRaisesRegex(SystemExit, "handy"):
            bench.app_config("nosuchapp")


if __name__ == "__main__":
    unittest.main()
