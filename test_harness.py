"""Tests for harness.py: key chords, app configs, and the clip loop against the stub app on a simulated
clock (outcomes, late text, latency from the end of speech, retries only for infrastructure faults,
resume bound to the run fingerprint). No macOS, network or key."""
import contextlib
import csv
import io
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness as hn  # noqa: E402

REF = "the quick brown fox jumps over the lazy dog"


def clip(i=1, seconds=3.0, ref=REF, speech_end=2.8):
    return hn.Clip(f"c{i}", pathlib.Path(f"c{i}.flac"), seconds, speech_end_s=speech_end, reference=ref)


def rig(**app_kw):
    clock = hn.FakeClock()
    app = hn.StubApp(clock, **app_kw)
    return clock, app, hn.StubDriver(app), hn.StubTarget(app)


QUIET = dict(log=lambda *_: None)


class HotkeyTests(unittest.TestCase):
    def test_a_modifier_alone_is_one_flags_changed_key(self):
        (k,) = hn.parse_hotkey("ralt")
        self.assertEqual((k.code, k.modifier), (61, True))
        self.assertEqual(hn.parse_hotkey("Right Option"), hn.parse_hotkey("rightoption"))

    def test_a_chord_puts_modifiers_first_and_rejects_other_orders(self):
        self.assertEqual([k.name for k in hn.parse_hotkey("option+space")], ["lalt", "space"])
        for bad in ("space+lalt", "space+grave", "nosuchkey", ""):
            with self.assertRaises(ValueError, msg=bad):
                hn.parse_hotkey(bad)

    def test_events_carry_the_right_flags(self):
        alt = hn.ALTERNATE | 0x20
        keys = hn.parse_hotkey("lalt+space")
        self.assertEqual(hn.key_events(keys, True), [(58, True, alt | hn.NON_COALESCED, True), (49, True, alt, False)])
        self.assertEqual(hn.key_events(keys, False), [(49, False, alt, False), (58, False, hn.NON_COALESCED, True)])
        self.assertEqual(hn.key_events(hn.parse_hotkey("ralt"), True)[0][2], hn.ALTERNATE | 0x40 | hn.NON_COALESCED)
        self.assertEqual(hn.key_events(hn.parse_hotkey("fn"), True)[0][2], hn.SECONDARY_FN | hn.NON_COALESCED)

    def test_hybrid_and_toggle_apps_restart_after_an_unclear_attempt(self):
        base = dict(version="1", process="p", app_path="/A.app", hotkey="ralt")
        self.assertFalse(hn.restarts_after_unclear(hn.App(name="h", **base)))
        self.assertTrue(hn.restarts_after_unclear(hn.App(name="t", mode="toggle", **base)))
        self.assertTrue(hn.restarts_after_unclear(hn.App(name="y", hybrid=True, **base)))
        with self.assertRaises(ValueError):
            hn.App(name="bad", mode="toggle", hybrid=True, **base)

    def test_ci_and_local_runs_use_one_protocol(self):
        a, b = hn.accuracy_only(), hn.Timing()
        self.assertFalse(a.timed)
        self.assertEqual((a.pad_ms, a.start_delay, a.tail, a.settle, a.timeout), (300, 0.5, 4.0, 2.0, 30.0))
        self.assertEqual((a.settle, a.timeout, a.quiet), (b.settle, b.timeout, b.quiet))

    def test_a_config_is_named_app_dot_profile(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.shipped.json"
            p.write_text(json.dumps({"name": "x.other", "version": "1", "process": "x", "app_path": "/X.app",
                                     "hotkey": "ralt"}))
            with self.assertRaisesRegex(ValueError, "name must be"):
                hn.load_app(p)
            q = pathlib.Path(d) / "x.fast.json"
            q.write_text(p.read_text().replace("x.other", "x.fast"))
            with self.assertRaisesRegex(ValueError, "profile"):
                hn.load_app(q)

    def test_every_checked_in_app_config_loads(self):
        for path in sorted((HERE / "apps").glob("*.json")):
            app = hn.load_app(path)
            self.assertEqual(app.name, path.stem)
            self.assertTrue(app.release.get("sha256") or app.release.get("build"), path.name)
            self.assertTrue(app.model.get("id"), path.name)


class SpeechEndTests(unittest.TestCase):
    def test_the_end_of_speech_is_the_last_loud_frame(self):
        rate = 1000
        sig = [0.0] * 300 + [0.5, -0.5] * 500 + [0.001] * 700  # 0.3 s silence, 1 s tone, 0.7 s near-silence
        self.assertAlmostEqual(hn.speech_end(sig, rate), 1.32, delta=0.03)
        self.assertEqual(hn.speech_end([0.0] * 100, rate), 0.0)


class ClipTests(unittest.TestCase):
    def test_ok_waits_for_the_microphone_and_times_from_the_end_of_speech(self):
        clock, app, drv, tgt = rig(open_delay=0.3, latency=1.2)
        res = hn.run_clip(clip(), drv, tgt, clock, hn.Timing())
        self.assertEqual((res.status, res.transcript), ("ok", REF))
        self.assertAlmostEqual(res.ready_s, 0.3, delta=0.006)
        # release = end of clip (3.0 s + 0.3 s pad + 0.3 s pad) + 4 s tail; speech ended at 0.3 + 2.8 s.
        self.assertAlmostEqual(res.post_stop_s, 1.2, delta=0.021)
        self.assertAlmostEqual(res.e2t_s, (3.6 - 3.1) + 4.0 + 1.2, delta=0.061)  # two 20 ms polls

    def test_streaming_text_is_seen_during_playback(self):
        clock, app, drv, tgt = rig(streaming=True)
        res = hn.run_clip(clip(), drv, tgt, clock, hn.Timing())
        self.assertEqual((res.status, res.transcript), ("ok", REF))
        self.assertLess(res.first_text_s, 0)            # the first words came while speaking
        self.assertLess(res.post_stop_s, 0)             # done before the release

    def test_text_pasted_in_pieces_is_timed_to_the_last_change(self):
        clock, app, drv, tgt = rig(latency=1.0, paste_in_chunks=4)
        res = hn.run_clip(clip(), drv, tgt, clock, hn.Timing())
        self.assertAlmostEqual(res.post_stop_s, 1.3, delta=0.021)

    def test_no_text_is_empty_and_text_after_the_deadline_is_late(self):
        clock, app, drv, tgt = rig()
        self.assertEqual(hn.run_clip(clip(ref=""), drv, tgt, clock, hn.Timing(timeout=5)).status, "empty")
        clock, app, drv, tgt = rig(latency=7.0)
        res = hn.run_clip(clip(), drv, tgt, clock, hn.Timing(timeout=5, quiet=5))
        self.assertEqual((res.status, res.transcript), ("late", REF))
        self.assertIsNone(res.e2t_s)

    def test_a_paste_just_after_the_deadline_earns_nothing(self):
        clock, app, drv, tgt = rig(latency=30.001)  # pastes 1 ms after stop + 30 s
        res = hn.run_clip(clip(), drv, tgt, clock, hn.Timing())
        self.assertEqual((res.status, res.post_stop_s), ("late", None))

    def test_a_read_that_straddles_the_deadline_is_dropped(self):
        def run(latency):
            clock, app, drv, tgt = rig(latency=latency)
            stop = {}
            release = drv.release
            drv.release = lambda: stop.setdefault("t", release())

            class Straddling(hn.StubTarget):
                def read(self):  # the read that starts in the last 0.1 s ends at stop + 30.05
                    if "t" in stop and 29.9 <= clock.now() - stop["t"] < 30:
                        clock.sleep(stop["t"] + 30.05 - clock.now())
                    return super().read()
            return hn.run_clip(clip(), drv, Straddling(app), clock, hn.Timing())
        res = run(30.02)  # pasted 20 ms after the deadline, seen only by the straddling read
        self.assertEqual((res.status, res.post_stop_s), ("late", None))
        self.assertEqual(run(29.5).status, "partial")  # pasted before: counts (still settling at the deadline)

    def test_text_still_changing_at_the_deadline_is_partial(self):
        clock, app, drv, tgt = rig(latency=4.0, paste_in_chunks=40)  # 40 pieces 0.1 s apart from 4 s
        res = hn.run_clip(clip(), drv, tgt, clock, hn.Timing(timeout=5, settle=2))
        self.assertEqual(res.status, "partial")
        self.assertTrue(REF.startswith(res.transcript))

    def test_waiting_for_the_microphone_keeps_the_start_of_the_clip(self):
        class Blind(hn.StubDriver):
            def mic_open(self):
                return None
        clock, app, drv, tgt = rig(open_delay=1.5)
        self.assertEqual(hn.run_clip(clip(), drv, tgt, clock, hn.Timing()).transcript, REF)
        clock, app, _, tgt = rig(open_delay=1.5)
        res = hn.run_clip(clip(), Blind(app), tgt, clock, hn.Timing())
        self.assertTrue(REF.startswith(res.transcript) and res.transcript != REF)

    def test_text_that_stopped_changing_before_the_speech_ended_isnt_this_clips(self):
        class Stale(hn.StubTarget):  # another app's paste, there from the start of playback, never changing
            def read(self):
                return "an earlier dictation" if drv.played else ""
        clock, app, drv, _ = rig()
        played = drv.play
        drv.played = False
        drv.play = lambda c: (setattr(drv, "played", True), played(c))[1]
        with self.assertRaises(hn.InfraError) as e:
            hn.run_clip(clip(ref=""), drv, Stale(app), clock, hn.Timing())
        self.assertEqual(e.exception.status, "err_stale")

    def test_a_microphone_that_never_opens_is_an_infrastructure_fault(self):
        clock, app, drv, tgt = rig(never_opens=True)
        with self.assertRaises(hn.InfraError) as e:
            hn.run_clip(clip(), drv, tgt, clock, hn.Timing())
        self.assertEqual(e.exception.status, "err_ready")


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.dir.name) / "run"

    def tearDown(self):
        self.dir.cleanup()

    def run_all(self, clips, rig_, timing=None, fp="f1", restart=False, attempts=3):
        clock, app, drv, tgt = rig_
        return hn.run_all(clips, drv, tgt, clock, timing or hn.Timing(), self.out,
                          run={"fingerprint": fp, "clip_ids": [c.clip_id for c in clips]},
                          restart_after_ambiguous=restart, attempts=attempts, **QUIET)

    def test_app_outcomes_are_final_and_faults_are_retried(self):
        r = rig(latency=50.0)  # never in time: empty/late on the first attempt, no retry
        rows = self.run_all([clip(1)], r, hn.Timing(timeout=5, quiet=1))
        self.assertEqual((rows["c1"]["status"], rows["c1"]["attempts"]), ("empty", 1))
        r = rig(never_opens=True)
        self.out = self.out.with_name("run2")
        rows = self.run_all([clip(2)], r, fp="f2")
        self.assertEqual((rows["c2"]["status"], rows["c2"]["attempts"]), ("err_ready", 3))

    def test_a_pasted_refusal_notice_fails_the_clip_and_stops_the_run_unscored(self):
        notice = "Your trial has ended. Upgrade to VoiceInk Pro at tryvoiceink.com/buy"
        clips = [clip(1, ref=notice + " " + REF), clip(2), clip(3)]
        r = rig()
        with self.assertRaises(hn.AppRefused):
            hn.run_all(clips, r[2], r[3], r[0], hn.Timing(), self.out,
                       run={"fingerprint": "f1", "clip_ids": [c.clip_id for c in clips]},
                       restart_after_ambiguous=False, refusal_text=["your trial has ended"], **QUIET)
        rows = hn.load_rows(self.out / "results.csv")
        self.assertEqual(list(rows), ["c1"])  # nothing after it was run
        self.assertEqual((rows["c1"]["status"], rows["c1"]["attempts"]), ("refused", "1"))
        self.assertNotIn("refused", hn.APP_OUTCOMES)  # so no score accepts the run
        self.assertEqual(r[1].restarts, 1)
        # Without refusal phrases (any other app) the same text is just a transcript.
        self.out = self.out.with_name("run2")
        rows = self.run_all(clips[:1], rig(), fp="f2")
        self.assertEqual(rows["c1"]["status"], "ok")

    def test_refusal_matches_case_insensitively(self):
        self.assertEqual(hn.refusal("x YOUR TRIAL HAS ENDED y", ["Your trial has ended"]), "Your trial has ended")
        self.assertEqual(hn.refusal("all fine", ["Your trial has ended"]), "")

    def test_any_app_is_restarted_after_an_outcome_that_isnt_ok(self):
        r = rig(latency=50.0)  # a hold app: empty, its paste still pending
        self.run_all([clip(1)], r, hn.Timing(timeout=5, quiet=1))
        self.assertEqual(r[1].restarts, 1)

    def test_a_toggle_app_is_restarted_after_an_unclear_outcome(self):
        r = rig(latency=50.0)
        self.run_all([clip(1)], r, hn.Timing(timeout=5, quiet=1), restart=True)
        self.assertEqual(r[1].restarts, 1)

    def test_resume_needs_the_same_fingerprint_and_redoes_only_faults(self):
        r = rig()
        clips = [clip(1), clip(2), clip(3)]
        self.run_all(clips, r)
        rows = hn.load_rows(self.out / "results.csv")
        rows["c2"]["status"] = "err_read"
        hn.write_rows(self.out / "results.csv", rows)
        before = r[1].presses
        rows = self.run_all(clips, r)
        self.assertEqual(r[1].presses - before, 1)
        self.assertEqual({x["status"] for x in rows.values()}, {"ok"})
        with self.assertRaises(SystemExit):
            self.run_all(clips, r, fp="another run")

    def test_resume_drops_rows_of_clips_not_in_this_selection(self):
        r = rig()
        self.run_all([clip(1), clip(2)], r)
        rows = self.run_all([clip(1)], r)
        self.assertEqual(list(rows), ["c1"])
        with (self.out / "results.csv").open(newline="") as fh:
            self.assertEqual([x["clip_id"] for x in csv.DictReader(fh)], ["c1"])

    def test_duplicate_rows_are_refused(self):
        self.out.mkdir()
        hn.write_rows(self.out / "results.csv", {"a": {"clip_id": "c1"}, "b": {"clip_id": "c1"}})
        with self.assertRaises(SystemExit):
            hn.load_rows(self.out / "results.csv")

    def test_the_apps_peak_memory_goes_into_run_json(self):
        clock, app, drv, tgt = rig()
        readings = iter([300.0, 512.5, 400.0])
        drv.rss_mb = lambda: next(readings)
        clips = [clip(1), clip(2), clip(3)]
        hn.run_all(clips, drv, tgt, clock, hn.Timing(), self.out, run={"fingerprint": "f", "clip_ids": ["c1", "c2", "c3"]},
                   restart_after_ambiguous=False, **QUIET)
        self.assertEqual(json.loads((self.out / "run.json").read_text())["peak_rss_mb"], 512.5)

    def test_run_json_records_the_run(self):
        self.run_all([clip(1)], rig())
        self.assertEqual(json.loads((self.out / "run.json").read_text())["fingerprint"], "f1")

    def test_fingerprints_change_with_any_part(self):
        a = hn.fingerprint(app={"model": "x"}, timing=1)
        self.assertEqual(a, hn.fingerprint(timing=1, app={"model": "x"}))
        self.assertNotEqual(a, hn.fingerprint(app={"model": "y"}, timing=1))

    def test_dry_run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rows = hn.dry_run(pathlib.Path(self.dir.name) / "dry", 3)
        self.assertEqual({r["status"] for r in rows.values()}, {"ok"})


class LoadClipsTests(unittest.TestCase):
    def test_load_clips_reads_the_manifest_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)
            with (p / "manifest.csv").open("w", newline="") as fh:
                w = csv.DictWriter(fh, ["file", "clip_id", "audio_length_s", "intended_text"])
                w.writeheader()
                w.writerow({"file": "b.flac", "clip_id": "b", "audio_length_s": "2.5", "intended_text": "B"})
                w.writerow({"file": "a.flac", "clip_id": "a", "audio_length_s": "1", "intended_text": "A"})
            self.assertEqual([(c.clip_id, c.path.name, c.audio_s) for c in hn.load_clips(p)],
                             [("b", "b.flac", 2.5), ("a", "a.flac", 1.0)])
            self.assertEqual([c.clip_id for c in hn.load_clips(p, only={"a"})], ["a"])


if __name__ == "__main__":
    unittest.main()
