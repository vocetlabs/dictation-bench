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
        ref, size = ctypes.c_void_p(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
        a = self.addr("lnam")
        if self.ca.AudioObjectGetPropertyData(dev, ctypes.byref(a), 0, None, ctypes.byref(size), ctypes.byref(ref)):
            return ""
        buf = ctypes.create_string_buffer(512)
        self.cf.CFStringGetCString(ref, buf, 512, 0x08000100)
        self.cf.CFRelease(ref)
        return buf.value.decode()

    def inputs(self) -> dict[str, int]:
        a, size = self.addr("dev#"), ctypes.c_uint32(0)
        self.ca.AudioObjectGetPropertyDataSize(1, ctypes.byref(a), 0, None, ctypes.byref(size))
        ids = (ctypes.c_uint32 * (size.value // 4))()
        self.ca.AudioObjectGetPropertyData(1, ctypes.byref(a), 0, None, ctypes.byref(size), ids)
        out = {}
        for d in ids:
            s, sa = ctypes.c_uint32(0), self.addr("stm#", "inpt")
            self.ca.AudioObjectGetPropertyDataSize(d, ctypes.byref(sa), 0, None, ctypes.byref(s))
            if s.value:
                out[self.name(d)] = d
        return out

    def find_input(self, device: str) -> int:
        inputs = self.inputs()
        hit = [d for n, d in inputs.items() if device.lower() in n.lower()]
        if not hit:
            raise SystemExit(f"no input device '{device}' (inputs: {list(inputs)})")
        return hit[0]

    def default_input_name(self) -> str:
        return self.name(self.u32(1, "dIn "))

    def set_default_input(self, dev: int) -> None:
        val, a = ctypes.c_uint32(dev), self.addr("dIn ")
        if self.ca.AudioObjectSetPropertyData(1, ctypes.byref(a), 0, None, 4, ctypes.byref(val)):
            raise SystemExit("couldn't set the default input")

    def running_somewhere(self, dev: int) -> bool:
        return bool(self.u32(dev, "gone"))


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
        try:
            return self.ca.running_somewhere(self.bh_in)
        except OSError as e:
            raise InfraError("err_ready", str(e)) from e

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
        return process_tree_rss_mb(self.app.process)


def process_tree_rss_mb(name: str) -> float | None:
    """Resident memory of the processes named `name` and all their descendants (helpers, model servers)."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,comm="], capture_output=True, text=True).stdout
    procs = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            procs.append((int(parts[0]), int(parts[1]), int(parts[2]), os.path.basename(parts[3])))
    tree = {p for p, _, _, comm in procs if comm == name}
    if not tree:
        return None
    grew = True
    while grew:
        more = {p for p, pp, _, _ in procs if pp in tree} - tree
        grew, tree = bool(more), tree | more
    return sum(rss for p, _, rss, _ in procs if p in tree) / 1024


def restart_app(app: App, settle: float = 10.0) -> None:
    """Quit (TERM, then KILL) and reopen the app in the background, then let it load its model."""
    subprocess.run(["pkill", "-TERM", "-x", app.process], check=False)
    for _ in range(50):
        if subprocess.run(["pgrep", "-x", app.process], capture_output=True).returncode:
            break
        time.sleep(0.2)
    else:
        subprocess.run(["pkill", "-KILL", "-x", app.process], check=False)
    subprocess.run(["open", "-g", os.path.expanduser(app.app_path)], check=False)
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
        subprocess.run(["pkill", "-x", "TextEdit"], check=False)
        time.sleep(1)
        subprocess.run(["open", "-F", "-a", "TextEdit", str(scratch)], check=False)

    def _get(self, el, attr):
        err, val = self.AS.AXUIElementCopyAttributeValue(el, attr, None)
        return (err, val)

    def _app(self):
        apps = self.AK.NSRunningApplication.runningApplicationsWithBundleIdentifier_(TEXTEDIT)
        if not apps:
            raise InfraError("err_target", "TextEdit isn't running")
        return self.AS.AXUIElementCreateApplication(apps[0].processIdentifier())

    def _find(self) -> None:
        self.app = self._app()
        _, wins = self._get(self.app, "AXWindows")
        self.window = next((w for w in wins or [] if str(self._get(w, "AXTitle")[1] or "").startswith(self.scratch.name)),
                           None)
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
    if subprocess.run(["pgrep", "-x", app.process], capture_output=True).returncode:
        raise SystemExit(f"{app.process} isn't running: ./bench setup --app {app.name}, or open it")
    exe = binary_path(app)
    env["binary_sha256"] = sha256_file(exe) if exe else ""
    return env


def binary_path(app: App) -> pathlib.Path | None:
    import plistlib
    bundle = pathlib.Path(os.path.expanduser(app.app_path))
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    except OSError:
        return None
    exe = bundle / "Contents" / "MacOS" / info.get("CFBundleExecutable", "")
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


TEXT_CORRECTIONS = ("NSAutomaticCapitalizationEnabled", "NSAutomaticPeriodSubstitutionEnabled",
                    "NSAutomaticSpellingCorrectionEnabled", "NSAutomaticQuoteSubstitutionEnabled",
                    "NSAutomaticDashSubstitutionEnabled", "NSAutomaticTextReplacementEnabled",
                    "NSAutomaticInlinePredictionEnabled", "NSAutomaticTextCompletionEnabled")


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


def save_machine_state() -> dict:
    """What `bench machine` changes: the default input and the Mac-wide text corrections."""
    return {"input": CoreAudio().default_input_name(),
            "defaults": {k: _out("defaults", "read", "-g", k) or None for k in TEXT_CORRECTIONS}}


def restore_machine_state(state: dict) -> None:
    for k, v in state["defaults"].items():
        if v is None:
            subprocess.run(["defaults", "delete", "-g", k], capture_output=True)
        else:
            subprocess.run(["defaults", "write", "-g", k, "-bool", "true" if v in ("1", "true") else "false"])
    try:
        ca = CoreAudio()
        ca.set_default_input(ca.find_input(state["input"]))
    except Exception as e:  # noqa: BLE001: tell the person, don't hide the run's own result
        print(f"host-run: set the input back to '{state['input']}' in System Settings > Sound ({e})", file=sys.stderr)
