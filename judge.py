"""Optional judge metrics: ZER (share of dictations that need no edit) and AMEND (pooled character edits /
characters), from amend-va 0.2.1's prompt `v1` and its `char_edits`, asked of one pinned LLM through an
OpenAI-compatible HTTP API (OpenRouter by default, so one key works; off unless JUDGE_API_KEY is set), or
through a logged-in Claude Code CLI (`--via claude-cli`: `claude -p` on the subscription, no key).

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


def ask(prompt: str, key: str, post=http_post) -> str:
    body = {"model": JUDGE_MODEL, "temperature": 0, "reasoning": {"effort": REASONING_EFFORT},
            "response_format": {"type": "json_schema", "json_schema": SCHEMA},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
    reply = post(JUDGE_URL, {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, body)
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


class OpenRouterJudge:
    """The default: JUDGE_MODEL through an OpenAI-compatible API, with JUDGE_API_KEY."""

    def __init__(self, key: str, post=http_post) -> None:
        self.key, self.post = key, post
        self.cache_id = JUDGE_MODEL  # the cache key's model part, as before
        self.meta = {"model": JUDGE_MODEL, "api": JUDGE_URL.split("/")[2], "reasoning_effort": REASONING_EFFORT}

    def ask(self, prompt: str, intended: str) -> str:
        return ask(prompt, self.key, self.post)


CLAUDE_MODEL = os.environ.get("BENCH_CLAUDE_MODEL", "claude-opus-5-5")
USAGE_CLAUDE = re.compile(r"usage limit|rate limit|quota|too many requests|\b429\b", re.I)
# Kept out of the CLI's environment, so it answers on the logged-in subscription, never an API key or another
# provider (and agent tooling that reports status when ORCA_* is set doesn't see the call).
CHILD_ENV_DROP = re.compile(r"^(ANTHROPIC_|OPENROUTER|JUDGE_API_KEY$|CLAUDE_CODE_USE_|ORCA_|AWS_|GOOGLE_)")


def child_env(env: dict | None = None) -> dict:
    return {k: v for k, v in (os.environ if env is None else env).items() if not CHILD_ENV_DROP.match(k)}


class ClaudeCliJudge:
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
              max_calls: int = 400, log=print) -> dict:
    """judge.json's content. The run must be complete (score.score_run's checks); the result has headline
    ZER/AMEND only when every clip has a judgement. A usage limit stops the run; rerun to resume (the cache
    answers what was already judged)."""
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
    per_clip, calls, failed, stopped = [], 0, 0, False
    for cid in sorted(rows):
        r, m = rows[cid], ref[cid]
        if r["status"] not in sc.SCORED_TEXT:  # verified no output: no call, no credit
            chars = len(m["intended_text"])
            per_clip.append({"clip_id": cid, "status": r["status"], "edits": chars, "chars": chars, "zero_edit": 0})
            continue
        draft = r["transcript"]
        prompt = build_prompt(m["intended_text"], draft, m["spoken_text"], PROMPT_VERSION)
        k = hashlib.sha256(f"{backend.cache_id}\n{PROMPT_VERSION}\n{JUDGE_VERSION}\n{prompt}".encode()).hexdigest()
        fixed = cache.get(k)
        for attempt in range(1, attempts + 1):
            if fixed is not None or calls >= max_calls or stopped:
                break
            calls += 1
            try:
                fixed = backend.ask(prompt, m["intended_text"])
                cache[k] = fixed
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": k, "fixed": fixed}, ensure_ascii=False) + "\n")
            except UsageLimit as e:
                log(f"judge: {e}; stopping (rerun to resume)")
                stopped = True
            except JudgeError as e:
                log(f"judge: clip {cid} attempt {attempt}: {e}")
                time.sleep(backoff * attempt)
        if fixed is None:
            failed += 1
            per_clip.append({"clip_id": cid, "status": r["status"], "edits": None, "chars": None, "zero_edit": None})
            continue
        e, ch = char_edits(draft, fixed)
        per_clip.append({"clip_id": cid, "status": r["status"], "edits": e, "chars": ch, "zero_edit": int(e == 0)})
    judged = [c for c in per_clip if c["edits"] is not None]
    out = {"judge": {**backend.meta, "prompt_version": PROMPT_VERSION, "prompt_fingerprint": fingerprint, "amend_va": amend_version,
                     "version": JUDGE_VERSION},
           "fingerprint": run["fingerprint"], "results_sha256": sc.digest(run_dir / "results.csv"),
           "clips": len(per_clip), "judged": len(judged), "per_clip": per_clip}
    if failed:
        out["incomplete"] = (f"{failed} clips without a judgement" + (" (usage limit)" if stopped else "")
                             + ": no headline")
    else:
        out["zer"] = round(100 * sum(c["zero_edit"] for c in judged) / len(judged), 1) if judged else None
        out["amend"] = round(100 * sum(c["edits"] for c in judged) / max(1, sum(c["chars"] for c in judged)), 2)
    log(f"judge: {len(judged)}/{len(per_clip)} judged, {calls} calls"
        + (f", ZER {out['zer']} AMEND {out['amend']}" if "zer" in out else f", {out['incomplete']}"))
    return out
