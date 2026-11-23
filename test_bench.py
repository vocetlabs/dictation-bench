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


class ProfileTests(unittest.TestCase):
    def test_names_resolve_to_profiles_shipped_by_default(self):
        self.assertEqual(bench.config_name("handy"), "handy.shipped")
        self.assertEqual(bench.config_name("handy:best-asr"), "handy.best-asr")
        self.assertEqual(bench.app_config("vocet:best-asr").name, "vocet.best-asr")

    def test_the_four_apps_have_all_three_profiles_and_same_as_points_at_a_real_one(self):
        for app in ("handy", "muesli", "fluidvoice", "vocet"):
            for profile in hn.PROFILES:
                a = bench.app_config(f"{app}:{profile}")
                self.assertTrue(a.model.get("id"), a.name)
                if a.same_as:
                    self.assertEqual(bench.app_config(a.same_as).base, app)
                    self.assertIsNotNone(bench.runnable(a))
        self.assertEqual(bench.app_config("vocet").group, "speech + local cleanup")
        self.assertEqual(bench.app_config("vocet:best-asr").group, "raw speech")
        self.assertEqual(bench.app_config("handy:local-cleanup").group, "speech + local cleanup")

    def test_vocet_is_installed_from_the_notarised_release_not_built(self):
        v = bench.app_config("vocet")
        self.assertTrue(v.release["url"].endswith("Vocet_0.2.0_aarch64.dmg"))
        self.assertEqual(len(v.release["sha256"]), 64)
        script = (HERE / "apps" / "vocet.sh").read_text()
        self.assertIn(v.release["sha256"], script)
        self.assertNotIn("git clone", script)

    def test_model_artifacts_hash_files_and_folders(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "m.gguf"
            f.write_bytes(b"x" * 2048)
            folder = pathlib.Path(d) / "m.mlmodelc"
            (folder / "weights").mkdir(parents=True)
            (folder / "weights" / "w.bin").write_bytes(b"y" * 1024)
            app = hn.App(name="a.shipped", version="1", process="p", app_path="/A.app", hotkey="ralt",
                         model={"id": "m", "files": [str(f), str(folder), str(pathlib.Path(d) / "gone")]})
            got = bench.model_artifacts(app)
            self.assertEqual(got["model_sha256"][str(f)], bench.sc.digest(f))
            self.assertEqual(len(got["model_sha256"][str(folder)]), 64)
            self.assertEqual(got["model_sha256"][str(pathlib.Path(d) / "gone")], "missing")
            self.assertEqual(got["model_size_mb"], round(3072 / 2**20, 1))


class HostPlanTests(unittest.TestCase):
    def test_app_order_rotates_and_each_pass_shares_one_clip_order(self):
        plan = bench.host_plan(["a.shipped", "b.shipped", "c.shipped"], passes=3, seed=10)
        self.assertEqual([p["order"][0] for p in plan], ["a.shipped", "b.shipped", "c.shipped"])
        self.assertEqual([p["seed"] for p in plan], [11, 12, 13])
        later = bench.host_plan(["a.shipped", "b.shipped"], passes=1, first_pass=2, seed=10)
        self.assertEqual((later[0]["pass"], later[0]["order"], later[0]["seed"]), (2, ["b.shipped", "a.shipped"], 12))


if __name__ == "__main__":
    unittest.main()
