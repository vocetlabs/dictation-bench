"""Tests for score.py: WER on fixed strings, Unicode folding, and refusal of incomplete, duplicated, faulty
or mismatched runs; the per-clip numbers recompute the headline. No network or key."""
import csv
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness as hn  # noqa: E402
import score as sc  # noqa: E402

MAN = [{"clip_id": "c1", "spoken_text": "hello bob", "intended_text": "Hello Bob."},
       {"clip_id": "c2", "spoken_text": "run make now", "intended_text": "Run make."},
       {"clip_id": "c3", "spoken_text": "done", "intended_text": "Done."}]


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, list(rows[0]))
        w.writeheader()
        w.writerows(rows)


class WordsTests(unittest.TestCase):
    def test_typography_does_not_change_the_words(self):
        self.assertEqual(sc.words("Don’t “stop” — now!"), ["don't", "stop", "now"])
        self.assertEqual(sc.words("Don't stop - now"), ["don't", "stop", "now"])
        self.assertEqual(sc.words("été"), sc.words("été"))  # NFKC
        self.assertEqual(sc.words("Bob's  team!"), ["bob's", "team"])

    def test_edit_distance(self):
        self.assertEqual(sc.edit_distance(["a", "b", "c"], ["a", "x", "c", "d"]), 2)
        self.assertEqual(sc.edit_distance([], ["a"]), 1)


class ScoreRunTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.dir.name)
        self.man = self.d / "manifest.csv"
        write_csv(self.man, MAN)
        self.run = self.d / "run"
        self.run.mkdir()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, rows, clip_ids=("c1", "c2", "c3"), timed=True, manifest_sha=None):
        full = [{**{f: "" for f in hn.FIELDS}, **r} for r in rows]
        hn.write_rows(self.run / "results.csv", {str(i): r for i, r in enumerate(full)})
        (self.run / "run.json").write_text(json.dumps({
            "fingerprint": "fp", "clip_ids": list(clip_ids), "timed": timed,
            "manifest_sha256": manifest_sha or sc.digest(self.man), "app": {"name": "x", "version": "1"}}))

    def good(self):
        return [{"clip_id": "c1", "status": "ok", "transcript": "Hello, Bob.", "e2t_s": "0.5", "post_stop_s": "0.4"},
                {"clip_id": "c2", "status": "ok", "transcript": "Run bake now.", "e2t_s": "0.7", "post_stop_s": "0.6"},
                {"clip_id": "c3", "status": "empty", "transcript": ""}]

    def test_a_complete_run_scores_and_no_output_is_deletions(self):
        self.write(self.good())
        s = sc.score_run(self.run, self.man)
        self.assertEqual((s["spoken_edits"], s["spoken_words"]), (2, 6))      # bake; "done" deleted
        self.assertEqual((s["intended_edits"], s["intended_words"]), (3, 5))  # bake, +now; "done" deleted
        self.assertEqual(s["wer_intended"], 60.0)
        self.assertEqual(s["outcomes"]["empty"], 1)
        self.assertEqual(s["failure_rate"], 33.3)
        self.assertEqual((s["e2t_p50_s"], s["latency_n"]), (0.5, 2))
        self.assertEqual(sc.verify(s), [])
        self.assertNotIn("transcript", json.dumps(s["per_clip"]))
        self.assertNotIn("Bob", json.dumps(s))  # numbers only

    def test_a_missing_clip_is_refused_not_scored_as_a_subset(self):
        self.write(self.good()[:2])
        with self.assertRaisesRegex(sc.Incomplete, "1 missing"):
            sc.score_run(self.run, self.man)

    def test_extra_duplicate_and_faulty_rows_are_refused(self):
        self.write(self.good(), clip_ids=("c1", "c2"))
        with self.assertRaisesRegex(sc.Incomplete, "1 extra"):
            sc.score_run(self.run, self.man)
        rows = self.good()
        rows[2] = {"clip_id": "c3", "status": "err_read", "transcript": ""}
        self.write(rows)
        with self.assertRaisesRegex(sc.Incomplete, "infrastructure fault"):
            sc.score_run(self.run, self.man)
        self.write(self.good() + [self.good()[0]])
        with self.assertRaises((sc.Incomplete, SystemExit)):
            sc.score_run(self.run, self.man)

    def test_another_manifest_is_refused(self):
        self.write(self.good(), manifest_sha="0" * 64)
        with self.assertRaisesRegex(sc.Incomplete, "manifest"):
            sc.score_run(self.run, self.man)

    def test_a_manifest_without_references_is_refused(self):
        write_csv(self.man, [{"clip_id": "c1", "intended_text": "x"}])
        self.write([self.good()[0]], clip_ids=("c1",))
        with self.assertRaisesRegex(sc.Incomplete, "spoken_text"):
            sc.score_run(self.run, self.man)

    def test_an_untimed_run_reports_no_latency(self):
        self.write(self.good(), timed=False)
        s = sc.score_run(self.run, self.man)
        self.assertNotIn("e2t_p50_s", s)
        self.assertIn("(no timed runs)", sc.table([s]))

    def test_verify_catches_an_edited_headline(self):
        self.write(self.good())
        s = sc.score_run(self.run, self.man)
        s["wer_intended"] = 1.0
        self.assertTrue(sc.verify(s))

    def test_the_table_keeps_accuracy_and_speed_apart(self):
        self.write(self.good())
        timed = sc.score_run(self.run, self.man) | {"machine": "mac"}
        untimed = dict(timed, timed=False, machine="github-macos-15")
        t = sc.table([timed, untimed])
        acc, speed = t.split("Speed")
        self.assertIn("github-macos-15", acc)
        self.assertNotIn("github-macos-15", speed)
        self.assertIn("| mac |", speed)


if __name__ == "__main__":
    unittest.main()
