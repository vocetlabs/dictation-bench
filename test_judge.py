"""Tests for judge.py and `./bench judge`: on with a fake endpoint, off without a key; numbers only; no
calls for clips with no output; errors without the provider's text; the cache. No network or key."""
import contextlib
import csv
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import harness as hn  # noqa: E402
import judge as jd  # noqa: E402
import score as sc  # noqa: E402

MAN = [{"clip_id": "c1", "spoken_text": "hello bob", "intended_text": "Hello Bob."},
       {"clip_id": "c2", "spoken_text": "run make", "intended_text": "Run make."},
       {"clip_id": "c3", "spoken_text": "done", "intended_text": "Done."}]


def fake_tools():
    def char_edits(draft, fixed):
        return sum(a != b for a, b in zip(draft, fixed)) + abs(len(draft) - len(fixed)), max(len(fixed), 1)
    return (lambda intended, draft, spoken, version: f"{version}|{intended}|{draft}", char_edits, "fp", "0.2.1")


class Endpoint:
    """A fake OpenAI-compatible endpoint: corrects every draft to the intended text."""

    def __init__(self, fail=False, model=jd.JUDGE_MODEL):
        self.calls, self.fail, self.model = [], fail, model

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        if self.fail:
            raise jd.JudgeError("HTTP 500")
        intended = body["messages"][1]["content"].split("|")[1]
        return {"model": self.model, "choices": [{"message": {"content": json.dumps({"fixed": intended})}}]}


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.dir.name)
        self.man = self.d / "manifest.csv"
        with self.man.open("w", newline="") as fh:
            w = csv.DictWriter(fh, list(MAN[0]))
            w.writeheader()
            w.writerows(MAN)
        self.run = self.d / "run"
        self.run.mkdir()
        rows = [{"clip_id": "c1", "status": "ok", "transcript": "Hello Bob."},
                {"clip_id": "c2", "status": "ok", "transcript": "Run bake."},
                {"clip_id": "c3", "status": "empty", "transcript": ""}]
        hn.write_rows(self.run / "results.csv", {r["clip_id"]: {**{f: "" for f in hn.FIELDS}, **r} for r in rows})
        (self.run / "run.json").write_text(json.dumps({"fingerprint": "fp", "clip_ids": ["c1", "c2", "c3"],
                                                       "timed": False, "manifest_sha256": sc.digest(self.man)}))
        self.cache = self.d / "cache"

    def tearDown(self):
        self.dir.cleanup()

    def judge(self, post, **kw):
        return jd.judge_run(self.run, self.man, "k-not-real", tools=fake_tools(), post=post, cache_dir=self.cache,
                            backoff=0, log=lambda *_: None, **kw)

    def test_on_with_a_key_it_scores_only_numbers_and_skips_clips_with_no_output(self):
        ep = Endpoint()
        out = self.judge(ep)
        self.assertEqual(len(ep.calls), 2)  # c3 had no output: not sent
        self.assertEqual((out["zer"], out["judged"]), (33.3, 3))  # c1 right; c2 one edit; c3 no credit
        self.assertEqual(out["judge"]["model"], jd.JUDGE_MODEL)
        url, headers, body = ep.calls[0]
        self.assertEqual(headers["Authorization"], "Bearer k-not-real")
        self.assertEqual(body["model"], jd.JUDGE_MODEL)
        text = json.dumps(out)
        for s in ("Hello", "Bob", "bake", "k-not-real"):
            self.assertNotIn(s, text)

    def test_the_cache_answers_a_rerun(self):
        self.judge(Endpoint())
        ep = Endpoint()
        out = self.judge(ep)
        self.assertEqual((len(ep.calls), out["zer"]), (0, 33.3))

    def test_a_failing_judge_gives_no_headline_and_no_provider_text(self):
        logs = []
        out = jd.judge_run(self.run, self.man, "k", tools=fake_tools(), post=Endpoint(fail=True),
                           cache_dir=self.cache, backoff=0, log=logs.append)
        self.assertNotIn("zer", out)
        self.assertIn("incomplete", out)
        self.assertTrue(all("HTTP 500" in x or "judged" in x for x in logs))

    def test_a_reply_from_another_model_is_refused(self):
        out = self.judge(Endpoint(model="someone/else"))
        self.assertIn("incomplete", out)

    def test_an_incomplete_run_is_not_judged(self):
        (self.run / "run.json").write_text(json.dumps({"fingerprint": "fp", "clip_ids": ["c1", "c2", "c3", "c4"],
                                                       "manifest_sha256": sc.digest(self.man)}))
        with self.assertRaises(sc.Incomplete):
            self.judge(Endpoint())

    def test_http_errors_carry_no_body(self):
        err = jd.urllib.error.HTTPError("u", 401, "secret body echo", {}, io.BytesIO(b"prompt text"))
        with mock.patch.object(jd.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(jd.JudgeError) as e:
                jd.http_post("https://x/y", {}, {})
        self.assertEqual(str(e.exception), "HTTP 401")


class BenchJudgeTests(unittest.TestCase):
    def test_off_without_a_key(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(buf):
            self.assertEqual(bench.judge_cmd(pathlib.Path("nowhere")), 0)
        self.assertEqual(buf.getvalue().strip(), "judge metrics: not computed (no key)")

    def test_the_table_says_when_judge_metrics_are_missing(self):
        s = {"app": {"name": "x"}, "clips": 1, "wer_intended": 1.0, "timed": False}
        self.assertIn("not computed (no key)", sc.table([s]))
        self.assertIn("| zer |", sc.table([dict(s, zer=50.0, amend=1.0, judge_model="m")]))


if __name__ == "__main__":
    unittest.main()
