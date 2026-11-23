"""The clip loop: one dictation per clip, the same way for every app. Pure Python (no macOS imports), so it
runs against the stub app in tests; macos.py supplies the real keys, audio and TextEdit.

For each clip:

  1. the target (a TextEdit document) is emptied, brought to the front and checked;
  2. the app's key is pressed (hold: key down; toggle: a tap);
  3. the harness waits until some process opens the BlackHole microphone, then the fixed start delay;
  4. the clip, padded with silence, starts playing into BlackHole, and from that moment the target is read
     every 20 ms, so text that arrives while speaking (streaming apps) is timed too;
  5. after the clip and the fixed tail the key is released (or tapped again);
  6. reading goes on until the text has not changed for `settle` seconds, or `timeout` after the release.

Outcomes. App outcomes are final on the first attempt (no rerunning an app until it passes):
  ok       the text settled before the deadline
  partial  text was still changing at the deadline (scored as it stood)
  empty    no text by the deadline, and none during the quiet period after it
  late     no text by the deadline, but text arrived during the quiet period (scored as no output)
Infrastructure faults are retried (up to `attempts`), each after the app is made idle again:
  err_target  the document lost the front or couldn't be emptied     err_read   Accessibility read failed
  err_audio   playback failed                                         err_ready  no process opened the mic

After anything but `ok` the harness waits a quiet period and makes the app idle (releases the key; a
toggle app, whose recording state is then unknown, is restarted), so a late paste can't land in the next
clip. Latency: `e2t_s` (primary) = end of speech in the clip -> last change of the text; `post_stop_s` =
key release -> last change; `first_text_s` = end of speech -> first text (negative for streaming apps).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import pathlib
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

HARNESS_VERSION = "2"

FIELDS = ["clip_id", "status", "attempts", "transcript", "audio_s", "speech_end_s", "ready_s", "e2t_s",
          "post_stop_s", "first_text_s", "error", "run_at", "raw_draft", "cleanup"]
# raw_draft and cleanup: an eval build's own record of the clip (App.drafts), empty otherwise. Like the
# transcript, they stay in the local results.csv: nothing uploads it.
APP_OUTCOMES = ("ok", "partial", "empty", "late")
INFRA_ERRORS = ("err_target", "err_read", "err_audio", "err_ready")


# --- Keys --------------------------------------------------------------------------------------------
# macOS virtual key codes, and for modifiers the CGEventFlags they set (generic mask | device bit).
SHIFT, CONTROL, ALTERNATE, COMMAND, SECONDARY_FN = 0x20000, 0x40000, 0x80000, 0x100000, 0x800000
NON_COALESCED = 0x100
MODIFIERS = {  # name: (keycode, flags)
    "lctrl": (59, CONTROL | 0x1), "rctrl": (62, CONTROL | 0x2000),
    "lshift": (56, SHIFT | 0x2), "rshift": (60, SHIFT | 0x4),
    "lcmd": (55, COMMAND | 0x8), "rcmd": (54, COMMAND | 0x10),
    "lalt": (58, ALTERNATE | 0x20), "ralt": (61, ALTERNATE | 0x40),
    "fn": (63, SECONDARY_FN),
}
KEYS = {"space": 49, "grave": 50, "return": 36, "escape": 53, "tab": 48, "a": 0, "delete": 51,
        "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90}
ALIASES = {"option": "lalt", "alt": "lalt", "ropt": "ralt", "rightoption": "ralt", "command": "lcmd",
           "cmd": "lcmd", "control": "lctrl", "ctrl": "lctrl", "shift": "lshift", "globe": "fn"}


@dataclass(frozen=True)
class Key:
    name: str
    code: int
    flags: int = 0  # non-zero for a modifier: it is sent as flagsChanged with these flags

    @property
    def modifier(self) -> bool:
        return self.flags != 0


def parse_hotkey(spec: str) -> list[Key]:
    """'ralt' or 'lalt+space' -> the keys in press order (released in reverse). Modifiers come first."""
    keys = []
    for part in spec.lower().replace(" ", "").split("+"):
        name = ALIASES.get(part, part)
        if name in MODIFIERS:
            code, flags = MODIFIERS[name]
            keys.append(Key(name, code, flags))
        elif name in KEYS:
            keys.append(Key(name, KEYS[name]))
        else:
            raise ValueError(f"unknown key '{part}' in hotkey '{spec}'")
    plain = [i for i, k in enumerate(keys) if not k.modifier]
    if plain and (len(plain) > 1 or plain[0] != len(keys) - 1):
        raise ValueError(f"at most one non-modifier key, and it goes last: '{spec}'")
    return keys


def key_events(keys: list[Key], down: bool) -> list[tuple[int, bool, int, bool]]:
    """The events for pressing (down) or releasing a chord: (keycode, is_down, flags, is_flags_changed).
    Modifiers carry the cumulative flags, as the hardware sends them; the main key carries the held ones."""
    events, held = [], 0
    if down:
        for k in keys:
            if k.modifier:
                held |= k.flags
                events.append((k.code, True, held | NON_COALESCED, True))
            else:
                events.append((k.code, True, held, False))
    else:
        for k in keys:
            held |= k.flags
        for k in reversed(keys):
            if k.modifier:
                held &= ~k.flags
                events.append((k.code, False, held | NON_COALESCED, True))
            else:
                events.append((k.code, False, held, False))
    return events


# --- Apps --------------------------------------------------------------------------------------------
@dataclass
class App:
    """One app's checked-in configuration (apps/<name>.json): the release, how it's driven, and the
    settings its setup applies. `effective` is filled in at run time from what the app actually uses."""
    name: str
    version: str
    process: str
    app_path: str
    hotkey: str
    mode: str = "hold"
    hybrid: bool = False                          # hold = push-to-talk, tap = toggle: driven by holding, but
                                                  #   its state is unclear after a fault, like a toggle app
    release: dict = field(default_factory=dict)   # url, sha256, tag, commit
    model: dict = field(default_factory=dict)     # id, size_mb, backend
    language: str = "en"
    cleanup: str = "off"
    notes: str = ""
    hosted_setup: bool = False                    # apps/<name>.sh can set it up with no person (CI)
    settings_files: list = field(default_factory=list)  # hashed into run.json: what the app ran with
    logs: str = ""                                # the app's log files (glob), shown when the smoke test fails
    log_counts: str = ""                          # a regex with one group: its values in the app's log are
                                                  #   counted over the run (numbers only, never the lines)
    drafts: str = ""                              # an eval build's drafts file (JSON lines with raw, final,
                                                  #   cleanup; Vocet's eval-capture): per clip into results.csv

    def __post_init__(self) -> None:
        parse_hotkey(self.hotkey)
        if self.hybrid and self.mode != "hold":
            raise ValueError(f"{self.name}: a hybrid app is driven by holding (mode hold)")
        if self.mode not in ("hold", "toggle"):
            raise ValueError(f"{self.name}: mode must be hold or toggle")


def restarts_after_unclear(app: App) -> bool:
    """A toggle or hybrid app's recording state is unknown after an unclear attempt: restart it."""
    return app.mode == "toggle" or app.hybrid


def load_app(path: pathlib.Path) -> App:
    return App(**json.loads(path.read_text()))


# --- Clips -------------------------------------------------------------------------------------------
@dataclass
class Clip:
    clip_id: str
    path: pathlib.Path
    audio_s: float
    speech_end_s: float | None = None  # end of speech within the clip (unpadded); the driver measures it
    reference: str = ""                # only the stub app uses it (it "transcribes" perfectly)


def load_clips(folder: pathlib.Path, limit: int | None = None, only: set[str] | None = None) -> list[Clip]:
    """Clips in manifest order (manifest.csv from fetch_clips.py: file, clip_id, audio_length_s, ...)."""
    with (folder / "manifest.csv").open(newline="", encoding="utf-8") as fh:
        man = list(csv.DictReader(fh))
    clips = [Clip(r["clip_id"], folder / pathlib.Path(r.get("file") or f"{r['clip_id']}.flac").name,
                  float(r.get("audio_length_s") or 0), reference=r.get("intended_text", ""))
             for r in man if not only or r["clip_id"] in only]
    return clips[:limit] if limit else clips


def speech_end(samples, rate: int, frame_s: float = 0.03) -> float:
    """End of speech in seconds: the end of the last 30 ms frame whose RMS is at least 10% of the loud
    frames' level (95th percentile). A plain energy rule: the clips are clean, close-talk recordings."""
    n = max(1, int(rate * frame_s))
    rms = []
    for i in range(0, len(samples), n):
        fr = samples[i:i + n]
        rms.append(math.sqrt(sum(float(x) * float(x) for x in fr) / max(1, len(fr))))
    if not rms:
        return 0.0
    loud = sorted(rms)[min(len(rms) - 1, int(0.95 * len(rms)))]
    if loud <= 0:
        return 0.0
    last = max(i for i, r in enumerate(rms) if r >= 0.1 * loud)
    return min(len(samples) / rate, (last + 1) * frame_s)


# --- The loop ----------------------------------------------------------------------------------------
@dataclass
class Timing:
    pad_ms: int = 300          # silence before and after each clip
    start_delay: float = 0.5   # after the app opened the microphone
    tail: float = 4.0          # after the clip, before the release
    settle: float = 2.0        # text unchanged this long = done
    timeout: float = 30.0      # after the release
    quiet: float = 5.0         # after a non-ok outcome: watch for late text before the next clip
    ready_timeout: float = 10.0  # the first dictation may also load the model
    gap: float = 1.0
    poll: float = 0.02
    timed: bool = True         # False: accuracy only (a machine whose speed doesn't count)


def accuracy_only() -> Timing:
    """Same audio, hold and tail; a longer wait for text, so a slow machine costs time, not accuracy."""
    return Timing(settle=3.0, timeout=60.0, quiet=10.0, timed=False)


class InfraError(Exception):
    def __init__(self, status: str, msg: str) -> None:
        assert status in INFRA_ERRORS
        super().__init__(msg)
        self.status = status


class Clock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, s: float) -> None:
        if s > 0:
            time.sleep(s)


class Target:
    """Where the text lands. prepare(): empty it, bring it to the front, check (InfraError err_target).
    read(): its text (InfraError err_read if Accessibility fails). verify(): still the focused target."""

    def prepare(self) -> None: ...
    def read(self) -> str: ...
    def verify(self) -> bool:
        return True


class Driver:
    """How the app is started and stopped, and how the audio reaches it."""

    def load(self, clip: Clip) -> None:
        """Prepare the clip's audio; set clip.speech_end_s."""
    def press(self) -> float:
        """Start the dictation; returns when the start event was posted."""
        raise NotImplementedError
    def mic_open(self) -> bool | None:
        """True once some process is capturing; None if this driver can't tell (fixed delay only)."""
        return None
    def play(self, clip: Clip) -> float:
        """Start playback (non-blocking); returns when it started."""
        return 0.0
    def playing(self) -> bool:
        return False
    def play_error(self) -> str | None:
        return None
    def release(self) -> float:
        """Stop the dictation; returns when the stop event was posted."""
        raise NotImplementedError
    def recover(self, restart: bool) -> None:
        """Make the app idle: release any held key; restart=True restarts the app."""


@dataclass
class Result:
    status: str
    transcript: str = ""
    ready_s: float | None = None
    speech_end_s: float | None = None
    e2t_s: float | None = None
    post_stop_s: float | None = None
    first_text_s: float | None = None
    error: str = ""


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def run_clip(clip: Clip, driver: Driver, target: Target, clock: Clock, timing: Timing) -> Result:
    """One attempt. Raises InfraError for a fault the harness, not the app, is responsible for."""
    target.prepare()
    driver.load(clip)
    t_down = driver.press()
    ready = None
    while True:
        state = driver.mic_open()
        if state is None:
            break
        if state:
            ready = clock.now() - t_down
            break
        if clock.now() - t_down >= timing.ready_timeout:
            driver.release()
            raise InfraError("err_ready", f"no process opened the microphone within {timing.ready_timeout:g} s")
        clock.sleep(0.005)
    clock.sleep(timing.start_delay)

    text, first, last = "", None, None

    def poll() -> None:
        nonlocal text, first, last
        cur = target.read()
        if cur != text:
            text, last = cur, clock.now()
            if first is None and cur.strip():
                first = last

    t_play = driver.play(clip)
    end_of_speech = t_play + timing.pad_ms / 1000 + (clip.speech_end_s if clip.speech_end_s is not None
                                                     else clip.audio_s)
    play_until = t_play + clip.audio_s + 2 * timing.pad_ms / 1000
    while clock.now() < play_until or driver.playing():
        poll()
        clock.sleep(timing.poll)
    if (err := driver.play_error()):
        driver.release()
        raise InfraError("err_audio", f"playback failed: {err}")
    while clock.now() < play_until + timing.tail:
        poll()
        clock.sleep(timing.poll)
    t_stop = driver.release()
    if not target.verify():
        raise InfraError("err_target", "the document lost the front while dictating")
    status = "empty"
    while True:
        poll()
        now = clock.now()
        if text.strip() and now - last >= timing.settle:
            status = "ok"
            break
        if now - t_stop >= timing.timeout:
            status = "partial" if text.strip() else "empty"
            break
        clock.sleep(timing.poll)
    if not target.verify():
        raise InfraError("err_target", "the document lost the front while waiting for text")
    res = Result(status, text.strip(), ready_s=_r(ready), speech_end_s=_r(clip.speech_end_s))
    if status in ("ok", "partial"):
        res.e2t_s, res.post_stop_s = _r(last - end_of_speech), _r(last - t_stop)
        res.first_text_s = _r(first - end_of_speech)
    if status != "ok":
        # A quiet period: anything that arrives now belongs to this clip, not the next.
        before, t_q = text, clock.now()
        while clock.now() - t_q < timing.quiet:
            poll()
            clock.sleep(timing.poll)
        if status == "empty" and text.strip():
            res = Result("late", text.strip(), ready_s=_r(ready), speech_end_s=_r(clip.speech_end_s),
                         error=f"text arrived after the {timing.timeout:g} s deadline")
        elif status == "partial":
            res.error = "text still changing at the deadline" + (" and after it" if text != before else "")
        else:
            res.error = f"no text within {timing.timeout:g} s of the release"
    return res


def fingerprint(**parts) -> str:
    """A short digest of everything a run's results depend on (app config, binary hash, harness version,
    timing, dataset, machine label, selection). Resume and scoring refuse rows from another fingerprint."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def load_rows(path: pathlib.Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    ids = [r["clip_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"{path}: duplicate clip ids")
    return {r["clip_id"]: r for r in rows}


def write_rows(path: pathlib.Path, rows: dict[str, dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows.values():
            w.writerow(r)
    tmp.replace(path)


def flushed(msg: str) -> None:
    print(msg, flush=True)


def fmt(v) -> str:
    return "" if v is None else str(v)


class Drafts:
    """An eval build's drafts file: one JSON line per dictation, appended by the app. The harness notes
    where the file ends before a clip and reads the last complete line written after that."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def mark(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def since(self, offset: int) -> dict:
        try:
            with self.path.open("rb") as fh:
                fh.seek(offset)
                new = fh.read()
        except OSError:
            return {}
        for line in reversed(new.split(b"\n")):
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a torn or partial line
            if isinstance(rec, dict):
                return rec
        return {}


def run_all(clips: list[Clip], driver: Driver, target: Target, clock: Clock, timing: Timing, out: pathlib.Path,
            *, run: dict, restart_after_ambiguous: bool, attempts: int = 3, log=None,
            drafts: Drafts | None = None) -> dict[str, dict]:
    """Runs the clips into out/results.csv, with out/run.json describing the run (`run`, which must carry
    a "fingerprint"). A rerun resumes only with the same fingerprint, and keeps only rows of these clips.
    App outcomes are final; infrastructure faults are retried. Returns the rows by clip_id."""
    log = log or flushed  # one line per clip, visible at once in a CI log (stdout is a pipe there)
    out.mkdir(parents=True, exist_ok=True)
    meta_path, rows_path = out / "run.json", out / "results.csv"
    if meta_path.exists():
        old = json.loads(meta_path.read_text())
        if old.get("fingerprint") != run["fingerprint"]:
            raise SystemExit(f"{out} holds a different run ({old.get('fingerprint')} vs {run['fingerprint']}): "
                             "use a new output folder")
    meta_path.write_text(json.dumps(run, indent=1, sort_keys=True) + "\n")
    wanted = {c.clip_id for c in clips}
    rows = {k: v for k, v in load_rows(rows_path).items() if k in wanted}
    write_rows(rows_path, rows)
    for i, clip in enumerate(clips, 1):
        if rows.get(clip.clip_id, {}).get("status") in APP_OUTCOMES:
            continue
        res, tries, mark = Result("err_target", error="not run"), 0, 0
        for tries in range(1, attempts + 1):
            mark = drafts.mark() if drafts else 0  # the last attempt's record is the clip's
            try:
                res = run_clip(clip, driver, target, clock, timing)
            except InfraError as e:
                res = Result(e.status, error=str(e))
            if res.status == "ok":
                break
            ambiguous_toggle = restart_after_ambiguous and res.status != "ok"
            driver.recover(restart=ambiguous_toggle or res.status in ("partial", "late"))
            if res.status in APP_OUTCOMES:
                break
            clock.sleep(timing.gap)
        rows[clip.clip_id] = {
            "clip_id": clip.clip_id, "status": res.status, "attempts": tries, "transcript": res.transcript,
            "audio_s": round(clip.audio_s, 3), "speech_end_s": fmt(res.speech_end_s), "ready_s": fmt(res.ready_s),
            "e2t_s": fmt(res.e2t_s), "post_stop_s": fmt(res.post_stop_s), "first_text_s": fmt(res.first_text_s),
            "error": res.error, "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if drafts:
            rec = drafts.since(mark)
            rows[clip.clip_id].update(raw_draft=str(rec.get("raw") or ""), cleanup=str(rec.get("cleanup") or ""))
        write_rows(rows_path, rows)
        log(f"[{i}/{len(clips)}] {clip.clip_id} {res.status} tries={tries} e2t={fmt(res.e2t_s)} "
            f"chars={len(res.transcript)} {res.error}")
        clock.sleep(timing.gap)
    return rows


# --- The stub app (dry run and tests) ----------------------------------------------------------------
class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(s, 0.0)


@dataclass
class StubApp:
    """A dictation app on a simulated clock. The microphone opens `open_delay` after the press (at once
    when warm); it hears what plays while it is open; `latency` after the release it pastes the clip's
    reference if it heard all of it (the words it heard otherwise), optionally in pieces. `streaming`
    pastes the words as they are spoken instead. `toggle` apps start and stop on a tap."""
    clock: FakeClock
    open_delay: float = 0.3
    latency: float = 1.2
    warm: bool = False
    never_opens: bool = False
    paste_in_chunks: int = 1
    streaming: bool = False
    drafts: pathlib.Path | None = None  # an eval build: one JSON line per paste, its raw draft "um " + text
    pressed_at: float | None = None
    played: tuple[float, float, Clip] | None = None
    pending: list[tuple[float, str]] = field(default_factory=list)
    text: str = ""
    presses: int = 0
    restarts: int = 0

    def key(self, down: bool) -> None:
        now = self.clock.now()
        if down:
            self.presses += 1
            self.pressed_at = now
            return
        if self.pressed_at is None:
            return
        open_at = self.pressed_at + (0 if self.warm else self.open_delay)
        self.pressed_at = None
        if self.never_opens or not self.played or self.streaming:
            return
        p0, p1, clip = self.played
        heard = max(0.0, min(now, p1) - max(open_at, p0)) / max(p1 - p0, 1e-9)
        words = clip.reference.split()
        out = " ".join(words[:round(len(words) * heard)]) if heard < 0.999 else clip.reference
        if out:
            n = max(1, self.paste_in_chunks)
            step = math.ceil(len(out) / n)
            for i in range(n):
                self.pending.append((now + self.latency + i * 0.1, out[:step * (i + 1)]))
            if self.drafts:
                with self.drafts.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"raw": "um " + out, "final": out, "cleanup": "applied"}) + "\n")
        self.played = None

    def mic_open(self) -> bool:
        if self.never_opens or self.pressed_at is None:
            return False
        return self.warm or self.clock.now() >= self.pressed_at + self.open_delay

    def play(self, clip: Clip, seconds: float) -> float:
        t0 = self.clock.now()
        self.played = (t0, t0 + seconds, clip)
        if self.streaming and clip.reference:
            words = clip.reference.split()
            for i in range(1, len(words) + 1):
                self.pending.append((t0 + seconds * i / len(words), " ".join(words[:i])))
        return t0

    def visible_text(self) -> str:
        now = self.clock.now()
        while self.pending and self.pending[0][0] <= now:
            self.text = self.pending.pop(0)[1]
        return self.text


class StubDriver(Driver):
    def __init__(self, app: StubApp, pad_ms: int = 300) -> None:
        self.app, self.pad_ms, self.until = app, pad_ms, 0.0

    def load(self, clip: Clip) -> None:
        clip.speech_end_s = clip.speech_end_s if clip.speech_end_s is not None else max(0.0, clip.audio_s - 0.2)

    def press(self) -> float:
        self.app.key(True)
        return self.app.clock.now()

    def mic_open(self) -> bool | None:
        return self.app.mic_open()

    def play(self, clip: Clip) -> float:
        secs = clip.audio_s + 2 * self.pad_ms / 1000
        self.until = self.app.clock.now() + secs
        return self.app.play(clip, secs)

    def playing(self) -> bool:
        return self.app.clock.now() < self.until

    def release(self) -> float:
        self.app.key(False)
        return self.app.clock.now()

    def recover(self, restart: bool) -> None:
        self.app.pressed_at = None
        if restart:
            self.app.restarts += 1
            self.app.pending = []


class StubTarget(Target):
    def __init__(self, app: StubApp) -> None:
        self.app = app

    def prepare(self) -> None:
        self.app.text = ""

    def read(self) -> str:
        return self.app.visible_text()


def dry_run(out: pathlib.Path, n: int = 5, log=print) -> dict[str, dict]:
    clock = FakeClock()
    app = StubApp(clock)
    clips = [Clip(f"stub{i:03d}", pathlib.Path(f"stub{i:03d}.flac"), 2.0 + i, reference=f"stub clip {i} says hello")
             for i in range(1, n + 1)]
    run = {"fingerprint": fingerprint(app="stub", n=n), "app": "stub", "timing": asdict(Timing())}
    return run_all(clips, StubDriver(app), StubTarget(app), clock, Timing(), out, run=run,
                   restart_after_ambiguous=False, log=log)
