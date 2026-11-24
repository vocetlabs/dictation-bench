"""BENCH2 reporting, numbers only: each configuration's passes, their mean, 95% bootstrap intervals that
resample speakers, paired differences between configurations (unresolved when the interval includes zero),
and the judge check's agreement between two judges. Standard library only."""
from __future__ import annotations

import json
import pathlib
import random
import statistics

DISCLOSURE = ("Disclosure: this bench is built and run by the developer of Vocet, one of the benchmarked apps. "
              "Every app's settings and the reasons for them are in CONFIGS.md.")
JUDGE_FILES = ("judge.json", "judge-claude-cli.json")
# (column, per-clip numerator, per-clip denominator); ZER counts a clip with no output as not sendable.
RATIOS = {"wer_spoken": ("spoken_edits", "spoken_words"), "wer_intended": ("intended_edits", "intended_words"),
          "zer": ("zero_edit", "judged")}


def load_run(run_dir: pathlib.Path) -> dict:
    """A scored run: score.json, the judge file that judged the same results (if any), and run.json."""
    s = json.loads((run_dir / "score.json").read_text())
    judge = None
    for name in JUDGE_FILES:
        p = run_dir / name
        if p.exists():
            j = json.loads(p.read_text())
            if j.get("results_sha256") == s.get("results_sha256") and "zer" in j:
                judge = j
                break
    run = json.loads((run_dir / "run.json").read_text())
    app = s.get("app", {})
    return {"dir": str(run_dir), "config": app.get("name", ""), "app": app, "score": s, "judge": judge, "run": run}


def clip_numbers(run: dict) -> dict[str, dict]:
    """Per clip: word edits and words against both references, and the judge's zero-edit verdict."""
    out = {c["clip_id"]: {"spoken_edits": c["spoken_edits"], "spoken_words": c["spoken_words"],
                          "intended_edits": c["intended_edits"], "intended_words": c["intended_words"],
                          "zero_edit": 0, "judged": 0} for c in run["score"]["per_clip"]}
    for c in (run["judge"] or {}).get("per_clip", []):
        if c.get("zero_edit") is not None and c["clip_id"] in out:
            out[c["clip_id"]].update(zero_edit=c["zero_edit"], judged=1)
    return out


def pool(runs: list[dict]) -> dict[str, dict]:
    """A configuration's passes summed per clip (each pass counts once)."""
    total: dict[str, dict] = {}
    for r in runs:
        for cid, c in clip_numbers(r).items():
            t = total.setdefault(cid, dict.fromkeys(c, 0))
            for k, v in c.items():
                t[k] += v
    return total


def ratio(pooled: dict[str, dict], clips, metric: str) -> float | None:
    num, den = RATIOS[metric]
    d = sum(pooled[c][den] for c in clips)
    return 100 * sum(pooled[c][num] for c in clips) / d if d else None


def _speaker_samples(speakers: dict[str, str], clips, n: int, seed: int):
    by: dict[str, list[str]] = {}
    for c in clips:
        by.setdefault(speakers.get(c, c), []).append(c)
    groups = list(by.values())
    rng = random.Random(seed)
    for _ in range(n):
        yield [c for g in rng.choices(groups, k=len(groups)) for c in g]


def _interval(values: list[float]) -> tuple[float, float] | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    lo, hi = values[int(0.025 * (len(values) - 1))], values[int(0.975 * (len(values) - 1))]
    return round(lo, 2), round(hi, 2)


def bootstrap(a: dict, b: dict | None, speakers: dict[str, str], metric: str, n: int = 2000,
              seed: int = 0) -> dict:
    """The metric for `a` (or the difference a - b on the clips both have), with a 95% interval from
    resampling speakers with replacement; a paired difference is unresolved when its interval holds 0."""
    clips = sorted(set(a) & set(b)) if b is not None else sorted(a)
    value = lambda cs: (ratio(a, cs, metric) if b is None  # noqa: E731
                        else None if ratio(a, cs, metric) is None or ratio(b, cs, metric) is None
                        else ratio(a, cs, metric) - ratio(b, cs, metric))
    point = value(clips)
    ci = _interval([value(cs) for cs in _speaker_samples(speakers, clips, n, seed)])
    out = {"value": None if point is None else round(point, 2), "ci95": ci}
    if b is not None:
        out["resolved"] = bool(ci) and (ci[0] > 0 or ci[1] < 0)
    return out


def check_comparable(runs: list[dict], speakers: dict[str, str]) -> list[str]:
    """Why these runs can't be pooled or ranked together: another protocol, manifest, dataset or machine, a
    different judge, or clips without a speaker ID (the bootstrap resamples speakers)."""
    problems = []

    def same(label, key):
        values = {json.dumps(key(r), sort_keys=True) for r in runs}
        if len(values) > 1:
            problems.append(f"different {label}: {sorted(values)[:3]}")
    same("protocol (timing)", lambda r: r["run"].get("timing"))
    same("harness version", lambda r: r["run"].get("harness"))
    same("manifest", lambda r: r["run"].get("manifest_sha256"))
    same("dataset", lambda r: r["run"].get("dataset"))
    same("clip selection", lambda r: sorted(r["run"].get("clip_ids") or []))
    same("machine label", lambda r: r["run"].get("machine"))
    same("machine", lambda r: {k: (r["run"].get("hardware") or {}).get(k) for k in ("chip", "ram_gb", "macos_build")})
    unverified = [r["dir"] for r in runs if r["run"].get("unverified")]
    if unverified:
        problems.append(f"{len(unverified)} runs used a profile still to verify (--unverified-ok trials, not "
                        f"reportable): {unverified[:3]}")
    by: dict[str, list[dict]] = {}
    for r in runs:
        by.setdefault(r["config"], []).append(r)
    for name, rs in by.items():  # the passes of one configuration must be the same app, model and settings
        eff = lambda r: {k: (r["run"].get("effective") or {}).get(k)  # noqa: E731
                         for k in ("binary_sha256", "release_sha256", "model_sha256", "settings", "settings_sha256")}
        if len({json.dumps(eff(r), sort_keys=True) for r in rs}) > 1:
            problems.append(f"{name}: its passes differ in app binary, release, model files, settings or settings files")
    judged = [r for r in runs if r["judge"]]
    if judged:
        if len(judged) != len(runs):
            problems.append(f"{len(runs) - len(judged)} of {len(runs)} runs have no judge file for their results")
        jid = lambda r: {k: r["judge"]["judge"].get(k) for k in ("model", "prompt_version", "prompt_fingerprint")}  # noqa: E731
        if len({json.dumps(jid(r), sort_keys=True) for r in judged}) > 1:
            problems.append("different judges (model, prompt version or fingerprint)")
    clips = {c for r in runs for c in clip_numbers(r)}
    missing = sorted(c for c in clips if not speakers.get(c))
    if missing:
        problems.append(f"{len(missing)} clips have no speaker ID (e.g. {missing[:3]})")
    return problems


def pass_row(run: dict) -> dict:
    s, j = run["score"], run["judge"] or {}
    return {"pass": run["dir"], "wer_spoken": s.get("wer_spoken"), "wer_intended": s.get("wer_intended"),
            "zer": j.get("zer"), "amend": j.get("amend"), "failure_rate": s.get("failure_rate"),
            "p50_s": s.get("post_stop_p50_s"), "p95_s": s.get("post_stop_p95_s"),
            "peak_rss_mb": run["run"].get("peak_rss_mb"),
            "model_mb": (run["run"].get("effective") or {}).get("model_size_mb")}


def summarize(runs: list[dict], speakers: dict[str, str], n: int = 2000, seed: int = 0) -> dict:
    """Configurations (their passes, means and intervals) and the paired differences between neighbours in
    each group's ranking, by spoken-text WER (speech accuracy) and by ZER where judged."""
    by: dict[str, list[dict]] = {}
    for r in runs:
        by.setdefault(r["config"], []).append(r)
    configs = []
    for name, rs in sorted(by.items()):
        app, passes = rs[0]["app"], [pass_row(r) for r in rs]
        mean = {k: (round(statistics.mean(v), 2) if (v := [p[k] for p in passes if p[k] is not None]) else None)
                for k in passes[0] if k != "pass"}
        pooled = pool(rs)
        cis = {m: bootstrap(pooled, None, speakers, m, n, seed) for m in RATIOS
               if m != "zer" or all(r["judge"] for r in rs)}
        configs.append({"config": name, "app": name.split(".")[0], "profile": name.split(".", 1)[-1],
                        "group": "raw speech" if app.get("cleanup", "off") == "off" else "speech + local cleanup",
                        "model": app.get("model", {}).get("id", ""), "version": app.get("version", ""),
                        "passes": passes, "mean": mean, "ci95": cis, "_pool": pooled})
    comparisons = []
    for metric in ("wer_spoken", "zer"):
        for key in sorted({(c["profile"], c["group"]) for c in configs}):
            ranked = [c for c in configs if (c["profile"], c["group"]) == key and metric in c["ci95"]]
            ranked.sort(key=lambda c: c["ci95"][metric]["value"] or 0, reverse=metric == "zer")
            for x, y in zip(ranked, ranked[1:]):
                d = bootstrap(x["_pool"], y["_pool"], speakers, metric, n, seed)
                comparisons.append({"metric": metric, "profile": key[0], "group": key[1], "a": x["config"],
                                    "b": y["config"], **d})
    for c in configs:
        c.pop("_pool")
    return {"configs": configs, "comparisons": comparisons, "bootstrap": {"resamples": n, "seed": seed,
                                                                          "unit": "speaker"}}


def _cell(v) -> str:
    return "-" if v in (None, "") else (f"{v:.2f}" if isinstance(v, float) else str(v))


def markdown(summary: dict, hardware: dict | None = None) -> str:
    """The results tables: shipped first, in its two groups; then the other profiles."""
    cols = ["config", "model", "passes", "wer_spoken", "wer_intended", "zer", "amend", "failure_rate", "p50_s",
            "p95_s", "peak_rss_mb", "model_mb"]
    head = ["App (profile)", "Model", "Passes", "Spoken WER % [95% CI]", "Intended WER % [95% CI]",
            "ZER % (judge est.) [95% CI]", "AMEND % (clips with output)", "Failure %", "p50 s (ok clips)",
            "p95 s (ok clips)", "Peak RAM MB", "Model MB"]
    out = [f"{DISCLOSURE}\n"]
    order = [(p, g) for p in ("shipped", "best-asr", "local-cleanup") for g in ("raw speech", "speech + local cleanup")]
    for profile, group in order:
        rows = [c for c in summary["configs"] if c["profile"] == profile and c["group"] == group]
        if not rows:
            continue
        pooled = lambda c: (c["ci95"].get("wer_spoken") or {}).get("value")  # noqa: E731
        rows.sort(key=lambda c: pooled(c) if pooled(c) is not None else 1e9)
        out.append(f"### {profile}: {group}\n")
        out.append("| " + " | ".join(head) + " |\n|" + "---|" * len(head))
        for c in rows:
            m = c["mean"]
            vals = [c["config"], c["model"], len(c["passes"])]
            for k in cols[3:]:
                if k in RATIOS:  # the pooled estimate over all passes, with its interval
                    est = c["ci95"].get(k) or {}
                    ci = est.get("ci95")
                    vals.append(_cell(est.get("value")) + (f" [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else ""))
                else:  # the mean of the passes
                    vals.append(_cell(m.get(k)))
            out.append("| " + " | ".join(str(v) for v in vals) + " |")
        out.append(f"\n{DISCLOSURE}\n")
    out.append("### Passes\n")
    pcols = ["wer_spoken", "wer_intended", "zer", "amend", "failure_rate", "p50_s", "p95_s", "peak_rss_mb"]
    out.append("| App (profile) | Pass | " + " | ".join(pcols) + " |\n|" + "---|" * (len(pcols) + 2))
    for c in summary["configs"]:
        for p in c["passes"]:
            out.append(f"| {c['config']} | {p['pass']} | " + " | ".join(_cell(p.get(k)) for k in pcols) + " |")
    out.append(f"\n{DISCLOSURE}\n")
    if summary["comparisons"]:
        out.append("### Paired differences (95% speaker bootstrap)\n")
        out.append("| Metric | Profile / group | A | B | A - B | 95% interval | |\n|---|---|---|---|---|---|---|")
        for d in summary["comparisons"]:
            ci = d["ci95"]
            out.append(f"| {d['metric']} | {d['profile']} / {d['group']} | {d['a']} | {d['b']} | {_cell(d['value'])} | "
                       + (f"[{ci[0]:.2f}, {ci[1]:.2f}]" if ci else "-") + f" | {'resolved' if d['resolved'] else 'unresolved'} |")
        out.append(f"\n{DISCLOSURE}\n")
    if hardware:
        out.append("Hardware: " + ", ".join(f"{k} {v}" for k, v in hardware.items() if k != "load") + "\n")
    out.append("WER and ZER: pooled over all passes, with 95% intervals that resample speakers (all of a speaker's clips "
               "together). Other columns: the mean of the passes. ZER and AMEND are judge estimates; AMEND is over clips "
               "with output and latency over ok clips (conditional): read them with the failure rate. Latency: release "
               "of the key to the final text.\n")
    return "\n".join(out) + "\n"


# --- The judge check ---------------------------------------------------------------------------------
def stratified_sample(per_clip: list[dict], n: int, seed: int) -> list[str]:
    """`n` clips with a judged draft, drawn in proportion to the primary judge's verdicts (zero-edit or not),
    at least one of each verdict that occurs; the same seed gives the same sample."""
    strata: dict[int, list[str]] = {}
    for c in per_clip:
        if c.get("zero_edit") is not None and c.get("status") in ("ok", "partial"):
            strata.setdefault(int(c["zero_edit"]), []).append(c["clip_id"])
    total = sum(len(v) for v in strata.values())
    if total <= n:
        return sorted(c for v in strata.values() for c in v)
    rng, out = random.Random(seed), []
    for verdict, ids in sorted(strata.items()):
        k = max(1, round(n * len(ids) / total))
        out += rng.sample(sorted(ids), min(k, len(ids)))
    rest = sorted(c for v in strata.values() for c in v if c not in out)
    out += rng.sample(rest, max(0, n - len(out)))  # rounding never leaves the sample short
    return sorted(out)


def agreement(primary: list[dict], secondary: list[dict], ids: list[str]) -> dict:
    """How often the two judges give the same zero-edit verdict on the sample, Cohen's kappa, and each
    judge's ZER on it."""
    a = {c["clip_id"]: c.get("zero_edit") for c in primary}
    b = {c["clip_id"]: c.get("zero_edit") for c in secondary}
    pairs = [(a[i], b[i]) for i in ids if a.get(i) is not None and b.get(i) is not None]
    n = len(pairs)
    if not n:
        return {"n": 0}
    same = sum(x == y for x, y in pairs)
    pa, pb = sum(x for x, _ in pairs) / n, sum(y for _, y in pairs) / n
    expected = pa * pb + (1 - pa) * (1 - pb)
    kappa = (same / n - expected) / (1 - expected) if expected < 1 else 1.0
    return {"n": n, "same_verdict": same, "agreement": round(100 * same / n, 1), "kappa": round(kappa, 3),
            "zer_primary": round(100 * pa, 1), "zer_secondary": round(100 * pb, 1),
            "disagreements": sorted(i for i in ids if a.get(i) is not None and b.get(i) is not None and a[i] != b[i])}


def rank_changes(checks: dict[str, dict]) -> list[str]:
    """Configurations whose ZER rank on the sample differs between the judges."""
    first = sorted(checks, key=lambda k: -checks[k].get("zer_primary", 0))
    second = sorted(checks, key=lambda k: -checks[k].get("zer_secondary", 0))
    return [f"{k}: rank {first.index(k) + 1} -> {second.index(k) + 1}" for k in checks if first.index(k) != second.index(k)]
