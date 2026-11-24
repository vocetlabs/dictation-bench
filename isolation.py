"""Host-run safety (BENCH2). Host runs happen only in the bench's own Standard macOS account, `vocetbench`
(fixed, no override; no admin rights, no Apple ID), so the owner's Keychain, settings and app data sit in
another account and the bench never touches them. /Applications is shared with the owner: the bench installs
only into its own work/Applications and quits only processes launched from there (macos.quit_tree).

Inside the bench account each job starts clean: the app's declared state (its data folders and preference
domains, `state` in apps/<app>.<profile>.json, all inside this account's home) is deleted before setup writes
the profile's. The default input device, which CoreAudio may share between accounts, is saved by its UID and
put back (macos.save_input / restore_input); a lock keeps a host run and `restore-host` apart, and termination
signals are held off while the input is saved or restored, so neither is cut in half.

Preference reads are strict: a key or domain is "absent" only when `defaults` says it doesn't exist; any
other failure stops the run."""
from __future__ import annotations

import contextlib
import fcntl
import os
import pathlib
import shutil
import signal
import subprocess

BENCH_USER = "vocetbench"  # fixed: an override could name the owner's account
TERMINATING = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


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


def check_bench_account(name: str | None = None, groups: set[str] | None = None) -> None:
    """Host runs start only as the bench's own account, `vocetbench` (no override), and only if it isn't an
    administrator: the owner's data stays in their own account, out of the bench's reach."""
    import grp
    import pwd
    pw = pwd.getpwuid(os.getuid())
    name = name if name is not None else pw.pw_name
    if name != BENCH_USER:
        raise SystemExit(f"host runs happen only in the bench's own macOS account '{BENCH_USER}'; this is "
                         f"'{name}'. Log in as {BENCH_USER}: a Standard user with no Apple ID (CONFIGS.md)")
    if groups is None:
        groups = set()
        for gid in os.getgrouplist(name, pw.pw_gid):
            with contextlib.suppress(KeyError):
                groups.add(grp.getgrgid(gid).gr_name)
    if "admin" in groups:
        raise SystemExit(f"'{name}' is an administrator: host runs need a Standard account (System Settings > "
                         "Users & Groups), so the bench can't change what other accounts own")


def check_bench_dirs(bench: pathlib.Path, home: str | None = None) -> None:
    """The bench's folder, work/ and work/Applications are real folders (no symlink that could point the
    installer's delete and copy at /Applications) inside this account's home."""
    root = pathlib.Path(home or os.path.expanduser("~")).resolve()
    for p in (bench, bench / "work", bench / "work" / "Applications"):
        if p.is_symlink():
            raise SystemExit(f"{p} is a symlink: the bench installs only into its own real folder")
    real = (bench / "work" / "Applications").resolve()
    if root not in real.parents:
        raise SystemExit(f"the bench's app folder {real} isn't inside this account's home ({root})")


def write_new(path: pathlib.Path, text: str) -> None:
    """Creates `path` afresh (mode 0600), never through a symlink: a stale regular file is removed first, and
    the create is exclusive and refuses to follow links (O_EXCL | O_NOFOLLOW)."""
    if path.is_symlink():
        raise SystemExit(f"{path} is a symlink: refusing to write through it")
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def clear_state(paths: list[str], domains: list[str], home: str | None = None, run=_run) -> None:
    """Deletes an app's data folders and preference domains in this (the bench's) account, so setup starts
    from nothing. Refuses any path outside this account's home."""
    root = pathlib.Path(home or os.path.expanduser("~")).resolve()
    targets = []
    for p in paths:
        path = pathlib.Path(os.path.expanduser(p))
        where = path.parent.resolve() / path.name  # a symlink itself is removed, never followed
        if root not in where.parents:
            raise SystemExit(f"{p} isn't inside this account's home ({root}): not deleted")
        targets.append(path)
    for path in targets:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    for d in domains:
        r = run(["defaults", "delete", d])
        if r.returncode and not domain_missing(r.stderr or ""):
            raise SystemExit(f"couldn't clear the {d} preferences: {(r.stderr or '').strip()[:200]}")


def domain_missing(stderr: str) -> bool:
    """`defaults delete` failed only because the domain isn't there (already clear). macOS words it
    "Domain (x) not found." or "... does not exist" depending on the release."""
    return "does not exist" in stderr or ("Domain" in stderr and "not found" in stderr)


@contextlib.contextmanager
def host_lock(path: pathlib.Path):
    """One host run or restore-host at a time (an exclusive lock on `path`, released when the process ends)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        raise SystemExit(f"another host-run or restore-host is running ({path})") from None
    try:
        yield
    finally:
        fh.close()


def on_termination() -> None:
    """SIGTERM and SIGHUP end the run through the same `finally` as Ctrl-C, so the input is restored."""
    def handler(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)
        raise SystemExit(f"stopped by signal {signum}")
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, handler)


@contextlib.contextmanager
def signals_held():
    """Ctrl-C, SIGTERM and SIGHUP wait until the block ends, then stop the run as usual: a save or restore
    inside is never cut in half."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATING)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)
