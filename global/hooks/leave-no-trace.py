#!/usr/bin/env python3
"""Claude Code Leave No Trace lifecycle hook and helper command."""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

POLL_SECONDS = 3
TERM_GRACE_SECONDS = 2.0
MAX_WATCH_SECONDS = 24 * 60 * 60
LNT_MARKER = "claude-lnt"

# Ephemeral profile directories created by browser automation. These are the
# exact prefixes the drivers use, not a wildcard over the tool name: a glob
# such as `playwright_*` would also match a user's own `playwright_test-results`
# and this sweep deletes permanently.
BROWSER_PROFILE_PATTERNS = (
    "playwright_chromiumdev_profile-*",
    "playwright_firefoxdev_profile-*",
    "playwright_webkitdev_profile-*",
    # Puppeteer builds this as puppeteer_dev_<browser>_profile-, so both
    # browsers it supports need naming.
    "puppeteer_dev_chrome_profile-*",
    "puppeteer_dev_firefox_profile-*",
)

# How old an unreferenced profile must be before it counts as abandoned.
ORPHAN_PROFILE_GRACE_SECONDS = 300


def private_file(path: Path) -> None:
    """Make a file private, tolerating one that is absent or not ours."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def private_dir(path: Path) -> Path:
    """Create a directory, and any missing parent, private to this user.

    mkdir's mode argument is masked by the umask, so a restrictive umask
    would otherwise produce a directory nothing can be written into. Each
    missing level is created and secured in turn, because mkdir(parents=True)
    applies the mode only to the final component. Directories that already
    exist above the missing ones are left alone.
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent

    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)

    if not path.is_dir():
        # Something that is not a directory already occupies the path.
        # Raising an OSError lets runtime_root fall through to its next
        # candidate rather than returning an unusable root.
        raise NotADirectoryError(
            errno.ENOTDIR,
            os.strerror(errno.ENOTDIR),
            str(path),
        )

    if not missing:
        os.chmod(path, 0o700)

    return path


def runtime_root() -> Path:
    candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(Path(xdg) / "claude-lnt")
    candidates.append(Path(f"/run/user/{os.getuid()}") / "claude-lnt")
    candidates.append(Path.home() / ".cache" / "claude-lnt" / "runtime")
    for path in candidates:
        try:
            return private_dir(path)
        except OSError:
            continue
    raise RuntimeError("Cannot create a writable Leave No Trace runtime directory")


def log_root() -> Path:
    return private_dir(Path.home() / ".cache" / "claude-lnt" / "logs")


def safe_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def state_dir(session_id: str) -> Path:
    return runtime_root() / safe_key(session_id)


def unit_name(session_id: str) -> str:
    return f"claude-lnt-watch-{safe_key(session_id)[:16]}"


def read_json_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def current_session(data: dict[str, Any] | None = None) -> str:
    if data:
        value = data.get("session_id")
        if isinstance(value, str) and value:
            return value
    value = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not value:
        raise RuntimeError("CLAUDE_CODE_SESSION_ID is unavailable")
    return value


# The procfs root, patchable in tests. On systems without /proc, such as
# macOS, everything derived from it degrades conservatively: liveness falls
# back to signal 0 probes and ownership scanning finds nothing, so cleanup
# never kills what it cannot attribute.
PROC_ROOT = Path("/proc")


def proc_available() -> bool:
    return PROC_ROOT.is_dir()


def proc_stat(pid: int) -> tuple[int, int] | None:
    try:
        text = (PROC_ROOT / str(pid) / "stat").read_text()
        rest = text[text.rfind(")") + 2 :].split()
        return int(rest[1]), int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def proc_start(pid: int) -> int | None:
    value = proc_stat(pid)
    return value[1] if value else None


def proc_ppid(pid: int) -> int | None:
    value = proc_stat(pid)
    return value[0] if value else None


def proc_cmdline(pid: int) -> str:
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def proc_env(pid: int) -> dict[str, str]:
    try:
        raw = (PROC_ROOT / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return result


def ancestors(pid: int) -> set[int]:
    result: set[int] = set()
    current = pid
    while current > 1 and current not in result:
        result.add(current)
        parent = proc_ppid(current)
        if parent is None or parent == current:
            break
        current = parent
    return result


def process_matches(pid: int, session_id: str) -> tuple[bool, dict[str, str]]:
    env = proc_env(pid)
    matched = (
        env.get("CLAUDE_CODE_SESSION_ID") == session_id
        and env.get("CLAUDE_CODE_CHILD_SESSION") == "1"
        and env.get("CLAUDE_LNT_INTERNAL") != "1"
    )
    return matched, env


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def append_log(session_id: str, message: str) -> None:
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = log_root() / f"{safe_key(session_id)}.log"

    # Repaired before the open, not after: a log left unreadable by a
    # restrictive umask cannot be opened at all, so a corrective chmod
    # afterwards would never be reached.
    private_file(path)
    with path.open("a") as handle:
        private_file(path)
        handle.write(f"{timestamp} {message}\n")


def metadata(session_id: str) -> dict[str, Any]:
    return load_json(state_dir(session_id) / "meta.json", {})


def live_same_process(pid: int, expected_start: int | None) -> bool:
    if pid <= 1:
        return False
    if not proc_available():
        # Without procfs there is no start time to compare, so liveness is
        # the best that can be known. Erring towards "alive" protects live
        # sessions from being reclaimed as stale.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    if not (PROC_ROOT / str(pid)).exists():
        return False
    if expected_start is None:
        return True
    return proc_start(pid) == expected_start


def live_verified_process(pid: object, start: object) -> bool:
    """True only when the identifier and the recorded start time both match.

    Unlike `live_same_process`, a missing start time is not accepted, and
    neither is a missing procfs: without it the start time cannot be
    checked at all, and a signal must never be authorised by an identifier
    alone, which a reused one would satisfy.
    """
    return (
        isinstance(pid, int)
        and pid > 1
        and isinstance(start, int)
        and proc_available()
        and live_same_process(pid, start)
    )


def lease_records(session_id: str) -> dict[str, Any]:
    value = load_json(state_dir(session_id) / "leases.json", {})
    return value if isinstance(value, dict) else {}


def session_lock_path(session_id: str) -> Path:
    # Deliberately outside the session directory: a lock inside it would be
    # destroyed by the very teardown it is meant to serialise, and would not
    # exist yet when a first launch races a cleanup.
    return runtime_root() / f".{safe_key(session_id)}.lock"


@contextlib.contextmanager
def session_lock(session_id: str) -> Any:
    """Serialise lease creation against teardown.

    Without it a lease can be recorded in the window between revoking the
    old leases and removing the directory the new one would protect, which
    leaves the new holder running with no state. Both sides take this lock,
    so teardown completes before a lease is granted, or the other way round.
    """
    with path_lock(session_lock_path(session_id)):
        yield


@contextlib.contextmanager
def path_lock(path: Path) -> Any:
    """Hold the lock file at `path`, by path rather than by session.

    Reclaiming a state directory whose metadata is gone cannot name its
    session, but the lock file is derived from the same key as the directory,
    so it can still be taken by path and serialise against a launch.
    """
    while True:
        handle = path.open("a+b")
        try:
            private_file(path)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

            # The pathname can be unlinked and recreated while this call
            # waits, and a lock held on an unlinked inode excludes nobody.
            # Confirm the descriptor is still the file the path names, or
            # start again on the file that replaced it.
            try:
                current = path.stat()
            except FileNotFoundError:
                handle.close()
                continue

            held = os.fstat(handle.fileno())
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                handle.close()
                continue
        except BaseException:
            handle.close()
            raise

        break

    try:
        yield
    finally:
        handle.close()


def scan_session_processes(
    session_id: str,
) -> list[tuple[int, str, dict[str, str]]]:
    """One pass over /proc: this session's processes and their environments.

    Ownership and lease holding have to be decided from the same
    observation. Reading /proc twice lets a process part way through `exec`,
    whose environment is briefly unreadable, look unleased to the first read
    and owned by the second, which is enough to kill work holding a valid
    lease. Read once, and such a process is simply invisible to both.
    """
    found: list[tuple[int, str, dict[str, str]]] = []

    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        # No procfs, no attribution: report nothing rather than guess.
        return found

    for entry in entries:
        if not entry.name.isdigit():
            continue

        pid = int(entry.name)
        env = proc_env(pid)
        if env.get("CLAUDE_CODE_SESSION_ID") != session_id:
            continue

        found.append((pid, proc_cmdline(pid), env))

    return found


def held_lease_ids(
    snapshot: list[tuple[int, str, dict[str, str]]],
) -> set[str]:
    """Lease identifiers carried by a live process in this snapshot."""
    return {
        env["CLAUDE_LNT_LEASE_ID"]
        for _pid, _command, env in snapshot
        if env.get("CLAUDE_LNT_LEASE_ID")
    }


def active_lease_ids(
    session_id: str,
    snapshot: list[tuple[int, str, dict[str, str]]] | None = None,
) -> set[str]:
    """Leases whose process is still running and whose term has not expired.

    A record is not removed when its process exits, so expiry alone would
    keep a finished lease protecting the session temporary directory for the
    rest of its term. The recorded start time distinguishes the process from
    a later one that reused its identifier.
    """
    now = time.time()
    active: set[str] = set()
    held: set[str] | None = None

    for lease_id, item in lease_records(session_id).items():
        if not isinstance(item, dict):
            continue
        try:
            if float(item.get("expires", 0)) <= now:
                continue
        except (TypeError, ValueError):
            continue

        pid = item.get("pid")
        start = item.get("start")
        if isinstance(pid, int) and live_same_process(
            pid, start if isinstance(start, int) else None
        ):
            active.add(lease_id)
            continue

        # The launcher may have daemonised, so the work outlives the process
        # that was recorded. Its descendants inherit the lease identifier,
        # and the lease belongs to the work rather than to one process.
        if held is None:
            if snapshot is None:
                snapshot = scan_session_processes(session_id)
            held = held_lease_ids(snapshot)
        if lease_id in held:
            active.add(lease_id)

    return active


def process_is_leased(env: dict[str, str], active_ids: set[str]) -> bool:
    lease_id = env.get("CLAUDE_LNT_LEASE_ID")
    return bool(lease_id and lease_id in active_ids)


def find_owned_processes(
    session_id: str,
    *,
    detached_only: bool,
    ignore_leases: bool,
    active_ids: set[str] | None = None,
    snapshot: list[tuple[int, str, dict[str, str]]] | None = None,
) -> list[tuple[int, str, dict[str, str]]]:
    meta = metadata(session_id)
    claude_pid = int(meta.get("claude_pid", 0) or 0)
    protected = ancestors(os.getpid())
    protected.add(claude_pid)
    if snapshot is None:
        snapshot = scan_session_processes(session_id)
    if active_ids is None:
        active_ids = (
            set() if ignore_leases else active_lease_ids(session_id, snapshot)
        )

    found: list[tuple[int, str, dict[str, str]]] = []

    for pid, command, env in snapshot:
        if pid in protected:
            continue
        if env.get("CLAUDE_CODE_CHILD_SESSION") != "1":
            continue
        if env.get("CLAUDE_LNT_INTERNAL") == "1":
            continue
        if process_is_leased(env, active_ids):
            continue
        if detached_only and claude_pid in ancestors(pid):
            continue
        if not command or LNT_MARKER in command:
            continue
        found.append((pid, command, env))
    return found


def profile_dirs_from_commands(commands: list[str]) -> set[Path]:
    result: set[Path] = set()
    for command in commands:
        try:
            parts = shlex.split(command)
        except ValueError:
            parts = command.split()
        index = 0
        while index < len(parts):
            token = parts[index]
            value = ""
            if token.startswith("--user-data-dir="):
                value = token.split("=", 1)[1]
            elif token == "--user-data-dir" and index + 1 < len(parts):
                index += 1
                value = parts[index]
            if value:
                path = Path(os.path.expandvars(os.path.expanduser(value)))
                if path.is_absolute():
                    result.add(path)
            index += 1
    return result


def live_command_lines() -> list[str]:
    lines: list[str] = []
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return lines
    for entry in entries:
        if not entry.name.isdigit():
            continue
        command = proc_cmdline(int(entry.name))
        if command:
            lines.append(command)
    return lines


def sweep_orphan_browser_profiles(
    session_id: str,
    roots: set[Path] | None = None,
) -> None:
    """Remove automation browser profiles that no live process is using.

    Playwright and Puppeteer put the profile under the system temporary
    directory and remove it on a clean close. A browser that is killed
    rather than closed, which is what an interrupted session or a torn down
    control group produces, leaves the directory behind for good: nothing
    ages `/tmp` entries out on this system, and a snap browser's profiles
    are not even visible there.

    Redirecting TMPDIR into the session state directory covers the browsers
    that inherit it. This is the backstop for the ones that do not, so a
    session cannot quietly accumulate profiles run after run.
    """
    if roots is None:
        # This is the one part of cleanup that reaches outside the session's
        # own state, so it has to be switchable off. A test that drives the
        # hook in a subprocess cannot patch it, and neither can anyone who
        # would rather manage the system temporary directory themselves.
        if os.environ.get("CLAUDE_LNT_SKIP_PROFILE_SWEEP") == "1":
            return

        # The redirected TMPDIR and the real system temporary directory,
        # because a browser that did not inherit the redirection leaks into
        # the latter.
        roots = {Path(tempfile.gettempdir()), Path("/tmp")}

    in_use = "\n".join(live_command_lines())
    cutoff = time.time() - ORPHAN_PROFILE_GRACE_SECONDS

    for root in roots:
        for pattern in BROWSER_PROFILE_PATTERNS:
            for path in sorted(root.glob(pattern)):
                try:
                    info = path.lstat()
                except OSError:
                    continue

                if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                    continue

                # A profile a live browser is still using names itself on
                # that browser's command line.
                if str(path) in in_use:
                    continue

                # Never race a browser between creating its profile and
                # becoming visible in /proc with its arguments. A running
                # browser keeps writing into its profile, so an old
                # modification time means nothing is using it.
                if info.st_mtime > cutoff:
                    continue

                try:
                    shutil.rmtree(path)
                    append_log(session_id, f"removed orphan profile {path}")
                except OSError as exc:
                    append_log(session_id, f"orphan profile {path}: {exc}")


def scope_for_pid(pid: int) -> str:
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return ""
    match = re.search(r"snap\.chromium\.chromium-[^/\n]+\.scope", text)
    return match.group(0) if match else ""


def stop_snap_scopes(processes: list[tuple[int, str, dict[str, str]]], session_id: str) -> None:
    if not shutil.which("systemctl"):
        return
    scopes: set[str] = set()
    for pid, command, _ in processes:
        if "--headless=new" not in command or "--type=" in command:
            continue
        if "chrome_crashpad_handler" in command:
            continue
        scope = scope_for_pid(pid)
        if scope:
            scopes.add(scope)
    for scope in sorted(scopes):
        result = subprocess.run(
            ["systemctl", "--user", "stop", scope],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        append_log(session_id, f"stop scope {scope}: rc={result.returncode} {result.stderr.strip()}")


def terminate_processes(processes: list[tuple[int, str, dict[str, str]]], session_id: str) -> None:
    pids = {pid for pid, _, _ in processes}
    ordered = sorted(pids, key=lambda pid: len(ancestors(pid)), reverse=True)
    for pid in ordered:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError) as exc:
            append_log(session_id, f"SIGTERM {pid}: {exc}")
    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline and any(
        live_same_process(pid, None) for pid in pids
    ):
        time.sleep(0.1)
    for pid in ordered:
        if not live_same_process(pid, None):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError) as exc:
            append_log(session_id, f"SIGKILL {pid}: {exc}")


def safe_remove_profile(path: Path, session_id: str) -> bool:
    """Remove a browser profile the session created, sparing everything else.

    Only two classes of directory are provably session-owned: one under the
    session's redirected temporary directory, and one in a system temporary
    directory whose name matches the ephemeral prefixes the automation
    drivers generate. Anything else may be a persistent profile that existed
    before the session: a browser that merely opened it updates its
    timestamps, so recency proves nothing about creation, and Linux exposes
    no creation time. A pre-existing profile holds logins, bookmarks and
    extensions and its deletion is irreversible, so it is preserved rather
    than guessed about. Preserving it is a success, not a failure.
    """
    try:
        resolved = path.resolve()
        info = resolved.stat()
    except OSError:
        return True
    home = Path.home().resolve()
    tmp_root = (state_dir(session_id) / "tmp").resolve()
    if resolved in {Path("/"), home} or info.st_uid != os.getuid():
        append_log(session_id, f"preserved profile {resolved}")
        return True
    # Strictly under the session temporary directory. The directory itself
    # is what TMPDIR names for the whole session, so removing it would break
    # every later mktemp in a session that is still running.
    in_tmp = tmp_root in resolved.parents
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/tmp")}
    ephemeral = resolved.parent in temp_roots and any(
        fnmatch.fnmatch(resolved.name, pattern)
        for pattern in BROWSER_PROFILE_PATTERNS
    )
    if not in_tmp and not ephemeral:
        append_log(session_id, f"preserved pre-existing profile {resolved}")
        return True
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        append_log(session_id, f"removed profile {resolved}")
        return True
    except OSError as exc:
        append_log(session_id, f"remove profile {resolved}: {exc}")
        return False


def run_registered_cleanup(session_id: str) -> list[str]:
    directory = state_dir(session_id) / "cleanup.d"
    failures: list[str] = []
    if not directory.exists():
        return failures
    for path in sorted(directory.glob("*.json"), reverse=True):
        item = load_json(path, {})
        cwd = str(metadata(session_id).get("cwd", Path.home()))
        env = os.environ.copy()
        env.pop("CLAUDE_CODE_CHILD_SESSION", None)
        env["CLAUDE_LNT_INTERNAL"] = "1"
        if isinstance(item.get("argv"), list):
            argv = [str(value) for value in item["argv"]]
        elif isinstance(item.get("shell"), str):
            argv = ["bash", "-lc", item["shell"]]
        else:
            failures.append(f"invalid cleanup entry {path.name}")
            continue
        try:
            # Its own session, so a command that times out can be killed as
            # a whole group. Killing only the direct child would leave any
            # process it forked running with CLAUDE_LNT_INTERNAL=1, which is
            # permanently invisible to every later sweep.
            process = subprocess.Popen(
                argv,
                cwd=cwd if Path(cwd).is_dir() else None,
                env=env,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                output, _ = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    process.communicate(timeout=5)
                raise
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{path.name}: {exc}")
            continue
        append_log(session_id, f"cleanup {path.name}: rc={process.returncode} {output.strip()}")
        if process.returncode == 0:
            path.unlink(missing_ok=True)
        else:
            failures.append(f"{path.name}: exit {process.returncode}")
    return failures


def cleanup(
    session_id: str,
    *,
    detached_only: bool,
    ignore_leases: bool,
    run_registry: bool,
    remove_state: bool = False,
) -> list[str]:
    """Reclaim what a session owns, serialised against lease creation.

    The lock covers the whole of cleanup, not just teardown. Scanning and
    terminating outside it would let a process launched under the lock be
    killed before its lease was recorded, and a launch completing between
    the last scan and the lock would have its brand new lease revoked.
    """
    with session_lock(session_id):
        failures = cleanup_locked(
            session_id,
            detached_only=detached_only,
            ignore_leases=ignore_leases,
            run_registry=run_registry,
        )

        # Removing the session directory belongs inside the lock too.
        # Outside it, a launch could record a lease and start a process
        # between cleanup finishing and the directory being deleted.
        if remove_state and not failures:
            shutil.rmtree(state_dir(session_id), ignore_errors=True)
            # The lock is reclaimed with the session it belonged to. Held
            # here, and the session is over, so nothing legitimate is
            # waiting on it.
            session_lock_path(session_id).unlink(missing_ok=True)

        return failures


def cleanup_locked(
    session_id: str,
    *,
    detached_only: bool,
    ignore_leases: bool,
    run_registry: bool,
) -> list[str]:
    # Lease protection only ever grows within one cleanup run. Each pass
    # reads /proc once and derives ownership and lease holding from that same
    # observation, then adds what it found to the set. A lease that expires
    # part way through therefore keeps protecting the process it spared and
    # the directory that process depends on, and a holder that only becomes
    # visible on a later pass is still honoured. The next cleanup starts
    # afresh and terminates whatever has genuinely finished.
    active_ids: set[str] = set()
    granting = True

    def owned(detached: bool) -> list[tuple[int, str, dict[str, str]]]:
        snapshot = scan_session_processes(session_id)
        if not ignore_leases and granting:
            active_ids.update(active_lease_ids(session_id, snapshot))
        return find_owned_processes(
            session_id,
            detached_only=detached,
            ignore_leases=ignore_leases,
            active_ids=active_ids,
            snapshot=snapshot,
        )

    processes = owned(detached_only)
    commands = [command for _, command, _ in processes]
    profile_dirs = profile_dirs_from_commands(commands)
    targeted: set[int] = set()
    if processes:
        append_log(session_id, f"cleanup found {len(processes)} owned process(es)")
        stop_snap_scopes(processes, session_id)
        processes = owned(detached_only)
        targeted = {pid for pid, _, _ in processes}
        terminate_processes(processes, session_id)

    # One more reading before anything is torn down, purely to complete the
    # lease picture. A holder that only becomes visible now must protect its
    # own state, so the decision cannot be frozen before this point.
    owned(detached_only)
    leased_work_in_progress = bool(active_ids)

    # Lease protection stops being granted here. Once the state a lease
    # protects has been torn down, sparing a holder that only becomes
    # visible afterwards would leave it running without the directory it
    # depends on. It is reported as residual instead, which is the honest
    # outcome, and the next cleanup terminates it.
    granting = False

    # While leased work is in progress the session defers teardown as a
    # whole. Running a registered rollback now could remove exactly what the
    # surviving process depends on, which is the same mistake as clearing its
    # temporary directory. Both run once the lease ends or the session does.
    failures = (
        run_registered_cleanup(session_id)
        if run_registry and not leased_work_in_progress
        else []
    )
    for path in profile_dirs:
        if not safe_remove_profile(path, session_id):
            failures.append(f"profile not removed safely: {path}")

    if not detached_only:
        sweep_orphan_browser_profiles(session_id)

    # A leased process is still using the session temporary directory, so
    # clearing it would leave that process alive without its sockets,
    # profiles and working data. Surviving the turn has to mean surviving
    # intact. The end of the session overrides leases and clears it.
    tore_down = False
    if (
        not detached_only
        and not leased_work_in_progress
        and state_dir(session_id).is_dir()
    ):
        tore_down = True
        # Teardown and lease revocation happen together, under the lock that
        # claude-lnt-start also takes. A lease whose holder was not
        # observable when its directory was removed is void: nothing is left
        # for it to protect, so honouring it on a later cleanup would spare a
        # process whose state is already gone, permanently.
        tmp_root = state_dir(session_id) / "tmp"
        save_json(state_dir(session_id) / "leases.json", {})

        try:
            shutil.rmtree(tmp_root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"temporary directory {tmp_root}: {exc}")

        # TMPDIR was exported to this value for the whole session, so
        # the directory has to outlive its contents. Removing it
        # outright makes every later mktemp and tempfile call in the
        # session fail. The state directory as a whole goes at
        # SessionEnd.
        try:
            private_dir(tmp_root)
        except OSError as exc:
            failures.append(f"temporary directory {tmp_root}: {exc}")

    # Residual means a process this cleanup tried to terminate and could
    # not. An unleased process that only appeared while cleanup was
    # running, such as a monitor's next short-lived poll child, belongs to
    # the next sweep; reporting it would block completion on something
    # nobody failed to clean. A late-visible lease holder after teardown is
    # different: its lease was just revoked and its state removed, so it is
    # stranded and has to be reported.
    residual = [
        entry
        for entry in owned(detached_only)
        if entry[0] in targeted
        or (tore_down and entry[2].get("CLAUDE_LNT_LEASE_ID"))
    ]
    failures.extend(f"process {pid}: {command}" for pid, command, _ in residual)
    return failures


def stop_watchdog(session_id: str) -> None:
    state = state_dir(session_id)
    if shutil.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "stop", unit_name(session_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    record = load_json(state / "watcher.json", {})
    watcher_pid = record.get("pid")
    # The start time distinguishes the recorded watcher from an unrelated
    # process that reused its identifier. A record without one, including
    # any written by an earlier version, is not acted on: the systemd unit
    # stopped above is the watchdog's primary handle either way.
    if watcher_pid != os.getpid() and live_verified_process(
        watcher_pid, record.get("start")
    ):
        try:
            os.kill(watcher_pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass


def start_watchdog(session_id: str, claude_pid: int, claude_start: int | None) -> None:
    if os.environ.get("CLAUDE_LNT_DISABLE_WATCHDOG") == "1" or claude_pid <= 1:
        return
    if shutil.which("systemctl"):
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit_name(session_id)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if active.returncode == 0:
            return
    script = str(Path(__file__).resolve())
    args = [sys.executable, script, "watch", session_id, str(claude_pid), str(claude_start or 0)]
    env = os.environ.copy()
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID"):
        env.pop(key, None)
    env["CLAUDE_LNT_INTERNAL"] = "1"
    if shutil.which("systemd-run") and shutil.which("systemctl"):
        # A transient unit inherits nothing from this process, so anything
        # the watchdog must honour has to be passed through explicitly.
        assignments = ["CLAUDE_LNT_INTERNAL=1"]
        if os.environ.get("CLAUDE_LNT_SKIP_PROFILE_SWEEP") == "1":
            assignments.append("CLAUDE_LNT_SKIP_PROFILE_SWEEP=1")

        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                f"--unit={unit_name(session_id)}",
                "--property=Type=exec",
                "--property=TimeoutStopSec=10s",
                "/usr/bin/env",
                "-u",
                "CLAUDE_CODE_SESSION_ID",
                "-u",
                "CLAUDE_CODE_CHILD_SESSION",
                "-u",
                "CLAUDE_PID",
                *assignments,
                *args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return
    process = subprocess.Popen(
        args,
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    save_json(
        state_dir(session_id) / "watcher.json",
        {"pid": process.pid, "start": proc_start(process.pid)},
    )


def sweep_orphan_locks() -> None:
    """Remove lock files whose session state is gone and which are unheld.

    A crashed session cannot reclaim its own lock. Acquiring it without
    blocking is what proves nobody is using it.
    """
    for path in runtime_root().glob(".*.lock"):
        key = path.name[1:-len(".lock")]
        if (runtime_root() / key).is_dir():
            continue

        try:
            with path.open("a+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    continue
                path.unlink(missing_ok=True)
        except OSError:
            continue


def reclaim_unattributed_state(path: Path) -> None:
    """Reclaim a state directory that has no readable session metadata.

    claude-lnt-start can recreate a state directory after its session has
    ended, and nothing writes metadata into it again, so without this the
    directory and any process recorded in its leases would leak for good.
    Lease terms are capped at a day, so once the directory has been
    untouched for that long nothing in it can still be legitimately
    protected. Processes are only killed when their recorded start time
    still matches, so a reused identifier is never a casualty.
    """
    try:
        if time.time() - path.stat().st_mtime < MAX_WATCH_SECONDS:
            return
    except OSError:
        return

    # The same lock a launch takes, named from the directory rather than
    # from a session identifier this state no longer records. Without it a
    # launch landing after the age check would have its brand new lease and
    # its process destroyed.
    with path_lock(path.parent / f".{path.name}.lock"):
        try:
            # Rechecked under the lock: a launch may have refreshed the
            # directory between the first check and the acquisition.
            if time.time() - path.stat().st_mtime < MAX_WATCH_SECONDS:
                return
        except OSError:
            return

        leases = load_json(path / "leases.json", {})
        if isinstance(leases, dict):
            for item in leases.values():
                if isinstance(item, dict) and live_verified_process(
                    item.get("pid"), item.get("start")
                ):
                    with contextlib.suppress(OSError):
                        os.kill(item["pid"], signal.SIGKILL)
        shutil.rmtree(path, ignore_errors=True)


def sweep_stale_states() -> None:
    sweep_orphan_locks()

    for path in runtime_root().iterdir():
        if not path.is_dir():
            continue
        meta = load_json(path / "meta.json", {})
        session_id = meta.get("session_id")
        pid = int(meta.get("claude_pid", 0) or 0)
        start = meta.get("claude_start")
        if not isinstance(session_id, str) or not session_id:
            reclaim_unattributed_state(path)
            continue
        if live_same_process(pid, int(start) if isinstance(start, int) else None):
            continue
        cleanup(
            session_id,
            detached_only=False,
            ignore_leases=True,
            run_registry=True,
            remove_state=True,
        )


def hook_start() -> int:
    data = read_json_stdin()
    session_id = current_session(data)
    state = state_dir(session_id)
    private_dir(state)
    private_dir(state / "cleanup.d")
    private_dir(state / "tmp")
    claude_pid = int(os.environ.get("CLAUDE_PID", "0") or 0)
    claude_start = proc_start(claude_pid)
    existing = metadata(session_id)
    same_process = (
        int(existing.get("claude_pid", 0) or 0) == claude_pid
        and existing.get("claude_start") == claude_start
        and live_same_process(claude_pid, claude_start)
    )
    meta = {
        "session_id": session_id,
        "cwd": str(data.get("cwd") or os.getcwd()),
        "start_epoch": existing.get("start_epoch", time.time()) if same_process else time.time(),
        "claude_pid": claude_pid,
        "claude_start": claude_start,
    }
    save_json(state / "meta.json", meta)
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with Path(env_file).open("a") as handle:
            handle.write(f"\n# claude-lnt:{safe_key(session_id)}\n")
            handle.write(f"export CLAUDE_LNT_STATE={shlex.quote(str(state))}\n")
            handle.write(f"export TMPDIR={shlex.quote(str(state / 'tmp'))}\n")
    if not same_process:
        start_watchdog(session_id, claude_pid, meta["claude_start"])
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": (
                        "Leave No Trace automation is active. Use claude-lnt-start for any "
                        "process that must span tool calls and claude-lnt-register before "
                        "an external mutation that needs a custom rollback. "
                        "Open the first reply of this session with one discreet line "
                        "naming the active model, in the form: "
                        "↳ Principal orchestrator · <model>. Surfaces such as "
                        "the VS Code extension show no model header of their own."
                    ),
                }
            }
        )
    )
    sys.stdout.flush()
    # The sweep of other sessions' stale state runs last. This session's own
    # state, TMPDIR redirection and watchdog must exist even when reclaiming
    # a dirty machine is slow enough to hit the hook timeout, because losing
    # them silently disables Leave No Trace for the whole session.
    sweep_stale_states()
    return 0


HEREDOC_OPENER = re.compile(
    r"<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|([\w.-]+))"
)

# A detach keyword only detaches when it is the command being run. The same
# word as an argument, a filename or a search pattern does not, so `git grep
# disown` must not be blocked. Assignments and prefix commands may precede it.
DETACH_COMMAND = re.compile(
    r"(?:^|[;&|(]|\bthen\b|\bdo\b|\belse\b)"
    r"(?:[ \t]*(?:[A-Za-z_]\w*=\S*|command|env|exec|nice|ionice|stdbuf|time))*"
    r"[ \t]*(?:nohup|setsid|disown)\b",
    re.M,
)

# An unquoted `&` that ends a command, rather than `&&`, `2>&1` or a literal.
TRAILING_AMPERSAND = re.compile(r"(?<![>&])&\s*(?:$|[;\n])")


def matching_paren(text: str, start: int) -> int:
    """Index of the `)` closing the `(` at `start`, or -1."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def collect_substitutions(text: str, pieces: list[str]) -> None:
    """Collect command substitutions from text that is otherwise literal.

    A heredoc body and a double-quoted string are data, but `$(...)` and
    backticks inside them still run, so their contents are executable even
    though everything around them is not.
    """
    index = 0
    while index < len(text):
        if text.startswith("$(", index):
            end = matching_paren(text, index + 1)
            if end == -1:
                return
            collect_executable(text[index + 2 : end], pieces)
            index = end + 1
            continue
        if text[index] == "`":
            end = text.find("`", index + 1)
            if end == -1:
                return
            collect_executable(text[index + 1 : end], pieces)
            index = end + 1
            continue
        index += 1


def collect_executable(text: str, pieces: list[str]) -> None:
    """Append every fragment of `text` that bash would execute to `pieces`.

    Detach detection has to run on what executes, not on the raw command.
    Single-quoted spans and heredoc bodies are literal data, so a script
    written with `cat <<EOF` that contains `server &` starts nothing. A
    substitution runs wherever it appears, including inside a double-quoted
    string or an unquoted heredoc body, so each one is collected as its own
    fragment: it begins a fresh command context, which is what makes a
    detach keyword inside it recognisable as the command being run.

    The result is not a runnable reconstruction. It only has to contain
    everything bash would run and nothing bash would treat as text.
    """
    code: list[str] = []
    queued: list[tuple[str, bool, bool]] = []
    index = 0
    length = len(text)

    while index < length:
        character = text[index]

        if character == "\n":
            code.append("\n")
            index += 1
            for terminator, expands, strip_tabs in queued:
                index, body = consume_heredoc(text, index, terminator, strip_tabs)
                if expands:
                    collect_substitutions(body, pieces)
            queued = []
            continue

        if character == "\\" and index + 1 < length:
            # An escaped alphanumeric is part of a word, so `no\hup` still
            # names nohup. An escaped punctuation character is a literal and
            # must not be read as syntax.
            if text[index + 1].isalnum():
                code.append(text[index + 1])
            index += 2
            continue

        if character == "'":
            end = text.find("'", index + 1)
            code.append(" ")
            index = length if end == -1 else end + 1
            continue

        if character == '"':
            end = index + 1
            while end < length:
                if text[end] == "\\" and end + 1 < length:
                    end += 2
                    continue
                if text[end] == '"':
                    break
                end += 1
            collect_substitutions(text[index + 1 : end], pieces)
            code.append(" ")
            index = end + 1
            continue

        if text.startswith("$(", index):
            end = matching_paren(text, index + 1)
            if end == -1:
                index = length
                continue
            collect_executable(text[index + 2 : end], pieces)
            code.append(" ")
            index = end + 1
            continue

        if character == "`":
            end = text.find("`", index + 1)
            if end == -1:
                index = length
                continue
            collect_executable(text[index + 1 : end], pieces)
            code.append(" ")
            index = end + 1
            continue

        if text.startswith("<<", index) and not text.startswith("<<<", index):
            opened = HEREDOC_OPENER.match(text, index)
            if opened:
                terminator = opened.group(2) or opened.group(3) or opened.group(4)
                # A single-quoted delimiter suppresses expansion, so even
                # substitutions in that body are literal.
                queued.append(
                    (terminator, opened.group(2) is None, opened.group(1) == "-")
                )
                code.append(" ")
                index = opened.end()
                continue

        code.append(character)
        index += 1

    pieces.append("".join(code))


def consume_heredoc(
    text: str,
    index: int,
    terminator: str,
    strip_tabs: bool,
) -> tuple[int, str]:
    """Skip a heredoc body, returning the index after it and the body.

    The delimiter line must match exactly, as bash requires. Only `<<-`
    strips leading tabs, so a tab-indented delimiter after a plain `<<` is
    still body text.
    """
    body: list[str] = []
    length = len(text)

    while index < length:
        end = text.find("\n", index)
        if end == -1:
            end = length
        line = text[index:end]
        index = end + 1 if end < length else length

        candidate = line.rstrip("\r")
        if strip_tabs:
            candidate = candidate.lstrip("\t")
        if candidate == terminator:
            break
        body.append(line)

    return index, "\n".join(body)


def executable_text(command: str) -> str:
    pieces: list[str] = []
    collect_executable(command, pieces)
    return "\n".join(pieces)


def hook_guard() -> int:
    data = read_json_stdin()
    if data.get("tool_name") != "Bash":
        return 0
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    command = str(tool_input.get("command", ""))
    background = bool(tool_input.get("run_in_background"))
    safe_wrapper = "claude-lnt-start" in command
    self_cleaning = bool(
        re.search(r"\btrap\b.*\b(EXIT|INT|TERM)\b", command, re.S)
        and re.search(r"\bwait\b", command)
    )
    executable = executable_text(command)
    raw_detach = bool(
        background
        or DETACH_COMMAND.search(executable)
        or TRAILING_AMPERSAND.search(executable)
    )
    if raw_detach and not safe_wrapper and not self_cleaning:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Unregistered detached process blocked by Leave No Trace. "
                            "Use claude-lnt-start --ttl <seconds> -- <command>, or keep the "
                            "process in this tool call with trap-based cleanup and wait."
                        ),
                    }
                }
            )
        )
    return 0


def hook_cleanup(action: str) -> int:
    data = read_json_stdin()
    session_id = current_session(data)
    event = str(data.get("hook_event_name", ""))
    detached_only = action == "sweep"

    # A lease is a promise that a process may outlive the tool call that
    # started it. Overriding leases at the end of every turn made
    # claude-lnt-start useless for the very thing it exists for, so a lease
    # that has not expired now survives Stop and PreCompact. Only the end of
    # the session overrides it, and the watchdog still overrides leases when
    # Claude itself dies.
    session_ending = event == "SessionEnd"

    if session_ending:
        # Stopped first so it cannot recreate state behind the cleanup.
        stop_watchdog(session_id)

    failures = cleanup(
        session_id,
        detached_only=detached_only,
        ignore_leases=session_ending,
        run_registry=not detached_only,
        remove_state=session_ending,
    )

    if failures:
        message = "; ".join(failures[:8])
        append_log(session_id, f"residual: {message}")
        if event == "Stop" and not bool(data.get("stop_hook_active")):
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": (
                            "Leave No Trace cleanup found residual session-owned resources: "
                            f"{message}. Clean them and rerun claude-lnt-status before completing."
                        ),
                    }
                )
            )
    return 0


def watch(session_id: str, claude_pid: int, claude_start: int | None) -> int:
    deadline = time.monotonic() + MAX_WATCH_SECONDS
    while live_same_process(claude_pid, claude_start):
        if time.monotonic() >= deadline:
            # Liveness is rechecked here rather than trusted from the loop
            # condition: Claude may have exited in between, and that is a
            # crash to clean up after, not a term to lapse.
            if not live_same_process(claude_pid, claude_start):
                break
            # The watch term ended with Claude still alive. Tearing down now
            # would kill a live session's processes, revoke valid leases and
            # delete the state directory its exported TMPDIR points into.
            # The watchdog lapses instead: the session's own Stop and
            # SessionEnd hooks still clean up, and only crash protection is
            # lost for the remainder of the session.
            append_log(
                session_id,
                "watchdog term expired with the session still alive",
            )
            return 0
        cleanup(
            session_id,
            detached_only=True,
            ignore_leases=False,
            run_registry=False,
        )
        time.sleep(POLL_SECONDS)
    cleanup(
        session_id,
        detached_only=False,
        ignore_leases=True,
        run_registry=True,
        remove_state=True,
    )
    return 0


def register_cleanup(args: argparse.Namespace) -> int:
    session_id = current_session()
    directory = private_dir(state_dir(session_id) / "cleanup.d")
    if args.shell is not None:
        item: dict[str, Any] = {"shell": args.shell}
    elif args.command:
        item = {"argv": args.command}
    else:
        raise RuntimeError("Provide a cleanup command after -- or use --shell")
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.json"
    save_json(directory / name, item)
    print(f"Registered cleanup: {name}")
    return 0


def start_process(args: argparse.Namespace) -> int:
    session_id = current_session()
    if not args.command:
        raise RuntimeError("Provide a command after --")
    lease_id = uuid.uuid4().hex

    # Creating the state, launching and recording the lease all happen under
    # the lock that teardown also takes. Creating it outside would let a
    # concurrent SessionEnd delete the directory between the mkdir and the
    # acquisition, and launching outside would leave a window in which the
    # process holds no recorded lease and is treated as unowned work.
    with session_lock(session_id):
        state = private_dir(state_dir(session_id))
        private_dir(state / "tmp")

        env = os.environ.copy()
        env["CLAUDE_CODE_SESSION_ID"] = session_id
        env["CLAUDE_CODE_CHILD_SESSION"] = "1"
        env["CLAUDE_LNT_LEASE_ID"] = lease_id
        env["TMPDIR"] = str(state / "tmp")

        log_path = state / f"process-{lease_id[:12]}.log"
        private_file(log_path)
        log_handle = log_path.open("ab", buffering=0)
        private_file(log_path)

        process = subprocess.Popen(
            args.command,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        records = lease_records(session_id)
        records[lease_id] = {
            "pid": process.pid,
            "start": proc_start(process.pid),
            "expires": time.time() + args.ttl,
            "command": args.command,
            "log": str(log_path),
        }
        save_json(state / "leases.json", records)

    print(f"PID={process.pid} TTL={args.ttl}s LOG={log_path}")
    return 0


def manual_cleanup(args: argparse.Namespace) -> int:
    session_id = args.session or current_session()
    failures = cleanup(
        session_id,
        detached_only=False,
        ignore_leases=True,
        run_registry=True,
    )
    if failures:
        print("Residual resources:")
        for item in failures:
            print(f"- {item}")
        return 1
    print(f"Clean: {session_id}")
    return 0


def status(args: argparse.Namespace) -> int:
    session_id = args.session or current_session()
    processes = find_owned_processes(
        session_id,
        detached_only=False,
        ignore_leases=False,
    )
    state = state_dir(session_id)
    entries = list((state / "cleanup.d").glob("*.json")) if (state / "cleanup.d").exists() else []
    print(f"Session: {session_id}")
    print(f"State: {state}")
    print(f"Owned unleased processes: {len(processes)}")
    for pid, command, _ in processes:
        print(f"  {pid} {command}")
    print(f"Registered cleanups: {len(entries)}")
    return 1 if processes else 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="action", required=True)
    for name in ("hook-start", "hook-guard", "hook-sweep", "hook-cleanup"):
        sub.add_parser(name)
    watch_parser = sub.add_parser("watch")
    watch_parser.add_argument("session")
    watch_parser.add_argument("claude_pid", type=int)
    watch_parser.add_argument("claude_start", type=int)
    register_parser = sub.add_parser("register")
    register_parser.add_argument("--shell")
    register_parser.add_argument("command", nargs=argparse.REMAINDER)
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--ttl", type=int, default=300)
    start_parser.add_argument("command", nargs=argparse.REMAINDER)
    cleanup_parser = sub.add_parser("cleanup")
    cleanup_parser.add_argument("session", nargs="?")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("session", nargs="?")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.action == "hook-start":
            return hook_start()
        if args.action == "hook-guard":
            return hook_guard()
        if args.action == "hook-sweep":
            return hook_cleanup("sweep")
        if args.action == "hook-cleanup":
            return hook_cleanup("cleanup")
        if args.action == "watch":
            return watch(args.session, args.claude_pid, args.claude_start or None)
        if args.action == "register":
            if args.command and args.command[0] == "--":
                args.command = args.command[1:]
            return register_cleanup(args)
        if args.action == "start":
            if args.command and args.command[0] == "--":
                args.command = args.command[1:]
            if args.ttl < 1 or args.ttl > 86400:
                raise RuntimeError("--ttl must be between 1 and 86400 seconds")
            return start_process(args)
        if args.action == "cleanup":
            return manual_cleanup(args)
        if args.action == "status":
            return status(args)
    except RuntimeError as exc:
        print(f"claude-lnt: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
