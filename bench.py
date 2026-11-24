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
    ./bench host-run --jobs handy:shipped,vocet:shipped --passes 1   # the headline passes on this Mac
    ./bench report out/host/*/pass*/*      # passes, means, speaker-bootstrap intervals (markdown)
    ./bench judge-check out/host/*/pass1/* # a second judge on a stratified sample (JUDGE_API_KEY)

Apps are configured per profile: apps/<app>.<profile>.json, profile shipped, best-asr or local-cleanup
(--app handy is handy.shipped; --app handy:best-asr picks a profile).

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


def config_name(name: str) -> str:
    """handy, handy:best-asr and handy.best-asr name apps/handy.<profile>.json (shipped by default)."""
    name = name.replace(":", ".")
    return name if "." in name else f"{name}.shipped"


def app_config(name: str) -> hn.App:
    name = config_name(name)
    path = HERE / "apps" / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"no apps/{name}.json; apps: {sorted(p.stem for p in (HERE / 'apps').glob('*.json'))}")
    return hn.load_app(path)


def runnable(app: hn.App) -> str | None:
    """Why this profile isn't run on its own (n/a, or the same configuration as another), else None."""
    if app.na:
        return f"{app.name}: n/a ({app.na})"
    if app.same_as:
        return f"{app.name}: the same configuration as {app.same_as}; run that"
    return None


def file_tree_sha256(path: pathlib.Path) -> tuple[str, int]:
    """A model file's SHA-256 and size; for a folder (a Core ML bundle), the SHA-256 of its files' sorted
    relative paths and hashes, and their total size."""
    if path.is_file():
        return sc.digest(path), path.stat().st_size
    import hashlib
    h, size = hashlib.sha256(), 0
    for f in sorted(x for x in path.rglob("*") if x.is_file()):
        h.update(f"{f.relative_to(path)}\0{sc.digest(f)}\n".encode())
        size += f.stat().st_size
    return h.hexdigest(), size


def model_artifacts(app: hn.App) -> dict:
    """What the profile's model files are on disk: SHA-256 each, and the total size (the model disk column)."""
    shas, total = {}, 0
    for f in app.model.get("files", []):
        p = pathlib.Path(os.path.expanduser(f))
        if p.exists():
            shas[f], size = file_tree_sha256(p)
            total += size
        else:
            shas[f] = "missing"
    return {"model_sha256": shas, "model_size_mb": round(total / 2**20, 1) if total else None}


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


def owner_state(app: hn.App) -> list[str]:
    """The app's settings, data or preferences that exist on this Mac now (a setup would overwrite them)."""
    import isolation
    found = [p for p in app.state.get("paths", []) if pathlib.Path(os.path.expanduser(p)).exists()]
    return found + [d for d in app.state.get("defaults", []) if isolation.export_domain(d) is not None]


def setup(name: str, ci: bool, overwrite: bool = False, isolated: bool = False) -> None:
    """Installs the app into the bench's own folder and writes its profile's settings. Outside CI and a host
    run (which runs in the bench's own account and clears the app's state first), it refuses to overwrite
    existing settings unless told."""
    app = app_config(name)
    if app.na:
        raise SystemExit(runnable(app))
    if not (ci or overwrite or isolated) and (existing := owner_state(app)):
        raise SystemExit(f"setting up {app.name} would overwrite this Mac's {app.base} settings or data "
                         f"({', '.join(existing)}). Use ./bench host-run in the bench's own account, "
                         "or pass --overwrite on a test machine or VM.")
    script = HERE / "apps" / f"{app.base}.sh"
    if ci and not app.hosted_setup:
        raise SystemExit(f"{app.name} has no unattended setup yet (apps/{app.name}.json hosted_setup=false): local only")
    if app.verify:
        print(f"setup: {app.name} needs the Lead to verify in a VM: {app.verify}", file=sys.stderr)
    grant = ci or (os.environ.get("BENCH_CI") == "1" and in_vm())  # a VM: `BENCH_CI=1 ./bench setup ...`
    if os.environ.get("BENCH_CI") == "1" and not grant:
        raise SystemExit("BENCH_CI=1 writes permission rows with sudo: only in a virtual machine, never on a host")
    env = dict(os.environ, BENCH_CI="1" if grant else "", BENCH=str(HERE), BENCH_PROFILE=app.profile,
               BENCH_HOST_RUN="1" if isolated else "")
    subprocess.run(["bash", str(script)], check=True, env=env)


def in_vm() -> bool:
    """This Mac is a virtual machine (the hypervisor flag macOS sets for its guests)."""
    r = subprocess.run(["sysctl", "-n", "kern.hv_vmm_present"], capture_output=True, text=True)
    return r.stdout.strip() == "1"


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


def log_health(app: hn.App) -> str:
    """After a failed smoke test: how many lines the app logged in the last 10 minutes, and how many say
    error or warn. Counts only: an app's log can hold earlier dictations' text."""
    import glob
    if app.logs.startswith("unified:"):  # unified:<process>[:<subsystem prefix>]
        _, proc, *sub = app.logs.split(":") + [""]
        pred = f'process == "{proc}"' + (f' AND subsystem BEGINSWITH "{sub[0]}"' if sub and sub[0] else "")
        lines = subprocess.run(["log", "show", "--last", "10m", "--style", "compact", "--info", "--debug",
                                "--predicate", pred], capture_output=True, text=True).stdout.splitlines()
    else:
        import time
        lines = []
        for f in glob.glob(os.path.expanduser(app.logs)) if app.logs else []:
            if time.time() - os.path.getmtime(f) < 600:
                lines += pathlib.Path(f).read_text(errors="replace").splitlines()[-2000:]
    err = sum("error" in x.lower() for x in lines)
    warn = sum("warn" in x.lower() for x in lines)
    return f"app log, last 10 min: {len(lines)} lines, {err} with 'error', {warn} with 'warn' (counts only)"


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """KEY=VALUE lines (an Electron app's .env): only the keys a profile names are ever looked at."""
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def json_in_pref(key: str, domain: str, export=None):
    """A value inside JSON an app keeps in one preference ("modeConfigurationsV2/0/selectedLanguage": the
    preference modeConfigurationsV2, JSON data, item 0, field selectedLanguage); None when any part is missing."""
    import plistlib
    import isolation
    raw = (export or isolation.export_domain)(domain)
    name, *path = key.split("/")
    try:
        value = plistlib.loads(raw).get(name) if raw else None
        value = json.loads(value) if isinstance(value, (bytes, str)) else value
        for part in path:
            value = value[int(part)] if isinstance(value, list) else value.get(part)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None
    return value


def settings_problems(app: hn.App, read_pref=None, export=None) -> tuple[dict, list[str]]:
    """The profile's declared settings as the app has them now, and what differs (BENCH2: a run is scored
    only under the settings its label names). Also the cleanup model's digest for an Ollama profile."""
    import isolation
    read_pref = read_pref or isolation.read_pref
    src, got, problems = app.settings_source, {}, []
    if src.get("env"):
        path = pathlib.Path(os.path.expanduser(src["env"]))
        try:
            env = read_env_file(path)
        except OSError as e:
            return {}, [f"can't read {src['env']}: {e}"]
        for k in app.settings:
            got[k] = env.get(k)
    elif src.get("file"):
        path = pathlib.Path(os.path.expanduser(src["file"]))
        try:
            data = json.loads(path.read_text())
            data = data.get(src["key"], {}) if src.get("key") else data
        except (OSError, ValueError) as e:
            return {}, [f"can't read {src['file']}: {e}"]
        for k in app.settings:
            got[k] = data.get(k)
    elif src.get("defaults"):
        for k in app.settings:
            got[k] = json_in_pref(k, src["defaults"], export) if "/" in k else read_pref(k, src["defaults"])
    elif app.settings:
        problems.append(f"{app.name} has settings but no settings_source")
    for k, want in app.settings.items():
        have = got.get(k)
        if isinstance(want, bool) and isinstance(have, str):
            have = have.strip() in ("1", "true", "YES")
        if have != want:
            problems.append(f"{k} is {have!r}, the profile says {want!r}")
    for f in app.absent_files:  # e.g. no cleanup model for a profile whose cleanup is off
        if pathlib.Path(os.path.expanduser(f)).exists():
            problems.append(f"{f} must be absent for this profile")
    if model := app.model.get("cleanup_ollama"):
        got["cleanup_ollama_digest"] = ollama_digest(model)
        want = app.model.get("cleanup_ollama_digest", "")
        if not got["cleanup_ollama_digest"]:
            problems.append(f"the Ollama model {model} isn't installed")
        elif got["cleanup_ollama_digest"] != want:
            problems.append(f"the Ollama model {model} is {got['cleanup_ollama_digest']}, not the pinned "
                            f"{want or '(none)'}")
    return got, problems


def ollama_digest(model: str, fetch=None) -> str | None:
    """The full manifest digest ("sha256:<64 hex>") of an installed Ollama model, from the local server's
    /api/tags, or None when it isn't installed or the server doesn't answer."""
    import urllib.request
    try:
        raw = fetch() if fetch else urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=10).read()
        tags = json.loads(raw).get("models", [])
    except (OSError, ValueError):
        return None
    hit = next((m.get("digest", "") for m in tags if model in (m.get("name"), m.get("model"))), "")
    return f"sha256:{hit}" if hit and not hit.startswith("sha256:") else hit or None


def run(a: argparse.Namespace) -> int:
    app = app_config(a.app)
    if why := runnable(app):
        raise SystemExit(why)
    if a.hotkey:
        app.hotkey, app.notes = a.hotkey, (app.notes + f" (hotkey overridden: {a.hotkey})").strip()
    return run_job(app, smoke_only=a.smoke, accuracy_only=a.accuracy_only, machine=a.machine, output=a.output,
                   limit=a.limit, attempts=a.attempts, unverified_ok=a.unverified_ok)


def run_job(app: hn.App, *, smoke_only: bool = False, accuracy_only: bool = False, machine: str = "",
            output: pathlib.Path | None = None, limit: int | None = None, attempts: int = 3,
            seed: int | None = None, extra: dict | None = None, unverified_ok: bool = False) -> int:
    """The smoke dictation (the gate and the warm-up), then the clips (in a seeded order when `seed` is
    set), then the score. run.json records the app's release, model files, settings and the machine."""
    from macos import BlackHoleDriver, TextEditTarget, hardware, preflight
    if app.verify and not unverified_ok:
        raise SystemExit(f"{app.name} isn't verified yet ({app.verify}): verify it, clear `verify` in its "
                         "config, or pass --unverified-ok for a trial (recorded in run.json)")
    effective, problems = settings_problems(app)
    artifacts = model_artifacts(app)
    problems += [f"model file missing: {f}" for f, h in artifacts["model_sha256"].items() if h == "missing"]
    if problems:
        raise SystemExit(f"{app.name} doesn't have its profile's settings, so it can't be scored under that "
                         "label:\n  " + "\n  ".join(problems))
    env = preflight(app)
    timing = hn.accuracy_only() if accuracy_only else hn.Timing()
    driver = BlackHoleDriver(app, timing)
    target = TextEditTarget(HERE / "work" / "scratch.txt")  # the bench's own document
    restart = hn.restarts_after_unclear(app)
    caff = subprocess.Popen(["caffeinate", "-dimsu"])
    rows: dict = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clip = smoke_clip(pathlib.Path(tmp))
            for attempt in (1, 2):  # the first dictation may also load the model or compile GPU kernels
                try:
                    res = hn.run_clip(clip, driver, target, hn.Clock(), timing)
                except hn.InfraError as e:
                    res = hn.Result(e.status, error=str(e))
                driver.recover(restart=res.status != "ok")  # a failed smoke try leaves an unknown state
                if hit := hn.refusal(res.transcript, app.refusal_text):
                    print(f"the smoke dictation pasted {app.base}'s refusal notice ({hit!r}): stop", file=sys.stderr)
                    return 3
                ok, msg = smoke_ok(res)
                print(f"{msg} (attempt {attempt})", flush=True)
                if ok:
                    break
            if not ok:
                print("the smoke dictation failed: fix the setup before a full run", file=sys.stderr)
                print(log_health(app), file=sys.stderr)
                return 2
        if smoke_only:
            return 0
        clips_dir = HERE / "clips"
        manifest = clips_dir / "manifest.csv"
        if not manifest.exists():
            raise SystemExit("no clips: ./bench fetch")
        clips = hn.load_clips(clips_dir, limit)
        if seed is not None:  # the same order for every app in a pass
            import random
            random.Random(seed).shuffle(clips)
        out = output or HERE / "out" / f"{app.name}-{machine or 'local'}"
        settings = {f: sc.digest(pathlib.Path(os.path.expanduser(f)))
                    for f in app.settings_files if pathlib.Path(os.path.expanduser(f)).exists()}
        info = {"app": asdict(app), "effective": {"binary_sha256": env["binary_sha256"], "settings_sha256": settings,
                                                  "release_sha256": app.release.get("sha256", ""),
                                                  "settings": effective, **artifacts},
                "unverified": app.verify if app.verify else "",
                "harness": hn.HARNESS_VERSION, "timing": asdict(timing), "timed": timing.timed,
                "dataset": DATASET, "manifest_sha256": sc.digest(manifest), "machine": machine or "local",
                "macos": env["macos"], "clip_ids": [c.clip_id for c in clips], "clip_order_seed": seed,
                **(extra or {})}
        info["fingerprint"] = hn.fingerprint(**info)
        info["hardware"] = hardware()  # not in the fingerprint: the load changes between resumes
        before = log_counts(app)
        try:
            rows = hn.run_all(clips, driver, target, hn.Clock(), timing, out, run=info,
                              restart_after_ambiguous=restart, attempts=attempts, refusal_text=app.refusal_text)
        except hn.AppRefused as e:
            print(f"{app.name}: {e}", file=sys.stderr)
            return 3
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


def host_plan(jobs: list[str], passes: int, first_pass: int = 1, seed: int = 2026) -> list[dict]:
    """The passes of a host run: the app order rotates by one each pass, and every app in a pass gets the
    same clip order (seed + pass)."""
    return [{"pass": p, "seed": seed + p, "order": jobs[(p - 1) % len(jobs):] + jobs[:(p - 1) % len(jobs)]}
            for p in range(first_pass, first_pass + passes)]


MACHINE_FILE = HERE / "work" / "host-input.json"  # the input device to put back after a cut-off host run
HOST_LOCK = HERE / "work" / "host.lock"


def bench_apps_prefix() -> str:
    from macos import apps_dir
    return str(apps_dir()) + "/"


def quit_bench_apps() -> None:
    """Quits everything launched from the bench's own app folder, by PID (never an /Applications copy)."""
    from macos import quit_tree
    quit_tree(bench_apps_prefix())


def refuse_foreign_copies() -> None:
    """A benchmarked app running in this account from anywhere but the bench's folder would compete for the
    microphone, and the bench never quits it: the host run doesn't start."""
    from macos import _paths, benchmarked_names, foreign_copies
    if found := foreign_copies(_paths(), benchmarked_names(), bench_apps_prefix()):
        raise SystemExit("quit these first (the bench never quits apps outside its own folder):\n  " +
                         "\n  ".join(found))


def restore_host() -> int:
    """After a host run that was cut off: quits the bench's apps and puts the default input back."""
    from isolation import check_bench_account, host_lock, signals_held
    from macos import restore_input
    check_bench_account()
    with host_lock(HOST_LOCK), signals_held():
        if MACHINE_FILE.exists():
            if restore_input(json.loads(MACHINE_FILE.read_text())):
                return 1
            MACHINE_FILE.unlink()
            print("restored the default input")
        quit_bench_apps()  # nothing of the bench's left running
    return 0


def host_run(a: argparse.Namespace) -> int:
    """Passes of app:profile jobs on the bench Mac (BENCH2), in the bench's own Standard account (BENCH_USER,
    default vocetbench), so the owner's Keychain, settings and app data are in another account. Apps go into
    the bench's own folder and are quit by PID, never from /Applications. Each job deletes the app's state in
    this account, sets it up for its profile and runs it. The default input is saved by UID and put back,
    also on SIGTERM and SIGHUP; `restore-host` finishes that after a crash."""
    from datetime import datetime
    from isolation import check_bench_account, clear_state, host_lock, on_termination, signals_held
    from macos import hardware, restore_input, save_input, vm_running
    check_bench_account()
    jobs = [config_name(j) for j in a.jobs.split(",") if j]
    for j in jobs:
        app = app_config(j)
        if why := runnable(app):
            raise SystemExit(why)
        if not app.state.get("paths") and not app.state.get("defaults"):
            raise SystemExit(f"{app.name} declares no `state` to clear: not run on a host")
        if app.verify and not a.unverified_ok:
            raise SystemExit(f"{app.name} isn't verified yet ({app.verify}); pass --unverified-ok for a trial")
    if vm_running():
        raise SystemExit("a virtual machine is running: stop it before a host run (timed numbers)")
    with host_lock(HOST_LOCK):
        if MACHINE_FILE.exists():
            raise SystemExit("an earlier host run didn't finish: run `./bench restore-host` first")
        refuse_foreign_copies()
        out = a.out or HERE / "out" / "host" / datetime.now().strftime("%Y%m%d-%H%M")
        out.mkdir(parents=True, exist_ok=True)
        plan = host_plan(jobs, a.passes, a.first_pass, a.seed)
        log = {"plan": plan, "hardware_start": hardware(), "jobs": []}
        write = lambda: (out / "host.json").write_text(json.dumps(log, indent=1) + "\n")  # noqa: E731
        write()
        print(f"host run -> {out}: {len(plan)} passes of {', '.join(jobs)}; load {log['hardware_start']['load']}")
        on_termination()
        with signals_held():  # saved before anything changes, and never half-written
            state = save_input()
            MACHINE_FILE.write_text(json.dumps(state))
        problems: list[str] = []
        try:
            quit_bench_apps()
            machine(False)
            for step in plan:
                for name in step["order"]:
                    app = app_config(name)
                    entry = {"pass": step["pass"], "config": name, "load_before": hardware()["load"]}
                    try:
                        quit_bench_apps()
                        clear_state(app.state.get("paths", []), app.state.get("defaults", []))
                        setup(name, False, isolated=True)
                        entry["exit"] = run_job(app, machine=a.machine, output=out / f"pass{step['pass']}" / name,
                                                seed=step["seed"], extra={"host_run": {"pass": step["pass"]}},
                                                unverified_ok=a.unverified_ok)
                    except (subprocess.CalledProcessError, SystemExit) as e:
                        entry["exit"], entry["error"] = 1, str(e)[:300]
                        if "signal" in str(e):
                            raise
                    finally:
                        quit_bench_apps()
                    entry["load_after"] = hardware()["load"]
                    log["jobs"].append(entry)
                    write()
        finally:
            with signals_held():  # the input first: a bench app that won't quit can't keep it from coming back
                problems = restore_input(state)
                if not problems:
                    MACHINE_FILE.unlink(missing_ok=True)
                log["hardware_end"], log["not_restored"] = hardware(), problems
                write()
                quit_bench_apps()
    failed = [j for j in log["jobs"] if j.get("exit")]
    print(f"host run done: {len(log['jobs']) - len(failed)}/{len(log['jobs'])} jobs scored; then: ./bench judge "
          f"<run> and ./bench report {out}/pass*/*")
    return 1 if failed or problems else 0


def speakers() -> dict[str, str]:
    man = HERE / "clips" / "manifest.csv"
    return {r["clip_id"]: r.get("speaker_id") or "" for r in sc.read_csv(man)} if man.exists() else {}


def report_cmd(dirs: list[pathlib.Path], as_json: bool, resamples: int, seed: int) -> int:
    import report as rp
    runs = [rp.load_run(d) for d in dirs if (d / "score.json").exists()]
    if not runs:
        raise SystemExit("no scored runs (folders with score.json)")
    spk = speakers()
    if problems := rp.check_comparable(runs, spk):
        raise SystemExit("these runs can't be pooled or ranked together:\n  " + "\n  ".join(problems))
    summary = rp.summarize(runs, spk, resamples, seed)
    host = next((json.loads(h.read_text()) for d in dirs for h in (d.parent.parent / "host.json",) if h.exists()), None)
    hw = (host or {}).get("hardware_start") or runs[0]["run"].get("hardware")
    print(json.dumps({**summary, "hardware": hw}, indent=1) if as_json else rp.markdown(summary, hw))
    return 0


def judge_check(dirs: list[pathlib.Path], sample: int, seed: int, model: str) -> int:
    """The second judge (another model family) on a seeded stratified sample of each run's clips: agreement
    with the run's judge, kappa, both ZERs, rank changes. judge-check.json per run (numbers and clip ids); the
    clips the judges disagree on go to work/judge-check/ for a blinded human check (local only: it has text)."""
    import judge as jd
    import report as rp
    key = os.environ.get("JUDGE_API_KEY", "")
    if not key:
        print("judge check: not run (no JUDGE_API_KEY)")
        return 0
    manifest, checks = HERE / "clips" / "manifest.csv", {}
    ref = {m["clip_id"]: m for m in sc.read_csv(manifest)}
    human = HERE / "work" / "judge-check"
    human.mkdir(parents=True, exist_ok=True)
    rows, keys = [], []
    for d in dirs:
        run = rp.load_run(d)
        if not run["judge"]:
            print(f"{d}: no judge file for these results: ./bench judge {d} first", file=sys.stderr)
            return 1
        ids = rp.stratified_sample(run["judge"]["per_clip"], sample, seed)
        second = jd.judge_run(d, manifest, backend=jd.OpenRouterJudge(key, model=model), only=set(ids))
        ag = rp.agreement(run["judge"]["per_clip"], second["per_clip"], ids)
        out = {"primary": run["judge"]["judge"], "secondary": second["judge"], "seed": seed, "sample": ids, **ag}
        (d / "judge-check.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
        checks[run["config"]] = ag
        drafts = {r["clip_id"]: r["transcript"] for r in sc.read_csv(d / "results.csv")}
        verdict = {c["clip_id"]: c["zero_edit"] for c in run["judge"]["per_clip"]}
        verdict2 = {c["clip_id"]: c["zero_edit"] for c in second["per_clip"]}
        for cid in ag.get("disagreements", []):
            n = len(rows) + 1
            rows.append({"item": n, "draft": drafts[cid], "intended": ref[cid]["intended_text"], "sendable_as_is": ""})
            keys.append({"item": n, "config": run["config"], "clip_id": cid, "primary_zero_edit": verdict[cid],
                         "secondary_zero_edit": verdict2[cid]})
    import csv
    import random
    order = list(range(len(rows)))
    random.Random(seed).shuffle(order)  # blinded: no config, no verdicts, shuffled
    for path, data in ((human / "items.csv", [rows[i] for i in order]), (human / "key.csv", keys)):
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, list(data[0]) if data else ["item"])
            w.writeheader()
            w.writerows(data)
    for name, ag in sorted(checks.items()):
        print(f"{name}: n={ag.get('n')} agreement {ag.get('agreement')}% kappa {ag.get('kappa')} "
              f"ZER {ag.get('zer_primary')} vs {ag.get('zer_secondary')}")
    changes = rp.rank_changes(checks)
    print("rank changes: " + ("; ".join(changes) if changes else "none"))
    print(f"blinded human check: {human}/items.csv (key.csv holds the answers; both local only)")
    return 0


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
    s.add_argument("--overwrite", action="store_true", help="a test machine or VM: replace the app's settings")
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
    r.add_argument("--unverified-ok", action="store_true", help="a trial with a profile still to verify")
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
    h = sub.add_parser("host-run", help="passes of app:profile jobs on this Mac, for the headline numbers")
    h.add_argument("--jobs", required=True, help="comma-separated app:profile, e.g. handy:shipped,vocet:shipped")
    h.add_argument("--passes", type=positive_int, default=1)
    h.add_argument("--first-pass", type=positive_int, default=1, help="number of the first pass (another night)")
    h.add_argument("--seed", type=int, default=2026, help="clip order: seed + pass, the same for every app")
    h.add_argument("--machine", default=os.environ.get("BENCH_MACHINE", "owner-mac"))
    h.add_argument("--out", type=pathlib.Path)
    h.add_argument("--unverified-ok", action="store_true", help="a trial with profiles still to verify")
    sub.add_parser("restore-host", help="after an unfinished host run: put the apps' state and machine back")
    rp_ = sub.add_parser("report", help="passes, means and speaker-bootstrap intervals (markdown)")
    rp_.add_argument("runs", nargs="+", type=pathlib.Path)
    rp_.add_argument("--json", action="store_true")
    rp_.add_argument("--resamples", type=positive_int, default=2000)
    rp_.add_argument("--seed", type=int, default=0)
    jc = sub.add_parser("judge-check", help="a second judge on a seeded stratified sample (JUDGE_API_KEY)")
    jc.add_argument("runs", nargs="+", type=pathlib.Path)
    jc.add_argument("--sample", type=positive_int, default=40)
    jc.add_argument("--seed", type=int, default=7)
    jc.add_argument("--model", default=None, help="the second judge (default: judge.SECOND_JUDGE_MODEL)")
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
        setup(a.app, a.ci, overwrite=a.overwrite)
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
    if a.cmd == "host-run":
        return host_run(a)
    if a.cmd == "restore-host":
        return restore_host()
    if a.cmd == "report":
        return report_cmd(a.runs, a.json, a.resamples, a.seed)
    if a.cmd == "judge-check":
        import judge as jd
        return judge_check(a.runs, a.sample, a.seed, a.model or jd.SECOND_JUDGE_MODEL)
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
