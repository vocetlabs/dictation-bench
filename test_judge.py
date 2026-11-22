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

    def test_claude_cli_writes_its_own_file_and_the_table_names_the_path(self):
        with tempfile.TemporaryDirectory() as d:
            run = pathlib.Path(d)
            (run / "judge.json").write_text("{}")  # an earlier API judgement stays as it was
            fake = {"zer": 50.0, "amend": 1.0, "results_sha256": "r",
                    "judge": {"model": "claude-opus-5.5 via claude -p (subscription)"}}
            with mock.patch.object(jd, "judge_run", return_value=fake), mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(bench.judge_cmd(run, "claude-cli"), 0)
            self.assertEqual((run / "judge.json").read_text(), "{}")
            self.assertEqual(json.loads((run / "judge-claude-cli.json").read_text())["zer"], 50.0)
            (run / "judge.json").unlink()
            (run / "score.json").write_text(json.dumps({"results_sha256": "r"}))
            s = bench.with_judge(run / "score.json")
            self.assertEqual((s["zer"], s["judge_model"]), (50.0, "claude-opus-5.5 via claude -p (subscription)"))

    def test_the_table_says_when_judge_metrics_are_missing(self):
        s = {"app": {"name": "x"}, "clips": 1, "wer_intended": 1.0, "timed": False}
        self.assertIn("not computed (no key)", sc.table([s]))
        self.assertIn("| zer |", sc.table([dict(s, zer=50.0, amend=1.0, judge_model="m")]))


FAKE_CLAUDE = """#!{python}
import json, os, sys
conf = json.load(open({conf!r}))  # the test's settings: the CLI gets no environment of ours to carry them
state = conf["state"]
calls = int(open(state).read()) if os.path.exists(state) else 0
open(state, "w").write(str(calls + 1))
prompt = sys.stdin.read()
with open(conf["log"], "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "cwd_files": os.listdir("."), "env": sorted(os.environ)}}) + "\\n")
if calls == conf.get("limit_at", -1):
    print("Claude AI usage limit reached", file=sys.stderr)
    sys.exit(1)
model = conf.get("model", "claude-opus-5-5")
intended = prompt.split("|")[1]
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "",
                  "structured_output": {{"fixed": intended}}, "modelUsage": {{model: {{"outputTokens": 9}}}}}}))
"""


class ClaudeCliTests(unittest.TestCase):
    """--via claude-cli against a fake `claude`: no real calls."""

    def setUp(self):
        JudgeTests.setUp(self)
        self.bin, self.conf = self.d / "claude", self.d / "fake.json"
        self.bin.write_text(FAKE_CLAUDE.format(python=sys.executable, conf=str(self.conf)))
        self.bin.chmod(0o755)
        self.state, self.log = self.d / "calls", self.d / "argv.jsonl"
        self.env = {"ANTHROPIC_API_KEY": "not-a-key", "OPENROUTER_API_KEY": "not-a-key", "JUDGE_API_KEY": "not-a-key",
                    "OPENAI_API_KEY": "not-a-key", "OTEL_LOG_USER_PROMPTS": "1", "HTTPS_PROXY": "http://x",
                    "ORCA_X": "1"}

    def tearDown(self):
        self.dir.cleanup()

    def judge(self, **conf):
        self.conf.write_text(json.dumps({"state": str(self.state), "log": str(self.log), **conf}))
        with mock.patch.dict(os.environ, self.env):
            return jd.judge_run(self.run, self.man, backend=jd.ClaudeCliJudge(binary=str(self.bin)),
                                tools=fake_tools(), cache_dir=self.cache, backoff=0, log=lambda *_: None)

    def test_it_scores_like_the_api_judge_and_records_the_path(self):
        out = self.judge()
        self.assertEqual((out["zer"], out["judged"]), (33.3, 3))
        self.assertEqual(out["judge"]["model"], "claude-opus-5.5 via claude -p (subscription)")
        self.assertEqual(out["judge"]["api"], "claude-cli")
        for s in ("Hello", "Bob", "bake"):
            self.assertNotIn(s, json.dumps(out))

    def test_the_call_has_tools_off_an_empty_folder_and_no_keys(self):
        self.judge()
        calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)  # c3 had no output: not sent
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("-p", argv)
        self.assertEqual(calls[0]["cwd_files"], [])
        for k in self.env:  # keys of any provider, telemetry that can log prompts, proxies, agent tooling
            self.assertNotIn(k, calls[0]["env"])
        self.assertIn("HOME", calls[0]["env"])

    def test_a_usage_limit_stops_and_a_rerun_resumes_from_the_cache(self):
        out = self.judge(limit_at=1)  # the second call hits the limit
        self.assertIn("stopped", out["incomplete"])
        self.assertEqual(out["judged"], 2)  # c1 judged, c3 (no output) needs no call
        self.assertEqual(int(self.state.read_text()), 2)  # stopped at once: no retries after the limit
        self.state.unlink()
        out = self.judge()
        self.assertEqual(out["zer"], 33.3)
        self.assertEqual(int(self.state.read_text()), 1)  # only c2 was asked again

    def test_an_answer_from_another_model_is_refused(self):
        self.assertIn("incomplete", self.judge(model="claude-sonnet-5-5"))

    def test_the_cache_is_separate_from_the_api_judges(self):
        self.judge()
        self.assertEqual(len(list(self.cache.glob("*claude-cli*"))), 1)
        ep = Endpoint()
        JudgeTests.judge(self, ep)
        self.assertEqual(len(ep.calls), 2)  # the API judge's cache was not answered by the CLI's


class FakeBatchApi:
    """OpenRouter's Batch API: POST /batches stores the requests; GET /batches/{id} is in progress for
    `polls_before_done` polls, then completed with each request answered with its intended text (or an error
    for the custom_ids in `fail`)."""

    def __init__(self, polls_before_done=1, fail=(), model="anthropic/claude-opus-5.5"):
        self.submits, self.polls, self.batches = [], 0, {}
        self.polls_before_done, self.fail, self.model = polls_before_done, set(fail), model

    def post(self, url, headers, body):
        assert url.endswith("/batches") and list(body)[-1] == "requests"  # `requests` must come last
        assert body["model"] == "anthropic/claude-opus-5.5"  # the base slug; the API resolves :batch
        bid = f"batch_{len(self.submits) + 1}"
        self.submits.append(body)
        self.batches[bid] = body["requests"]
        return {"id": bid, "status": "validating"}

    def get(self, url, headers):
        bid = url.rsplit("/", 1)[1]
        self.polls += 1
        if self.polls <= self.polls_before_done:
            return {"id": bid, "status": "in_progress", "request_counts": {"total": 2, "completed": 0}}
        results = []
        for r in self.batches[bid]:
            if r["custom_id"] in self.fail:
                self.fail.discard(r["custom_id"])
                results.append({"custom_id": r["custom_id"], "error": {"type": "server_error"}})
                continue
            intended = r["body"]["messages"][1]["content"].split("|")[1]
            results.append({"custom_id": r["custom_id"], "response": {"status_code": 200, "body": {
                "model": self.model, "choices": [{"message": {"content": json.dumps({"fixed": intended})}}]}}})
        return {"id": bid, "status": "completed", "results": results[::-1]}  # not in input order


class BatchTests(unittest.TestCase):
    def setUp(self):
        JudgeTests.setUp(self)

    def tearDown(self):
        self.dir.cleanup()

    def backend(self, api, **kw):
        return jd.OpenRouterBatchJudge("k-not-real", post=api.post, get=api.get, sleep=lambda s: None,
                                       cache_dir=self.cache, **kw)

    def judge(self, backend, **kw):
        return jd.judge_run(self.run, self.man, backend=backend, tools=fake_tools(), cache_dir=self.cache,
                            backoff=0, log=lambda *_: None, **kw)

    def test_one_batch_scores_the_run_and_the_variant_is_recorded(self):
        api = FakeBatchApi(polls_before_done=2)
        out = self.judge(self.backend(api))
        self.assertEqual((out["zer"], len(api.submits), len(api.submits[0]["requests"])), (33.3, 1, 2))
        self.assertEqual(out["judge"]["model"], "anthropic/claude-opus-5.5:batch")
        self.assertNotIn("Bob", json.dumps(out))
        self.assertFalse((self.run / ".judge-batch-pending.json").exists())
        self.assertFalse((self.run / ".judge-batch.lock").exists())

    def test_verdicts_cached_by_the_synchronous_judge_are_reused(self):
        JudgeTests.judge(self, Endpoint())
        api = FakeBatchApi()
        out = self.judge(self.backend(api))
        self.assertEqual((out["zer"], len(api.submits)), (33.3, 0))

    def test_an_unfinished_batch_is_resumed_not_paid_for_twice(self):
        api = FakeBatchApi(polls_before_done=10)
        out = self.judge(self.backend(api, wait_s=120))
        self.assertIn("stopped", out["incomplete"])
        self.assertTrue((self.run / ".judge-batch-pending.json").exists())
        out = self.judge(self.backend(api))
        self.assertEqual((out["zer"], len(api.submits)), (33.3, 1))

    def test_failed_requests_are_resubmitted(self):
        first = None
        api = FakeBatchApi()  # the first submitted request fails once

        def post(url, headers, body):
            nonlocal first
            if first is None:
                first = body["requests"][0]["custom_id"]
                api.fail = {first}
            return FakeBatchApi.post(api, url, headers, body)
        b = jd.OpenRouterBatchJudge("k", post=post, get=api.get, sleep=lambda s: None, cache_dir=self.cache)
        out = self.judge(b)
        self.assertEqual((out["zer"], len(api.submits), len(api.submits[1]["requests"])), (33.3, 2, 1))

    def test_a_recheck_bypasses_the_cache_and_counts_matches(self):
        self.judge(self.backend(FakeBatchApi()))
        before = jd.cache_path(self.cache).read_text()
        api = FakeBatchApi()
        out = self.judge(self.backend(api), use_cache=False, limit=2)
        self.assertEqual(len(api.submits[0]["requests"]), 2)
        self.assertEqual(out["recheck"], {"fresh": 2, "compared": 2, "same_zero_edit_verdict": 2, "same_edit_count": 2})
        self.assertNotIn("zer", out)
        self.assertEqual(jd.cache_path(self.cache).read_text(), before)


class BatchStateTests(unittest.TestCase):
    """The batch judge's per-run pending record, lock, expired batches and duplicate prompts."""

    def setUp(self):
        JudgeTests.setUp(self)
        self.logs = []

    def tearDown(self):
        self.dir.cleanup()

    def judge(self, api, **kw):
        b = jd.OpenRouterBatchJudge("k", post=api.post, get=api.get, sleep=lambda s: None, cache_dir=self.cache)
        return jd.judge_run(self.run, self.man, backend=b, tools=fake_tools(), cache_dir=self.cache, backoff=0,
                            log=self.logs.append, **kw)

    def pending(self, bid, requests, api):
        api.batches[bid] = requests
        (self.run / ".judge-batch-pending.json").write_text(json.dumps(
            {"id": bid, "endpoint": jd.BATCH_URL, "prompt_set": "from-an-earlier-run"}))

    @staticmethod
    def request(custom_id, intended):
        return {"custom_id": custom_id, "body": {"messages": [{"role": "system", "content": ""},
                                                             {"role": "user", "content": f"v1|{intended}|x"}]}}

    def test_a_pending_batch_is_collected_whole_before_anything_new_is_submitted(self):
        api = FakeBatchApi(polls_before_done=0)
        # an earlier run's batch: an answer this run doesn't ask for (paid all the same), nothing it does
        self.pending("batch_old", [self.request("k-not-in-this-run", "Elsewhere.")], api)
        out = self.judge(api)
        self.assertEqual(out["zer"], 33.3)
        cached = [json.loads(x)["key"] for x in jd.cache_path(self.cache).read_text().splitlines()]
        self.assertIn("k-not-in-this-run", cached)  # kept, not stranded
        self.assertEqual(len(api.submits), 1)  # then one batch for what was still missing
        self.assertFalse((self.run / ".judge-batch-pending.json").exists())

    def test_a_dead_judges_lock_is_taken_over_and_a_live_ones_refuses(self):
        (self.run / ".judge-batch.lock").write_text("99999999")  # no such process
        out = self.judge(FakeBatchApi())
        self.assertEqual(out["zer"], 33.3)
        self.assertTrue(any("taking over" in x for x in self.logs))
        (self.run / ".judge-batch.lock").write_text(str(os.getpid()))  # this process: alive
        with self.assertRaisesRegex(jd.JudgeError, "another judge"):
            self.judge(FakeBatchApi())

    def test_an_unreadable_pending_record_stops_the_run_and_is_kept(self):
        rec = self.run / ".judge-batch-pending.json"
        rec.write_text('{"id": "batch_')  # torn
        api = FakeBatchApi()
        with self.assertRaisesRegex(jd.JudgeError, "judge-batch-pending.json"):
            self.judge(api)
        self.assertEqual((rec.read_text(), len(api.submits)), ('{"id": "batch_', 0))
        self.assertFalse((self.run / ".judge-batch.lock").exists())

    def test_the_pending_record_is_written_atomically(self):
        with mock.patch.object(jd.os, "replace", wraps=os.replace) as replace:
            self.judge(FakeBatchApi())
        self.assertTrue(any(str(c.args[1]).endswith(".judge-batch-pending.json") for c in replace.call_args_list))

    def test_an_unknown_or_expired_batch_stops_and_only_drop_pending_removes_it(self):
        api = FakeBatchApi()
        self.pending("batch_old", [], api)
        get = api.get

        def gone(url, headers):
            if url.endswith("batch_old"):
                raise jd.JudgeError("HTTP 404", 404)
            return get(url, headers)
        api.get = gone
        out = self.judge(api)
        self.assertIn("stopped", out["incomplete"])
        self.assertTrue((self.run / ".judge-batch-pending.json").exists())  # never deleted on its own
        self.assertEqual(len(api.submits), 0)
        self.assertTrue(any("--drop-pending" in x for x in self.logs))
        self.assertTrue(jd.drop_pending(self.run, log=self.logs.append))
        self.assertFalse((self.run / ".judge-batch-pending.json").exists())
        self.assertEqual(self.judge(api)["zer"], 33.3)

    def test_the_pending_record_is_written_before_polling_and_kept_until_answers_are_cached(self):
        seen = {}
        api = FakeBatchApi()
        get = api.get

        def watch(url, headers):
            seen["pending_while_polling"] = (self.run / ".judge-batch-pending.json").exists()
            return get(url, headers)
        api.get = watch
        self.judge(api)
        self.assertTrue(seen["pending_while_polling"])
        self.assertFalse((self.run / ".judge-batch-pending.json").exists())

    def test_identical_prompts_go_once_and_score_every_clip(self):
        man = MAN + [{"clip_id": "c4", "spoken_text": "hello bob", "intended_text": "Hello Bob."}]
        with self.man.open("w", newline="") as fh:
            w = csv.DictWriter(fh, list(MAN[0]))
            w.writeheader()
            w.writerows(man)
        rows = sc.read_csv(self.run / "results.csv") + [{**{f: "" for f in hn.FIELDS}, "clip_id": "c4", "status": "ok",
                                                         "transcript": "Hello Bob."}]
        hn.write_rows(self.run / "results.csv", {r["clip_id"]: r for r in rows})
        (self.run / "run.json").write_text(json.dumps({"fingerprint": "fp", "clip_ids": ["c1", "c2", "c3", "c4"],
                                                       "timed": False, "manifest_sha256": sc.digest(self.man)}))
        api = FakeBatchApi()
        out = self.judge(api)
        ids = [r["custom_id"] for r in api.submits[0]["requests"]]
        self.assertEqual((len(ids), len(set(ids))), (2, 2))  # c1 and c4 share one request
        self.assertEqual((out["judged"], out["zer"]), (4, 50.0))

    def test_limits_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.judge(FakeBatchApi(), limit=0)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            bench.main(["judge", "out/x", "--no-cache", "--limit", "0"])

    def test_drop_pending_on_the_command_line_removes_the_record_first(self):
        (self.run / ".judge-batch-pending.json").write_text('{"id": "batch_old"}')
        with mock.patch.object(jd, "judge_run", return_value={"zer": 1.0}), \
                mock.patch.dict(os.environ, {"JUDGE_API_KEY": "k"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(bench.main(["judge", str(self.run), "--drop-pending"]), 0)
        self.assertFalse((self.run / ".judge-batch-pending.json").exists())

    def test_a_recheck_without_a_fresh_verdict_fails(self):
        none = {"recheck": {"fresh": 0, "compared": 0, "same_zero_edit_verdict": 0, "same_edit_count": 0}}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(jd, "judge_run", return_value=none), \
                mock.patch.dict(os.environ, {"JUDGE_API_KEY": "k"}):
            self.assertEqual(bench.judge_cmd(pathlib.Path(d), use_cache=False, limit=10), 1)
            some = {"recheck": dict(none["recheck"], fresh=10, compared=10)}
            with mock.patch.object(jd, "judge_run", return_value=some):
                self.assertEqual(bench.judge_cmd(pathlib.Path(d), use_cache=False, limit=10), 0)


if __name__ == "__main__":
    unittest.main()
