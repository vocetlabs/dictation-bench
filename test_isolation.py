"""Tests for host-run safety (isolation.py, bench.py, macos.py): host runs only in the bench's Standard
account, app state cleared only inside its home, one host run at a time, signals held during a restore, apps
quit by PID from the bench folder only, the scratch document matched by exact path, strict preference reads,
setup never overwrites by default, runs scored only under their profile's settings, and a failed smoke test
prints counts only. Fakes only, no macOS."""
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


class AccountTests(unittest.TestCase):
    def test_host_runs_only_as_the_bench_user_and_never_as_an_admin(self):
        old = os.environ.pop("BENCH_USER", None)
        try:
            iso.check_bench_account("vocetbench", {"staff", "everyone"})
            with self.assertRaisesRegex(SystemExit, "only in the bench's own macOS account"):
                iso.check_bench_account("owner", {"staff"})
            with self.assertRaisesRegex(SystemExit, "administrator"):
                iso.check_bench_account("vocetbench", {"staff", "admin"})
            os.environ["BENCH_USER"] = "otherbench"
            iso.check_bench_account("otherbench", {"staff"})
            with self.assertRaises(SystemExit):
                iso.check_bench_account("vocetbench", {"staff"})
        finally:
            os.environ.pop("BENCH_USER", None)
            if old is not None:
                os.environ["BENCH_USER"] = old

    def test_host_run_and_restore_host_check_the_account_first(self):
        orig = iso.check_bench_account

        def refuse(*_a, **_k):
            raise SystemExit("not the bench account")
        iso.check_bench_account = refuse
        try:
            with self.assertRaisesRegex(SystemExit, "not the bench account"):
                bench.restore_host()
            with self.assertRaisesRegex(SystemExit, "not the bench account"):
                bench.main(["host-run", "--jobs", "handy:shipped"])
        finally:
            iso.check_bench_account = orig


class ClearStateTests(unittest.TestCase):
    def test_state_is_deleted_inside_home_only(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d) / "home"
            data = home / "Library" / "Application Support" / "com.example"
            (data / "models").mkdir(parents=True)
            (data / "models" / "m.bin").write_bytes(b"m")
            calls = []
            run = lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=1, stderr="Domain (x) not found. does not exist")  # noqa: E731
            iso.clear_state([str(data), str(home / "never-existed")], ["com.example"], home=str(home), run=run)
            self.assertFalse(data.exists())
            self.assertEqual(calls, [["defaults", "delete", "com.example"]])
            outside = pathlib.Path(d) / "Applications" / "X.app"
            outside.mkdir(parents=True)
            with self.assertRaisesRegex(SystemExit, "isn't inside this account's home"):
                iso.clear_state([str(outside)], [], home=str(home), run=run)
            with self.assertRaisesRegex(SystemExit, "isn't inside"):
                iso.clear_state([str(home / ".." / "Applications" / "X.app")], [], home=str(home), run=run)
            self.assertTrue(outside.exists())

    def test_a_symlink_is_removed_never_followed(self):
        with tempfile.TemporaryDirectory() as d:
            home, target = pathlib.Path(d) / "home", pathlib.Path(d) / "elsewhere"
            home.mkdir()
            target.mkdir()
            (target / "keep").write_text("x")
            link = home / "link"
            link.symlink_to(target)
            iso.clear_state([str(link)], [], home=str(home), run=lambda cmd: SimpleNamespace(returncode=0))
            self.assertFalse(link.is_symlink())
            self.assertTrue((target / "keep").exists())

    def test_a_failed_preference_delete_stops_the_job(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(SystemExit, "couldn't clear"):
                iso.clear_state([], ["com.x"], home=d,
                                run=lambda cmd: SimpleNamespace(returncode=1, stderr="cfprefsd not responding"))


class LockTests(unittest.TestCase):
    def test_one_host_run_or_restore_at_a_time(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            lock = pathlib.Path(d) / "host.lock"
            with iso.host_lock(lock):
                other = subprocess.run([sys.executable, "-c", "import sys, isolation; "
                                        "exec('with isolation.host_lock(__import__(\"pathlib\").Path(sys.argv[1])): pass')",
                                        str(lock)], cwd=HERE, capture_output=True, text=True)
                self.assertNotEqual(other.returncode, 0)
                self.assertIn("another host-run or restore-host", other.stderr)
            with iso.host_lock(lock):  # released
                pass

class PrefTests(unittest.TestCase):
    def test_absent_only_when_defaults_says_so(self):
        absent = lambda cmd: SimpleNamespace(returncode=1, stdout="", stderr="The domain/default pair of (x, y) does not exist")  # noqa: E731
        failed = lambda cmd: SimpleNamespace(returncode=1, stdout="", stderr="cfprefsd not responding")  # noqa: E731
        present = lambda cmd: SimpleNamespace(returncode=0, stdout="0\n", stderr="")  # noqa: E731
        self.assertIsNone(iso.read_pref("K", run=absent))
        self.assertEqual(iso.read_pref("K", run=present), "0")
        with self.assertRaises(iso.RestoreError):
            iso.read_pref("K", run=failed)


class SignalTests(unittest.TestCase):
    def test_signals_wait_while_a_restore_runs_then_stop_the_run(self):
        old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
        try:
            iso.on_termination()
            done = []
            with self.assertRaises(SystemExit):
                with iso.signals_held():
                    os.kill(os.getpid(), signal.SIGTERM)
                    for _ in range(1000):
                        pass
                    done.append("restored")  # not cut off
            self.assertEqual(done, ["restored"])
        finally:
            for s, h in old.items():
                signal.signal(s, h)

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

    def test_settings_inside_json_preference_data_and_env_files_are_read_back(self):
        import plistlib
        mode = [{"selectedLanguage": "en", "isAIEnhancementEnabled": False}]
        plist = plistlib.dumps({"modeConfigurationsV2": json.dumps(mode).encode(), "Current": "parakeet"})
        a = app(settings={"Current": "parakeet", "modeConfigurationsV2/0/selectedLanguage": "en",
                          "modeConfigurationsV2/0/isAIEnhancementEnabled": False},
                settings_source={"defaults": "com.x"})
        prefs = {"Current": "parakeet"}
        got, problems = bench.settings_problems(a, read_pref=lambda k, dom: prefs.get(k), export=lambda dom: plist)
        self.assertEqual(problems, [])
        self.assertIs(got["modeConfigurationsV2/0/isAIEnhancementEnabled"], False)
        a.settings["modeConfigurationsV2/0/selectedLanguage"] = "auto"
        self.assertIn("selectedLanguage", bench.settings_problems(a, read_pref=lambda k, dom: prefs.get(k),
                                                                  export=lambda dom: plist)[1][0])
        self.assertIsNone(bench.json_in_pref("modeConfigurationsV2/5/x", "com.x", export=lambda dom: plist))
        self.assertIsNone(bench.json_in_pref("missing/0", "com.x", export=lambda dom: None))
        with tempfile.TemporaryDirectory() as d:
            env = pathlib.Path(d) / ".env"
            env.write_text("# OpenWhispr\nDICTATION_KEY=RightOption\nOPENAI_API_KEY=not-read\n")
            b = app(settings={"DICTATION_KEY": "RightOption", "CLEANUP_PROVIDER": None},
                    settings_source={"env": str(env)})
            got, problems = bench.settings_problems(b)
            self.assertEqual((got, problems), ({"DICTATION_KEY": "RightOption", "CLEANUP_PROVIDER": None}, []))
            env.write_text("DICTATION_KEY=GLOBE\nCLEANUP_PROVIDER=local\n")
            self.assertEqual(len(bench.settings_problems(b)[1]), 2)

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


class QuitTests(unittest.TestCase):
    """Apps are quit by PID, only the processes launched from the bench's folder; never by name."""

    def test_quit_signals_only_the_bench_tree_by_pid(self):
        import macos
        rows = list(BindingTests.ROWS)
        sent = []

        def kill(pid, sig):
            sent.append((pid, sig))
            rows[:] = [r for r in rows if r[0] != pid]
        macos.quit_tree("/bench/work/Applications/", rows=lambda: rows, kill=kill)
        self.assertEqual(sorted(p for p, _ in sent), [10, 11, 12])
        self.assertEqual([r[0] for r in rows], [20])  # the /Applications copy is untouched

    def test_a_copy_outside_the_bench_folder_is_reported_not_quit(self):
        import macos
        self.assertEqual(macos.foreign_copies(BindingTests.ROWS, {"Vocet"}, "/bench/work/Applications/"),
                         ["/Applications/Vocet.app/Contents/MacOS/Vocet"])

    def test_a_run_refuses_while_another_benchmarked_app_runs(self):
        import macos
        rows = BindingTests.ROWS + [(30, 1, "/bench/work/Applications/VoiceInk.app/Contents/MacOS/VoiceInk"),
                                    (31, 1, "/usr/bin/zsh")]
        own, bench_prefix = "/bench/work/Applications/Vocet.app/Contents/", "/bench/work/Applications/"
        self.assertEqual(macos.other_apps(rows, own, {"Vocet", "VoiceInk"}, bench_prefix),
                         ["/Applications/Vocet.app/Contents/MacOS/Vocet",
                          "/bench/work/Applications/VoiceInk.app/Contents/MacOS/VoiceInk"])
        self.assertEqual(macos.other_apps(BindingTests.ROWS[:3], own, {"Vocet"}, bench_prefix), [])
        self.assertIn("VoiceInk", macos.benchmarked_names())

    def test_installs_always_go_to_the_benchs_own_folder(self):
        import macos
        os.environ["BENCH_APPS"] = "/Applications"
        try:
            self.assertEqual(macos.apps_dir(), HERE / "work" / "Applications")
        finally:
            del os.environ["BENCH_APPS"]
        self.assertNotIn("BENCH_APPS", (HERE / "apps" / "common.sh").read_text())

    def test_the_scratch_document_is_matched_by_its_exact_path(self):
        import macos
        scratch = HERE / "work" / "scratch.txt"
        self.assertTrue(macos.is_document(scratch.as_uri(), scratch))
        for other in (None, "", (HERE / "work" / "scratch.txt copy").as_uri(),
                      pathlib.Path("/Users/owner/Documents/scratch.txt").as_uri(), "https://x/scratch.txt"):
            self.assertFalse(macos.is_document(other, scratch), other)


class ExpectedTests(unittest.TestCase):
    def test_expected_absences_and_the_pinned_ollama_digest(self):
        with tempfile.TemporaryDirectory() as d:
            gemma = pathlib.Path(d) / "gemma.gguf"
            a = app(absent_files=[str(gemma)])
            self.assertEqual(bench.settings_problems(a)[1], [])
            gemma.write_bytes(b"x")
            self.assertIn("must be absent", bench.settings_problems(a)[1][0])
        full = "sha256:68ceab7e468a31c988b44d5a4984a0e21f58c4f7158c6ce29c1804babac38df7"
        b = app(model={"id": "m", "cleanup_ollama": "gemma3:4b", "cleanup_ollama_digest": full})
        orig = bench.ollama_digest
        try:
            bench.ollama_digest = lambda m: full
            self.assertEqual(bench.settings_problems(b)[1], [])
            bench.ollama_digest = lambda m: full[:-1] + "0"  # the same 12-hex prefix, another model
            self.assertIn("not the pinned", bench.settings_problems(b)[1][0])
            bench.ollama_digest = lambda m: None
            self.assertIn("isn't installed", bench.settings_problems(b)[1][0])
        finally:
            bench.ollama_digest = orig
        tags = json.dumps({"models": [{"name": "gemma3:4b", "model": "gemma3:4b", "digest": full[7:]}]})
        self.assertEqual(bench.ollama_digest("gemma3:4b", fetch=lambda: tags), full)
        self.assertIsNone(bench.ollama_digest("qwen3:4b", fetch=lambda: tags))

    def test_the_profiles_pin_what_they_expect(self):
        gemma = "models/gemma-4-E2B-it-Q4_K_M.gguf"
        self.assertTrue(any(f.endswith(gemma) for f in bench.app_config("vocet:best-asr").absent_files))
        for n in ("handy:local-cleanup", "fluidvoice:local-cleanup"):
            self.assertRegex(bench.app_config(n).model["cleanup_ollama_digest"], r"^sha256:[0-9a-f]{64}$")


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


class GrantTests(unittest.TestCase):
    """A VM grants permissions through ./bench (`BENCH_CI=1 ./bench setup ...`); a host never does."""

    def setup_env(self, vm: bool, env_ci: str):
        a, seen = app(name="x.shipped"), {}
        orig = (bench.app_config, bench.in_vm, bench.subprocess.run, os.environ.get("BENCH_CI"))
        bench.app_config, bench.in_vm = (lambda n: a), (lambda: vm)
        bench.subprocess.run = lambda cmd, check, env: seen.update(env)
        os.environ["BENCH_CI"] = env_ci
        try:
            bench.setup("x", ci=False, overwrite=True)
        finally:
            bench.app_config, bench.in_vm, bench.subprocess.run = orig[:3]
            if orig[3] is None:
                os.environ.pop("BENCH_CI", None)
            else:
                os.environ["BENCH_CI"] = orig[3]
        return seen.get("BENCH_CI")

    def test_an_inherited_bench_ci_grants_in_a_vm_only(self):
        self.assertEqual(self.setup_env(vm=True, env_ci="1"), "1")
        self.assertEqual(self.setup_env(vm=False, env_ci=""), "")
        with self.assertRaisesRegex(SystemExit, "only in a virtual machine"):
            self.setup_env(vm=False, env_ci="1")


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
