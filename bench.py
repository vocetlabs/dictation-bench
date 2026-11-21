# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["sounddevice==0.5.1", "soundfile==0.12.1", "numpy==2.2.4", "soxr==0.5.0.post1",
#                 "pyobjc-framework-Quartz==11.0; sys_platform == 'darwin'",
#                 "pyobjc-framework-ApplicationServices==11.0; sys_platform == 'darwin'",
#                 "pyobjc-framework-Cocoa==11.0; sys_platform == 'darwin'"]
# ///
"""The bench's one command, the same locally and in CI (run it as ./bench, which uses uv).

    ./bench fetch                          # the clips and references from their source, into clips/
    ./bench machine [--ci]                 # BlackHole as the default input, automatic corrections off
    ./bench setup --app handy [--ci]       # install and configure the app (apps/handy.sh); --ci also grants
                                           #   its permissions (a CI runner: SIP off, passwordless sudo)
    ./bench run --app handy --smoke        # one synthetic dictation (macOS `say`): does the setup work?
    ./bench run --app handy --full         # the smoke gate, then all 151 clips, then score
    ./bench score out/handy                # WER from a finished run (refuses incomplete runs)
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
    for f in sorted(glob.glob(os.path.expanduser(app.logs)))[-2:] if app.logs else []:
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
        rows = hn.run_all(clips, driver, target, hn.Clock(), timing, out, run=info,
                          restart_after_ambiguous=restart, attempts=a.attempts)
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
    if a.cmd == "verify":
        bad = sc.verify(json.loads(a.score_json.read_text()))
        print("consistent" if not bad else "\n".join(bad))
        return 1 if bad else 0
    if a.cmd == "table":
        print(sc.table([json.loads(p.read_text()) for p in a.scores]))
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
