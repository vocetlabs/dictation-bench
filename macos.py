"""The real Mac side of the harness: key events, the BlackHole microphone, playback, the TextEdit target,
restarting an app. Needs macOS, Accessibility for the process running it (Terminal, or the CI runner) and
the dependencies in bench.py's header."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import pathlib
import subprocess
import sys
import time

from harness import (Clip, Driver, InfraError, Key, MODIFIERS, KEYS, App, Target, Timing, key_events,
                     parse_hotkey, speech_end)

TEXTEDIT = "com.apple.TextEdit"


class MacKeys:
    """Posts a chord's events at the HID tap; returns the time of the last event."""

    def __init__(self, keys: list[Key]) -> None:
        import Quartz
        self.Q, self.keys, self.held = Quartz, keys, False

    def _post(self, down: bool) -> float:
        Q, t = self.Q, time.monotonic()
        for code, is_down, flags, is_flags in key_events(self.keys, down):
            ev = Q.CGEventCreateKeyboardEvent(None, code, is_down)
            if is_flags:
                Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
            Q.CGEventSetFlags(ev, flags)
            t = time.monotonic()
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.01)
        return t

    def down(self) -> float:
        self.held = True
        return self._post(True)

    def up(self) -> float:
        t = self._post(False)
        self.held = False
        return t

    def tap(self) -> float:
        """Returns the time of the key-down, the event a toggle app acts on."""
        t = self.down()
        time.sleep(0.08)
        self.up()
        return t


class CoreAudio:
    """Device lookups through the CoreAudio HAL (ctypes; pyobjc doesn't wrap it)."""

    def __init__(self) -> None:
        self.ca = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
        self.cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

        class Addr(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]
        self.Addr = Addr
        u32p = ctypes.POINTER(ctypes.c_uint32)
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                                           ctypes.c_void_p, u32p]
        self.ca.AudioObjectGetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                                       ctypes.c_void_p, u32p, ctypes.c_void_p]
        self.ca.AudioObjectSetPropertyData.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32,
                                                       ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    def addr(self, sel: str, scope: str = "glob"):
        return self.Addr(int.from_bytes(sel.encode(), "big"), int.from_bytes(scope.encode(), "big"), 0)

    def u32(self, obj: int, sel: str, scope: str = "glob") -> int:
        val, size = ctypes.c_uint32(0), ctypes.c_uint32(4)
        a = self.addr(sel, scope)
        err = self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(a), 0, None, ctypes.byref(size), ctypes.byref(val))
        if err:
            raise OSError(f"CoreAudio property {sel} failed (OSStatus {err})")
        return val.value

    def name(self, dev: int) -> str:
        return self.string(dev, "lnam")

    def uid(self, dev: int) -> str:
        """The device's persistent unique ID (kAudioDevicePropertyDeviceUID): two devices can share a name."""
        return self.string(dev, "uid ")

    def string(self, dev: int, sel: str) -> str:
        ref, size = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        a = self.addr(sel)
        if self.ca.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None, ctypes.byref(size), ctypes.byref(ref)):
            return ""
        buf = ctypes.create_string_buffer(512)
        self.cf.CFStringGetCString(ref, buf, 512, 0x08000100)
        self.cf.CFRelease(ref)
        return buf.value.decode()

    def input_ids(self) -> list[int]:
        a, size = self.addr("dev#"), ctypes.c_uint32(0)
        self.ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(size))
        ids = (ctypes.c_uint32 * (size.value // 4))()
        self.ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(size), ids)
        out = []
        for d in ids:
            s, sa = ctypes.c_uint32(0), self.addr("stm#", "inpt")
            self.ca.AudioObjectGetPropertyDataSize(d, ctypes.byref(sa), 0, None, ctypes.byref(s))
            if s.value:
                out.append(d)
        return out

    def inputs(self) -> dict[str, int]:
        return {self.name(d): d for d in self.input_ids()}

    def input_by_uid(self, uid: str) -> int:
        hit = next((d for d in self.input_ids() if self.uid(d) == uid), None)
        if hit is None:
            raise SystemExit(f"no input device with UID '{uid}'")
        return hit

    def find_input(self, device: str) -> int:
        """The input device with exactly this name."""
        inputs = self.inputs()
        if device not in inputs:
            raise SystemExit(f"no input device named '{device}' (inputs: {list(inputs)})")
        return inputs[device]

    def capturing_pids(self) -> set[int] | None:
        """PIDs of the processes capturing audio input now (macOS 14+ process objects); None if unknown."""
        a, size = self.addr("prs#"), ctypes.c_uint32(0)
        if self.ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(size)):
            return None
        objs = (ctypes.c_uint32 * (size.value // 4))()
        if self.ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(size), objs):
            return None
        try:
            return {self.u32(o, "ppid") for o in objs if self.u32(o, "piri")}
        except OSError:
            return None

    def default_input_name(self) -> str:
        return self.name(self.u32(1, "dIn "))

    def default_input_uid(self) -> str:
        return self.uid(self.u32(1, "dIn "))

    def set_default_input(self, dev: int) -> None:
        val, a = ctypes.c_uint32(dev), self.addr("dIn ")
        if self.ca.AudioObjectSetPropertyData(1, ctypes.byref(a), 0, None, 4, ctypes.byref(val)):
            raise SystemExit("couldn't set the default input")


class BlackHoleDriver(Driver):
    """The black-box path: the app's key, the clip played into BlackHole 2ch, the mic-open signal."""

    def __init__(self, app: App, timing: Timing, device: str = "BlackHole 2ch") -> None:
        import numpy as np
        import sounddevice as sd
        self.np, self.sd, self.app, self.timing = np, sd, app, timing
        self.keys = MacKeys(parse_hotkey(app.hotkey))
        self.ca = CoreAudio()
        self.bh_in = self.ca.find_input(device)
        self.out_dev = next((i for i, d in enumerate(sd.query_devices())
                             if device.lower() in d["name"].lower() and d["max_output_channels"] > 0), None)
        if self.out_dev is None:
            raise SystemExit(f"no output device '{device}'")
        info = sd.query_devices(self.out_dev)
        self.rate, self.channels = int(info["default_samplerate"]), info["max_output_channels"]
        self.audio, self.error = None, None

    def load(self, clip: Clip) -> None:
        import soundfile as sf
        import soxr
        np = self.np
        a, sr = sf.read(clip.path, dtype="float32", always_2d=True)
        a = a.mean(axis=1)
        clip.speech_end_s = speech_end(a, sr)
        if sr != self.rate:
            a = soxr.resample(a, sr, self.rate).astype("float32")
        peak = float(np.abs(a).max()) if a.size else 0.0
        if peak > 1.0:
            a /= peak
        pad = np.zeros(int(self.rate * self.timing.pad_ms / 1000), dtype="float32")
        self.audio = np.repeat(np.concatenate([pad, a, pad])[:, None], self.channels, axis=1)

    def press(self) -> float:
        return self.keys.tap() if self.app.mode == "toggle" else self.keys.down()

    def mic_open(self) -> bool | None:
        return mic_decision(self.ca.capturing_pids(), bundle_pids(self.app))

    def play(self, clip: Clip) -> float:
        self.error = None
        t = time.monotonic()
        try:
            self.sd.play(self.audio, self.rate, device=self.out_dev)
        except Exception as e:  # noqa: BLE001 - reported as a playback fault
            self.error = str(e)
        return t

    def playing(self) -> bool:
        try:
            return bool(self.sd.get_stream().active)
        except Exception:  # noqa: BLE001 - no stream: not playing
            return False

    def play_error(self) -> str | None:
        if self.error:
            return self.error
        try:
            st = self.sd.get_status()
            return "output underflow" if st and st.output_underflow else None
        except Exception:  # noqa: BLE001
            return None

    def release(self) -> float:
        if self.app.mode == "toggle":
            return self.keys.tap()
        if self.keys.held:
            return self.keys.up()
        return time.monotonic()

    def recover(self, restart: bool) -> None:
        try:
            self.sd.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.keys.held:
            self.keys.up()
        if restart:
            restart_app(self.app)

    def rss_mb(self) -> float | None:
        pids = bundle_pids(self.app)  # the bench's copy and its children only
        return sum(rss for pid, _, rss, _ in _procs() if pid in pids) / 1024 if pids else None


def _ps(fields: str) -> list[list[str]]:
    """This account's processes (`ps -U <uid>`): the bench never looks at, or stops, another account's."""
    out = subprocess.run(["ps", "-U", str(os.getuid()), "-o", fields], capture_output=True, text=True).stdout
    n = fields.count(",") + 1
    return [p for p in (line.split(None, n - 1) for line in out.splitlines())
            if len(p) == n and all(x.isdigit() for x in p[:-1])]


def _procs() -> list[tuple[int, int, int, str]]:
    return [(int(a), int(b), int(c), os.path.basename(d)) for a, b, c, d in _ps("pid=,ppid=,rss=,comm=")]


def _paths() -> list[tuple[int, int, str]]:
    return [(int(a), int(b), c) for a, b, c in _ps("pid=,ppid=,comm=")]


def tree_under(rows: list[tuple[int, int, str]], prefix: str) -> set[int]:
    """PIDs of the processes whose executable lies inside `prefix` (the bench's own app bundle), and all
    their descendants (helpers, model servers)."""
    tree = {pid for pid, _, exe in rows if exe.startswith(prefix)}
    grew = bool(tree)
    while grew:
        more = {pid for pid, ppid, _ in rows if ppid in tree} - tree
        grew, tree = bool(more), tree | more
    return tree


def foreign_copies(rows: list[tuple[int, int, str]], names: set[str], prefix: str) -> list[str]:
    """Executables named like a benchmarked app but launched from elsewhere (an /Applications copy): the
    bench never quits those, so a host run refuses to start while one runs."""
    return sorted({exe for _, _, exe in rows if os.path.basename(exe) in names and not exe.startswith(prefix)})


def other_apps(rows: list[tuple[int, int, str]], own: str, names: set[str], bench_prefix: str) -> list[str]:
    """Running executables, other than the app under test (`own`, its bench bundle), that are another app from
    the bench's folder or carry a benchmarked app's process name (a copy anywhere else)."""
    return sorted({exe for _, _, exe in rows if not exe.startswith(own)
                   and (exe.startswith(bench_prefix) or os.path.basename(exe) in names)})


def benchmarked_names() -> set[str]:
    import json
    here = pathlib.Path(__file__).resolve().parent / "apps"
    return {n for p in here.glob("*.*.json") if (n := json.loads(p.read_text()).get("process"))}


def launched_from(rows: list[tuple[int, int, str]], prefix: str) -> set[int]:
    """PIDs of the processes whose own executable lies inside `prefix` (no descendants from elsewhere)."""
    return {pid for pid, _, exe in rows if exe.startswith(prefix)}


def quit_tree(prefix: str, rows=None, kill=os.kill, wait: float = 10.0) -> None:
    """Quits the processes whose executable lies inside `prefix` (a bench bundle or the bench's app folder) by
    PID: TERM, then KILL. Never by process name, and never a process whose executable is elsewhere, even a
    descendant of a bench app (an /Applications copy it opened stays untouched)."""
    import signal
    rows = rows or _paths
    for sig, timeout in ((signal.SIGTERM, wait), (signal.SIGKILL, 5.0)):
        pids = launched_from(rows(), prefix)
        if not pids:
            return
        for pid in pids:
            try:
                kill(pid, sig)
            except ProcessLookupError:
                pass
        for _ in range(max(1, int(timeout / 0.2))):
            if not launched_from(rows(), prefix) & pids:
                return
            time.sleep(0.2)
    raise SystemExit(f"processes from {prefix} won't quit")


def bundle_pids(app: App) -> set[int]:
    """The processes launched from the bench's copy of the app (never an owner's copy of the same name)."""
    return tree_under(_paths(), str(bundle(app)) + "/Contents/")


def mic_decision(capturing: set[int] | None, app_tree: set[int]) -> bool:
    """Readiness: True once the app under test (launched from the bench bundle) captures audio, False while
    nothing does. No device-wide fallback: if CoreAudio can't say who is capturing, or anyone else is, or
    the bench's app isn't running, the attempt is an infrastructure fault."""
    if capturing is None:
        raise InfraError("err_ready", "CoreAudio can't tell which process is capturing (needs macOS 14+); "
                                      "the bench doesn't fall back to device-wide activity")
    if not app_tree:
        raise InfraError("err_ready", "the app launched from the bench folder isn't running")
    others = capturing - app_tree
    if others:
        raise InfraError("err_ready", f"another process is capturing audio (pid {sorted(others)})")
    return bool(capturing & app_tree)


def apps_dir() -> pathlib.Path:
    """Where the bench installs apps: always its own folder, never /Applications (shared with the owner)."""
    return pathlib.Path(__file__).resolve().parent / "work" / "Applications"


def bundle(app: App) -> pathlib.Path:
    return apps_dir() / pathlib.Path(app.app_path).name


def restart_app(app: App, settle: float = 10.0) -> None:
    """Quit the bench's copy (by PID: TERM, then KILL) and reopen it in the background, then let it load its
    model."""
    quit_tree(str(bundle(app)) + "/Contents/")
    subprocess.run(["open", "-g", str(bundle(app))], check=False)
    time.sleep(settle)


class TextEditTarget(Target):
    """One plain-text TextEdit document (`scratch`), the only window of a fresh TextEdit. prepare() empties
    it, raises it as the key window and checks; read() fails loudly; verify() checks it still has the
    keyboard focus (TextEdit frontmost and this window focused)."""

    def __init__(self, scratch: pathlib.Path) -> None:
        import AppKit
        import ApplicationServices as AS
        self.AK, self.AS, self.scratch = AppKit, AS, scratch
        scratch.parent.mkdir(parents=True, exist_ok=True)
        if not scratch.exists():  # never rewritten while open: TextEdit would report a conflicting change
            scratch.write_text("")
        self.app = self.window = self.area = None
        # Never quit TextEdit: it may hold other documents. It must be closed, or show only this one.
        if self.AK.NSRunningApplication.runningApplicationsWithBundleIdentifier_(TEXTEDIT):
            _, wins = self._get(self._app(), "AXWindows")
            others = [w for w in wins or [] if not self._is_scratch(w)]
            if others:
                raise SystemExit(f"TextEdit has other documents open ({len(others)}): save and quit it first; "
                                 "the bench uses its own scratch document")
        subprocess.run(["open", "-F", "-a", "TextEdit", str(scratch)], check=False)

    def _get(self, el, attr):
        err, val = self.AS.AXUIElementCopyAttributeValue(el, attr, None)
        return (err, val)

    def _is_scratch(self, window) -> bool:
        """The window's document (AXDocument, a file URL) is exactly the scratch file, not just a namesake."""
        return is_document(self._get(window, "AXDocument")[1], self.scratch)

    def _app(self):
        apps = self.AK.NSRunningApplication.runningApplicationsWithBundleIdentifier_(TEXTEDIT)
        if not apps:
            raise InfraError("err_target", "TextEdit isn't running")
        return self.AS.AXUIElementCreateApplication(apps[0].processIdentifier())

    def _find(self) -> None:
        self.app = self._app()
        _, wins = self._get(self.app, "AXWindows")
        self.window = next((w for w in wins or [] if self._is_scratch(w)), None)
        if self.window is None:
            raise InfraError("err_target", f"no TextEdit window for {self.scratch.name}")

        def find(node, depth=0):
            if node is None or depth > 8:
                return None
            if self._get(node, "AXRole")[1] == "AXTextArea":
                return node
            for ch in self._get(node, "AXChildren")[1] or []:
                hit = find(ch, depth + 1)
                if hit is not None:
                    return hit
            return None
        self.area = find(self.window)
        if self.area is None:
            raise InfraError("err_target", "no text area in the scratch document")

    def verify(self) -> bool:
        """TextEdit frontmost (asked of TextEdit: system-wide AX queries fail in VMs, and NSWorkspace goes
        stale without a run loop) and the scratch window is TextEdit's focused window."""
        if self.app is None:
            return False
        err, front = self._get(self.app, "AXFrontmost")
        err2, focused = self._get(self.app, "AXFocusedWindow")
        import CoreFoundation
        return err == 0 and bool(front) and err2 == 0 and focused is not None and \
            bool(CoreFoundation.CFEqual(focused, self.window))

    def prepare(self) -> None:
        subprocess.run(["open", "-a", "TextEdit", str(self.scratch)], check=False)
        for _ in range(150):  # TextEdit's first launch on a fresh machine can take seconds
            try:
                self._find()
                self.AS.AXUIElementPerformAction(self.window, "AXRaise")
                self.AS.AXUIElementSetAttributeValue(self.window, "AXMain", True)
                self.AS.AXUIElementSetAttributeValue(self.area, "AXFocused", True)
                if self.verify():
                    break
            except InfraError:
                pass
            time.sleep(0.1)
        else:
            raise InfraError("err_target", "the scratch document didn't come to the front")
        self.AS.AXUIElementSetAttributeValue(self.area, "AXValue", "")
        time.sleep(0.2)
        if self.read():  # not settable: select all and delete, as a person would
            MacKeysChord("lcmd+a").tap()
            MacKeysChord("delete").tap()
            time.sleep(0.3)
        if self.read():
            raise InfraError("err_target", "the document didn't empty")
        time.sleep(0.3)
        if self.read():
            raise InfraError("err_target", "text arrived in the emptied document (a late paste)")

    def read(self) -> str:
        err, v = self._get(self.area, "AXValue")
        if err != 0:
            raise InfraError("err_read", f"reading the document failed (AXError {err})")
        return "" if v is None else str(v)


def is_document(url, path: pathlib.Path) -> bool:
    """Whether an AXDocument value (a file:// URL, or None for an unsaved document) names exactly `path`."""
    from urllib.parse import unquote, urlparse
    if not url:
        return False
    u = urlparse(str(url))
    return u.scheme == "file" and pathlib.Path(unquote(u.path)).resolve() == path.resolve()


def MacKeysChord(spec: str) -> MacKeys:  # noqa: N802 - reads like a constructor
    return MacKeys(parse_hotkey(spec))


def preflight(app: App) -> dict:
    """What the run depends on; exits if it's wrong. Returns the facts for run.json."""
    import platform
    env = {"macos": platform.mac_ver()[0], "arch": platform.machine()}
    for k in ("NSAutomaticSpellingCorrectionEnabled", "NSAutomaticQuoteSubstitutionEnabled",
              "NSAutomaticDashSubstitutionEnabled", "NSAutomaticTextReplacementEnabled",
              "NSAutomaticCapitalizationEnabled", "NSAutomaticPeriodSubstitutionEnabled"):
        v = subprocess.run(["defaults", "read", "-g", k], capture_output=True, text=True).stdout.strip()
        if v not in ("0", "false"):
            raise SystemExit(f"Mac-wide text substitution {k} is on: defaults write -g {k} -bool false")
    env["default_input"] = CoreAudio().default_input_name()
    if "blackhole" not in env["default_input"].lower():
        raise SystemExit(f"the default input is '{env['default_input']}', not BlackHole 2ch (./bench machine)")
    if not bundle_pids(app):
        raise SystemExit(f"{app.process} isn't running from the bench's copy ({bundle(app)}): "
                         f"./bench setup --app {app.name}")
    if others := other_apps(_paths(), str(bundle(app)) + "/Contents/", benchmarked_names(), str(apps_dir()) + "/"):
        raise SystemExit("other benchmarked apps are running (they would paste into, or listen during, this run): "
                         "quit them first:\n  " + "\n  ".join(others))
    exe = binary_path(app)
    env["binary_sha256"] = sha256_file(exe) if exe else ""
    return env


def binary_path(app: App) -> pathlib.Path | None:
    import plistlib
    b = bundle(app)
    try:
        info = plistlib.loads((b / "Contents" / "Info.plist").read_bytes())
    except OSError:
        return None
    exe = b / "Contents" / "MacOS" / info.get("CFBundleExecutable", "")
    return exe if exe.is_file() else None


def sha256_file(path: pathlib.Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["BlackHoleDriver", "CoreAudio", "MacKeys", "TextEditTarget", "preflight", "restart_app",
           "MODIFIERS", "KEYS"]


def _out(*cmd: str) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def hardware() -> dict:
    """The machine a run measured: chip, memory, macOS version and build, power source, load average."""
    mem = _out("sysctl", "-n", "hw.memsize")
    power = _out("pmset", "-g", "batt").splitlines()
    return {"chip": _out("sysctl", "-n", "machdep.cpu.brand_string"),
            "ram_gb": round(int(mem) / 2**30) if mem.isdigit() else None,
            "macos": _out("sw_vers", "-productVersion"), "macos_build": _out("sw_vers", "-buildVersion"),
            "power": power[0].split("'")[1] if power and "'" in power[0] else "",
            "load": _out("sysctl", "-n", "vm.loadavg").strip("{} ")}


def vm_running() -> bool:
    """A virtual machine on this Mac (Virtualization.framework, which Lume and others use)."""
    return bool(_out("pgrep", "-f", "com.apple.Virtualization.VirtualMachine"))


def save_input() -> dict:
    """The default input device, by UID (unique, unlike names) and name (for messages): `bench machine`
    changes it, and CoreAudio may share it with other accounts. The text corrections it turns off are this
    (the bench's) account's own preferences and stay off."""
    ca = CoreAudio()
    return {"input_uid": ca.default_input_uid(), "input": ca.default_input_name()}


def restore_input(state: dict) -> list[str]:
    """Puts the default input back by UID and reads it back; returns what still differs."""
    problems = []
    try:
        ca = CoreAudio()
        ca.set_default_input(ca.input_by_uid(state["input_uid"]))
        if ca.default_input_uid() != state["input_uid"]:
            problems.append(f"the default input is {ca.default_input_name()!r}, was {state['input']!r}")
    except (SystemExit, OSError, KeyError) as e:
        problems.append(f"the default input: {e}")
    for p in problems:
        print(f"host-run: NOT RESTORED: {p}", file=sys.stderr)
    return problems
