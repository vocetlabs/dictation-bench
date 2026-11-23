"""Tests for report.py: means over passes, speaker-bootstrap intervals and paired differences, the results
markdown (groups, conditional columns, disclosure), and the judge check's sample and agreement. No macOS."""
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import report as rp  # noqa: E402

SPEAKERS = {f"c{i}": f"s{i // 2}" for i in range(20)}  # two clips per speaker


def run(config, spoken_edits, cleanup="off", zero=None, wer=None, pass_dir="p1"):
    per_clip = [{"clip_id": f"c{i}", "status": "ok", "spoken_edits": e, "spoken_words": 10,
                 "intended_edits": e, "intended_words": 10} for i, e in enumerate(spoken_edits)]
    judge = None
    if zero is not None:
        judge = {"zer": 100 * sum(zero) / len(zero), "amend": 1.0,
                 "per_clip": [{"clip_id": f"c{i}", "status": "ok", "zero_edit": z} for i, z in enumerate(zero)]}
    w = wer if wer is not None else 100 * sum(spoken_edits) / (10 * len(spoken_edits))
    return {"dir": f"{pass_dir}/{config}", "config": config,
            "app": {"name": config, "cleanup": cleanup, "model": {"id": f"model of {config}"}, "version": "1"},
            "score": {"per_clip": per_clip, "wer_spoken": w, "wer_intended": w, "failure_rate": 0.0,
                      "post_stop_p50_s": 1.0, "post_stop_p95_s": 2.0},
            "judge": judge, "run": {"peak_rss_mb": 800.0, "effective": {"model_size_mb": 600.0}}}


class BootstrapTests(unittest.TestCase):
    def test_a_clear_difference_is_resolved_and_a_tie_is_not(self):
        good = rp.pool([run("a.shipped", [0] * 20)])
        bad = rp.pool([run("b.shipped", [3] * 20)])
        d = rp.bootstrap(bad, good, SPEAKERS, "wer_spoken", n=300, seed=1)
        self.assertEqual(d["value"], 30.0)
        self.assertTrue(d["resolved"])
        tie = rp.bootstrap(good, rp.pool([run("c.shipped", [0] * 20)]), SPEAKERS, "wer_spoken", n=300, seed=1)
        self.assertEqual((tie["value"], tie["resolved"]), (0.0, False))

    def test_noise_in_both_directions_is_unresolved_and_the_seed_repeats(self):
        a = rp.pool([run("a.shipped", [1, 0] * 10)])
        b = rp.pool([run("b.shipped", [0, 1] * 10)])
        d = rp.bootstrap(a, b, SPEAKERS, "wer_spoken", n=500, seed=3)
        self.assertFalse(d["resolved"])
        self.assertEqual(d, rp.bootstrap(a, b, SPEAKERS, "wer_spoken", n=500, seed=3))

    def test_speakers_are_resampled_whole(self):
        samples = list(rp._speaker_samples(SPEAKERS, sorted(SPEAKERS), 50, 0))
        for s in samples:
            for c in s:  # a clip comes with its speaker's other clip
                partner = f"c{int(c[1:]) ^ 1}"
                self.assertEqual(s.count(c), s.count(partner))

    def test_passes_are_pooled_per_clip(self):
        p = rp.pool([run("a.shipped", [1] * 20), run("a.shipped", [3] * 20, pass_dir="p2")])
        self.assertEqual((p["c0"]["spoken_edits"], p["c0"]["spoken_words"]), (4, 20))
        self.assertEqual(rp.ratio(p, list(p), "wer_spoken"), 20.0)


class SummaryTests(unittest.TestCase):
    def runs(self):
        return [run("a.shipped", [1] * 20, zero=[1] * 20), run("a.shipped", [2] * 20, zero=[1] * 20, pass_dir="p2"),
                run("b.shipped", [4] * 20, zero=[0] * 20), run("v.shipped", [2] * 20, cleanup="on", zero=[1] * 20)]

    def test_means_groups_and_neighbour_comparisons(self):
        s = rp.summarize(self.runs(), SPEAKERS, n=200)
        by = {c["config"]: c for c in s["configs"]}
        self.assertEqual(len(by["a.shipped"]["passes"]), 2)
        self.assertEqual(by["a.shipped"]["mean"]["wer_spoken"], 15.0)
        self.assertEqual(by["v.shipped"]["group"], "speech + local cleanup")
        pairs = [(d["metric"], d["a"], d["b"]) for d in s["comparisons"]]
        self.assertIn(("wer_spoken", "a.shipped", "b.shipped"), pairs)  # same group: compared
        self.assertFalse(any("v.shipped" in p for p in pairs))  # alone in its group

    def test_the_markdown_has_both_groups_conditional_labels_and_the_disclosure(self):
        md = rp.markdown(rp.summarize(self.runs(), SPEAKERS, n=100), {"chip": "Apple M4 Pro", "ram_gb": 48})
        self.assertIn("### shipped: raw speech", md)
        self.assertIn("### shipped: speech + local cleanup", md)
        for label in ("Spoken WER % [95% CI]", "ZER % (judge est.)", "AMEND % (clips with output)", "Failure %",
                      "p50 s (ok clips)", "Peak RAM MB", "Model MB"):
            self.assertIn(label, md)
        self.assertIn("model of a.shipped", md)
        self.assertIn("Apple M4 Pro", md)
        tables = md.split("### ")[1:]
        self.assertTrue(all(rp.DISCLOSURE in t for t in tables), "the disclosure beside every table")
        self.assertIn("| a.shipped | 15.00 [15.00, 15.00]".split(" | ")[0], md)
        self.assertIn("| a.shipped | p2/a.shipped | 20.00", md)  # each pass shown on its own
        self.assertIn("| a.shipped | model of a.shipped | 2 | 15.00 [15.00, 15.00]", md)  # pooled, with its CI

    def test_runs_that_cant_be_compared_are_refused(self):
        runs = self.runs()
        for r in runs:
            r["run"].update(timing={"timeout": 30}, manifest_sha256="m", dataset={"n": 1},
                            hardware={"chip": "M4", "ram_gb": 48, "macos_build": "24G"})
            if r["judge"]:
                r["judge"]["judge"] = {"model": "j", "prompt_version": "v1", "prompt_fingerprint": "f"}
        self.assertEqual(rp.check_comparable(runs, SPEAKERS), [])
        runs[1]["run"]["timing"] = {"timeout": 60}
        runs[2]["judge"]["judge"]["model"] = "other"
        problems = rp.check_comparable(runs, {k: v for k, v in SPEAKERS.items() if k != "c3"})
        self.assertTrue(any("protocol" in p for p in problems))
        self.assertTrue(any("judges" in p for p in problems))
        self.assertTrue(any("speaker" in p for p in problems))

    def test_load_run_takes_the_judge_that_judged_these_results(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "score.json").write_text(json.dumps({"app": {"name": "a.shipped"}, "results_sha256": "r1",
                                                      "per_clip": []}))
            (d / "run.json").write_text("{}")
            (d / "judge.json").write_text(json.dumps({"results_sha256": "old", "zer": 1}))
            (d / "judge-claude-cli.json").write_text(json.dumps({"results_sha256": "r1", "zer": 2}))
            self.assertEqual(rp.load_run(d)["judge"]["zer"], 2)


class JudgeCheckTests(unittest.TestCase):
    def test_the_sample_is_stratified_seeded_and_judged_drafts_only(self):
        per_clip = ([{"clip_id": f"z{i}", "status": "ok", "zero_edit": 1} for i in range(90)]
                    + [{"clip_id": f"n{i}", "status": "ok", "zero_edit": 0} for i in range(10)]
                    + [{"clip_id": "e", "status": "empty", "zero_edit": 0}])
        s = rp.stratified_sample(per_clip, 40, seed=5)
        self.assertEqual(len(s), 40)
        self.assertEqual(sum(c.startswith("n") for c in s), 4)  # in proportion
        self.assertNotIn("e", s)
        self.assertEqual(s, rp.stratified_sample(per_clip, 40, seed=5))
        self.assertNotEqual(s, rp.stratified_sample(per_clip, 40, seed=6))
        rare = per_clip[:99] + [{"clip_id": "n_only", "status": "ok", "zero_edit": 0}]
        self.assertIn("n_only", rp.stratified_sample(rare[:90] + rare[-1:], 40, seed=1))  # one of each verdict

    def test_agreement_kappa_and_rank_changes(self):
        ids = [f"c{i}" for i in range(10)]
        a = [{"clip_id": c, "zero_edit": int(i < 5)} for i, c in enumerate(ids)]
        b = [{"clip_id": c, "zero_edit": int(i < 4)} for i, c in enumerate(ids)]
        ag = rp.agreement(a, b, ids)
        self.assertEqual((ag["n"], ag["same_verdict"], ag["agreement"]), (10, 9, 90.0))
        self.assertEqual((ag["zer_primary"], ag["zer_secondary"]), (50.0, 40.0))
        self.assertAlmostEqual(ag["kappa"], 0.8, places=3)
        self.assertEqual(ag["disagreements"], ["c4"])
        checks = {"x": {"zer_primary": 80, "zer_secondary": 60}, "y": {"zer_primary": 70, "zer_secondary": 75}}
        self.assertEqual(sorted(rp.rank_changes(checks)), ["x: rank 1 -> 2", "y: rank 2 -> 1"])


if __name__ == "__main__":
    unittest.main()
