"""Governed memory: facts that can be re-checked, and decisions.

The handoff's own gap analysis is the design brief here: many systems
call a vector store memory, and what an engineering memory needs is
provenance, correction and expiry. So a fact in this store is a claim
*and the command that checks it*. Nothing is admitted that cannot be
re-run, which makes provenance and expiry properties the runner
establishes rather than metadata a writer chose for itself.

The trust boundary is the whole of the security design and it runs in
two places, because a fact has two halves and both reach somewhere
dangerous.

- The command is executed by the runner, outside the delegate's
  containment. It is therefore never taken from a delegate: admission
  copies it from a sealed contract's own acceptance criterion, by
  digest, or the operator types it.
- The claim is concatenated into a later delegate's assignment. Prose
  is not made inert by the provider-text filter, which drops control
  bytes and breaks Orrery's protocol token and leaves ordinary English
  exactly as written, so an admitted provider-authored claim would be
  prompt injection in a trusted position. The claim is therefore
  operator-written, or copied from an operator-authored contract field.

A delegate may still propose. A proposal is a message to the operator:
stored, displayed, and permanently tainted. Neither half of it can
become part of a fact.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orrery_ledger import (
    LedgerError,
    _atomic_write,
    control_lock,
    read_ledger,
    validate_contract,
)


FACT_ID = re.compile(r"^M-[0-9]+$")
PROPOSAL_ID = re.compile(r"^P-[0-9]+$")
DECISION_ID = re.compile(r"^D-[0-9]+$")
DEFAULT_REVIEW_DAYS = 90
CLAIM_LIMIT = 2000
COMMAND_LIMIT = 4000

STATUSES = ("current", "stale", "refuted", "proposed")


class MemoryError(LedgerError):
    """A memory-store operational or validation error."""


def store_dir(root: Path) -> Path:
    return root / ".orrery" / "memory"


def _facts_dir(root: Path) -> Path:
    return store_dir(root) / "facts"


def _proposals_dir(root: Path) -> Path:
    return store_dir(root) / "proposals"


def _decisions_dir(root: Path) -> Path:
    return root / ".orrery" / "decisions"


def _next_id(directory: Path, prefix: str) -> str:
    """The next free identifier, from what is already on disk.

    Deliberately not a counter file: the store is small, the directory
    is the truth, and one fewer file is one fewer thing to leave torn.

    Identifiers already named by a `superseded_by` link count as taken
    even when their record is absent. A crash between the back link and
    the successor leaves such a dangling name, and reusing it would
    attach an unrelated later fact to an older one as its successor.
    """
    highest = 0
    if directory.is_dir():
        for entry in directory.glob(f"{prefix}-*.json"):
            try:
                highest = max(highest, int(entry.stem.split("-", 1)[1]))
            except (IndexError, ValueError):
                continue
            try:
                named = json.loads(entry.read_text()).get("superseded_by")
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if isinstance(named, str) and named.startswith(f"{prefix}-"):
                with contextlib.suppress(IndexError, ValueError):
                    highest = max(highest, int(named.split("-", 1)[1]))
    return f"{prefix}-{highest + 1}"


def _checked_id(identity: Any, pattern: re.Pattern[str], field: str) -> str:
    """An identifier that is safe to put in a path.

    `--supersedes /tmp/victim` used to be interpolated straight into a
    filename, so an unvalidated value could read or overwrite a version
    1 JSON document anywhere the user can write.
    """
    if not isinstance(identity, str) or not pattern.match(identity):
        raise MemoryError(f"{field} is not a valid identifier: {identity!r}")
    return identity


def _confined(path: Path, root: Path) -> Path:
    """Refuse a path that leaves the store, by any component.

    Checking only the final name and its immediate parent let a
    symlinked ancestor redirect every read and write out of the store.
    """
    resolved = path.resolve()
    base = root.resolve()
    if resolved != base and base not in resolved.parents:
        raise MemoryError(f"memory path leaves the store: {path}")
    for component in [path, *path.parents]:
        if component.is_symlink():
            raise MemoryError(f"memory path is a symlink: {component}")
        if component == root:
            break
    return path


def _read(path: Path, root: Path | None = None) -> dict[str, Any]:
    try:
        if root is not None:
            _confined(path, root)
        elif path.is_symlink():
            raise MemoryError(f"memory record is a symlink: {path}")
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise MemoryError(f"{path.stem} does not exist") from exc
    except (OSError, json.JSONDecodeError) as exc:
        # Absent and unreadable are different: one is an empty store and
        # the other is corruption, and a corrupt store must never be
        # reported as simply having nothing in it.
        raise MemoryError(f"{path.stem} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise MemoryError(f"{path.stem} is not a version 1 record")
    # A record must say it is the record its filename claims, so a file
    # dropped into the store under a chosen name cannot impersonate one.
    if data.get("id") != path.stem:
        raise MemoryError(
            f"{path.stem} holds a record identifying itself as {data.get('id')!r}"
        )
    return data


def _write(path: Path, record: dict[str, Any], root: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _confined(path, root)
    _atomic_write(
        path, (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode(), 0o600
    )


def fact_digest(fact: dict[str, Any]) -> str:
    """What a verification run is pinned to.

    The verification outcome is excluded, so recording a result does not
    change the digest that result was taken against.
    """
    body = {key: value for key, value in fact.items() if key != "last_verified"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MemoryError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise MemoryError(f"{field} must be at most {limit} characters")
    return value


def _review_by(days: int | None) -> str:
    span = DEFAULT_REVIEW_DAYS if days is None else days
    if not isinstance(span, int) or isinstance(span, bool) or not 1 <= span <= 3650:
        raise MemoryError("--review-days must be an integer from 1 to 3650")
    return (datetime.now(timezone.utc).date() + timedelta(days=span)).isoformat()


def criterion_source(
    root: Path, store: Path, task_id: str, criterion_id: str
) -> dict[str, Any]:
    """The operator-authored claim and command behind one criterion.

    Both halves come from the sealed contract, and the ledger's digest
    for it is recorded, so an admitted fact names exactly which sealed
    bytes it was taken from. This is the only route by which a command
    enters the store without the operator typing it, and it is not a
    delegate-controlled one: a contract is sealed read-only at creation
    and amendment retains what it replaced.
    """
    records, torn, _boundary = read_ledger(store, task_id)
    if torn:
        raise MemoryError(f"{task_id} has a torn ledger tail; run status --repair")
    digest = records[-1]["contract_digest"]
    # The bytes are hashed, not merely loaded. Labelling a claim and a
    # command with a digest that was never checked against them would
    # launder whatever is on disk as sealed operator text, which is the
    # one thing this provenance is supposed to establish.
    path = store / "contracts" / f"{task_id}.json"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise MemoryError(f"{task_id} has an unreadable contract") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise MemoryError(
            f"{task_id}'s contract does not match the digest its ledger "
            "records; amend the task before taking a fact from it"
        )
    # The bytes that were hashed, parsed. Re-reading through
    # load_contract left a window in which the file could be swapped
    # between the hash and the parse, admitting attacker-controlled text
    # under the digest of the legitimate contract.
    contract = validate_contract(json.loads(data), task_id)
    for criterion in contract["acceptance_criteria"]:
        if criterion["id"] != criterion_id:
            continue
        return {
            "claim": criterion["statement"],
            "command": criterion["verification"]["command"],
            "workdir": criterion["verification"].get("workdir"),
            "source": {
                "task_id": task_id,
                "criterion": criterion_id,
                "contract_digest": digest,
            },
        }
    raise MemoryError(f"{task_id} has no acceptance criterion {criterion_id}")


def add_fact(
    root: Path,
    *,
    claim: str | None,
    command: str | None,
    workdir: str | None,
    from_task: str | None,
    criterion: str | None,
    review_days: int | None = None,
    anchor_commit: str | None = None,
    supersedes: str | None = None,
) -> str:
    """Admit a fact from a contract criterion or from operator text.

    There is no third route. In particular there is no route that reads
    a proposal, because a proposal is delegate-controlled text and both
    halves of a fact reach somewhere a delegate must not.
    """
    if from_task or criterion:
        if not (from_task and criterion):
            raise MemoryError("--from-task and --criterion are given together")
        if claim or command:
            raise MemoryError(
                "a contract criterion supplies both claim and command; "
                "--claim and --command are for facts the operator writes"
            )
    elif not (claim and command):
        raise MemoryError(
            "a fact needs a claim and the command that checks it: give "
            "--from-task with --criterion, or both --claim and --command"
        )

    review_by = _review_by(review_days)
    with control_lock(root) as store:
        if from_task:
            fields = criterion_source(root, store, from_task, criterion or "")
        else:
            fields = {
                "claim": _text(claim, "--claim", CLAIM_LIMIT),
                "command": _text(command, "--command", COMMAND_LIMIT),
                "workdir": workdir,
                "source": {"operator": True},
            }
        if supersedes is not None:
            supersedes = _checked_id(supersedes, FACT_ID, "--supersedes")
            older = _read(_facts_dir(root) / f"{supersedes}.json", store_dir(root))
            # Only a leaf. Superseding an already-superseded fact forks
            # the chain, and after verification both successors read as
            # current and both reach a handoff.
            if older.get("superseded_by"):
                raise MemoryError(
                    f"{supersedes} was already superseded by "
                    f"{older['superseded_by']}; supersede that instead"
                )
        identity = _next_id(_facts_dir(root), "M")
        fact = {
            "v": 1,
            "id": identity,
            "revision": 1,
            **fields,
            "asserted_by": "operator",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "review_by": review_by,
            "anchor_commit": anchor_commit,
            "last_verified": None,
            "supersedes": supersedes,
            "superseded_by": None,
        }
        # The back link first, then the record it points at. A crash
        # between the two then leaves an older fact naming a successor
        # that does not exist, which reads as stale and is repaired by
        # writing the successor; the reverse order would leave two
        # unsuperseded facts, both current, both in every handoff.
        if supersedes is not None:
            older = _read(_facts_dir(root) / f"{supersedes}.json", store_dir(root))
            older["superseded_by"] = identity
            older["revision"] = int(older.get("revision", 1)) + 1
            _write(_facts_dir(root) / f"{supersedes}.json", older, store_dir(root))
        _write(_facts_dir(root) / f"{identity}.json", fact, store_dir(root))
        return identity


def propose_fact(
    root: Path,
    *,
    claim: str,
    command: str | None,
    task_id: str | None,
    origin: str = "delegate",
    criterion: str | None = None,
    anchor_commit: str | None = None,
) -> str:
    """Record what a delegate suggested, without believing any of it.

    Stored so an operator can read it and decide. Never verified, never
    handed to another delegate, and never the source of a fact's text:
    admission ignores this record entirely and takes both halves from a
    sealed contract or from the operator.
    """
    with control_lock(root):
        identity = _next_id(_proposals_dir(root), "P")
        _write(
            _proposals_dir(root) / f"{identity}.json",
            {
                "v": 1,
                "id": identity,
                "claim": _text(claim, "claim", CLAIM_LIMIT),
                "command": command,
                "task_id": task_id,
                "criterion": criterion,
                "origin": origin,
                "anchor_commit": anchor_commit,
                "asserted_by": origin,
                "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "note": (
                    "delegate-authored text; neither half may be copied into "
                    "a fact"
                    if origin == "delegate"
                    else "a reference to a sealed contract criterion; "
                    "admission re-reads both halves from the contract"
                ),
            },
            store_dir(root),
        )
        return identity


def read_facts(root: Path) -> list[dict[str, Any]]:
    directory = _facts_dir(root)
    if not directory.is_dir():
        return []
    return [
        _read(path, store_dir(root))
        for path in sorted(directory.glob("M-*.json"), key=_numeric_stem)
        if _checked_id(path.stem, FACT_ID, "fact file")
    ]


def read_proposals(root: Path) -> list[dict[str, Any]]:
    directory = _proposals_dir(root)
    if not directory.is_dir():
        return []
    return [
        _read(path, store_dir(root))
        for path in sorted(directory.glob("P-*.json"), key=_numeric_stem)
        if _checked_id(path.stem, PROPOSAL_ID, "proposal file")
    ]


def _numeric_stem(path: Path) -> int:
    try:
        return int(path.stem.split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def status_of(fact: dict[str, Any], today: date | None = None) -> str:
    """Current, stale or refuted, from what was recorded.

    Not a score. A number between zero and one would have to come from
    somewhere, and there is nowhere for it to come from.
    """
    if fact.get("superseded_by"):
        return "stale"
    verified = fact.get("last_verified")
    if not isinstance(verified, dict):
        return "stale"
    if verified.get("outcome") != "passed":
        return "refuted"
    try:
        review_by = date.fromisoformat(str(fact.get("review_by")))
    except ValueError:
        return "stale"
    return "current" if (today or _today()) <= review_by else "stale"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def current_facts(root: Path) -> list[dict[str, Any]]:
    """Exactly what a delegate may be told, and nothing else."""
    return [fact for fact in read_facts(root) if status_of(fact) == "current"]


def record_verification(
    root: Path,
    fact_id: str,
    *,
    expected_digest: str,
    expected_revision: int,
    commit: str,
    outcome: str,
    detail: str = "",
    target_reader: Any = None,
) -> dict[str, Any]:
    """Write a verification result, but only against what it tested.

    The command runs outside the control lock, because an arbitrary
    command holding it would stop every task operation for its duration.
    That means the fact may be superseded or rewritten while the command
    runs, so the result is admitted only if the fact is still the one
    that was tested. A stale result is reported, never written.
    """
    with control_lock(root):
        path = _facts_dir(root) / f"{_checked_id(fact_id, FACT_ID, 'fact')}.json"
        fact = _read(path, store_dir(root))
        if int(fact.get("revision", 1)) != expected_revision or fact_digest(fact) != expected_digest:
            raise MemoryError(
                f"{fact_id} changed while its command ran; the result was "
                "discarded rather than recorded against a different fact"
            )
        # Read here, under the lock, rather than handed in: a value
        # sampled by the caller before acquiring the lock is already
        # stale, and a merge landing in that window would have its
        # result dated to a commit the command never ran against.
        target_now = target_reader() if target_reader is not None else None
        if target_now is not None and target_now != commit:
            raise MemoryError(
                f"the target moved from {commit[:12]} to {target_now[:12]} "
                f"while {fact_id} was being checked; the result was discarded"
            )
        fact["last_verified"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "commit": commit,
            "outcome": outcome,
            "detail": detail,
        }
        fact["revision"] = expected_revision + 1
        _write(path, fact, store_dir(root))
        return fact


def write_decision(
    root: Path, fields: dict[str, Any], evidence: list[dict[str, Any]]
) -> str:
    with control_lock(root):
        supersedes = fields.get("supersedes")
        if supersedes:
            supersedes = _checked_id(supersedes, DECISION_ID, "--supersedes")
            fields = dict(fields, supersedes=supersedes)
            existing = _read(_decisions_dir(root) / f"{supersedes}.json", _decisions_dir(root))
            if existing.get("superseded_by"):
                raise MemoryError(
                    f"{supersedes} was already superseded by "
                    f"{existing['superseded_by']}; supersede that instead"
                )
        identity = _next_id(_decisions_dir(root), "D")
        record = {
            "v": 1,
            "id": identity,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "evidence": evidence,
            "superseded_by": None,
            **fields,
        }
        # The back link first, as facts do. A crash after it leaves an
        # older decision naming a successor that does not exist, which
        # reads as superseded and is repaired by writing the successor;
        # the reverse order leaves two live decisions and a later fork.
        if supersedes:
            older = _read(_decisions_dir(root) / f"{supersedes}.json", _decisions_dir(root))
            older["superseded_by"] = identity
            older["status"] = "Superseded"
            _write(_decisions_dir(root) / f"{supersedes}.json", older, _decisions_dir(root))
        _write(_decisions_dir(root) / f"{identity}.json", record, _decisions_dir(root))
        return identity


def read_decisions(root: Path) -> list[dict[str, Any]]:
    directory = _decisions_dir(root)
    if not directory.is_dir():
        return []
    return [
        _read(path, _decisions_dir(root))
        for path in sorted(directory.glob("D-*.json"), key=_numeric_stem)
        if _checked_id(path.stem, DECISION_ID, "decision file")
    ]


def facts_block(facts: list[dict[str, Any]]) -> str:
    """How current facts appear in a handoff.

    Every line is operator-authored: a claim is either typed by the
    operator or copied from a sealed contract's own criterion statement.
    Nothing a provider wrote reaches this text, which is the reason the
    block can be stated plainly rather than fenced off as untrusted.
    """
    if not facts:
        return ""
    lines = [
        "Verified repository facts. Each was checked by running the "
        "command beside it, on the date given.",
    ]
    for fact in facts:
        verified = fact.get("last_verified") or {}
        lines.append(
            f"- {fact['claim']}\n"
            f"  Checked by: {fact['command']}\n"
            f"  Last verified: {str(verified.get('at', 'never'))[:10]} "
            f"at {str(verified.get('commit', 'unknown'))[:12]}"
        )
    return "\n".join(lines)
