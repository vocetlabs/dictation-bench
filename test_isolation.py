"""Tests for host-run safety (isolation.py, bench.py): the owner's app state is set aside and put back
exactly, preference reads are strict, signals restore, setup never overwrites by default, runs are scored
only under their profile's settings, and a failed smoke test prints counts only. Fakes only, no macOS."""
import json
import os
import pathlib
import signal
import sys
import tempfile
import unittest
from types import SimpleNamespace

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import harness as hn  # noqa: E402
import isolation as iso  # noqa: E402


class FakeDefaults:
    """`defaults` for a few domains: read, export, delete, import, as plist bytes."""

    def __init__(self, domains=None):
        import plistlib
        self.plistlib, self.domains = plistlib, dict(domains or {})

    def export(self, domain):
        return self.plistlib.dumps(self.domains[domain]) if domain in self.domains else None

    def run(self, cmd, **_):
        op, domain = cmd[1], cmd[2]
        if op == "delete":
            self.domains.pop(domain, None)
        elif op == "import":
            self.domains[domain] = self.plistlib.loads(pathlib.Path(cmd[3]).read_bytes())
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class AppStateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.dir.name)
        self.data = self.d / "Application Support" / "com.example"
        (self.data / "models").mkdir(parents=True)
        (self.data / "settings.json").write_text('{"mine": true}')
        (self.data / "models" / "big.gguf").write_bytes(b"m" * 4096)
        self.inode = (self.data / "models" / "big.gguf").stat().st_ino
        self.prefs = FakeDefaults({"com.example": {"Key": "owner", "Flag": True}})

    def tearDown(self):
        self.dir.cleanup()

    def state(self):
        return iso.AppState("example", [str(self.data), str(self.d / "never-existed")], ["com.example", "com.absent"],
                            self.d / "stash", export=self.prefs.export, run=self.prefs.run)

    def test_the_owners_state_comes_back_byte_for_byte_and_the_benchs_is_gone(self):
        st = self.state()
        st.save()
        self.assertFalse(self.data.exists())
        self.assertNotIn("com.example", self.prefs.domains)
        # the bench's job writes its own state
        self.data.mkdir(parents=True)
        (self.data / "settings.json").write_text('{"bench": true}')
        (self.d / "never-existed").write_text("bench")
        self.prefs.domains["com.example"] = {"Key": "bench"}
        self.prefs.domains["com.absent"] = {"X": 1}
        st.restore()
        self.assertEqual((self.data / "settings.json").read_text(), '{"mine": true}')
        self.assertEqual((self.data / "models" / "big.gguf").stat().st_ino, self.inode)  # moved, never copied
        self.assertFalse((self.d / "never-existed").exists())
        self.assertEqual(self.prefs.domains, {"com.example": {"Key": "owner", "Flag": True}})
        self.assertFalse((self.d / "stash" / "manifest.json").exists())

    def test_a_crashed_job_is_finished_by_a_new_process_from_the_stash(self):
        self.state().save()
        self.data.mkdir(parents=True)  # the bench's leftovers
        iso.AppState("example", [], [], self.d / "stash", export=self.prefs.export, run=self.prefs.run).restore()
        self.assertEqual((self.data / "settings.json").read_text(), '{"mine": true}')
        self.assertEqual(self.prefs.domains["com.example"]["Key"], "owner")

    def test_an_unfinished_stash_blocks_a_new_job_and_restore_is_idempotent(self):
        st = self.state()
        st.save()
        with self.assertRaisesRegex(iso.RestoreError, "unfinished"):
            self.state().save()
        st.restore()
        st.restore()  # nothing left to do
        self.assertTrue(self.data.exists())

    def test_a_restore_that_doesnt_match_is_reported(self):
        st = self.state()
        st.save()
        original_import = self.prefs.run

        def broken(cmd, **kw):
            r = original_import(cmd, **kw)
            if cmd[1] == "import":
                self.prefs.domains[cmd[2]] = {"Key": "changed"}
            return r
        st.run = broken
        with self.assertRaisesRegex(iso.RestoreError, "not as they were"):
            st.restore()


class PrefTests(unittest.TestCase):
    def test_absent_only_when_defaults_says_so(self):
        absent = lambda cmd: SimpleNamespace(returncode=1, stdout="", stderr="The domain/default pair of (x, y) does not exist")  # noqa: E731
        failed = lambda cmd: SimpleNamespace(returncode=1, stdout="", stderr="cfprefsd not responding")  # noqa: E731
        present = lambda cmd: SimpleNamespace(returncode=0, stdout="0\n", stderr="")  # noqa: E731
        self.assertIsNone(iso.read_pref("K", run=absent))
        self.assertEqual(iso.read_pref("K", run=present), "0")
        with self.assertRaises(iso.RestoreError):
            iso.read_pref("K", run=failed)

    def test_writes_are_checked(self):
        calls = []
        ok = lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=0, stderr="")  # noqa: E731
        iso.write_bool_pref("K", "1", run=ok)
        iso.write_bool_pref("K", None, run=ok)
        self.assertEqual([c[1] for c in calls], ["write", "delete"])
        self.assertEqual(calls[0][-1], "true")
        with self.assertRaises(iso.RestoreError):
            iso.write_bool_pref("K", "0", run=lambda cmd: SimpleNamespace(returncode=1, stderr="denied"))


class SignalTests(unittest.TestCase):
    def test_sigterm_and_sighup_end_the_run_through_finally(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        try:
            for s in (signal.SIGTERM, signal.SIGHUP):
                iso.on_termination()
                restored = []
                with self.assertRaises(SystemExit):
                    try:
                        os.kill(os.getpid(), s)  # the handler raises at the next bytecode
                        for _ in range(1000):
                            pass
                    finally:
                        restored.append(True)
                self.assertEqual(restored, [True])
        finally:
            for s, h in old.items():
                signal.signal(s, h)


def app(**kw):
    base = dict(name="x.shipped", version="1", process="X", app_path="/X.app", hotkey="ralt")
    return hn.App(**{**base, **kw})


class GateTests(unittest.TestCase):
    def test_a_run_is_scored_only_under_its_profiles_settings(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "settings_store.json"
            f.write_text(json.dumps({"settings": {"selected_model": "m", "post_process_enabled": True}}))
            a = app(settings={"selected_model": "m", "post_process_enabled": True},
                    settings_source={"file": str(f), "key": "settings"})
            got, problems = bench.settings_problems(a)
            self.assertEqual((got["selected_model"], problems), ("m", []))
            a.settings["post_process_selected_prompt_id"] = "default_improve_transcriptions"
            self.assertTrue(bench.settings_problems(a)[1])  # missing: not that profile
        prefs = {"SelectedSpeechModel": "parakeet-tdt", "EnableAIProcessing": "1"}
        b = app(settings={"SelectedSpeechModel": "parakeet-tdt", "EnableAIProcessing": True},
                settings_source={"defaults": "com.x"})
        self.assertEqual(bench.settings_problems(b, read_pref=lambda k, dom: prefs.get(k))[1], [])
        prefs["EnableAIProcessing"] = "0"
        self.assertIn("EnableAIProcessing", bench.settings_problems(b, read_pref=lambda k, dom: prefs.get(k))[1][0])

    def test_unverified_profiles_and_wrong_settings_are_refused_before_anything_runs(self):
        with self.assertRaisesRegex(SystemExit, "isn't verified"):
            bench.run_job(app(verify="check it in a VM"))
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "s.json"
            f.write_text('{"a": 1}')
            with self.assertRaisesRegex(SystemExit, "can't be scored"):
                bench.run_job(app(settings={"a": 2}, settings_source={"file": str(f)}))
            with self.assertRaisesRegex(SystemExit, "model file missing"):
                bench.run_job(app(model={"id": "m", "files": [str(pathlib.Path(d) / "gone.gguf")]}))

    def test_the_checked_in_profiles_name_their_settings_source_and_state(self):
        for name in ("handy", "muesli", "fluidvoice", "vocet"):
            for profile in hn.PROFILES:
                a = bench.app_config(f"{name}:{profile}")
                self.assertTrue(a.settings_source, a.name)
                self.assertTrue(a.state.get("paths") or a.state.get("defaults"), a.name)
        self.assertEqual(bench.app_config("handy:local-cleanup").settings["post_process_selected_prompt_id"],
                         "default_improve_transcriptions")
        self.assertEqual(bench.app_config("fluidvoice:best-asr").settings["SelectedSpeechModel"], "whisper-large")
        self.assertIn("com.dictationapp.desktop", " ".join(bench.app_config("vocet").state["paths"]))


class BindingTests(unittest.TestCase):
    """Readiness binds to the processes launched from the bench's own bundle, with no device-wide fallback."""

    ROWS = [(10, 1, "/bench/work/Applications/Vocet.app/Contents/MacOS/Vocet"),
            (11, 10, "/bench/work/Applications/Vocet.app/Contents/MacOS/llama-helper"),
            (12, 11, "/usr/bin/some-child"),
            (20, 1, "/Applications/Vocet.app/Contents/MacOS/Vocet")]  # the owner's copy, same name

    def test_the_tree_is_the_bench_copy_and_its_descendants_only(self):
        import macos
        self.assertEqual(macos.tree_under(self.ROWS, "/bench/work/Applications/Vocet.app/Contents/"), {10, 11, 12})

    def test_readiness_needs_the_bench_copy_to_be_the_only_capturer(self):
        import macos
        tree = {10, 11, 12}
        self.assertTrue(macos.mic_decision({11}, tree))      # its helper captures
        self.assertFalse(macos.mic_decision(set(), tree))    # nothing yet
        for capturing, app_tree, why in ((None, tree, "can't tell"), ({11, 20}, tree, "another process"),
                                         ({20}, set(), "isn't running")):
            with self.assertRaisesRegex(hn.InfraError, why):
                macos.mic_decision(capturing, app_tree)


class ExpectedTests(unittest.TestCase):
    def test_expected_absences_and_the_pinned_ollama_digest(self):
        with tempfile.TemporaryDirectory() as d:
            gemma = pathlib.Path(d) / "gemma.gguf"
            a = app(absent_files=[str(gemma)])
            self.assertEqual(bench.settings_problems(a)[1], [])
            gemma.write_bytes(b"x")
            self.assertIn("must be absent", bench.settings_problems(a)[1][0])
        b = app(model={"id": "m", "cleanup_ollama": "gemma3:4b", "cleanup_ollama_digest": "sha256:68ceab7e468a31c9"})
        orig = bench.ollama_id
        try:
            bench.ollama_id = lambda m: "68ceab7e468a"
            self.assertEqual(bench.settings_problems(b)[1], [])
            bench.ollama_id = lambda m: "0123456789ab"
            self.assertIn("not the pinned", bench.settings_problems(b)[1][0])
            bench.ollama_id = lambda m: None
            self.assertIn("isn't installed", bench.settings_problems(b)[1][0])
        finally:
            bench.ollama_id = orig
        listing = SimpleNamespace(stdout="NAME ID SIZE MODIFIED\ngemma3:4b 68ceab7e468a 3.3 GB now\n")
        self.assertEqual(bench.ollama_id("gemma3:4b", run=lambda *a, **k: listing), "68ceab7e468a")

    def test_the_profiles_pin_what_they_expect(self):
        gemma = "models/gemma-4-E2B-it-Q4_K_M.gguf"
        self.assertTrue(any(f.endswith(gemma) for f in bench.app_config("vocet:best-asr").absent_files))
        for n in ("handy:local-cleanup", "fluidvoice:local-cleanup"):
            self.assertTrue(bench.app_config(n).model["cleanup_ollama_digest"].startswith("sha256:68ceab7e468a"))


class SetupTests(unittest.TestCase):
    def test_setup_refuses_to_overwrite_the_owners_settings(self):
        with tempfile.TemporaryDirectory() as d:
            mine = pathlib.Path(d) / "com.example"
            mine.mkdir()
            a = app(name="x.shipped", state={"paths": [str(mine)]})
            orig = bench.app_config
            bench.app_config = lambda n: a
            try:
                with self.assertRaisesRegex(SystemExit, "would overwrite"):
                    bench.setup("x", ci=False)
            finally:
                bench.app_config = orig


class LogTests(unittest.TestCase):
    def test_a_failed_smoke_prints_counts_never_lines(self):
        with tempfile.TemporaryDirectory() as d:
            log = pathlib.Path(d) / "app.log"
            log.write_text("INFO transcript: please send the quarterly report\nERROR model failed\nWARN slow\n")
            out = bench.log_health(app(logs=str(log)))
            self.assertIn("3 lines, 1 with 'error', 1 with 'warn'", out)
            self.assertNotIn("quarterly", out)


if __name__ == "__main__":
    unittest.main()
