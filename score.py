"""Scoring: word error rate against the dataset's intended text (headline) and spoken text (diagnostic),
deterministic, no key. Stdlib only.

`score` refuses to produce a number from an incomplete or mixed run: the results must hold exactly one row
for every clip the run selected (all 151 for a full run), each with an app outcome (ok, partial, empty,
late; infrastructure faults make the run incomplete), and the results file must match the run.json
fingerprint it was written with. A clip with no usable text (empty, late) counts every reference word as
a deletion. The output, score.json, holds numbers only: per clip the status, edit and word counts and
latencies, plus the totals, so anyone can recompute the headline (`verify`).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import pathlib
import unicodedata

SCORER_VERSION = "wer-2"
SCORED_TEXT = ("ok", "partial")       # transcripts that count as the app's output
NO_OUTPUT = ("empty", "late")         # scored as deletions
APP_OUTCOMES = SCORED_TEXT + NO_OUTPUT
# Typography that differs between apps but not in meaning.
FOLD = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "“": '"', "”": '"',
                      "–": "-", "—": "-", "−": "-", " ": " "})


def words(text: str) -> list[str]:
    """NFKC, typographic quotes and dashes folded to ASCII, lowercase; words are runs of letters and digits
    (apostrophes inside a word kept, so "don't" is one word whatever the quote style)."""
    text = unicodedata.normalize("NFKC", text).translate(FOLD).lower()
    out, cur = [], []
    for ch in text:
        if ch.isalnum() or (ch == "'" and cur):
            cur.append(ch)
        elif cur:
            out.append("".join(cur).rstrip("'"))
            cur = []
    if cur:
        out.append("".join(cur).rstrip("'"))
    return [w for w in out if w]


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def read_csv(path: pathlib.Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


class Incomplete(Exception):
    pass


def score_run(run_dir: pathlib.Path, manifest: pathlib.Path) -> dict:
    """score.json's content for a run folder (run.json + results.csv). Raises Incomplete with the reason
    when the run can't be scored as a whole."""
    run = json.loads((run_dir / "run.json").read_text())
    rows = read_csv(run_dir / "results.csv")
    man = read_csv(manifest)
    for col in ("clip_id", "spoken_text", "intended_text"):
        if not man or col not in man[0]:
            raise Incomplete(f"the manifest has no {col} column")
    ref = {m["clip_id"]: m for m in man}
    if digest(manifest) != run.get("manifest_sha256"):
        raise Incomplete("the manifest is not the one the run used")
    selected = run.get("clip_ids") or []
    ids = [r["clip_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise Incomplete("duplicate clip ids in results.csv")
    missing, extra = sorted(set(selected) - set(ids)), sorted(set(ids) - set(selected))
    if missing or extra:
        raise Incomplete(f"results don't match the run's clips: {len(missing)} missing, {len(extra)} extra")
    faults = [r["clip_id"] for r in rows if r["status"] not in APP_OUTCOMES]
    if faults:
        raise Incomplete(f"{len(faults)} clips ended in an infrastructure fault (rerun to resume): {faults[:5]}")
    unknown = [c for c in ids if c not in ref]
    if unknown:
        raise Incomplete(f"clips not in the manifest: {unknown[:5]}")
    per_clip, counts = [], {s: 0 for s in APP_OUTCOMES}
    for r in sorted(rows, key=lambda r: r["clip_id"]):
        m = ref[r["clip_id"]]
        hyp = words(r["transcript"]) if r["status"] in SCORED_TEXT else []
        ri, rs = words(m["intended_text"]), words(m["spoken_text"])
        counts[r["status"]] += 1
        per_clip.append({"clip_id": r["clip_id"], "status": r["status"],
                         "intended_edits": edit_distance(ri, hyp), "intended_words": len(ri),
                         "spoken_edits": edit_distance(rs, hyp), "spoken_words": len(rs),
                         "e2t_s": _f(r.get("e2t_s")), "post_stop_s": _f(r.get("post_stop_s"))})
    out = {"scorer": SCORER_VERSION, "fingerprint": run["fingerprint"], "app": run.get("app", {}),
           "effective": run.get("effective", {}), "machine": run.get("machine", ""), "timed": run.get("timed"),
           "dataset": run.get("dataset", {}), "results_sha256": digest(run_dir / "results.csv"),
           "app_log_counts": run.get("app_log_counts", {}),
           "clips": len(per_clip), "outcomes": counts, "per_clip": per_clip}
    out.update(totals(per_clip, bool(run.get("timed"))))
    return out


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def totals(per_clip: list[dict], timed: bool) -> dict:
    """The headline numbers, recomputable from per_clip alone."""
    t = {}
    for key in ("intended", "spoken"):
        e = sum(c[f"{key}_edits"] for c in per_clip)
        w = sum(c[f"{key}_words"] for c in per_clip)
        t[f"wer_{key}"] = round(100 * e / w, 2) if w else None
        t[f"{key}_edits"], t[f"{key}_words"] = e, w
    n = len(per_clip)
    t["failure_rate"] = round(100 * sum(c["status"] != "ok" for c in per_clip) / n, 1) if n else None
    if timed:
        ok = [c for c in per_clip if c["status"] == "ok"]
        e2t = [c["e2t_s"] for c in ok if c["e2t_s"] is not None]
        ps = [c["post_stop_s"] for c in ok if c["post_stop_s"] is not None]
        t.update(e2t_p50_s=percentile(e2t, 50), e2t_p95_s=percentile(e2t, 95),
                 post_stop_p50_s=percentile(ps, 50), post_stop_p95_s=percentile(ps, 95), latency_n=len(e2t))
    return t


def verify(score: dict) -> list[str]:
    """Recompute the headline from per_clip; returns the mismatches (empty = consistent)."""
    again = totals(score["per_clip"], bool(score.get("timed")))
    bad = [f"{k}: {score.get(k)} != {v}" for k, v in again.items() if score.get(k) != v]
    if score.get("clips") != len(score["per_clip"]):
        bad.append("clips count")
    return bad


COLS = ["app", "version", "model", "cleanup", "machine", "clips", "wer_intended", "wer_spoken", "failure_rate",
        "e2t_p50_s", "e2t_p95_s", "post_stop_p50_s", "post_stop_p95_s"]
JUDGE_COLS = ["zer", "amend", "judge_model"]


def table(scores: list[dict]) -> str:
    """Accuracy and speed rows kept apart: speed only from timed runs."""
    def row(s):
        a = s.get("app", {})
        return {**s, "app": a.get("name", ""), "version": a.get("version", ""),
                "model": a.get("model", {}).get("id", ""), "cleanup": a.get("cleanup", ""),
                "machine": s.get("machine", "")}

    def md(rows, cols):
        cell = lambda v: "-" if v in (None, "") else (f"{v:.2f}" if isinstance(v, float) else str(v))  # noqa: E731
        return ("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
                + "".join("| " + " | ".join(cell(r.get(c)) for c in cols) + " |\n" for r in rows))
    rows = [row(s) for s in scores]
    judged = any(r.get("zer") is not None for r in rows)
    acc = md(rows, COLS[:9] + (JUDGE_COLS if judged else []))
    if not judged:
        acc += "\nJudge metrics (ZER, AMEND): not computed (no key).\n"
    timed = [r for r in rows if r.get("timed")]
    speed = md(timed, COLS[:6] + COLS[8:]) if timed else "(no timed runs)\n"
    from report import DISCLOSURE
    return f"Accuracy (WER %, lower is better)\n\n{acc}\nSpeed (seconds; timed runs only)\n\n{speed}\n{DISCLOSURE}\n"
