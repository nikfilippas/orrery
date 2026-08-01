#!/usr/bin/env python3
"""Standing fallback approvals: prior recorded consent with bounded scope.

A standing approval records that the user approved one exact fallback
candidate for one role, either for the current login session or until a
provider-stated reset time. Later invocations start that recorded
candidate directly, disclose it, and never re-rank. Records live outside
the repository, are revocable, and always lose to `--no-fallback`, to an
explicit `--approve-fallback`, and to the changed-workspace refusal.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orrery_runtime import (  # noqa: E402
    MODEL_ID,
    PROVIDERS,
    THINKING_LEVEL,
    Role,
    RuntimeConfigError,
)


SESSION_SCOPE = "session"
UNTIL_SCOPE = "until"
RUN_SCOPE = "run"
SESSION_CAP_SECONDS = 24 * 3600
STORE_VERSION = 1
STORE_NAME = "standing.json"
LOCK_NAME = "standing.lock"
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
_FAILURE_SCOPES = frozenset({"provider", "model", "transient"})


def current_boot_id() -> str | None:
    try:
        value = BOOT_ID_PATH.read_text().strip()
    except OSError:
        return None
    return value or None


def session_store_path() -> Path | None:
    """The session-scope store, or None when no login session dir exists."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir or not Path(runtime_dir).is_dir():
        return None
    return Path(runtime_dir) / "orrery" / STORE_NAME


def until_store_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(state_home).expanduser()
        if state_home
        else Path.home() / ".local" / "state"
    )
    return base / "orrery" / STORE_NAME


def session_scope_available() -> bool:
    return session_store_path() is not None


def available_scopes(reset_time: datetime | None) -> list[str]:
    """The approval scopes that are currently offerable, in menu order."""
    scopes = [RUN_SCOPE]
    if session_scope_available():
        scopes.append(SESSION_SCOPE)
    if reset_time is not None:
        scopes.append(UNTIL_SCOPE)
    return scopes


def parse_approval_scope(value: str) -> tuple[str, float | None]:
    """Validate a --approval-scope value into (scope, expires_at epoch).

    The until scope must carry its own timestamp because an approved rerun
    makes no configured-provider attempt and so has no diagnostics left to
    parse a reset time from.
    """
    if value == RUN_SCOPE:
        return RUN_SCOPE, None
    if value == SESSION_SCOPE:
        if not session_scope_available():
            raise RuntimeConfigError(
                "--approval-scope session requires a login-session runtime "
                "directory (XDG_RUNTIME_DIR); use run or until:<ISO8601>"
            )
        return SESSION_SCOPE, None
    if value == UNTIL_SCOPE:
        raise RuntimeConfigError(
            "--approval-scope until must carry the provider-stated reset "
            "time, for example until:2026-08-05T16:49"
        )
    if value.startswith(f"{UNTIL_SCOPE}:"):
        raw = value.split(":", 1)[1]
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeConfigError(
                f"--approval-scope until carries an unreadable time: {raw!r}"
            ) from exc
        if moment.tzinfo is None:
            moment = moment.astimezone()
        expires_at = moment.timestamp()
        if expires_at <= time.time():
            raise RuntimeConfigError(
                "--approval-scope until names a time that is already in "
                f"the past: {raw}"
            )
        return UNTIL_SCOPE, expires_at
    raise RuntimeConfigError(
        "--approval-scope must be run, session, or until:<ISO8601>"
    )


def _fingerprint(role: Role) -> list[str | None]:
    return [role.provider, role.model, role.thinking]


@contextlib.contextmanager
def _locked_store(path: Path) -> Iterator[None]:
    """Hold the sibling lock for a whole read-modify-replace.

    The lock file is created once and never replaced, so every writer
    serialises on the same inode; replacing the data file while the lock
    is held cannot lose a concurrent writer's view.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor = os.open(directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_records(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except OSError:
        return []
    except json.JSONDecodeError:
        print(
            f"orrery: ignoring an unreadable standing-approval store: {path}",
            file=sys.stderr,
        )
        return []
    approvals = data.get("approvals") if isinstance(data, dict) else None
    if not isinstance(approvals, list):
        return []
    validated = (_valid_record(entry) for entry in approvals)
    return [record for record in validated if record is not None]


def _valid_record(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    fingerprint = entry.get("fingerprint")
    thinking = entry.get("candidate_thinking")
    expires_at = entry.get("expires_at")
    if not (
        isinstance(entry.get("role_id"), str)
        and entry.get("failed_provider") in PROVIDERS
        and isinstance(entry.get("failed_model"), str)
        and MODEL_ID.fullmatch(entry.get("failed_model", ""))
        and entry.get("failure_scope") in _FAILURE_SCOPES
        and entry.get("candidate_provider") in PROVIDERS
        and isinstance(entry.get("candidate_model"), str)
        and MODEL_ID.fullmatch(entry.get("candidate_model", ""))
        and (
            thinking is None
            or isinstance(thinking, str)
            and THINKING_LEVEL.fullmatch(thinking)
        )
        and entry.get("scope") in {SESSION_SCOPE, UNTIL_SCOPE}
        and (
            expires_at is None
            or isinstance(expires_at, (int, float))
            and not isinstance(expires_at, bool)
        )
        and isinstance(fingerprint, list)
        and len(fingerprint) == 3
        and isinstance(entry.get("created_at"), (int, float))
    ):
        return None
    return entry


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    payload = json.dumps(
        {"version": STORE_VERSION, "approvals": records},
        indent=2,
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(payload + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _expired(record: dict[str, Any], now: float) -> bool:
    if record["scope"] == UNTIL_SCOPE:
        return record["expires_at"] is None or record["expires_at"] <= now
    boot_id = record.get("boot_id")
    if boot_id:
        current = current_boot_id()
        return current is not None and current != boot_id
    return now >= record["created_at"] + SESSION_CAP_SECONDS


def _store_for_scope(scope: str) -> Path:
    if scope == SESSION_SCOPE:
        path = session_store_path()
        if path is None:
            raise RuntimeConfigError(
                "a session-scope approval needs a login-session runtime "
                "directory"
            )
        return path
    if scope == UNTIL_SCOPE:
        return until_store_path()
    raise RuntimeConfigError(f"unknown standing-approval scope: {scope}")


def record_approval(
    *,
    configured: Role,
    candidate: Role,
    scope: str,
    expires_at: float | None,
    reason: str,
    failure_scope: str,
    _test_hook: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Persist one standing approval, replacing any older one for the role."""
    if scope == UNTIL_SCOPE and expires_at is None:
        raise RuntimeConfigError(
            "an until-scope approval requires its reset time"
        )
    if failure_scope not in _FAILURE_SCOPES:
        raise RuntimeConfigError(
            f"unknown failure scope for a standing approval: {failure_scope}"
        )
    record = {
        "role_id": configured.id,
        "failed_provider": configured.provider,
        "failed_model": configured.model,
        "failure_scope": failure_scope,
        "candidate_provider": candidate.provider,
        "candidate_model": candidate.model,
        "candidate_thinking": candidate.thinking,
        "scope": scope,
        "expires_at": expires_at,
        "fingerprint": _fingerprint(configured),
        "boot_id": current_boot_id() if scope == SESSION_SCOPE else None,
        "reason": re.sub(r"\s+", " ", reason).strip()[:300],
        "created_at": time.time(),
    }
    path = _store_for_scope(scope)
    with _locked_store(path):
        records = _read_records(path)
        if _test_hook is not None:
            _test_hook()
        records = [
            entry for entry in records if entry["role_id"] != configured.id
        ]
        records.append(record)
        _write_records(path, records)
    return record


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    session = session_store_path()
    if session is not None:
        paths.append(session)
    paths.append(until_store_path())
    return paths


def match(configured: Role) -> dict[str, Any] | None:
    """The live standing approval for this configured role, if any.

    Expired entries are deleted while the store is locked. An entry whose
    fingerprint no longer equals the configured identity is skipped but
    kept: a repository override can change the effective identity per
    directory without invalidating an approval that is still correct for
    the global configuration.
    """
    now = time.time()
    for path in _candidate_paths():
        found: dict[str, Any] | None = None
        try:
            with _locked_store(path):
                records = _read_records(path)
                live: list[dict[str, Any]] = []
                changed = False
                for record in records:
                    if _expired(record, now):
                        changed = True
                        continue
                    live.append(record)
                    if (
                        found is None
                        and record["role_id"] == configured.id
                        and record["fingerprint"] == _fingerprint(configured)
                    ):
                        found = record
                if changed:
                    _write_records(path, live)
        except OSError as exc:
            # A broken store must degrade to the normal consent flow, not
            # crash a launch that never needed the store to exist.
            print(
                f"orrery: the standing-approval store at {path} is "
                f"unusable ({exc}); continuing without it.",
                file=sys.stderr,
            )
            continue
        if found is not None:
            return found
    return None


def candidate_role(configured: Role, record: dict[str, Any]) -> Role:
    return replace(
        configured,
        provider=record["candidate_provider"],
        model=record["candidate_model"],
        thinking=record["candidate_thinking"],
        # A recorded approval names a first-party provider and model, so
        # the candidate must not keep the configured role's endpoint.
        endpoint=None,
    )


def list_active(now: float | None = None) -> list[dict[str, Any]]:
    """Live records across both stores, without mutating anything."""
    moment = time.time() if now is None else now
    active: list[dict[str, Any]] = []
    for path in _candidate_paths():
        for record in _read_records(path):
            if not _expired(record, moment):
                active.append(record)
    return active


def revoke_all(
    _test_hook: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    for path in _candidate_paths():
        with _locked_store(path):
            records = _read_records(path)
            if _test_hook is not None:
                _test_hook()
            if records:
                removed.extend(records)
                _write_records(path, [])
    return removed


def describe(record: dict[str, Any]) -> str:
    """One line naming the substitution, its scope, and its lifetime."""
    thinking = record.get("candidate_thinking")
    candidate = (
        f"{record['candidate_provider']}/{record['candidate_model']}"
        + (f" (thinking {thinking})" if thinking else "")
    )
    if record["scope"] == UNTIL_SCOPE:
        expires = datetime.fromtimestamp(record["expires_at"]).astimezone()
        lifetime = f"until {expires:%Y-%m-%d %H:%M %Z}"
    else:
        lifetime = "for this login session"
    return (
        f"{record['role_id']}: {record['failed_provider']}/"
        f"{record['failed_model']} -> {candidate} {lifetime}"
    )
