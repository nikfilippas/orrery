#!/usr/bin/env python3
"""The provider allowance ceiling and the spend rollup it is measured on.

Nothing bounded what the principal itself spent. The task ledger cannot
see it: `orrery_ledger` keeps its store under `<repo>/.orrery`, one per
repository and per task, holding delegate attempt spend for a single
contract and no principal turns at all, with no rolling window of any
kind. So the ceiling reads a rollup of its own, in the shared state
directory, keyed by provider and model, and every enforcement point
reads that one file.

What the rollup measures is local spend from three sources: every
Claude transcript under the Claude configuration directory, subagent
and workflow-agent transcripts nested beneath a session included, Codex
rollout files under CODEX_HOME, and the `spend` records of the incident
log. A delegated run appears in no transcript or rollout, by
construction, since it is launched with `--no-session-persistence` or
`--ephemeral`; what it spent is known only from the `spend` record its
wrapper writes on the way out, so that record is read here. A run under
`--receipts`, as `orrery-task` dispatches, writes the same record as
well as its task's attempt record, both from one parse of its log, so
a task dispatch is measured here without the per-repository ledger
ever being read. A record whose usage was not recovered carries no
counts and adds nothing; one recovered from a run that died before its
result is a floor, marked `partial`.

Five properties are load-bearing.

Responses are deduplicated by identity, never by byte offset alone. A
session resumed into a second transcript replays the responses it
already recorded, so offsets alone overcount, and an overcount is the
direction that refuses work the allowance would have covered. Every
counted response leaves a digest of its `(message id, requestId)` pair
beside the per-file offsets, and a file whose recorded offset exceeds
its current size is read again from the start.

A model reaches a provider through `model_identity`, against the
first-party catalogue. The manifest names a provider, a transcript
records an API identifier such as `claude-fable-5-1`, and nothing else
maps between them. A model the catalogue does not know is counted but
attributed to no provider: the directory a transcript sits in says which
CLI wrote it, not which account paid for it, and the Claude CLI can be
pointed at a third-party endpoint. That spend is kept under an empty provider so it
is visible rather than silently absent.

A delegated run is counted once however the incident log rotates. Its
record's identity is the wrapper's process run id with the record's own
millisecond timestamp, since a transient retry or an approved fallback
re-enters the wrapper in the same process and writes a second record
under the same run id. Rotation renames the live log to the previous
one, so an offset is trusted only while the file under a name is still
the file it was taken in, by inode and by a digest of its first line,
since a freed inode is reused; otherwise the file is read again from
the start and the identities discard what was already counted.

The total counts cache reads. It sums the same four token classes
`orrery-usage` reports, and cache reads dominate real figures by an
order of magnitude, so a ceiling set from a provider's own headline
number would mean something else entirely.

Spend is bucketed by hour rather than by response. This file is written
on every session start, and 1.3 GiB of local transcripts is an ordinary
amount, so a row per response would make the state grow with exactly the
activity the ceiling exists to notice. Hours retire on their own and
carry their identities out with them, which bounds both. The cost is a
window edge no finer than an hour, which is well inside the fidelity of
a measurement that is a local estimate rather than the provider's
accounting.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orrery_incidents import (  # noqa: E402
    previous_path,
    store_dir,
    store_path,
    valid_event,
)
from orrery_runtime import (  # noqa: E402
    PROVIDERS,
    Role,
    RuntimeConfigError,
    adopted_root,
    codex_home,
    effective_manifest,
    load_catalogue,
    load_role,
    user_config_path,
)
from orrery_model_catalogue import _bare_claude_id, model_identity  # noqa: E402


# 2: a versioned model the catalogue lists only by its family alias is
# filed under its own id, so hours stored under the alias are rebuilt.
# 3: a versioned Claude id is filed under its bare form. A version-2
# file is re-keyed in place, since each old key alone decides its new
# spelling, and its sources are kept.
ROLLUP_VERSION = 3
ROLLUP_NAME = "allowance.json"
LOCK_NAME = "allowance.lock"

# The four classes `orrery-usage` sums into its own total, so a ceiling
# is expressed in units the user can already read off that report.
TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)

# How far back the rollup keeps anything. Only the configured windows
# have to be answerable, and identities live exactly as long as the hour
# they deduplicate, so this bounds the file: a response older than the
# horizon is neither counted nor remembered.
DEFAULT_RETENTION_DAYS = 8
MAX_WINDOW_DAYS = 90

# What a crossing does to the principal's own turn. The first is the
# default, and the enforcing one is opt-in.
ON_EXCEEDED_MODES = ("warn", "block")

# Enough of a digest that a collision is not a practical concern at any
# plausible response count, and short enough that a busy week of
# identities stays a few hundred kilobytes.
IDENTITY_DIGITS = 16

# Only lines that could carry what each parser wants are decoded. A
# transcript can be tens of megabytes and most of it is neither an
# assistant response nor a token count.
CLAUDE_MARKER = b'"usage"'
CODEX_MARKERS = (b"token_count", b"turn_context")
SPEND_MARKER = b'"spend"'

# The token classes a `spend` record carries, as `orrery_spend` names
# them: the same four `TOKEN_FIELDS` sums, under the wrapper's names.
SPEND_FIELDS = ("fresh_in", "cache_read", "cache_write", "output")


@dataclass(frozen=True)
class Allowance:
    provider: str
    tokens: int
    window_days: int


def rollup_path() -> Path:
    """The one accounting source, beside the incident store."""
    return store_dir() / ROLLUP_NAME


def load_allowances(manifest: dict[str, Any] | None = None) -> dict[str, Allowance]:
    """The configured ceilings, keyed by provider.

    Keyed by provider and not by model because the measured constraint
    is account-wide: Anthropic's own rate-limit telemetry reports one
    five-hour and one seven-day window for the account with no per-model
    breakdown, so a per-model key would sum a fraction of the pool and
    pass while the real limit had been reached.
    """
    if manifest is None:
        manifest = effective_manifest()
    raw = manifest.get("allowances", {})
    if not isinstance(raw, dict):
        raise RuntimeConfigError("the manifest allowances must be an object")
    allowances: dict[str, Allowance] = {}
    for provider, entry in raw.items():
        if provider not in PROVIDERS:
            raise RuntimeConfigError(
                "the manifest has an allowance for an unknown provider: "
                f"{provider!r}"
            )
        if not isinstance(entry, dict):
            raise RuntimeConfigError(f"the {provider} allowance must be an object")
        unknown = set(entry) - {"tokens", "window_days"}
        if unknown:
            raise RuntimeConfigError(
                f"the {provider} allowance has unknown fields: {sorted(unknown)}"
            )
        tokens = entry.get("tokens")
        window_days = entry.get("window_days")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
            raise RuntimeConfigError(
                f"the {provider} allowance needs a positive token ceiling"
            )
        if (
            isinstance(window_days, bool)
            or not isinstance(window_days, int)
            or not 1 <= window_days <= MAX_WINDOW_DAYS
        ):
            raise RuntimeConfigError(
                f"the {provider} allowance window_days must be an integer "
                f"between 1 and {MAX_WINDOW_DAYS}"
            )
        allowances[provider] = Allowance(provider, tokens, window_days)
    return allowances


def on_exceeded(manifest: dict[str, Any] | None = None) -> str:
    """What a crossing does to a principal turn: warn, or block.

    Warning is the default because the accounting can overcount. A
    session resumed into a second transcript replays responses the first
    one recorded, and while identity deduplication is what stops that
    becoming spend, an overcount is the direction that refuses work the
    allowance would have covered. In warn mode that costs a sentence; in
    block mode it costs the session.
    """
    if manifest is None:
        manifest = effective_manifest()
    value = manifest.get("on_exceeded", ON_EXCEEDED_MODES[0])
    if value not in ON_EXCEEDED_MODES:
        raise RuntimeConfigError(
            "the manifest on_exceeded must be "
            f"{' or '.join(ON_EXCEEDED_MODES)}"
        )
    return str(value)


def retention_seconds(allowances: dict[str, Allowance]) -> float:
    """How far back hours are kept: the widest window, with a day's slack."""
    widest = max(
        (allowance.window_days for allowance in allowances.values()), default=0
    )
    return max(DEFAULT_RETENTION_DAYS, widest + 1) * 86400.0


def canonical_model(provider: str, model: str) -> str:
    """The one spelling a model's spend is filed under.

    One Claude model reaches the logs under several ids: with and
    without its snapshot date, packaged for Bedrock or Vertex, with a
    `[1m]` window. Filed under each, one model would be several lines of
    spend under one name, so a versioned id is filed under its bare
    form, `claude-haiku-4-5`. An alias, an OpenAI id and a custom
    literal already name one thing each and are kept as they are.

    An inference-profile ARN or a `-latest` id is not stripped to that
    form. The resolver files one under the key of the catalogue entry
    for its version where there is one; otherwise it keeps a key of its
    own, and only the breakdown shows it, the provider's total unchanged.
    """
    if model_identity(provider, model).version is None:
        return model
    return _bare_claude_id(model)


def model_resolver(manifest: dict[str, Any] | None = None) -> Any:
    """A memoised `raw model id -> provider/model key` lookup.

    The catalogue is the authority on which provider owns which model,
    and `model_identity` is what reaches a catalogue entry from the API
    identifier a transcript records. An entry naming the exact version
    is preferred over an alias that only shares its family, so two
    versions listed side by side each keep their own spend, and a
    version reached only through its family's alias keeps its own id:
    the alias names the provider. Either way a versioned id is keyed by
    `canonical_model`, so each spelling it strips shares the model's one
    key, and an entry's own id is kept only where it is that spelling
    already. Memoised because a busy window holds tens of thousands of
    responses and the catalogue is a file.

    The manifest's own role assignments are consulted after it. A
    bundled catalogue goes stale between releases, and a role the user
    has assigned is their own statement of which provider serves that
    model: measured here, a `gpt-6-astra` session the catalogue had
    never heard of reached no provider at all and its spend counted
    towards no allowance. A role routed at an endpoint is excluded,
    because that names a third-party service and its billing is not the
    first-party allowance being measured.
    """
    try:
        catalogue = load_catalogue()
    except RuntimeConfigError:
        catalogue = {}
    known = [
        (provider, str(entry["id"]))
        for provider in sorted(catalogue)
        for entry in catalogue[provider]
        if isinstance(entry.get("id"), str) and entry["id"]
    ]
    assigned: list[tuple[str, str]] = []
    try:
        steps = (effective_manifest() if manifest is None else manifest).get("steps")
    except RuntimeConfigError:
        steps = None
    if isinstance(steps, list):
        assigned.extend(
            (step["provider"], step["model"])
            for step in steps
            if isinstance(step, dict)
            and step.get("provider") in PROVIDERS
            and isinstance(step.get("model"), str)
            and step["model"]
            and step.get("endpoint") is None
        )
    cache: dict[str, str] = {}

    def owner(model: str) -> tuple[str, str]:
        # The catalogue is searched in full, exact then family, before
        # any manifest step: preferring exactness across both would let
        # a step assigning a model to the wrong provider outrank the
        # catalogue's own owner of it. A family match settles only the
        # provider: a versioned id is filed under itself, not under the
        # alias, or Sonnet 5 would read as "Sonnet, version not
        # recorded" merely because the bundled catalogue lists no row
        # for that version.
        for entries in (known, assigned):
            for exact in (True, False):
                for provider, name in entries:
                    entry = model_identity(provider, name)
                    active = model_identity(provider, model)
                    if (entry == active) if exact else entry.matches(active):
                        if not exact and active.version is not None:
                            return provider, canonical_model(provider, model)
                        return provider, canonical_model(provider, name)
        return "", model

    def resolve(model: str) -> str:
        if model not in cache:
            provider, name = owner(model)
            cache[model] = f"{provider}/{name}"
        return cache[model]

    return resolve


def split_key(key: str) -> tuple[str, str]:
    """A bucket key back into its provider and model.

    Split on the first separator only: a provider never contains one and
    a model identifier may.
    """
    provider, _found, model = key.partition("/")
    return provider, model


def claude_projects_root() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(raw) if raw else Path.home() / ".claude"
    return base / "projects"


def source_files() -> list[tuple[str, Path]]:
    """Every local spend log, with the parser each one needs.

    Claude transcripts are found at any depth: a session's subagents
    write theirs under `<session>/subagents/`, and a workflow's agents
    under `<session>/subagents/workflows/<run>/`, and neither response
    is recorded in the parent transcript. The previous incident log is
    listed before the live one, so a rotation reads in write order.
    """
    found: list[tuple[str, Path]] = []
    with contextlib.suppress(OSError):
        found.extend(
            ("claude", path)
            for path in sorted(claude_projects_root().rglob("*.jsonl"))
        )
    try:
        sessions = codex_home() / "sessions"
    except RuntimeConfigError:
        sessions = None
    if sessions is not None:
        with contextlib.suppress(OSError):
            found.extend(
                ("codex", path)
                for path in sorted(sessions.rglob("rollout-*.jsonl"))
            )
    found.extend(("incidents", path) for path in (previous_path(), store_path()))
    return found


def empty_rollup() -> dict[str, Any]:
    return {"v": ROLLUP_VERSION, "updated": 0.0, "sources": {}, "hours": {}}


def hour_of(moment: float) -> str:
    return str(int(moment // 3600))


def identity_digest(message_id: str, request_id: str) -> str:
    return hashlib.sha256(
        f"{message_id}\x00{request_id}".encode()
    ).hexdigest()[:IDENTITY_DIGITS]


def _integer(value: Any) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def valid_hour(entry: Any) -> dict[str, Any] | None:
    """One stored hour, or None where the record cannot be trusted.

    A torn write or a foreign document is skipped rather than read as
    spend: an invented total is the one error that refuses work the
    allowance would have covered.
    """
    if not isinstance(entry, dict):
        return None
    spend = entry.get("spend")
    seen = entry.get("seen")
    if not isinstance(spend, dict) or not isinstance(seen, list):
        return None
    kept: dict[str, int] = {}
    for key, total in spend.items():
        provider, model = split_key(key) if isinstance(key, str) else ("?", "")
        if provider and provider not in PROVIDERS:
            return None
        if not model or isinstance(total, bool) or not isinstance(total, int):
            return None
        if total < 0:
            return None
        kept[key] = total
    return {
        "spend": kept,
        "seen": [
            value
            for value in seen
            if isinstance(value, str) and len(value) == IDENTITY_DIGITS
        ],
    }


def _rekeyed(spend: dict[str, int]) -> dict[str, int]:
    """A version-2 hour's spend under the keys a refresh files it by now.

    Version 2 kept a versioned Claude id as it was reported, context
    suffix removed, or as a catalogue entry spells it. Its
    `canonical_model` spelling is the key the same spend is filed under
    now, so the spellings of one model are summed into it.
    """
    kept: dict[str, int] = {}
    for key, total in spend.items():
        provider, model = split_key(key)
        key = f"{provider}/{canonical_model(provider, model)}"
        kept[key] = kept.get(key, 0) + total
    return kept


def read_rollup(path: Path | None = None) -> dict[str, Any]:
    """The stored rollup, or an empty one where it cannot be read.

    Unreadable is treated as empty rather than as an error: this is an
    estimate consulted to decide whether to stop, so a corrupt file must
    not itself become a refusal. The next refresh rebuilds what is still
    in the session logs, which are the durable record.

    A version-2 file is re-keyed rather than discarded: its keys differ
    from the current ones only in spelling, which each key alone decides.
    Discarding it would read every log again from the start and lose
    what no log still holds: a deleted transcript's spend, and each Codex
    rollout's baseline, without which growth already counted from a
    rollout begun before the window is dropped. Any other version is
    discarded.
    """
    target = rollup_path() if path is None else path
    try:
        stored = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return empty_rollup()
    if not isinstance(stored, dict) or stored.get("v") not in (2, ROLLUP_VERSION):
        return empty_rollup()
    rollup = empty_rollup()
    updated = stored.get("updated")
    if isinstance(updated, (int, float)) and not isinstance(updated, bool):
        rollup["updated"] = float(updated)
    sources = stored.get("sources")
    if isinstance(sources, dict):
        for name, record in sources.items():
            if not isinstance(name, str) or not isinstance(record, dict):
                continue
            kept: dict[str, Any] = {}
            for field in ("offset", "counted", "inode"):
                value = record.get(field)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                ):
                    kept[field] = value
            if "offset" not in kept:
                continue
            for field in ("model", "head"):
                if isinstance(record.get(field), str):
                    kept[field] = record[field]
            rollup["sources"][name] = kept
    hours = stored.get("hours")
    if isinstance(hours, dict):
        for name, entry in hours.items():
            if not isinstance(name, str) or not name.lstrip("-").isdigit():
                continue
            validated = valid_hour(entry)
            if validated is not None:
                if stored["v"] == 2:
                    validated["spend"] = _rekeyed(validated["spend"])
                rollup["hours"][name] = validated
    return rollup


@contextlib.contextmanager
def _locked_rollup(path: Path) -> Iterator[None]:
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


def write_rollup(rollup: dict[str, Any], path: Path | None = None) -> None:
    target = rollup_path() if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        json.dump(rollup, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def _lines(handle: Any, offset: int) -> Iterator[tuple[bytes, int]]:
    """Whole lines after `offset`, each with the offset it ends at.

    A slice cut at an arbitrary byte can end mid-line, so a line without
    its terminator is left unread and its bytes are not consumed; the
    next refresh sees it complete. Streamed rather than read whole
    because one transcript can be tens of megabytes.
    """
    handle.seek(offset)
    consumed = offset
    for line in handle:
        if not line.endswith(b"\n"):
            return
        consumed += len(line)
        yield line, consumed


def _scan_claude(
    handle: Any,
    offset: int,
    horizon: float,
    resolve: Any,
) -> tuple[list[tuple[float, str, int, str]], int]:
    """Counted responses after `offset`, and the offset they end at."""
    rows: list[tuple[float, str, int, str]] = []
    consumed = offset
    for raw, position in _lines(handle, offset):
        consumed = position
        if CLAUDE_MARKER not in raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict) or data.get("type") != "assistant":
            continue
        message = data.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        total = sum(_integer(usage.get(field)) for field in TOKEN_FIELDS)
        if not total:
            # Synthetic bookkeeping messages carry no tokens. Skipped
            # before the identity is taken, so a zero-token twin cannot
            # swallow the counted response's identity.
            continue
        stamp = _timestamp(data.get("timestamp"))
        if stamp is None or stamp < horizon:
            continue
        identifier = message.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        model = message.get("model")
        if not isinstance(model, str) or not model:
            model = "unknown"
        rows.append(
            (
                stamp,
                resolve(model),
                total,
                identity_digest(identifier, str(data.get("requestId", ""))),
            )
        )
    return rows, consumed


def _head_digest(handle: Any) -> str | None:
    """A digest of the file's first line, or None until it is complete.

    Every incident line carries a millisecond timestamp and a process
    run id, so the first line names the file as its inode cannot.
    """
    handle.seek(0)
    first = handle.readline()
    if not first.endswith(b"\n"):
        return None
    return hashlib.sha256(first).hexdigest()[:IDENTITY_DIGITS]


def _scan_incidents(
    handle: Any,
    offset: int,
    horizon: float,
    resolve: Any,
) -> tuple[list[tuple[float, str, int, str]], int]:
    """Counted delegated runs after `offset`, and the offset they end at."""
    rows: list[tuple[float, str, int, str]] = []
    consumed = offset
    for raw, position in _lines(handle, offset):
        consumed = position
        if SPEND_MARKER not in raw:
            continue
        try:
            event = valid_event(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if event is None or event.get("kind") != "spend":
            continue
        total = sum(_integer(event.get(field)) for field in SPEND_FIELDS)
        if not total:
            # A run whose usage was not recovered carries no counts.
            continue
        stamp = _timestamp(event.get("ts"))
        if stamp is None or stamp < horizon:
            continue
        run = event.get("run")
        if not isinstance(run, str) or not run:
            continue
        rows.append(
            (
                stamp,
                spend_key(event, resolve),
                total,
                identity_digest(f"spend:{run}", event["ts"]),
            )
        )
    return rows, consumed


def spend_key(event: dict[str, Any], resolve: Any) -> str:
    """The bucket a `spend` record's run is counted under.

    The record names the provider the role was configured on, which is
    authoritative for a role with no endpoint, so the catalogue's key for
    the model is used only when it names that provider. A run routed
    at an endpoint drew on a third-party service, and is kept under an
    empty provider exactly as an uncatalogued transcript model is.

    The model is the one the provider reported running, where it
    reported exactly one of the configured model's family (a side-call
    model of another family is set aside): a transcript records that
    model, so a delegated `opus` run and a principal session on Opus
    5.5 then share one key, and Opus 5 and Opus 5.5 are never folded
    together under the alias. The configured name is used only where
    the run reported no single model of the family. Either one is keyed
    by its `canonical_model` spelling, as a transcript's model is.
    """
    model = event.get("model")
    if not isinstance(model, str) or not model:
        model = "unknown"
    provider = event.get("provider")
    if event.get("endpoint") is not None or provider not in PROVIDERS:
        return f"/{model}"
    model = single_reported_model(event) or model
    key = resolve(model)
    if split_key(key)[0] != provider:
        key = f"{provider}/{canonical_model(provider, model)}"
    return key


def single_reported_model(event: dict[str, Any]) -> str | None:
    """The one model a `spend` record says its run used, or None.

    Where the configured model has a family, only reported models of
    that family count: a Claude run also reports the small model its
    CLI makes side calls on (Haiku beside Opus), which says nothing
    about what the configured name ran as. The side calls' tokens stay
    in the run's figure. By identity, not by name: `claude-opus-5-5`
    and its `[1m]` window in one run are one model, named by the plain
    id. Two models of the family in one run, or none, say nothing
    unambiguous.
    """
    provider = event.get("provider")
    reported = event.get("reported_models")
    if not isinstance(provider, str) or not isinstance(reported, str):
        return None
    names = [name for name in reported.split(",") if name]
    configured = event.get("model")
    if isinstance(configured, str):
        family = model_identity(provider, configured).family
        if family is not None:
            names = [
                name
                for name in names
                if model_identity(provider, name).family == family
            ]
    if not names or len(
        {model_identity(provider, name) for name in names}
    ) != 1:
        return None
    plain = [name for name in names if not name.endswith("]")]
    return (plain or names)[0]


def _scan_codex(
    handle: Any,
    offset: int,
    record: dict[str, Any],
) -> tuple[float | None, int | None, float | None, int]:
    """The newest cumulative count after `offset`, and the live model.

    Also the earliest timestamp in the scanned region, which the caller
    needs on a first sight: a cumulative total says nothing about when
    it was spent, so whether the whole of it belongs inside the window
    can only be decided by when the rollout began.

    Codex records a running total for the session, so only the last
    complete count matters; summing the intermediate ones would multiply
    the truth. The model is carried in the source record because an
    incremental read need not contain the `turn_context` that set it,
    and the count is attributed to the model current when it was taken.
    """
    stamp: float | None = None
    total: int | None = None
    earliest: float | None = None
    consumed = offset
    for raw, position in _lines(handle, offset):
        consumed = position
        if not any(marker in raw for marker in CODEX_MARKERS):
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        payload = data.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type") or data.get("type")
        if kind == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                record["model"] = model
        elif kind == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("total_token_usage")
            if not isinstance(usage, dict) or not usage:
                continue
            cache_read = _integer(usage.get("cached_input_tokens"))
            fresh = max(0, _integer(usage.get("input_tokens")) - cache_read)
            total = _integer(usage.get("total_tokens")) or (
                fresh
                + cache_read
                + _integer(usage.get("cache_write_input_tokens"))
                + _integer(usage.get("output_tokens"))
            )
            moment = _timestamp(data.get("timestamp"))
            if moment is not None:
                stamp = moment
                if earliest is None:
                    earliest = moment
    return stamp, total, earliest, consumed


def _bucket(rollup: dict[str, Any], moment: float) -> dict[str, Any]:
    return rollup["hours"].setdefault(
        hour_of(moment), {"spend": {}, "seen": []}
    )


def _add(rollup: dict[str, Any], moment: float, key: str, total: int) -> None:
    spend = _bucket(rollup, moment)["spend"]
    spend[key] = spend.get(key, 0) + total


def refresh(
    *,
    now: float | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold every new session log and spend record in, and store it.

    Only files whose recorded offset no longer equals their size are
    opened, so a refresh on an unchanged machine reads no transcript at
    all; incident logs are the exception, opened to check their head. A
    file shorter than its recorded offset was truncated or replaced and
    is read from the start again.
    """
    moment = time.time() if now is None else now
    horizon = moment - retention_seconds(load_allowances(manifest))
    resolve = model_resolver(manifest)
    path = rollup_path()
    sources = source_files()

    with _locked_rollup(path):
        rollup = read_rollup(path)
        rollup["hours"] = {
            name: entry
            for name, entry in rollup["hours"].items()
            if (int(name) + 1) * 3600 >= horizon
        }
        seen = {
            digest
            for entry in rollup["hours"].values()
            for digest in entry["seen"]
        }
        for kind, source in sources:
            name = str(source)
            record = dict(rollup["sources"].get(name, {}))
            try:
                details = source.stat()
            except OSError:
                continue
            if kind == "incidents" and record.get("inode") != details.st_ino:
                # Rotation renames the live log over the previous one, so
                # an offset under either name may belong to another file.
                # Read from the start; the identities discard the rest.
                record = {"inode": details.st_ino}
            offset = record.get("offset")
            if offset is None:
                if details.st_mtime < horizon:
                    # Nothing in it can fall inside the window, so it is
                    # marked consumed without ever being opened.
                    rollup["sources"][name] = {
                        **record,
                        "offset": details.st_size,
                    }
                    continue
                offset = 0
            elif offset == details.st_size and kind != "incidents":
                # Not for an incident log: a different file under a reused
                # inode can happen to equal the old offset, and only its
                # head says so. The logs are small enough to open always.
                continue
            elif offset > details.st_size:
                # Truncated or replaced under the same name. `counted`
                # survives on purpose: it records what has already been
                # attributed, and resetting it would count a Codex
                # session's whole total a second time.
                offset = 0
            rows: list[tuple[float, str, int, str]] = []
            stamp: float | None = None
            total: int | None = None
            earliest: float | None = None
            try:
                with source.open("rb") as handle:
                    if kind == "claude":
                        rows, offset = _scan_claude(
                            handle, offset, horizon, resolve
                        )
                    elif kind == "incidents":
                        if os.fstat(handle.fileno()).st_ino != details.st_ino:
                            # Rotated between the stat and the open. The
                            # next refresh finds it under its new name.
                            continue
                        head = _head_digest(handle)
                        if head != record.get("head"):
                            # ext4 reuses a freed inode, so two rotations
                            # between refreshes can leave a different
                            # file under the recorded inode. Its first
                            # line says which file it is.
                            record = {"inode": details.st_ino}
                            offset = 0
                        if head is not None:
                            record["head"] = head
                        rows, offset = _scan_incidents(
                            handle, offset, horizon, resolve
                        )
                    else:
                        stamp, total, earliest, offset = _scan_codex(
                            handle, offset, record
                        )
            except OSError:
                continue
            record["offset"] = offset
            if kind != "codex":
                for moment_of, key, counted, digest in rows:
                    if digest in seen:
                        continue
                    seen.add(digest)
                    _bucket(rollup, moment_of)["seen"].append(digest)
                    _add(rollup, moment_of, key, counted)
            elif total is not None and stamp is not None:
                # A running total, so only its growth is new spend, and
                # the growth is attributed to the hour it was observed.
                first_sight = "counted" not in record
                delta = max(0, total - _integer(record.get("counted")))
                record["counted"] = total
                if first_sight and (earliest is None or earliest < horizon):
                    # A rollout carries a cumulative total and no
                    # per-response timestamp, so a first sight cannot say
                    # when the bulk of it was spent. Where the rollout
                    # began before the window, attributing the total to
                    # the hour it was noticed would drop a whole session
                    # lifetime inside the ceiling: a session running for
                    # three weeks would land three weeks of spend in a
                    # seven-day window, which is the overcount direction
                    # that locks a user out. The baseline is recorded
                    # without being attributed, and only growth counts
                    # from there.
                    #
                    # A rollout that began inside the window needs no such
                    # caution: all of it belongs there, so it is counted
                    # in full. That is the ordinary case for a session
                    # started after the ceiling was configured, and
                    # skipping it would undercount every new rollout
                    # rather than only the historical ones.
                    delta = 0
                if delta and stamp >= horizon:
                    _add(
                        rollup,
                        stamp,
                        resolve(str(record.get("model", "unknown"))),
                        delta,
                    )
            rollup["sources"][name] = record

        # A transcript the surface has deleted leaves nothing to read
        # again, so its offset is dropped rather than kept for ever.
        live = {str(source) for _kind, source in sources}
        rollup["sources"] = {
            name: record
            for name, record in rollup["sources"].items()
            if name in live
        }
        rollup["updated"] = moment
        try:
            write_rollup(rollup, path)
        except OSError:
            # A state directory that is full, unwritable, or on a
            # filesystem that refuses the chmod must not turn every
            # gated dispatch into a traceback. The measurement is still
            # correct for this call; only its persistence is lost, so
            # the next run rescans. `orrery_incidents.record` is
            # never-raises for the same directory and the same reason.
            pass
    return rollup


def window_spend(
    provider: str,
    window_days: int,
    *,
    rollup: dict[str, Any] | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Measured spend for one provider over its window.

    Every model on the provider sums into the one bucket, because the
    allowance is the account's and not a model's. An hour is included
    when it begins at or after the start of the window.
    """
    moment = time.time() if now is None else now
    since = moment - window_days * 86400.0
    source = read_rollup() if rollup is None else rollup
    total = 0
    models: dict[str, int] = {}
    for name, entry in source["hours"].items():
        if int(name) * 3600 < since:
            continue
        for key, spend in entry["spend"].items():
            owner, model = split_key(key)
            if owner != provider:
                continue
            total += spend
            models[model] = models.get(model, 0) + spend
    return {"provider": provider, "total": total, "models": models, "since": since}


def describe_models(models: dict[str, int], provider: str = "") -> str:
    """A window's spend by model, each id followed by the version it is.

    Opus 5 and Opus 5.5 are separate lines of spend, and an id alone
    leaves a reader to know which is which.
    """
    from orrery_model_names import spend_name

    def entry(model: str, spend: int) -> str:
        named = spend_name(provider, model) if provider else model
        return f"{model} {spend:,}" + (f" ({named})" if named != model else "")

    return ", ".join(
        entry(model, spend)
        for model, spend in sorted(
            models.items(), key=lambda item: (-item[1], item[0])
        )
    )


def ceiling_refusal(
    role: Role,
    *,
    now: float | None = None,
    manifest: dict[str, Any] | None = None,
) -> str | None:
    """Why this role may not start, or None when the allowance covers it.

    The rollup is refreshed here rather than read as stored, because the
    spend this exists to catch is the principal's own and is still being
    written by the session making the call.
    """
    if role.endpoint is not None:
        # A role routed at a third-party service draws on that service's
        # billing, not on the first-party allowance being measured.
        return None
    allowance = load_allowances(manifest).get(role.provider)
    if allowance is None:
        return None
    rollup = refresh(now=now, manifest=manifest)
    spend = window_spend(
        role.provider, allowance.window_days, rollup=rollup, now=now
    )
    if spend["total"] < allowance.tokens:
        return None
    breakdown = describe_models(spend["models"], role.provider)
    return (
        f"the {role.provider} allowance is spent: {spend['total']:,} tokens "
        f"measured over the last {allowance.window_days} day(s) against a "
        f"ceiling of {allowance.tokens:,}"
        + (f" ({breakdown})" if breakdown else "")
        + ". The count includes cache reads, as orrery-usage does. Raise "
        f"allowances.{role.provider} in {user_config_path()}, route "
        "the role at another provider, or wait for the window to pass"
    )


@dataclass(frozen=True)
class Crossing:
    """A measured window spend that has reached its provider's ceiling."""

    provider: str
    total: int
    tokens: int
    window_days: int
    breakdown: str


def principal_crossing(
    model: Any,
    *,
    now: float | None = None,
    manifest: dict[str, Any] | None = None,
) -> Crossing | None:
    """The allowance the model already running has crossed, or None.

    The dispatch refusal measures the role about to start; this measures
    what is running, which its caller reads from the transcript rather
    than from the configuration. The provider follows the model for the
    same reason: a session on one provider must not be stopped by
    another provider's ceiling, nor by a ceiling on a model it is not
    running.
    """
    if not isinstance(model, str) or not model:
        return None
    if manifest is None:
        manifest = effective_manifest()
    allowances = load_allowances(manifest)
    if not allowances:
        return None
    provider, _name = split_key(model_resolver(manifest)(model))
    allowance = allowances.get(provider)
    if allowance is None:
        return None
    spend = window_spend(
        provider,
        allowance.window_days,
        rollup=refresh(now=now, manifest=manifest),
        now=now,
    )
    if spend["total"] < allowance.tokens:
        return None
    return Crossing(
        provider,
        spend["total"],
        allowance.tokens,
        allowance.window_days,
        describe_models(spend["models"], provider),
    )


def _age_phrase(seconds: float) -> str:
    """How stale a stored measurement is, in the coarsest useful unit."""
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)} minute(s) ago"
    if seconds < 172800:
        return f"{int(seconds // 3600)} hour(s) ago"
    return f"{int(seconds // 86400)} day(s) ago"


def doctor_report(cwd: Path | None = None) -> list[str]:
    """Lines for `orrery-doctor`, each prefixed PASS, WARN or SKIP.

    The ceiling ships on by default once an allowance is configured, so
    what the doctor reports is the absence of one: an adopted repository
    whose principal runs on a provider with no allowance has nothing
    bounding what that session spends.
    """
    working = Path.cwd() if cwd is None else cwd
    try:
        allowances = load_allowances()
    except RuntimeConfigError as exc:
        return [f"WARN|the configured allowances are invalid: {exc}"]
    try:
        adopted = adopted_root(working) is not None
    except RuntimeConfigError as exc:
        return [f"SKIP|adoption could not be determined: {exc}"]
    if not adopted:
        return ["SKIP|this repository is not adopted, so no allowance applies"]
    try:
        principal = load_role("orchestrator", cwd=working)
    except RuntimeConfigError as exc:
        return [f"WARN|the configured principal could not be read: {exc}"]

    lines: list[str] = []
    try:
        on_exceeded()
    except RuntimeConfigError as exc:
        # Reported only when it is wrong, because the hook reading it
        # permits the turn on any configuration error: a misspelt value
        # would otherwise leave a ceiling that neither warns nor blocks
        # and says nothing about why.
        lines.append(f"WARN|{exc}, so a crossing would do neither")
    if principal.provider not in allowances:
        lines.append(
            f"WARN|no allowance is configured for {principal.provider}, the "
            f"provider this repository's principal ({principal.provider}/"
            f"{principal.model}) runs on, so nothing bounds what it spends. "
            f'Add allowances.{principal.provider} ({{"tokens": N, '
            f'"window_days": 7}}) to {user_config_path()}'
        )
    for provider in sorted(allowances):
        entry = allowances[provider]
        spend = window_spend(provider, entry.window_days)
        share = spend["total"] * 100 // entry.tokens
        # The doctor reads the stored rollup and never refreshes, so the
        # figure is only as current as the last session start or gated
        # dispatch. Saying so is the difference between a measurement
        # and an assertion: before either has run it is honestly zero
        # of nothing, not a measured zero.
        updated = read_rollup().get("updated")
        when = (
            "never measured"
            if not isinstance(updated, (int, float)) or updated <= 0
            else f"as of {_age_phrase(time.time() - updated)}"
        )
        lines.append(
            f"{'WARN' if spend['total'] >= entry.tokens else 'PASS'}|"
            f"{provider}: {spend['total']:,} of {entry.tokens:,} tokens "
            f"({share}%) over {entry.window_days} day(s), {when}"
        )
    return lines
