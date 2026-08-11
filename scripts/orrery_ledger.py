#!/usr/bin/env python3
"""Durable repository task contracts and transition ledgers."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


STATES = frozenset(
    {
        "NEW", "READY", "DISPATCHED", "IN_PROGRESS", "IMPLEMENTED",
        "NO_CHANGE", "DISPATCH_FAILED", "INTERRUPTED", "VERIFICATION_PASSED",
        "VERIFICATION_FAILED", "IN_REVIEW", "REVIEW_PASSED", "REVIEW_BLOCKED",
        "AWAITING_MERGE", "MERGED", "CLOSED", "CANCELLED",
    }
)
TERMINAL_STATES = frozenset({"MERGED", "CLOSED", "CANCELLED"})
TRANSITIONS = {
    "NEW": frozenset({"READY", "CANCELLED"}),
    "READY": frozenset({"READY", "DISPATCHED", "CANCELLED"}),
    "DISPATCHED": frozenset(
        {"IN_PROGRESS", "DISPATCH_FAILED", "INTERRUPTED", "CANCELLED"}
    ),
    "IN_PROGRESS": frozenset(
        {"IN_PROGRESS", "IMPLEMENTED", "NO_CHANGE", "DISPATCH_FAILED", "INTERRUPTED", "CANCELLED"}
    ),
    "IMPLEMENTED": frozenset(
        {"VERIFICATION_PASSED", "VERIFICATION_FAILED", "CANCELLED"}
    ),
    "NO_CHANGE": frozenset({"NO_CHANGE", "CLOSED", "READY", "CANCELLED"}),
    "DISPATCH_FAILED": frozenset({"READY", "CANCELLED"}),
    # A review can be interrupted too, and must resume as a review: sending
    # it to READY would re-dispatch contract["assigned_role"], which is a
    # write-capable role, and implement a second time over verified work.
    "INTERRUPTED": frozenset({"READY", "IN_REVIEW", "CANCELLED"}),
    "VERIFICATION_PASSED": frozenset(
        {"IN_REVIEW", "AWAITING_MERGE", "CANCELLED"}
    ),
    "IN_REVIEW": frozenset(
        {"REVIEW_PASSED", "REVIEW_BLOCKED", "INTERRUPTED", "CANCELLED"}
    ),
    "REVIEW_PASSED": frozenset({"AWAITING_MERGE", "CANCELLED"}),
    # READY for rework, REVIEW_PASSED when every blocking finding has been
    # resolved, and CANCELLED like every other non-terminal state: without
    # it, cancelling a reviewed task reports a corrupt ledger.
    # The self-loop is how a re-review of blocked work records its own
    # outcome: it starts from REVIEW_BLOCKED and may legitimately leave it
    # blocked, exactly as NO_CHANGE and AWAITING_MERGE repeat themselves.
    # INTERRUPTED because a re-review can fail exactly as a first review
    # can, and its spend and its reviewer's identity have to be
    # recordable from here too; without it the append raised and the
    # paid run went unrecorded.
    "REVIEW_BLOCKED": frozenset(
        {"REVIEW_BLOCKED", "READY", "REVIEW_PASSED", "INTERRUPTED", "CANCELLED"}
    ),
    "VERIFICATION_FAILED": frozenset({"READY", "VERIFICATION_PASSED", "CANCELLED"}),
    "AWAITING_MERGE": frozenset({"AWAITING_MERGE", "MERGED", "CANCELLED"}),
    "MERGED": frozenset({"CLOSED"}),
    "CLOSED": frozenset(),
    "CANCELLED": frozenset(),
}
DISPATCH_FAILURE_REASONS = frozenset(
    {
        "timeout",
        "provider-exit",
        "missing-result",
        "spawn-failure",
        "receipt-missing",
        "receipt-corrupt",
        "approval-required",
    }
)
_CONTRACT_KEYS = frozenset(
    {"task_id", "title", "goal", "acceptance_criteria", "scope", "risk", "assigned_role", "target_ref", "budget", "notes", "review", "depends_on", "waiver"}
)
# The two time bounds are seconds and predate this table; `tokens` and
# `dispatches` are the ceilings Phase 5 adds. Each key gets its own
# range because one shared range for seconds and tokens would have made
# a token ceiling of 30 the smallest expressible bound.
BUDGET_BOUNDS = {
    "timeout_seconds": (30, 14400),
    "hard_timeout_seconds": (30, 14400),
    "tokens": (1, 1_000_000_000),
    "dispatches": (1, 100),
}


# Every local plumbing call is bounded: an unbounded wait turns a held
# lock into a runner that never returns.
PLUMBING_TIMEOUT_SECONDS = 120
_TASK_ID = re.compile(r"^T-[0-9]+$")


class LedgerError(Exception):
    """A task-control operational or validation error."""


def repository_root(cwd: Path | None = None) -> Path:
    """Return the git worktree root containing cwd."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LedgerError("current directory is not inside a git worktree") from exc
    return Path(result.stdout.strip())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_store(root: Path) -> Path:
    store = root / ".orrery"
    new_store = not store.exists()
    store.mkdir(mode=0o700, exist_ok=True)
    os.chmod(store, 0o700)
    for name in ("contracts", "ledger"):
        directory = store / name
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    if new_store:
        _add_exclude(root)
    return store


def _add_exclude(root: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"], cwd=root,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LedgerError("could not resolve git exclude path") from exc
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if ".orrery/" not in existing.splitlines():
        with path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(".orrery/\n")
        os.chmod(path, 0o600)


@contextlib.contextmanager
def control_lock(root: Path, timeout: float | None = None) -> Iterator[Path]:
    """Hold the stable repository control lock for a bounded period."""
    if timeout is None:
        try:
            timeout = float(os.environ.get("ORRERY_TASK_LOCK_TIMEOUT", "10"))
        except ValueError:
            timeout = 10.0
    store = _ensure_store(root)
    descriptor = os.open(store / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LedgerError("timed out waiting for repository control lock")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield store
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _error(path: str, message: str) -> LedgerError:
    return LedgerError(f"{path}: {message}")


def _string(value: Any, path: str, maximum: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(path, "must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise _error(path, f"must be at most {maximum} characters")
    return value


def _relative_path(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(path, "must be a relative path")
    normalised = os.path.normpath(value)
    if (
        os.path.isabs(value)
        or os.path.isabs(normalised)
        or ".." in Path(value).parts
    ):
        raise _error(path, "must not traverse outside the repository")
    return normalised


def _object(value: Any, path: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    unknown = set(value) - keys
    if unknown:
        raise _error(f"{path}.{sorted(unknown)[0]}", "is not allowed")
    return value


def validate_contract(contract: Any, task_id: str | None = None) -> dict[str, Any]:
    """Strictly validate a v1 task contract and return it unchanged."""
    data = _object(contract, "$", _CONTRACT_KEYS)
    if task_id is not None and data.get("task_id") != task_id:
        raise _error("$.task_id", f"must equal {task_id}")
    _string(data.get("task_id"), "$.task_id")
    _string(data.get("title"), "$.title", 200)
    _string(data.get("goal"), "$.goal")
    criteria = data.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise _error("$.acceptance_criteria", "must be a non-empty list")
    seen: set[str] = set()
    for index, criterion in enumerate(criteria):
        path = f"$.acceptance_criteria[{index}]"
        entry = _object(criterion, path, frozenset({"id", "statement", "verification"}))
        identity = _string(entry.get("id"), f"{path}.id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", identity):
            raise _error(f"{path}.id", "must contain only letters, numbers, ., _, or -")
        if identity in seen:
            raise _error(f"{path}.id", "must be unique")
        seen.add(identity)
        _string(entry.get("statement"), f"{path}.statement")
        verification = _object(entry.get("verification"), f"{path}.verification", frozenset({"command", "workdir"}))
        _string(verification.get("command"), f"{path}.verification.command")
        if "workdir" in verification:
            verification["workdir"] = _relative_path(verification["workdir"], f"{path}.verification.workdir")
    scope = _object(data.get("scope"), "$.scope", frozenset({"include", "exclude"}))
    for name in ("include", "exclude"):
        paths = scope.get(name)
        if not isinstance(paths, list):
            raise _error(f"$.scope.{name}", "must be a list")
        for index, entry in enumerate(paths):
            paths[index] = _relative_path(entry, f"$.scope.{name}[{index}]")
    risk = _object(data.get("risk"), "$.risk", frozenset({"level", "reasons"}))
    if risk.get("level") not in {"low", "medium", "high"}:
        raise _error("$.risk.level", "must be low, medium, or high")
    reasons = risk.get("reasons")
    if not isinstance(reasons, list):
        raise _error("$.risk.reasons", "must be a list")
    for index, reason in enumerate(reasons):
        _string(reason, f"$.risk.reasons[{index}]")
    if data.get("assigned_role") not in {"mechanic", "implementer"}:
        raise _error("$.assigned_role", "must be mechanic or implementer")
    _string(data.get("target_ref"), "$.target_ref")
    if "budget" in data:
        budget = _object(data["budget"], "$.budget", frozenset(BUDGET_BOUNDS))
        for name, value in budget.items():
            low, high = BUDGET_BOUNDS[name]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise _error(f"$.budget.{name}", f"must be an integer from {low} to {high}")
    if "notes" in data and not isinstance(data["notes"], str):
        raise _error("$.notes", "must be a string")
    if "depends_on" in data:
        # Shape only. Whether the named tasks exist, and whether the graph
        # is acyclic, needs the store and is checked when the contract is
        # sealed: a cycle refused at creation is a cycle that cannot be
        # discovered at runtime with two agents already waiting.
        depends = data["depends_on"]
        if not isinstance(depends, list):
            raise _error("$.depends_on", "must be a list")
        seen: set[str] = set()
        for index, entry in enumerate(depends):
            _string(entry, f"$.depends_on[{index}]")
            if not _TASK_ID.match(entry):
                raise _error(f"$.depends_on[{index}]", "must be a task identifier")
            if task_id is not None and entry == task_id:
                raise _error(f"$.depends_on[{index}]", "a task cannot depend on itself")
            if entry in seen:
                raise _error(f"$.depends_on[{index}]", f"names {entry} twice")
            seen.add(entry)
    if "review" in data and not isinstance(data["review"], bool):
        # Optional and absent means no review, so every contract written
        # before this key existed keeps its Phase 1 behaviour exactly.
        raise _error("$.review", "must be true or false")
    if "waiver" in data:
        # Optional, and the reason an operator gives for declining review
        # on high-risk work. It only informs the review queue; the gate
        # rule is unchanged, so a contract without it behaves exactly as
        # before and one with it must at least say something.
        if not isinstance(data["waiver"], str) or not data["waiver"].strip():
            raise _error("$.waiver", "must be a non-empty string")
    return data


def _stored_bytes(contract: dict[str, Any]) -> bytes:
    return (json.dumps(contract, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "incomplete write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _allocate_id(store: Path) -> str:
    counter = store / "counter"
    try:
        previous = int(counter.read_text().strip()) if counter.exists() else 0
    except (OSError, ValueError) as exc:
        raise LedgerError("repository task counter is unreadable") from exc
    next_value = previous + 1
    _atomic_write(counter, f"{next_value}\n".encode(), 0o600)
    return f"T-{next_value}"


def _ledger_path(store: Path, task_id: str) -> Path:
    return store / "ledger" / f"{task_id}.jsonl"


def _contract_path(store: Path, task_id: str) -> Path:
    return store / "contracts" / f"{task_id}.json"


def _validate_record(record: Any, line: int, previous: dict[str, Any] | None) -> None:
    if not isinstance(record, dict):
        raise LedgerError(f"ledger corrupt at line {line}")
    if record.get("v") != 1 or record.get("seq") != line:
        raise LedgerError(f"ledger corrupt at line {line}")
    if record.get("to") not in STATES or record.get("from") not in STATES | {None}:
        raise LedgerError(f"ledger corrupt at line {line}")
    if record.get("actor") not in {"user", "runner", "principal"} or not isinstance(record.get("ts"), str):
        raise LedgerError(f"ledger corrupt at line {line}")
    if previous is None:
        if record.get("from") is not None or record.get("to") != "NEW":
            raise LedgerError(f"ledger corrupt at line {line}")
    else:
        repair = record.get("reason") == "ledger-repaired"
        if record.get("from") != previous["to"] or (
            record["to"] not in TRANSITIONS[previous["to"]]
            and not repair
        ):
            raise LedgerError(f"ledger corrupt at line {line}")
    if record.get("to") != "NEW" and not isinstance(record.get("contract_digest"), str):
        raise LedgerError(f"ledger corrupt at line {line}")
    if record.get("to") == "DISPATCH_FAILED" and record.get("reason") not in DISPATCH_FAILURE_REASONS:
        raise LedgerError(f"ledger corrupt at line {line}")


def read_ledger(store: Path, task_id: str) -> tuple[list[dict[str, Any]], bool, int]:
    """Read a ledger without modifying it, returning records and torn-tail data."""
    path = _ledger_path(store, task_id)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise LedgerError(f"task {task_id} has no ledger") from exc
    records: list[dict[str, Any]] = []
    boundary = 0
    lines = data.splitlines(keepends=True)
    for index, raw in enumerate(lines, 1):
        final = index == len(lines)
        if not raw.endswith(b"\n"):
            if final:
                return records, True, boundary
            raise LedgerError(f"ledger corrupt at line {index}")
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"ledger corrupt at line {index}") from exc
        _validate_record(record, index, records[-1] if records else None)
        records.append(record)
        boundary += len(raw)
    return records, False, boundary


def _digest(store: Path, task_id: str) -> str:
    try:
        return hashlib.sha256(_contract_path(store, task_id).read_bytes()).hexdigest()
    except OSError as exc:
        raise LedgerError(f"task {task_id} has no contract") from exc


def append_record(store: Path, task_id: str, record: dict[str, Any]) -> None:
    """Durably append a validated transition while the caller holds control_lock."""
    path = _ledger_path(store, task_id)
    if path.exists():
        records, torn, _boundary = read_ledger(store, task_id)
    else:
        records, torn = [], False
    if torn:
        raise LedgerError("ledger has a torn tail; run status --repair")
    expected = len(records) + 1
    candidate = dict(record)
    candidate["v"] = 1
    candidate["seq"] = expected
    candidate["ts"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    _validate_record(candidate, expected, records[-1] if records else None)
    line = (json.dumps(candidate, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    created = not path.exists()
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        start = os.lseek(descriptor, 0, os.SEEK_END)
        offset = 0
        try:
            while offset < len(line):
                written = os.write(descriptor, line[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "incomplete ledger write")
                offset += written
            os.fsync(descriptor)
        except OSError:
            os.ftruncate(descriptor, start)
            os.fsync(descriptor)
            raise
    finally:
        os.close(descriptor)
    if created:
        _fsync_directory(path.parent)


def repair_ledger(store: Path, task_id: str) -> bool:
    """Repair only a torn final ledger line, then record that repair."""
    records, torn, boundary = read_ledger(store, task_id)
    if not torn:
        return False
    if not records:
        raise LedgerError("ledger corrupt at line 1")
    digest = records[-1].get("contract_digest")
    if digest and _digest(store, task_id) != digest:
        raise LedgerError("contract changed; amend the task before repairing its ledger")
    path = _ledger_path(store, task_id)
    descriptor = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(descriptor, boundary)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    state = records[-1]["to"]
    record: dict[str, Any] = {"from": state, "to": state, "actor": "user", "reason": "ledger-repaired"}
    if state != "NEW":
        record["contract_digest"] = digest
    append_record(store, task_id, record)
    return True



def dependency_graph_ok(store: Path, task_id: str, depends_on: list[str]) -> None:
    """Refuse a dependency that does not exist, or any cycle it would close.

    Walked when the contract is sealed rather than when it runs. A cycle
    discovered at dispatch is a cycle discovered with agents already
    waiting on each other, which is the failure this design exists to
    make impossible rather than to detect.
    """
    def declared(identifier: str) -> list[str]:
        path = store / "contracts" / f"{identifier}.json"
        try:
            data = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            return []
        listed = data.get("depends_on")
        return [entry for entry in listed if isinstance(entry, str)] if isinstance(listed, list) else []

    for entry in depends_on:
        if not (store / "contracts" / f"{entry}.json").exists():
            raise _error("$.depends_on", f"names a task that does not exist: {entry}")

    # Depth-first from each declared dependency, looking for a path back.
    # The new task is not yet on disk, so its own edges are the ones
    # supplied here.
    for start in depends_on:
        seen: set[str] = set()
        stack = [(start, [task_id, start])]
        while stack:
            node, path = stack.pop()
            if node == task_id:
                raise _error("$.depends_on", "would close a cycle: " + " -> ".join(path))
            if node in seen:
                continue
            seen.add(node)
            for nxt in declared(node):
                stack.append((nxt, path + [nxt]))

def risk_review_refusal(contract: dict[str, Any]) -> str | None:
    """Why declared high-risk work may not proceed unreviewed.

    A contract can declare `risk.level: high` and omit `review`, and it
    then walks from verification to the merge gate with no independent
    look at it. The operator has already supplied the fact that decides
    this, so omission stops counting as consent. An explicit
    `review: false` is still allowed, because there may be a reason, but
    it is a decision on the record rather than a gap in one.

    Deliberately not part of `validate_contract`: reading a contract
    sealed before this rule must keep working, and only sealing and
    merging enforce it.
    """
    level = (contract.get("risk") or {}).get("level")
    if level == "high" and "review" not in contract:
        return (
            "risk.level is high but the contract does not say whether it is "
            "reviewed; set review to true, or to false to record a waiver"
        )
    return None


def create_task(root: Path, contract: dict[str, Any]) -> str:
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=root,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LedgerError(
            "repository has no commits; create an initial commit first"
        ) from exc
    with control_lock(root) as store:
        task_id = _allocate_id(store)
        contract = dict(contract)
        supplied = contract.get("task_id")
        if supplied is not None and supplied != task_id:
            raise _error("$.task_id", f"must equal the allocated {task_id}")
        contract["task_id"] = task_id
        if not contract.get("target_ref"):
            try:
                result = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    timeout=PLUMBING_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise LedgerError("detached HEAD requires an explicit $.target_ref") from exc
            contract["target_ref"] = result.stdout.strip()
        validate_contract(contract, task_id)
        if refusal := risk_review_refusal(contract):
            raise LedgerError(refusal)
        dependency_graph_ok(store, task_id, contract.get("depends_on", []))
        data = _stored_bytes(contract)
        path = _contract_path(store, task_id)
        _atomic_write(path, data, 0o600)
        append_record(store, task_id, {"from": None, "to": "NEW", "actor": "user"})
        append_record(store, task_id, {"from": "NEW", "to": "READY", "actor": "user", "contract_digest": hashlib.sha256(data).hexdigest()})
        os.chmod(path, 0o400)
        return task_id


def amend_task(root: Path, task_id: str, contract: dict[str, Any]) -> None:
    with control_lock(root) as store:
        records, torn, _boundary = read_ledger(store, task_id)
        if torn:
            raise LedgerError("ledger has a torn tail; run status --repair")
        if not records or records[-1]["to"] != "READY":
            raise LedgerError("task is not READY")
        validate_contract(contract, task_id)
        if refusal := risk_review_refusal(contract):
            raise LedgerError(refusal)
        dependency_graph_ok(store, task_id, contract.get("depends_on", []))
        data = _stored_bytes(contract)
        path = _contract_path(store, task_id)
        retain_superseded(store, task_id, path)
        _atomic_write(path, data, 0o600)
        os.chmod(path, 0o400)
        append_record(store, task_id, {"from": "READY", "to": "READY", "actor": "user", "reason": "AMENDED", "contract_digest": hashlib.sha256(data).hexdigest()})


def retain_superseded(store: Path, task_id: str, path: Path) -> None:
    """Keep the bytes an amendment is about to replace.

    Every ledger record names its contract by digest, and the amendment
    used to overwrite the only copy: the risk level, scope, role and
    ceilings that produced an earlier admission or refusal were then
    unrecoverable, which makes those decision records unreadable exactly
    when an audit needs them.
    """
    try:
        previous = path.read_bytes()
    except OSError:
        return
    digest = hashlib.sha256(previous).hexdigest()
    superseded = _superseded_path(store, task_id, digest)
    if superseded.exists():
        return
    superseded.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write(superseded, previous, 0o600)
    os.chmod(superseded, 0o400)


def _superseded_path(store: Path, task_id: str, digest: str) -> Path:
    return store / "contracts" / "superseded" / f"{task_id}.{digest}.json"


def contract_bytes_for(store: Path, task_id: str, digest: str) -> bytes | None:
    """The exact contract a ledger record was written against."""
    current_path = _contract_path(store, task_id)
    try:
        data = current_path.read_bytes()
    except OSError:
        data = None
    if data is not None and hashlib.sha256(data).hexdigest() == digest:
        return data
    try:
        return _superseded_path(store, task_id, digest).read_bytes()
    except OSError:
        return None


def task_ids(store: Path) -> list[str]:
    """Every task the ledger knows, whatever became of its contract.

    Enumerating contracts instead made a task with a missing contract
    invisible rather than an error, which is precisely backwards: an
    active holder that cannot be read must stop the work that would
    collide with it, not disappear from the scan.
    """
    seen = {path.stem for path in (store / "ledger").glob("T-*.jsonl")}
    seen.update(path.stem for path in (store / "contracts").glob("T-*.json"))
    return sorted(seen, key=lambda task_id: int(task_id[2:]))


def load_contract(store: Path, task_id: str) -> dict[str, Any]:
    try:
        contract = json.loads(_contract_path(store, task_id).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"task {task_id} has an unreadable contract") from exc
    return validate_contract(contract, task_id)


def repo_identity(root: Path) -> str:
    """Return the stable, local identity used for task worktrees."""
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]


def worktree_path(root: Path, task_id: str) -> Path:
    """Return the private task worktree location for root and task_id."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return state_home / "orrery" / "worktrees" / repo_identity(root) / task_id


def ensure_worktree(root: Path, task_id: str, base_commit: str) -> Path:
    """Create or reuse the dedicated branch worktree for a task."""
    path = worktree_path(root, task_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    branch = f"orrery/{task_id}"
    if path.exists() and (path / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--quiet", "--short", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
        if result.returncode == 0 and result.stdout.strip() == branch:
            return path
        raise LedgerError(result.stderr.strip() or "existing worktree has the wrong branch")
    if path.exists() and any(path.iterdir()):
        raise LedgerError("worktree path already exists and is not a git worktree")
    exists = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        timeout=PLUMBING_TIMEOUT_SECONDS,
    ).returncode == 0
    command = ["git", "-C", str(root), "worktree", "add"]
    if not exists:
        command.extend(["-b", branch])
    command.extend([str(path), branch if exists else base_commit])
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        timeout=PLUMBING_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise LedgerError(result.stderr.strip() or "could not create worktree")
    os.chmod(path, 0o700)
    return path


def merge_branch(root: Path, commit: str) -> tuple[str | None, str]:
    """Merge an exact commit into root, aborting and returning stderr on failure."""
    result = subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.name=Orrery",
            "-c", "user.email=orrery@localhost", "merge", "--no-ff",
            "--no-edit", commit,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
        timeout=PLUMBING_TIMEOUT_SECONDS,
    )
    if result.returncode:
        subprocess.run(
            ["git", "-C", str(root), "merge", "--abort"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
        return None, result.stderr
    oid = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, check=False,
        timeout=PLUMBING_TIMEOUT_SECONDS,
    )
    return oid.stdout.strip(), ""


def remove_worktree(root: Path, task_id: str) -> list[str]:
    """Remove a task worktree and branch, reporting targets that remain."""
    path = worktree_path(root, task_id)
    remaining: list[str] = []
    if path.exists():
        result = subprocess.run(
            ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
        # Git may unregister a worktree before a failed filesystem removal
        # (for example, when its parent is temporarily unwritable).  A later
        # discard must still be able to clean that known task directory.
        if result.returncode and path.exists() and os.access(path.parent, os.W_OK | os.X_OK):
            with contextlib.suppress(OSError):
                shutil.rmtree(path)
        if path.exists():
            remaining.append(f"worktree {path}")
    if not remaining:
        subprocess.run(
            ["git", "-C", str(root), "worktree", "prune"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
    branch = f"orrery/{task_id}"
    exists = subprocess.run(
        ["git", "-C", str(root), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False, timeout=PLUMBING_TIMEOUT_SECONDS,
    ).returncode == 0
    if exists:
        result = subprocess.run(
            ["git", "-C", str(root), "branch", "-D", branch],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
            timeout=PLUMBING_TIMEOUT_SECONDS,
        )
        if result.returncode:
            remaining.append(f"branch {branch}")
    return remaining
