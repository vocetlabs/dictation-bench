"""Host-run safety (BENCH2): the owner's own apps, settings and models are never deleted or overwritten.

For each job, the app's state (its data folders and preference domains, listed in apps/<app>.<profile>.json
as `state`) is moved aside before setup and put back exactly afterwards: folders by rename (so models and
history come back byte for byte, without copying), preference domains by `defaults export` / `import`,
checked by reading them back. A stash records what was moved, so `bench restore-host` can finish the job
after a crash. Termination signals run the same restoration. The Keychain is never touched.

Preference reads are strict: a key or domain is "absent" only when `defaults` says it doesn't exist; any
other failure stops the run."""
from __future__ import annotations

import json
import os
import pathlib
import plistlib
import shutil
import signal
import subprocess
import time

ABSENT = "absent"


class RestoreError(RuntimeError):
    pass


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def read_pref(key: str, domain: str = "-g", run=_run) -> str | None:
    """A preference's value, or None when `defaults` says it doesn't exist. Any other failure raises."""
    r = run(["defaults", "read", domain, key])
    if r.returncode == 0:
        return r.stdout.strip()
    if "does not exist" in (r.stderr or ""):
        return None
    raise RestoreError(f"couldn't read {domain} {key}: {(r.stderr or '').strip()[:200]}")


def write_bool_pref(key: str, value: str | None, domain: str = "-g", run=_run) -> None:
    """Puts a boolean preference back: deleted when it was absent, else written as it was."""
    if value is None:
        r = run(["defaults", "delete", domain, key])
        if r.returncode and "does not exist" not in (r.stderr or ""):
            raise RestoreError(f"couldn't delete {domain} {key}")
        return
    r = run(["defaults", "write", domain, key, "-bool", "true" if value in ("1", "true", "YES") else "false"])
    if r.returncode:
        raise RestoreError(f"couldn't write {domain} {key}")


def export_domain(domain: str, run=_run) -> bytes | None:
    """A preference domain as a plist, or None when it doesn't exist."""
    r = run(["defaults", "read", domain])
    if r.returncode:
        if "does not exist" in (r.stderr or ""):
            return None
        raise RestoreError(f"couldn't read the {domain} preferences: {(r.stderr or '').strip()[:200]}")
    out = subprocess.run(["defaults", "export", domain, "-"], capture_output=True)
    if out.returncode or not out.stdout:
        raise RestoreError(f"couldn't export the {domain} preferences")
    return out.stdout


def same_plist(a: bytes | None, b: bytes | None) -> bool:
    if a is None or b is None:
        return a is b
    return plistlib.loads(a) == plistlib.loads(b)


class AppState:
    """One app's state on this Mac, moved aside for a bench job and put back exactly."""

    def __init__(self, name: str, paths: list[str], domains: list[str], stash: pathlib.Path,
                 export=export_domain, run=_run) -> None:
        self.name, self.stash, self.export, self.run = name, stash, export, run
        self.paths = [pathlib.Path(os.path.expanduser(p)) for p in paths]
        self.domains = list(domains)
        self.manifest = stash / "manifest.json"

    def save(self) -> None:
        """Moves each folder or file aside (rename, same volume) and exports and clears each domain."""
        if self.manifest.exists():
            raise RestoreError(f"{self.stash} holds an unfinished job: run `bench restore-host` first")
        self.stash.mkdir(parents=True, exist_ok=True)
        rec = {"app": self.name, "paths": [], "domains": []}
        for i, p in enumerate(self.paths):
            aside = p.with_name(f"{p.name}.bench-owner")
            if aside.exists():
                raise RestoreError(f"{aside} exists: an earlier job didn't finish (bench restore-host)")
            rec["paths"].append({"path": str(p), "aside": str(aside), "existed": p.exists()})
        for d in self.domains:
            data = self.export(d)
            rec["domains"].append({"domain": d, "existed": data is not None})
            if data is not None:
                (self.stash / f"{d}.plist").write_bytes(data)
        self.manifest.write_text(json.dumps(rec, indent=1))  # before anything moves: a crash can be undone
        for p in rec["paths"]:
            if p["existed"]:
                os.rename(p["path"], p["aside"])
        for d in rec["domains"]:
            if d["existed"]:
                self.run(["defaults", "delete", d["domain"]])

    def restore(self) -> None:
        """Removes what the bench left and puts the owner's state back; checks it. Idempotent."""
        if not self.manifest.exists():
            return
        rec = json.loads(self.manifest.read_text())
        for p in rec["paths"]:
            path, aside = pathlib.Path(p["path"]), pathlib.Path(p["aside"])
            if p["existed"] and not aside.exists() and path.exists():
                continue  # already back
            if path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            if p["existed"]:
                os.rename(aside, path)
        for d in rec["domains"]:
            self.run(["defaults", "delete", d["domain"]])
            if d["existed"]:
                r = self.run(["defaults", "import", d["domain"], str(self.stash / f"{d['domain']}.plist")])
                if r.returncode:
                    raise RestoreError(f"couldn't import the {d['domain']} preferences back")
        self.verify(rec)
        self.manifest.unlink()

    def verify(self, rec: dict) -> None:
        for p in rec["paths"]:
            back = pathlib.Path(p["path"]).exists()
            if back != p["existed"] or pathlib.Path(p["aside"]).exists():
                raise RestoreError(f"{p['path']} is not as it was")
        for d in rec["domains"]:
            want = (self.stash / f"{d['domain']}.plist").read_bytes() if d["existed"] else None
            if not same_plist(self.export(d["domain"]), want):
                raise RestoreError(f"the {d['domain']} preferences are not as they were")


def on_termination() -> None:
    """SIGTERM and SIGHUP end the run through the same `finally` as Ctrl-C, so the state is restored."""
    def handler(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(f"stopped by signal {signum}")
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, handler)


def wait_gone(process: str, timeout: float = 10.0) -> bool:
    for _ in range(int(timeout / 0.2)):
        if _run(["pgrep", "-x", process]).returncode:
            return True
        time.sleep(0.2)
    return False
