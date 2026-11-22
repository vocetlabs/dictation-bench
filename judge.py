"""Optional judge metrics: ZER (share of dictations that need no edit) and AMEND (pooled character edits /
characters), from amend-va 0.2.1's prompt `v1` and its `char_edits`, asked of one pinned LLM. By default
that is Claude Opus 5.5 through OpenRouter's Batch API (the `:batch` variant: the same model at half the
price, answered asynchronously, usually within minutes; off unless JUDGE_API_KEY is set). Also available:
the same model through OpenRouter's synchronous API (`--via openrouter-sync`), or through a logged-in
Claude Code CLI (`--via claude-cli`: `claude -p` on the subscription, no key).

What is sent, per clip with text: the clip's intended text, its spoken text and the app's draft (amend-va's
prompt); the judge returns its minimal correction. Clips with no output (empty, late) are not sent: they get
zero ZER credit and their edits are the intended text's characters. Nothing textual is written to the
output or the log: judge.json holds the judge's configuration and per clip only numbers. The cache
(.judge-cache/, git-ignored, never uploaded) holds the corrections, keyed by judge model, prompt version,
scorer version and the prompt's hash, so a rerun asks nothing new.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import score as sc

JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "anthropic/claude-opus-5.5")
JUDGE_URL = os.environ.get("BENCH_JUDGE_URL", "https://openrouter.ai/api/v1/chat/completions")
BATCH_URL = os.environ.get("BENCH_JUDGE_BATCH_URL", "https://openrouter.ai/api/v1/batches")
BATCH_POLL_S = 60          # the Batch API's advice: once a minute; back off on 429
BATCH_WAIT_S = float(os.environ.get("BENCH_JUDGE_BATCH_WAIT_S", 6 * 3600))  # then stop; a rerun resumes
REASONING_EFFORT = "high"
PROMPT_VERSION = "v1"
JUDGE_VERSION = "judge-1"
SYSTEM = "You are evaluating dictation output. Follow the user's instructions exactly."
SCHEMA = {"name": "correction", "strict": True,
          "schema": {"type": "object", "properties": {"fixed": {"type": "string"}}, "required": ["fixed"],
                     "additionalProperties": False}}
HERE = pathlib.Path(__file__).resolve().parent


class JudgeError(Exception):
    """A failed judge request, described without the provider's response body (it can echo the prompt)."""


class UsageLimit(JudgeError):
    """The judge's plan or account is out of quota: stop asking; the cache keeps what was judged."""


def amend_tools():
    """(build_prompt, char_edits, prompt fingerprint, amend-va version), imported late so the rest of the
    bench and the tests don't need the package."""
    import importlib.metadata
    from amend.metric import char_edits
    from amend.prompts import BASE, LENIENT, build_prompt
    return (build_prompt, char_edits, hashlib.sha256((BASE + LENIENT).encode()).hexdigest()[:12],
            importlib.metadata.version("amend-va"))


def http_post(url: str, headers: dict, body: dict, timeout: float = 180) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise JudgeError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JudgeError(f"network error ({type(e).__name__})") from None
    except json.JSONDecodeError:
        raise JudgeError("the reply was not JSON") from None


def http_get(url: str, headers: dict, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise JudgeError(f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JudgeError(f"network error ({type(e).__name__})") from None
    except json.JSONDecodeError:
        raise JudgeError("the reply was not JSON") from None


def request_body(prompt: str) -> dict:
    """The chat-completions body, the same for the synchronous and the batch API (which takes the model
    from the batch)."""
    return {"temperature": 0, "reasoning": {"effort": REASONING_EFFORT},
            "response_format": {"type": "json_schema", "json_schema": SCHEMA},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}


def parse_reply(reply: dict) -> str:
    """{fixed} from a chat-completions response, refused if it came from another model."""
    try:
        content = reply["choices"][0]["message"]["content"]
        fixed = json.loads(content)["fixed"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise JudgeError("the reply had no {fixed} object") from None
    if not isinstance(fixed, str):
        raise JudgeError("the reply's fixed is not a string")
    served = str(reply.get("model", JUDGE_MODEL))
    if not served.startswith(JUDGE_MODEL.split(":")[0]):
        raise JudgeError("the reply came from another model")
    return fixed


def ask(prompt: str, key: str, post=http_post) -> str:
    body = {"model": JUDGE_MODEL, **request_body(prompt)}
    return parse_reply(post(JUDGE_URL, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            body))


class OneAtATime:
    """ask_many for judges that answer one prompt per call: in order, retried, stopped by a usage limit."""

    def ask_many(self, items, *, attempts=3, backoff=5.0, log=print):
        answers, stopped = {}, False
        self.stopped = False
        for k, prompt, intended, label in items:
            for attempt in range(1, attempts + 1):
                if stopped:
                    break
                try:
                    answers[k] = self.ask(prompt, intended)
                    yield k, answers[k]
                    break
                except UsageLimit as e:
                    log(f"judge: {e}; stopping (rerun to resume)")
                    stopped = True
                except JudgeError as e:
                    log(f"judge: clip {label} attempt {attempt}: {e}")
                    time.sleep(backoff * attempt)
        self.stopped = stopped


class OpenRouterJudge(OneAtATime):
    """JUDGE_MODEL through OpenRouter's synchronous chat-completions API, with JUDGE_API_KEY."""

    def __init__(self, key: str, post=http_post) -> None:
        self.key, self.post = key, post
        self.cache_id = JUDGE_MODEL  # the cache key's model part, as before
        self.meta = {"model": JUDGE_MODEL, "api": JUDGE_URL.split("/")[2], "reasoning_effort": REASONING_EFFORT}

    def ask(self, prompt: str, intended: str) -> str:
        return ask(prompt, self.key, self.post)


class OpenRouterBatchJudge:
    """The default: JUDGE_MODEL's `:batch` variant through OpenRouter's Batch API. Every prompt the cache can't
    answer goes into one batch (the base slug; the API resolves the `:batch` entry), polled once a minute until
    it is final, and its answers fill the cache. The batch's id is kept beside the cache until it is final,
    so an interrupted run polls the same batch instead of paying for a second one. The cache key is the base
    model's, so verdicts from the synchronous API are reused."""

    def __init__(self, key: str, post=http_post, get=http_get, sleep=time.sleep, cache_dir=None,
                 poll_s: float = BATCH_POLL_S, wait_s: float = BATCH_WAIT_S) -> None:
        self.key, self.post, self.get, self.sleep = key, post, get, sleep
        self.poll_s, self.wait_s = poll_s, wait_s
        self.cache_id = JUDGE_MODEL
        self.meta = {"model": f"{JUDGE_MODEL}:batch", "api": f"{BATCH_URL.split('/')[2]} batch",
                     "reasoning_effort": REASONING_EFFORT}
        self.pending = pathlib.Path(cache_dir or HERE / ".judge-cache") / "pending-batch.json"
        self.stopped = False

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def submit(self, items) -> str:
        body = {"endpoint": "/v1/chat/completions", "model": JUDGE_MODEL, "completion_window": "24h",
                "requests": [{"custom_id": k, "body": request_body(prompt)} for k, prompt, _, _ in items]}
        reply = self.post(BATCH_URL, self.headers(), body)  # the API wants `requests` last
        if not reply.get("id"):
            raise JudgeError("the batch was not accepted")
        self.pending.parent.mkdir(parents=True, exist_ok=True)
        self.pending.write_text(json.dumps({"id": reply["id"], "keys": [k for k, *_ in items]}))
        return reply["id"]

    def wait(self, batch_id: str, log) -> dict | None:
        """The finished batch, or None if it didn't finish within wait_s (it stays pending for a rerun)."""
        waited, interval = 0.0, self.poll_s
        while True:
            try:
                b = self.get(f"{BATCH_URL}/{batch_id}", self.headers())
                interval = self.poll_s
            except JudgeError as e:
                log(f"judge: polling {batch_id}: {e}")
                b, interval = {}, 2 * self.poll_s
            status = b.get("status")
            if status in ("completed", "failed", "expired", "cancelled"):
                return b
            if waited >= self.wait_s:
                log(f"judge: batch {batch_id} still {status} after {waited:.0f} s; rerun to keep waiting")
                return None
            counts = b.get("request_counts") or {}
            log(f"judge: batch {batch_id} {status}: {counts.get('completed', 0)}/{counts.get('total', '?')} done")
            self.sleep(interval)
            waited += interval

    def ask_many(self, items, *, attempts=3, backoff=5.0, log=print):
        items = list(items)
        wanted = {k for k, *_ in items}
        rounds = 0
        while True:
            if self.pending.exists():
                batch_id = json.loads(self.pending.read_text())["id"]
                log(f"judge: resuming batch {batch_id}")
            else:
                todo = [i for i in items if i[0] in wanted]
                if not todo or rounds >= attempts:
                    return
                rounds += 1
                batch_id = self.submit(todo)
                log(f"judge: batch {batch_id} submitted with {len(todo)} requests")
            b = self.wait(batch_id, log)
            if b is None:
                self.stopped = True
                return
            self.pending.unlink(missing_ok=True)
            if b.get("status") != "completed":
                log(f"judge: batch {batch_id} ended {b.get('status')}")
            failed = 0
            for entry in b.get("results") or []:
                k = entry.get("custom_id")
                if k not in wanted:
                    continue
                if entry.get("error") or not entry.get("response"):
                    failed += 1
                    continue
                try:
                    fixed = parse_reply((entry["response"] or {}).get("body") or {})
                except JudgeError:
                    failed += 1
                    continue
                wanted.discard(k)
                yield k, fixed
            if failed:
                log(f"judge: {failed} requests in batch {batch_id} failed; resubmitting them")
            if not wanted:
                return


CLAUDE_MODEL = os.environ.get("BENCH_CLAUDE_MODEL", "claude-opus-5-5")
USAGE_CLAUDE = re.compile(r"usage limit|rate limit|quota|too many requests|\b429\b", re.I)
# Kept out of the CLI's environment, so it answers on the logged-in subscription, never an API key or another
# provider (and agent tooling that reports status when ORCA_* is set doesn't see the call).
CHILD_ENV_DROP = re.compile(r"^(ANTHROPIC_|OPENROUTER|JUDGE_API_KEY$|CLAUDE_CODE_USE_|ORCA_|AWS_|GOOGLE_)")


def child_env(env: dict | None = None) -> dict:
    return {k: v for k, v in (os.environ if env is None else env).items() if not CHILD_ENV_DROP.match(k)}


class ClaudeCliJudge(OneAtATime):
    """`claude -p` on the user's Claude Code login (subscription): the same prompt, schema and parsing as the
    API judge; tools off, no settings or MCP servers, run from an empty temporary folder, one call at a time."""

    def __init__(self, binary: str = "claude", model: str = CLAUDE_MODEL, effort: str = REASONING_EFFORT,
                 timeout: float = 900) -> None:
        self.binary, self.model, self.effort, self.timeout = binary, model, effort, timeout
        self.cache_id = f"{model}@claude-cli"
        self.meta = {"model": f"{model.replace('-5-5', '-5.5')} via claude -p (subscription)", "api": "claude-cli",
                     "reasoning_effort": effort}

    def command(self) -> list[str]:
        return [self.binary, "-p", "--model", self.model, "--effort", self.effort, "--tools", "",
                "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
                "--system-prompt", SYSTEM, "--output-format", "json", "--json-schema", json.dumps(SCHEMA["schema"])]

    def ask(self, prompt: str, intended: str) -> str:
        with tempfile.TemporaryDirectory() as empty:
            try:
                p = subprocess.run(self.command(), input=prompt, capture_output=True, text=True, cwd=empty,
                                   env=child_env(), timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise JudgeError("claude -p timed out") from None
            except OSError as e:
                raise JudgeError(f"claude -p could not start ({type(e).__name__})") from None
        try:
            d = json.loads(p.stdout)
        except json.JSONDecodeError:
            d = {}
        said = f"{p.stderr}\n{d.get('result', '') if d.get('is_error') else ''}"
        if USAGE_CLAUDE.search(said):
            raise UsageLimit("claude -p reports a usage limit")
        if p.returncode != 0 or not d:
            raise JudgeError(f"claude -p exited {p.returncode}")  # its text can echo the prompt: not shown
        if d.get("is_error") or d.get("subtype") != "success":
            raise JudgeError(f"claude -p error ({d.get('subtype')})")
        if self.model not in (d.get("modelUsage") or {}):
            raise JudgeError("the answer came from another model")
        out = d.get("structured_output")
        try:
            if not isinstance(out, dict) or not isinstance(out.get("fixed"), str):
                out = json.loads(str(d.get("result", "")).strip())
            fixed = out["fixed"].strip()
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            raise JudgeError("the reply had no {fixed} object") from None
        if not fixed and intended.strip():
            raise JudgeError("empty answer for a non-empty intended text")
        return fixed


def cache_path(cache_dir: pathlib.Path, cache_id: str = JUDGE_MODEL) -> pathlib.Path:
    slug = cache_id.replace("/", "_").replace(":", "_").replace("@", "_")
    return cache_dir / f"{slug}-{PROMPT_VERSION}-{JUDGE_VERSION}.jsonl"


def judge_run(run_dir: pathlib.Path, manifest: pathlib.Path, key: str = "", *, backend=None, tools=None,
              post=http_post, cache_dir: pathlib.Path | None = None, attempts: int = 3, backoff: float = 5.0,
              max_calls: int = 400, use_cache: bool = True, limit: int | None = None, log=print) -> dict:
    """judge.json's content. The run must be complete (score.score_run's checks); the result has headline
    ZER/AMEND only when every clip has a judgement. A usage limit or an unfinished batch stops the run; rerun
    to resume (the cache answers what was already judged). use_cache=False with a limit re-asks the first
    `limit` clips with text, leaves the cache as it is, and reports how many fresh verdicts match it."""
    backend = backend or OpenRouterJudge(key, post)
    sc.score_run(run_dir, manifest)  # raises sc.Incomplete for an incomplete or mismatched run
    build_prompt, char_edits, fingerprint, amend_version = tools or amend_tools()
    run = json.loads((run_dir / "run.json").read_text())
    rows = {r["clip_id"]: r for r in sc.read_csv(run_dir / "results.csv")}
    ref = {m["clip_id"]: m for m in sc.read_csv(manifest)}
    path = cache_path(cache_dir or HERE / ".judge-cache", backend.cache_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                cache[r["key"]] = r["fixed"]
    entries = []  # (clip id, prompt, intended, cache key) for clips with text
    no_output = {}
    for cid in sorted(rows):
        r, m = rows[cid], ref[cid]
        if r["status"] not in sc.SCORED_TEXT:  # verified no output: no call, no credit
            chars = len(m["intended_text"])
            no_output[cid] = {"clip_id": cid, "status": r["status"], "edits": chars, "chars": chars, "zero_edit": 0}
            continue
        prompt = build_prompt(m["intended_text"], r["transcript"], m["spoken_text"], PROMPT_VERSION)
        k = hashlib.sha256(f"{backend.cache_id}\n{PROMPT_VERSION}\n{JUDGE_VERSION}\n{prompt}".encode()).hexdigest()
        entries.append((cid, prompt, m["intended_text"], k))
    if limit:
        entries = entries[:limit]
        no_output = {}
    ask = [(k, prompt, intended, cid) for cid, prompt, intended, k in entries if not use_cache or k not in cache]
    fresh = {}
    for k, fixed in backend.ask_many(ask[:max_calls], attempts=attempts, backoff=backoff, log=log):
        fresh[k] = fixed
        if use_cache:
            cache[k] = fixed
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"key": k, "fixed": fixed}, ensure_ascii=False) + "\n")
    stopped = getattr(backend, "stopped", False)
    per_clip, failed = list(no_output.values()), 0
    for cid, prompt, intended, k in entries:
        r = rows[cid]
        fixed = fresh.get(k) if not use_cache else cache.get(k)
        if fixed is None:
            failed += 1
            per_clip.append({"clip_id": cid, "status": r["status"], "edits": None, "chars": None, "zero_edit": None})
            continue
        e, ch = char_edits(r["transcript"], fixed)
        per_clip.append({"clip_id": cid, "status": r["status"], "edits": e, "chars": ch, "zero_edit": int(e == 0)})
    per_clip.sort(key=lambda c: c["clip_id"])
    judged = [c for c in per_clip if c["edits"] is not None]
    out = {"judge": {**backend.meta, "prompt_version": PROMPT_VERSION, "prompt_fingerprint": fingerprint,
                     "amend_va": amend_version, "version": JUDGE_VERSION},
           "fingerprint": run["fingerprint"], "results_sha256": sc.digest(run_dir / "results.csv"),
           "clips": len(per_clip), "judged": len(judged), "calls": len(ask[:max_calls]), "per_clip": per_clip}
    if not use_cache:  # a recheck: fresh verdicts against the cached ones, numbers only
        both = [(cid, k) for cid, _, _, k in entries if k in fresh and k in cache]
        same_zero = same_edits = 0
        for cid, k in both:
            draft = rows[cid]["transcript"]
            e_new, e_old = char_edits(draft, fresh[k])[0], char_edits(draft, cache[k])[0]
            same_zero += (e_new == 0) == (e_old == 0)
            same_edits += e_new == e_old
        out["recheck"] = {"compared": len(both), "same_zero_edit_verdict": same_zero, "same_edit_count": same_edits}
        log(f"judge recheck: {len(both)} compared, {same_zero} same zero-edit verdict, {same_edits} same edit count")
        return out
    if failed or limit:
        out["incomplete"] = (f"{failed} clips without a judgement" + (" (stopped: rerun to resume)" if stopped else "")
                             if failed else "a limited run") + ": no headline"
    else:
        out["zer"] = round(100 * sum(c["zero_edit"] for c in judged) / len(judged), 1) if judged else None
        out["amend"] = round(100 * sum(c["edits"] for c in judged) / max(1, sum(c["chars"] for c in judged)), 2)
    log(f"judge: {len(judged)}/{len(per_clip)} judged, {out['calls']} asked"
        + (f", ZER {out['zer']} AMEND {out['amend']}" if "zer" in out else f", {out['incomplete']}"))
    return out
