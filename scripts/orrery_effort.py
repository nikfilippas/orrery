#!/usr/bin/env python3
"""What the running session is actually doing, and what a route deserves.

Orrery cannot set a thinking level inside a running session. `--effort`
is start-time, a provider's settings are read at start, and a hook
cannot issue a slash command. What it can do is observe: Claude records
the level on every assistant record it writes, so the level is readable
even where it is not settable.

The gap this closes is narrow and exact. `orrery-session-start` already
reads CLAUDE_CODE_EFFORT_LEVEL and CLAUDE_EFFORT at start and records
the level it found, so what was invisible is an in-session change after
start, and that is the only blind spot claimed here.

Two other things live beside the reader.

The route-to-effort map is the principal's own recommendation table:
what an investigation, a trivial fix or a complex build is worth running
at. No hook consults it, deliberately. The classified route exists only
in the principal's reasoning and is written to no file a hook could
read, so recommending per route is the principal's job and observing
what is running is the hook's.

And the per-session bookkeeping that keeps the prompt hook's incidents
rate-bounded. The incident store rotates at 1 MiB keeping one previous
file, and the fallback precondition reads that store for control flow,
so an event written on every prompt submission would flush the failure
records it depends on within a couple of days at observed rates. An
event is therefore written once per session, and again only when the
value it reports changes.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orrery_allowance import claude_projects_root  # noqa: E402
from orrery_incidents import store_dir  # noqa: E402
from orrery_runtime import (  # noqa: E402
    MODEL_ID,
    THINKING_LEVEL,
    RuntimeConfigError,
    effective_manifest,
    load_role,
)


# The routes the policy classifies work into, in the order it names
# them. A map that omits one keeps this default for it.
ROUTES = ("investigation", "trivial", "mechanical", "standard", "complex")
DEFAULT_ROUTE_EFFORT = {
    "investigation": "medium",
    "trivial": "low",
    "mechanical": "low",
    "standard": "high",
    "complex": "max",
}

# How much of a transcript's end is read. A transcript can be tens of
# megabytes and this runs on every prompt submission, while the records
# wanted are the last few. Where the level is further back than this the
# answer is that it was not observed, which is the safe direction.
TAIL_BYTES = 256 * 1024
MODEL_MARKER = b'"model"'
EFFORT_MARKER = b'"effort"'

STATE_VERSION = 1
STATE_NAME = "prompt-hook.json"
LOCK_NAME = "prompt-hook.lock"
# Long enough to outlive any single session, short enough that the file
# cannot grow with the machine's history.
SESSION_RETENTION_SECONDS = 7 * 86400.0
MAX_SESSIONS = 200


@dataclass(frozen=True)
class Observation:
    """What a transcript's tail says is running. Absent is not a value."""

    model: str | None = None
    effort: str | None = None


def route_effort(manifest: dict[str, Any] | None = None) -> dict[str, str]:
    """The recommended thinking level per classified route.

    The defaults are provisional: whether changing the level inside a
    session costs a cache re-read has not been measured, and if it does
    then switching is worse than staying put. They are named here so a
    recommendation is one table rather than five opinions.
    """
    if manifest is None:
        manifest = effective_manifest()
    configured = manifest.get("route_effort", {})
    if not isinstance(configured, dict):
        raise RuntimeConfigError("the manifest route_effort must be an object")
    unknown = set(configured) - set(ROUTES)
    if unknown:
        raise RuntimeConfigError(
            f"the manifest route_effort names unknown routes: {sorted(unknown)}"
        )
    mapping = dict(DEFAULT_ROUTE_EFFORT)
    for route, level in configured.items():
        if not isinstance(level, str) or not THINKING_LEVEL.fullmatch(level):
            raise RuntimeConfigError(
                f"the manifest route_effort has an invalid level for "
                f"{route}: {level!r}"
            )
        mapping[route] = level
    return mapping


def observe(path: Any) -> Observation:
    """The newest model and thinking level a transcript records.

    Absent is absent. A field that is not there is unobserved and never
    a difference, so both halves stay None until a record says
    otherwise; treating a missing level as drift would report an
    incident on every session whose surface does not write one.
    """
    if not isinstance(path, str) or not path:
        return Observation()
    target = Path(path)
    try:
        with target.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - TAIL_BYTES)
            handle.seek(start)
            tail = handle.read()
    except OSError:
        return Observation()
    lines = tail.split(b"\n")
    if start:
        # A slice cut at an arbitrary byte can begin mid-line.
        lines = lines[1:]
    model: str | None = None
    effort: str | None = None
    for raw in reversed(lines):
        if MODEL_MARKER not in raw and EFFORT_MARKER not in raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or data.get("type") != "assistant":
            continue
        if effort is None:
            level = data.get("effort")
            if isinstance(level, str) and THINKING_LEVEL.fullmatch(level):
                effort = level
        if model is None:
            message = data.get("message")
            name = message.get("model") if isinstance(message, dict) else None
            if isinstance(name, str) and MODEL_ID.fullmatch(name):
                model = name
        if model is not None and effort is not None:
            break
    return Observation(model, effort)


def project_directory(cwd: Path) -> str:
    """The Claude projects directory name for a working directory.

    The surface encodes the path by replacing everything that is not a
    letter, digit or dash. The mapping is deliberately used in this
    direction only: it is not reversible, and a reader that tried would
    confuse `a/b` with `a-b`.
    """
    return re.sub(r"[^A-Za-z0-9-]", "-", str(cwd))


def newest_transcript(cwd: Path) -> Path | None:
    """The most recently written transcript for this working directory."""
    directory = claude_projects_root() / project_directory(cwd)
    newest: Path | None = None
    latest = 0.0
    try:
        candidates = list(directory.glob("*.jsonl"))
    except OSError:
        return None
    for path in candidates:
        try:
            moment = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or moment > latest:
            newest, latest = path, moment
    return newest


def state_path() -> Path:
    """The prompt hook's own bookkeeping, beside the incident store."""
    return store_dir() / STATE_NAME


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
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


def read_state(path: Path | None = None) -> dict[str, Any]:
    """The stored bookkeeping, or an empty one where it cannot be read.

    Unreadable is empty, which costs one repeated incident rather than a
    hook failure.
    """
    target = state_path() if path is None else path
    empty: dict[str, Any] = {"v": STATE_VERSION, "sessions": {}}
    try:
        stored = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(stored, dict) or stored.get("v") != STATE_VERSION:
        return empty
    sessions = stored.get("sessions")
    if not isinstance(sessions, dict):
        return empty
    empty["sessions"] = {
        name: record
        for name, record in sessions.items()
        if isinstance(name, str) and isinstance(record, dict)
    }
    return empty


def _pruned(sessions: dict[str, Any], now: float) -> dict[str, Any]:
    def stamp(record: Any) -> float:
        value = record.get("updated") if isinstance(record, dict) else None
        return float(value) if isinstance(value, (int, float)) else 0.0

    live = {
        name: record
        for name, record in sessions.items()
        if now - stamp(record) <= SESSION_RETENTION_SECONDS
    }
    if len(live) <= MAX_SESSIONS:
        return live
    keep = sorted(live.items(), key=lambda item: -stamp(item[1]))[:MAX_SESSIONS]
    return dict(keep)


def write_state(state: dict[str, Any], path: Path | None = None) -> None:
    target = state_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        json.dump(state, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def changed_for_session(
    session: str,
    field: str,
    value: str,
    *,
    now: float | None = None,
) -> bool:
    """Whether `value` is new for this session, remembering it if so.

    This is the rate bound both of the prompt hook's incidents are
    written behind: true the first time a session reports a value and
    again whenever it reports a different one, false while it repeats
    itself. The value is stored even when the caller will not record
    anything, so that returning to an earlier value counts as a change.

    A store that cannot be written answers false. An unbounded incident
    stream is worse than a missing diagnostic, because the store it
    would fill is read for control flow elsewhere.
    """
    if not session or not field:
        return False
    moment = time.time() if now is None else now
    path = state_path()
    try:
        with _locked(path):
            state = read_state(path)
            sessions = state["sessions"]
            record = dict(sessions.get(session) or {})
            if record.get(field) == value:
                return False
            record[field] = value
            record["updated"] = moment
            sessions[session] = record
            state["sessions"] = _pruned(sessions, moment)
            write_state(state, path)
    except OSError:
        return False
    return True


def doctor_report(cwd: Path | None = None) -> list[str]:
    """Lines for `orrery-doctor`, each prefixed PASS, WARN or SKIP.

    What is reported is what was read, and nothing else. Where no
    transcript can be read the line says so rather than naming the
    configured level, which would assert a level nobody observed.
    """
    working = Path.cwd() if cwd is None else cwd
    lines: list[str] = []
    try:
        mapping = route_effort()
    except RuntimeConfigError as exc:
        lines.append(f"WARN|the configured route_effort map is invalid: {exc}")
    else:
        lines.append(
            "PASS|recommended thinking by route: "
            + ", ".join(f"{route} {mapping[route]}" for route in ROUTES)
        )

    transcript = newest_transcript(working)
    if transcript is None:
        lines.append(
            "SKIP|no Claude transcript for this repository could be found, "
            "so the thinking level it runs at is unknown"
        )
        return lines
    seen = observe(str(transcript))
    if seen.effort is None:
        lines.append(
            f"SKIP|{transcript} records no thinking level, so the level it "
            "runs at is unknown"
        )
        return lines
    try:
        principal = load_role("orchestrator", cwd=working)
    except RuntimeConfigError as exc:
        lines.append(
            f"SKIP|{transcript} last recorded thinking {seen.effort}; the "
            f"configured principal could not be read for comparison: {exc}"
        )
        return lines
    configured = principal.thinking if principal.provider == "anthropic" else None
    if configured is None:
        lines.append(
            f"PASS|{transcript} last recorded thinking {seen.effort}; the "
            "configured principal states no comparable level"
        )
    elif configured == seen.effort:
        lines.append(
            f"PASS|{transcript} last recorded thinking {seen.effort}, "
            "matching the configured principal level"
        )
    else:
        lines.append(
            f"WARN|{transcript} last recorded thinking {seen.effort}, not "
            f"the configured principal level {configured}"
        )
    return lines
