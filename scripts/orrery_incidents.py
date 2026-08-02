#!/usr/bin/env python3
"""Best-effort incident log for Orrery runs.

Everything that goes wrong in a launcher - blockers, timeouts,
fallbacks, consent stops, degraded containment, cleanup problems - is
appended as one JSON line to a store outside every repository, so edge
cases can be assessed later and the default configuration tuned with
evidence rather than memory.

The log is diagnostics, never control flow: a write failure warns once
per process and is otherwise ignored, and nothing here may raise into a
launch that was otherwise going to succeed. Events carry only
wrapper-authored reason strings and structured identities; prompts,
verdicts, provider diagnostics, credentials, and endpoint URLs are
never stored.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
STORE_NAME = "incidents.jsonl"
PREVIOUS_NAME = "incidents.jsonl.1"
LOCK_NAME = "incidents.lock"
ROTATE_BYTES = 1024 * 1024
DETAIL_LIMIT = 300
KIND_LIMIT = 40
IDENTITY_LIMIT = 128
WORKDIR_LIMIT = 1024

_run_id: str | None = None
_warned = False


def store_dir() -> Path:
    """The persistent state directory, shared with the standing store."""
    state_home = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(state_home).expanduser()
        if state_home
        else Path.home() / ".local" / "state"
    )
    return base / "orrery"


def store_path() -> Path:
    return store_dir() / STORE_NAME


def previous_path() -> Path:
    return store_dir() / PREVIOUS_NAME


def _squash(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _process_run_id() -> str:
    global _run_id
    if _run_id is None:
        _run_id = uuid.uuid4().hex[:12]
    return _run_id


def _warn_once(problem: Exception) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    try:
        print(
            f"orrery: the incident log could not be written ({problem}); "
            "continuing without it.",
            file=sys.stderr,
        )
    except Exception:
        pass


def record(
    kind: str,
    *,
    program: str,
    role: Any = None,
    detail: str = "",
    status: int | None = None,
    **fields: Any,
) -> None:
    """Append one incident event; never raises.

    `role` is an orrery_runtime.Role or None. Extra fields must be
    scalars; anything else is stringified defensively so a caller bug
    cannot crash the launch being observed.
    """
    try:
        event: dict[str, Any] = {
            "v": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "run": _process_run_id(),
            "pid": os.getpid(),
            "program": _squash(str(program), IDENTITY_LIMIT),
            "kind": _squash(str(kind), KIND_LIMIT),
        }
        try:
            event["workdir"] = str(Path.cwd())[:WORKDIR_LIMIT]
        except OSError:
            event["workdir"] = ""
        if role is not None:
            event["role"] = _squash(str(role.id), IDENTITY_LIMIT)
            event["provider"] = _squash(str(role.provider), IDENTITY_LIMIT)
            event["model"] = _squash(str(role.model), IDENTITY_LIMIT)
            if role.thinking:
                event["thinking"] = _squash(
                    str(role.thinking), IDENTITY_LIMIT
                )
            if role.endpoint is not None:
                event["endpoint"] = _squash(
                    str(role.endpoint.id), IDENTITY_LIMIT
                )
        if detail:
            event["detail"] = _squash(str(detail), DETAIL_LIMIT)
        if status is not None:
            event["status"] = int(status)
        for name, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool) or isinstance(value, (int, float)):
                event[name] = value
            else:
                event[name] = _squash(str(value), DETAIL_LIMIT)

        line = (json.dumps(event, ensure_ascii=False) + "\n").encode()

        directory = store_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        lock_descriptor = os.open(
            directory / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            # Non-blocking with bounded retries: a stalled lock holder
            # (or a hung filesystem) must cost one dropped diagnostic
            # event, never a stalled launch.
            for attempt in range(5):
                try:
                    fcntl.flock(
                        lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    break
                except OSError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            path = store_path()
            try:
                if path.stat().st_size > ROTATE_BYTES:
                    os.replace(path, previous_path())
            except OSError:
                pass
            descriptor = os.open(
                path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
            )
            try:
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        _warn_once(exc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _bounded_string(value: Any, limit: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def valid_event(entry: Any) -> dict[str, Any] | None:
    """One validated event, or None.

    The store is world-invisible but not trusted: a torn write, an
    older schema, or foreign junk must be skipped, never crash the
    reader or the doctor. Kinds are deliberately not a closed set, so
    a newer writer's events still render.
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("v") != SCHEMA_VERSION or isinstance(entry.get("v"), bool):
        return None
    timestamp = _parse_timestamp(entry.get("ts"))
    if timestamp is None:
        return None
    if not _bounded_string(entry.get("kind"), KIND_LIMIT):
        return None
    if not _bounded_string(entry.get("program"), IDENTITY_LIMIT):
        return None
    for name in ("role", "provider", "model", "thinking", "endpoint"):
        value = entry.get(name)
        if value is not None and not _bounded_string(value, IDENTITY_LIMIT):
            return None
    for name, limit in (("detail", DETAIL_LIMIT), ("workdir", WORKDIR_LIMIT)):
        value = entry.get(name)
        if value is not None and (
            not isinstance(value, str) or len(value) > limit
        ):
            return None
    for name, value in entry.items():
        if isinstance(value, str):
            if len(value) > WORKDIR_LIMIT:
                return None
        elif not isinstance(value, (int, float, bool)) and value is not None:
            return None
    return entry


def read_events(since: datetime | None = None) -> list[dict[str, Any]]:
    """Validated events from both store files, oldest first."""
    events: list[tuple[datetime, dict[str, Any]]] = []
    for path in (previous_path(), store_path()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = valid_event(entry)
            if event is None:
                continue
            timestamp = _parse_timestamp(event["ts"])
            if timestamp is None:
                continue
            if since is not None and timestamp < since:
                continue
            events.append((timestamp, event))
    events.sort(key=lambda item: item[0])
    return [event for _timestamp, event in events]
