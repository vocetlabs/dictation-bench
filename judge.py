"""Optional judge metrics: ZER (share of dictations that need no edit) and AMEND (pooled character edits /
characters), from amend-va 0.2.1's prompt `v1` and its `char_edits`, asked of one pinned LLM through an
OpenAI-compatible HTTP API (OpenRouter by default, so one key works). Off unless JUDGE_API_KEY is set.

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


def cache_path(cache_dir: pathlib.Path) -> pathlib.Path:
    slug = JUDGE_MODEL.replace("/", "_").replace(":", "_")
    return cache_dir / f"{slug}-{PROMPT_VERSION}-{JUDGE_VERSION}.jsonl"


def judge_run(run_dir: pathlib.Path, manifest: pathlib.Path, key: str, *, tools=None, post=http_post,
              cache_dir: pathlib.Path | None = None, attempts: int = 3, backoff: float = 5.0,
              max_calls: int = 400, log=print) -> dict:
    """judge.json's content. The run must be complete (score.score_run's checks); the result has headline
    ZER/AMEND only when every clip has a judgement."""
    sc.score_run(run_dir, manifest)  # raises sc.Incomplete for an incomplete or mismatched run
    build_prompt, char_edits, fingerprint, amend_version = tools or amend_tools()
    run = json.loads((run_dir / "run.json").read_text())
    rows = {r["clip_id"]: r for r in sc.read_csv(run_dir / "results.csv")}
    ref = {m["clip_id"]: m for m in sc.read_csv(manifest)}
    path = cache_path(cache_dir or HERE / ".judge-cache")
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                cache[r["key"]] = r["fixed"]
    per_clip, calls, failed = [], 0, 0
    for cid in sorted(rows):
        r, m = rows[cid], ref[cid]
        if r["status"] not in sc.SCORED_TEXT:  # verified no output: no call, no credit
            chars = len(m["intended_text"])
            per_clip.append({"clip_id": cid, "status": r["status"], "edits": chars, "chars": chars, "zero_edit": 0})
            continue
        draft = r["transcript"]
        prompt = build_prompt(m["intended_text"], draft, m["spoken_text"], PROMPT_VERSION)
        k = hashlib.sha256(f"{JUDGE_MODEL}\n{PROMPT_VERSION}\n{JUDGE_VERSION}\n{prompt}".encode()).hexdigest()
        fixed = cache.get(k)
        for attempt in range(1, attempts + 1):
            if fixed is not None or calls >= max_calls:
                break
            calls += 1
            try:
                fixed = ask(prompt, key, post)
                cache[k] = fixed
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": k, "fixed": fixed}, ensure_ascii=False) + "\n")
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
    out = {"judge": {"model": JUDGE_MODEL, "api": JUDGE_URL.split("/")[2], "reasoning_effort": REASONING_EFFORT,
                     "prompt_version": PROMPT_VERSION, "prompt_fingerprint": fingerprint, "amend_va": amend_version,
                     "version": JUDGE_VERSION},
           "fingerprint": run["fingerprint"], "results_sha256": sc.digest(run_dir / "results.csv"),
           "clips": len(per_clip), "judged": len(judged), "per_clip": per_clip}
    if failed:
        out["incomplete"] = f"{failed} clips without a judgement: no headline"
    else:
        out["zer"] = round(100 * sum(c["zero_edit"] for c in judged) / len(judged), 1) if judged else None
        out["amend"] = round(100 * sum(c["edits"] for c in judged) / max(1, sum(c["chars"] for c in judged)), 2)
    log(f"judge: {len(judged)}/{len(per_clip)} judged, {calls} calls"
        + (f", ZER {out['zer']} AMEND {out['amend']}" if "zer" in out else f", {out['incomplete']}"))
    return out
