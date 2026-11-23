# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["sounddevice==0.5.1", "soundfile==0.12.1", "numpy==2.2.4", "soxr==0.5.0.post1",
#                 "pyobjc-framework-Quartz==11.0; sys_platform == 'darwin'",
#                 "pyobjc-framework-ApplicationServices==11.0; sys_platform == 'darwin'",
#                 "pyobjc-framework-Cocoa==11.0; sys_platform == 'darwin'", "amend-va==0.2.1"]
# ///
"""The bench's one command, the same locally and in CI (run it as ./bench, which uses uv).

    ./bench fetch                          # the clips and references from their source, into clips/
    ./bench machine [--ci]                 # BlackHole as the default input, automatic corrections off
    ./bench setup --app handy [--ci]       # install and configure the app (apps/handy.sh); --ci also grants
                                           #   its permissions (a CI runner: SIP off, passwordless sudo)
    ./bench run --app handy --smoke        # one synthetic dictation (macOS `say`): does the setup work?
    ./bench run --app handy --full         # the smoke gate, then all 151 clips, then score
    ./bench score out/handy                # WER from a finished run (refuses incomplete runs)
    ./bench judge out/handy                # optional ZER/AMEND (needs JUDGE_API_KEY; skipped without)
    ./bench judge out/handy --via claude-cli   # the same judge prompt through a logged-in `claude -p`
    ./bench judge out/handy --no-cache --limit 10   # re-ask 10 clips; how many verdicts match the cache
    ./bench verify out/handy/score.json    # recompute the headline from the per-clip numbers
    ./bench table out/*/score.json         # accuracy and speed tables
    ./bench dry-run                        # the loop against a simulated app (no macOS)

A run on a machine whose speed doesn't count (a CI runner) uses --accuracy-only: no latency is reported.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import asdict

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import harness as hn  # noqa: E402
import score as sc  # noqa: E402

DATASET = {"name": "VoiceArena/Dictation_Dataset", "split": "test", "revision": "ab671bb2", "clips": 151}
SMOKE_TEXT = "Please send the quarterly report to the finance team before Friday afternoon."
SMOKE_MIN_WORDS = 0.8  # share of the smoke sentence's words the app must get right


def app_config(name: str) -> hn.App:
    path = HERE / "apps" / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"no apps/{name}.json; apps: {sorted(p.stem for p in (HERE / 'apps').glob('*.json'))}")
    return hn.load_app(path)


def machine(ci: bool) -> None:
    if ci:
        subprocess.run(["brew", "install", "--quiet", "--cask", "blackhole-2ch"], check=True)
        subprocess.run(["sudo", "killall", "coreaudiod"], check=False)
        import time
        time.sleep(5)
    for k in ("NSAutomaticCapitalizationEnabled", "NSAutomaticPeriodSubstitutionEnabled",
              "NSAutomaticSpellingCorrectionEnabled", "NSAutomaticQuoteSubstitutionEnabled",
              "NSAutomaticDashSubstitutionEnabled", "NSAutomaticTextReplacementEnabled",
              "NSAutomaticInlinePredictionEnabled", "NSAutomaticTextCompletionEnabled"):
        subprocess.run(["defaults", "write", "-g", k, "-bool", "false"], check=True)
    from macos import CoreAudio
    ca = CoreAudio()
    ca.set_default_input(ca.find_input("BlackHole 2ch"))
    print(f"default input: {ca.default_input_name()}; automatic corrections off")


def setup(name: str, ci: bool) -> None:
    script = HERE / "apps" / f"{name}.sh"
    app = app_config(name)
    if ci and not app.hosted_setup:
        raise SystemExit(f"{name} has no unattended setup yet (apps/{name}.json hosted_setup=false): local only")
    env = dict(os.environ, BENCH_CI="1" if ci else "", BENCH=str(HERE))
    subprocess.run(["bash", str(script)], check=True, env=env)


def smoke_clip(folder: pathlib.Path) -> hn.Clip:
    """A sentence spoken by macOS's `say`, 16 kHz WAV: no dataset audio, so the gate can run anywhere."""
    aiff, wav = folder / "smoke.aiff", folder / "smoke.wav"
    subprocess.run(["say", "-o", str(aiff), SMOKE_TEXT], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", str(aiff), str(wav)], check=True)
    import soundfile as sf
    return hn.Clip("smoke", wav, sf.info(wav).duration, reference=SMOKE_TEXT)


def smoke_ok(res: hn.Result) -> tuple[bool, str]:
    want, got = sc.words(SMOKE_TEXT), sc.words(res.transcript)
    right = len(want) - min(len(want), sc.edit_distance(want, got))
    ok = res.status == "ok" and right >= SMOKE_MIN_WORDS * len(want)
    return ok, f"smoke: {res.status}, {right}/{len(want)} words right, e2t={res.e2t_s} {res.error}"


def log_counts(app: hn.App) -> dict[str, int]:
    """How often each value of the app's log_counts group appears in its log files so far: numbers only."""
    import glob
    import re
    if not app.log_counts or not app.logs or app.logs.startswith("unified:"):
        return {}
    rx, counts = re.compile(app.log_counts), {}
    for f in sorted(glob.glob(os.path.expanduser(app.logs))):
        for m in rx.finditer(pathlib.Path(f).read_text(errors="replace")):
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    return counts


def count_diff(after: dict[str, int], before: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before.get(k, 0) for k in sorted(after) if after[k] - before.get(k, 0)}


def show_logs(app: hn.App, lines: int = 40) -> None:
    """The tail of the app's own log, after a failed smoke test only (its audio is our synthetic
    sentence, so nothing from the dataset can be in it)."""
    import glob
    if app.logs.startswith("unified:"):  # unified:<process>[:<subsystem prefix>]
        _, proc, *sub = app.logs.split(":") + [""]
        pred = f'process == "{proc}"' + (f' AND subsystem BEGINSWITH "{sub[0]}"' if sub and sub[0] else "")
        out = subprocess.run(["log", "show", "--last", "10m", "--style", "compact", "--info", "--debug",
                              "--predicate", pred], capture_output=True, text=True).stdout.splitlines()
        print("\n".join(x[:400] for x in out[-4 * lines:]), file=sys.stderr)
        return
    files = sorted(glob.glob(os.path.expanduser(app.logs))) if app.logs else []
    if app.logs and not files:
        print(f"(no log files match {app.logs})", file=sys.stderr)
    for f in files[-2:]:
        print(f"--- {f}", file=sys.stderr)
        tail = pathlib.Path(f).read_text(errors="replace").splitlines()[-lines:]
        print("\n".join(x[:300] for x in tail), file=sys.stderr)


def run(a: argparse.Namespace) -> int:
    from macos import BlackHoleDriver, TextEditTarget, preflight
    app = app_config(a.app)
    if a.hotkey:
        app.hotkey, app.notes = a.hotkey, (app.notes + f" (hotkey overridden: {a.hotkey})").strip()
    env = preflight(app)
    timing = hn.accuracy_only() if a.accuracy_only else hn.Timing()
    driver = BlackHoleDriver(app, timing)
    target = TextEditTarget(pathlib.Path.home() / "bench" / "scratch.txt")
    restart = hn.restarts_after_unclear(app)
    caff = subprocess.Popen(["caffeinate", "-dimsu"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clip = smoke_clip(pathlib.Path(tmp))
            for attempt in (1, 2):  # the first dictation may also load the model or compile GPU kernels
                try:
                    res = hn.run_clip(clip, driver, target, hn.Clock(), timing)
                except hn.InfraError as e:
                    res = hn.Result(e.status, error=str(e))
                driver.recover(restart=res.status != "ok")  # a failed smoke try leaves an unknown state
                ok, msg = smoke_ok(res)
                print(f"{msg} (attempt {attempt})", flush=True)
                if ok:
                    break
            if not ok:
                print("the smoke dictation failed: fix the setup before a full run", file=sys.stderr)
                show_logs(app)
                return 2
        if a.smoke:
            return 0
        clips_dir = HERE / "clips"
        manifest = clips_dir / "manifest.csv"
        if not manifest.exists():
            raise SystemExit("no clips: ./bench fetch")
        clips = hn.load_clips(clips_dir, a.limit)
        out = a.output or HERE / "out" / f"{app.name}-{a.machine or 'local'}"
        settings = {f: sc.digest(pathlib.Path(os.path.expanduser(f)))
                    for f in app.settings_files if pathlib.Path(os.path.expanduser(f)).exists()}
        info = {"app": asdict(app), "effective": {"binary_sha256": env["binary_sha256"], "settings_sha256": settings},
                "harness": hn.HARNESS_VERSION, "timing": asdict(timing), "timed": timing.timed,
                "dataset": DATASET, "manifest_sha256": sc.digest(manifest), "machine": a.machine or "local",
                "macos": env["macos"], "clip_ids": [c.clip_id for c in clips]}
        info["fingerprint"] = hn.fingerprint(**info)
        before = log_counts(app)
        drafts = hn.Drafts(pathlib.Path(os.path.expanduser(app.drafts))) if app.drafts else None
        rows = hn.run_all(clips, driver, target, hn.Clock(), timing, out, run=info,
                          restart_after_ambiguous=restart, attempts=a.attempts, drafts=drafts)
        if app.log_counts:  # e.g. how many cleanups were applied or fell back, over this run's clips
            meta = json.loads((out / "run.json").read_text())
            meta["app_log_counts"] = count_diff(log_counts(app), before)
            (out / "run.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
            print(f"app log counts: {meta['app_log_counts']}")
    finally:
        driver.recover(restart=False)
        caff.terminate()
    faults = [r for r in rows.values() if r["status"] not in hn.APP_OUTCOMES]
    print(f"done: {len(rows) - len(faults)}/{len(rows)} with an app outcome -> {out}")
    return score_cmd(out, manifest)


def score_cmd(run_dir: pathlib.Path, manifest: pathlib.Path | None = None) -> int:
    manifest = manifest or HERE / "clips" / "manifest.csv"
    try:
        s = sc.score_run(run_dir, manifest)
    except sc.Incomplete as e:
        print(f"not scored: {e}", file=sys.stderr)
        return 1
    (run_dir / "score.json").write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
    print(sc.table([s]))
    return 0


JUDGE_FILES = {"openrouter": "judge.json", "openrouter-sync": "judge.json", "claude-cli": "judge-claude-cli.json"}


def judge_cmd(run_dir: pathlib.Path, via: str = "openrouter", out_name: str | None = None, use_cache: bool = True,
              limit: int | None = None, drop_pending: bool = False) -> int:
    """ZER/AMEND for a finished run. openrouter (default): Claude Opus 5.5's batch variant through OpenRouter's
    Batch API; openrouter-sync: the same model through its synchronous API (both need JUDGE_API_KEY);
    claude-cli: the logged-in Claude Code CLI (`claude -p`, the subscription), no key. --no-cache --limit N
    re-asks N clips and reports how many verdicts match the cache (judge-recheck.json)."""
    import judge as jd
    if drop_pending and not jd.drop_pending(run_dir):
        print(f"no pending batch record in {run_dir}")
    if via == "claude-cli":
        backend = jd.ClaudeCliJudge()
    else:
        key = os.environ.get("JUDGE_API_KEY", "")
        if not key:
            print("judge metrics: not computed (no key)")
            return 0
        backend = jd.OpenRouterBatchJudge(key) if via == "openrouter" else jd.OpenRouterJudge(key)
    try:
        out = jd.judge_run(run_dir, HERE / "clips" / "manifest.csv", backend=backend, use_cache=use_cache,
                           limit=limit)
    except (sc.Incomplete, jd.JudgeError) as e:
        print(f"judge metrics: not computed ({e})", file=sys.stderr)
        return 1
    name = out_name or ("judge-recheck.json" if not use_cache else JUDGE_FILES[via])
    (run_dir / name).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    if "recheck" in out:  # a recheck that got no fresh verdict proves nothing
        return 0 if out["recheck"]["fresh"] else 1
    return 0 if "zer" in out else 1


def with_judge(score_json: pathlib.Path) -> dict:
    """score.json, plus ZER/AMEND from a judge file beside it (judge.json, else judge-claude-cli.json) when
    that judged the same results; judge_model says which judge and path scored it."""
    s = json.loads(score_json.read_text())
    for name in dict.fromkeys(JUDGE_FILES.values()):
        jp = score_json.with_name(name)
        if jp.exists():
            j = json.loads(jp.read_text())
            if j.get("results_sha256") == s.get("results_sha256") and "zer" in j:
                s.update(zer=j["zer"], amend=j["amend"], judge_model=j["judge"]["model"])
                break
    return s


def positive_int(v: str) -> int:
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    m = sub.add_parser("machine")
    m.add_argument("--ci", action="store_true")
    s = sub.add_parser("setup")
    s.add_argument("--app", required=True)
    s.add_argument("--ci", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--app", required=True)
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--smoke", action="store_true")
    g.add_argument("--full", action="store_true")
    r.add_argument("--accuracy-only", action="store_true", help="a machine whose speed doesn't count (CI)")
    r.add_argument("--machine", default=os.environ.get("BENCH_MACHINE", ""), help="label, e.g. github-macos-15")
    r.add_argument("-o", "--output", type=pathlib.Path)
    r.add_argument("--limit", type=int, help="first N clips only (a trial; scores only those)")
    r.add_argument("--attempts", type=int, default=3, help="tries per clip for infrastructure faults")
    r.add_argument("--hotkey", help="override the app's key (recorded in run.json)")
    sc_ = sub.add_parser("score")
    sc_.add_argument("run_dir", type=pathlib.Path)
    j = sub.add_parser("judge")
    j.add_argument("run_dir", type=pathlib.Path)
    j.add_argument("--via", choices=sorted(JUDGE_FILES), default="openrouter",
                   help="openrouter (batch, JUDGE_API_KEY), openrouter-sync (JUDGE_API_KEY) or claude-cli "
                        "(a logged-in `claude`, no key)")
    j.add_argument("--out", help="output file name in the run folder (default: per --via)")
    j.add_argument("--no-cache", action="store_true", help="re-ask without reading or writing the cache; "
                   "reports how many verdicts match the cached ones (use with --limit)")
    j.add_argument("--limit", type=positive_int, help="only the first N clips with text (no headline)")
    j.add_argument("--drop-pending", action="store_true", help="first remove the run's pending batch record "
                   "(after a 404/410 or an unreadable record; its answers, if any, are given up)")
    v = sub.add_parser("verify")
    v.add_argument("score_json", type=pathlib.Path)
    t = sub.add_parser("table")
    t.add_argument("scores", nargs="+", type=pathlib.Path)
    sub.add_parser("windows", help="on-screen windows (owner and title), to debug a setup")
    d = sub.add_parser("dry-run")
    d.add_argument("-n", type=int, default=5)
    a = ap.parse_args(argv)

    if a.cmd == "fetch":
        return subprocess.run(["uv", "run", "--quiet", str(HERE / "fetch_clips.py"), str(HERE / "clips")]).returncode
    if a.cmd == "machine":
        machine(a.ci)
        return 0
    if a.cmd == "setup":
        setup(a.app, a.ci)
        return 0
    if a.cmd == "run":
        return run(a)
    if a.cmd == "score":
        return score_cmd(a.run_dir)
    if a.cmd == "judge":
        return judge_cmd(a.run_dir, a.via, a.out, use_cache=not a.no_cache, limit=a.limit,
                         drop_pending=a.drop_pending)
    if a.cmd == "verify":
        bad = sc.verify(json.loads(a.score_json.read_text()))
        print("consistent" if not bad else "\n".join(bad))
        return 1 if bad else 0
    if a.cmd == "table":
        print(sc.table([with_judge(p) for p in a.scores]))
        return 0
    if a.cmd == "windows":
        import Quartz
        for w in Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID):
            if int(w.get("kCGWindowLayer", 0)) >= 0:
                print(f"{w.get('kCGWindowOwnerName')} | {w.get('kCGWindowName') or ''} | layer {w.get('kCGWindowLayer')}")
        return 0
    if a.cmd == "dry-run":
        with tempfile.TemporaryDirectory() as tmp:
            rows = hn.dry_run(pathlib.Path(tmp), a.n)
        bad = [r for r in rows.values() if r["status"] != "ok"]
        print(f"dry run: {len(rows) - len(bad)}/{len(rows)} ok")
        return 1 if bad else 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
