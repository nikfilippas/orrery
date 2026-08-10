"""Per-attempt token accounting for delegated runs.

Two things live here. The first is the attempt record: one immutable
JSON document per provider-started attempt, opened before the child is
spawned and completed after it exits. The order is the point. The child
starts before its unit registers, and `unit.json` names no provider or
model at all, so a wrapper that dies in that window leaves nothing
saying what ran; a record opened first turns that silence into a fact,
namely that a paid run began and its cost is unknown.

The second is pricing. Tokens are the durable measurement and money is
derived from an operator-maintained table, so every figure carries the
table's date and digest and a stale or missing table yields no money at
all rather than a confident zero.

Both are read by `orrery-task`, which adds the task correlation it alone
knows, and by `orrery-usage --task`.

The receipts directory is the delegate's own writable grant. These
records are therefore an operational measurement and not a defence
against a delegate that lies about its own usage; containment is that.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


# The same four classes `orrery-usage` already counts, so the per-task
# and global views cannot disagree about what a token is.
TOKEN_CLASSES = ("fresh_in", "cache_read", "cache_write", "output")

# Rotated wholesale on a retry. `result.txt` matters most: the wrapper
# creates it O_EXCL before launching, so leaving it in place made every
# retry under --receipts die with EEXIST instead of running again.
ATTEMPT_ARTEFACTS = (
    "attempt.json",
    "receipt.json",
    "result.txt",
    "stdout.log",
    "stderr.log",
    "unit.json",
)


class SpendError(Exception):
    """An accounting or price-table fault."""


def empty_tokens() -> dict[str, int]:
    return {name: 0 for name in TOKEN_CLASSES}


def _integer(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


# Which provider field feeds which class. A usage object carrying none
# of these, or one whose values are not counts, is a shape this parser
# does not understand: it becomes a named gap, never four zeroes. A
# provider that renames a field would otherwise make every run free, and
# a token ceiling would fail open at exactly the wrong moment.
CLAUDE_FIELDS = {
    "fresh_in": ("inputTokens", "input_tokens"),
    "output": ("outputTokens", "output_tokens"),
    "cache_write": ("cacheCreationInputTokens", "cache_creation_input_tokens"),
    "cache_read": ("cacheReadInputTokens", "cache_read_input_tokens"),
}
CLAUDE_FLAT_FIELDS = {
    "fresh_in": ("input_tokens",),
    "output": ("output_tokens",),
    "cache_write": ("cache_creation_input_tokens",),
    "cache_read": ("cache_read_input_tokens",),
}


def _accumulate(
    tokens: dict[str, int], usage: dict[str, Any], fields: dict[str, tuple[str, ...]]
) -> int | None:
    """Add one usage object's counts, or refuse a shape it does not fit.

    Refuses when no expected field is present at all, and when one is
    present but is not a non-negative integer. A partially recognised
    object is the dangerous case: it looks parsed and undercounts.
    """
    recognised = 0
    for name, aliases in fields.items():
        for alias in aliases:
            if alias not in usage:
                continue
            value = usage[alias]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            tokens[name] += value
            recognised += 1
            break
    return recognised or None


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every JSON object in a provider log, in order.

    Providers mix diagnostics into the stream, and Claude prints one
    large object while Codex prints a line per event, so both the whole
    text and each line are tried.
    """
    found: list[dict[str, Any]] = []
    try:
        whole = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    else:
        if isinstance(whole, dict):
            return [whole]
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            found.append(parsed)
    return found


def parse_claude_usage(text: str) -> dict[str, Any] | None:
    """Usage from a `claude --output-format json` run.

    The per-model breakdown is preferred because it includes subagent
    spend, which the top-level `usage` field omits. A delegated run
    cannot spawn subagents today, but reading the narrower field would
    silently under-report the moment one can.
    """
    result = None
    for candidate in _json_objects(text):
        if "usage" in candidate or "total_cost_usd" in candidate:
            result = candidate
    if result is None:
        return None

    tokens = empty_tokens()
    models: list[str] = []
    breakdown = result.get("modelUsage") or result.get("model_usage")
    if isinstance(breakdown, dict) and breakdown:
        for name, usage in breakdown.items():
            if not isinstance(usage, dict):
                return None
            models.append(str(name))
            counted = _accumulate(tokens, usage, CLAUDE_FIELDS)
            if counted is None:
                return None
    else:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        if _accumulate(tokens, usage, CLAUDE_FLAT_FIELDS) is None:
            return None

    cost = result.get("total_cost_usd")
    return {
        "tokens": tokens,
        "provider_cost_usd": cost if isinstance(cost, (int, float)) else None,
        "models": sorted(set(models)),
    }


def parse_codex_usage(text: str) -> dict[str, Any] | None:
    """Usage from a `codex exec --json` run.

    `turn.completed` carries the usage for that turn, so the turns are
    summed. Codex counts cached input inside `input_tokens`, exactly as
    its rollout files do, so the fresh count subtracts it rather than
    double counting; reasoning output is a separate field and is added.
    """
    tokens = empty_tokens()
    seen = False
    for event in _json_objects(text):
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return None
        counts: dict[str, int] = {}
        for name in ("input_tokens", "cached_input_tokens", "output_tokens",
                     "cache_write_input_tokens", "reasoning_output_tokens"):
            if name not in usage:
                continue
            value = usage[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            counts[name] = value
        if not counts:
            return None
        seen = True
        cached = counts.get("cached_input_tokens", 0)
        tokens["cache_read"] += cached
        tokens["fresh_in"] += max(0, counts.get("input_tokens", 0) - cached)
        tokens["cache_write"] += counts.get("cache_write_input_tokens", 0)
        tokens["output"] += counts.get("output_tokens", 0) + counts.get(
            "reasoning_output_tokens", 0
        )
    if not seen:
        return None
    return {"tokens": tokens, "provider_cost_usd": None, "models": []}


def parse_usage(provider: str, text: str) -> dict[str, Any] | None:
    if provider == "anthropic":
        return parse_claude_usage(text)
    if provider == "openai":
        return parse_codex_usage(text)
    return None


def _write(path: Path, payload: dict[str, Any]) -> None:
    """Publish a record atomically, never truncating the one in place.

    Completing a record used to open it O_TRUNC: a kill or a full disk
    between the truncate and the write erased the opening half, and a
    paid run then read as costing nothing. The temporary is fsynced and
    renamed, so the file is either the old record or the new one.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def rotate_attempt(receipts: Path) -> int | None:
    """Move a completed attempt's artefacts aside, returning its number.

    Called at the start of every attempt rather than at the end of the
    failing one, so it covers a transient retry and an approved fallback
    alike without either path having to remember.
    """
    if not (receipts / "attempt.json").exists():
        return None
    prior = receipts / "prior"
    if prior.is_symlink():
        raise SpendError(f"receipts prior directory is a symlink: {prior}")
    prior.mkdir(mode=0o700, exist_ok=True)
    number = 1
    while (prior / str(number)).exists():
        number += 1
    # A rotation killed partway leaves a directory holding the logs but
    # no record, because the record moves last. Finishing that one is
    # right; starting a fresh one would split a single attempt across
    # two directories and strand its logs where a later launch could
    # truncate them.
    resumed = prior / str(number - 1)
    target = resumed if number > 1 and not (resumed / "attempt.json").exists() else prior / str(number)
    target.mkdir(mode=0o700, exist_ok=True)
    # The record last, so its presence at the root means the rotation is
    # not finished and the next call resumes rather than restarts.
    for name in [name for name in ATTEMPT_ARTEFACTS if name != "attempt.json"] + ["attempt.json"]:
        source = receipts / name
        if source.exists() or source.is_symlink():
            os.replace(source, target / name)
    return int(target.name)


def open_attempt(
    receipts: Path,
    *,
    run_id: str,
    role: str,
    provider: str,
    model: str,
    endpoint: str | None,
    thinking: str | None,
    access: str,
    fallback_from: dict[str, str] | None,
) -> None:
    """Record what is about to run, before it runs."""
    _write(
        receipts / "attempt.json",
        {
            "v": 1,
            "run_id": run_id,
            "role": role,
            "provider": provider,
            "model": model,
            "endpoint": endpoint,
            "thinking": thinking,
            "access": access,
            "fallback_from": fallback_from,
            "started": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "ended": None,
            "exit_status": None,
            "outcome": None,
            "usage": None,
            "usage_gap": "attempt did not complete",
            "provider_cost_usd": None,
        },
    )


def close_attempt(
    receipts: Path,
    *,
    exit_status: int | None,
    outcome: str,
    log_path: Path | None,
    duration_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Complete the open record, parsing usage from the provider's log.

    A log that cannot be parsed leaves a named gap. A zero would be
    worse than a gap: a ceiling reading a missing measurement as free
    would wave through exactly the runs it cannot see.
    """
    path = receipts / "attempt.json"
    try:
        record = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None

    usage: dict[str, Any] | None = None
    gap: str | None = None
    if log_path is None:
        gap = "no provider log was kept"
    else:
        try:
            text = log_path.read_text(errors="replace")
        except OSError as exc:
            gap = f"provider log unreadable: {exc.strerror or exc}"
        else:
            usage = parse_usage(str(record.get("provider")), text)
            if usage is None:
                gap = "provider output carried no usage record"

    record["ended"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    # The wrapper's own measurement, written after the child is dead and
    # therefore beyond its reach.
    if duration_seconds is not None:
        record["duration_seconds"] = round(float(duration_seconds), 3)
    record["exit_status"] = exit_status
    record["outcome"] = outcome
    record["usage"] = usage["tokens"] if usage else None
    record["usage_gap"] = gap
    record["provider_cost_usd"] = usage["provider_cost_usd"] if usage else None
    if usage and usage["models"]:
        record["reported_models"] = usage["models"]
    _write(path, record)
    return record


def attempt_directories(receipts: Path) -> list[Path]:
    """Each attempt's own directory, oldest first, ending at the root."""
    found: list[Path] = []
    prior = receipts / "prior"
    if prior.is_dir() and not prior.is_symlink():
        found.extend(
            entry
            for entry in sorted(
                prior.iterdir(), key=lambda item: (len(item.name), item.name)
            )
            if entry.is_dir() and not entry.is_symlink()
        )
    found.append(receipts)
    return found


def read_attempt(directory: Path) -> dict[str, Any] | None:
    """The one record written in this directory, if there is one."""
    return _load_attempt(directory / "attempt.json")


# What a provider run leaves behind besides its own record. The receipts
# directory is the delegate's writable grant, so a delegate can delete
# the record it is accounted by; these are the traces that say a run
# happened anyway.
RUN_TRACES = ("receipt.json", "result.txt", "stdout.log", "stderr.log", "unit.json")


def read_attempts(receipts: Path) -> list[dict[str, Any]]:
    """Every attempt record under one receipts directory, oldest first.

    A directory that shows a run happened but holds no record yields an
    unknown-spend sentinel rather than nothing. Nothing would be a
    *known* zero, and a delegate that deletes its own record before
    exiting would turn a refusal into a pass: the ceiling reads unknown
    spend as a stop and no spend as free, so removing one file inverted
    the guard exactly.
    """
    found: list[dict[str, Any]] = []
    for directory in attempt_directories(receipts):
        record = read_attempt(directory)
        if record is not None:
            found.append(record)
        elif any((directory / name).exists() for name in RUN_TRACES):
            found.append(
                _unreadable("a run left artefacts but no attempt record")
            )
    return found


def _load_attempt(path: Path) -> dict[str, Any] | None:
    """One record, or an unknown-spend sentinel where one is unreadable.

    Absent and unreadable are different facts. No file means no provider
    run started and the cost is zero; a file that will not parse belongs
    to a run that did start, so it is reported as unknown rather than
    discarded into a total that then looks free.
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _unreadable(f"attempt record unreadable: {exc.strerror or exc}")
    try:
        record = json.loads(raw)
    except json.JSONDecodeError:
        return _unreadable("attempt record is not valid JSON")
    if not isinstance(record, dict) or record.get("v") != 1:
        return _unreadable("attempt record is not a version 1 document")
    return record


def _unreadable(reason: str) -> dict[str, Any]:
    return {
        "v": 1,
        "run_id": "unreadable",
        "provider": None,
        "model": None,
        "usage": None,
        "usage_gap": reason,
    }


# A delegated run cannot outlast the wrapper's hard budget by any
# sensible margin, so a longer span is a forged or corrupt pair rather
# than a measurement.
MAX_ATTEMPT_SECONDS = 24 * 60 * 60


def spend_of(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum attributed tokens and say plainly what is not known.

    An attempt with no completion is the crash case: the provider was
    called and the cost cannot be recovered. It is reported as unknown,
    never as nothing.
    """
    tokens = empty_tokens()
    gaps: list[str] = []
    reported_cost = 0.0
    have_cost = False
    # Carried into the ledger with the tokens, because the timings exist
    # only in the attempt records and a later question about how long a
    # role takes cannot be answered from artefacts that may be reclaimed.
    durations: list[float] = []
    for record in attempts:
        # Measured by the wrapper around the child, never derived from
        # the two timestamps in the record. The receipts directory is
        # delegate-writable, and a forged pair that is merely plausible
        # is indistinguishable from a real one, so the only sound source
        # is the one the delegate cannot reach. A record written before
        # this field existed simply contributes no duration.
        measured = record.get("duration_seconds")
        if (
            isinstance(measured, (int, float))
            and not isinstance(measured, bool)
            and 0 <= measured <= MAX_ATTEMPT_SECONDS
        ):
            durations.append(round(float(measured), 3))
        usage = record.get("usage")
        if isinstance(usage, dict):
            for name in TOKEN_CLASSES:
                tokens[name] += _integer(usage.get(name))
        else:
            gaps.append(
                f"{record.get('run_id', 'unknown run')}: "
                f"{record.get('usage_gap') or 'usage is missing'}"
            )
        cost = record.get("provider_cost_usd")
        if isinstance(cost, (int, float)):
            reported_cost += float(cost)
            have_cost = True
    return {
        "tokens": tokens,
        "total": sum(tokens.values()),
        "attempts": len(attempts),
        "unknown": bool(gaps),
        "gaps": gaps,
        "durations": durations,
        "provider_cost_usd": reported_cost if have_cost else None,
    }


def price_key(provider: str, model: str, endpoint: str | None) -> str:
    """How a rate is named.

    An endpoint is part of the identity because the same model name at a
    third-party service need not bill the same way, and a custom
    endpoint must never inherit the first-party rate by accident.
    """
    return f"{provider}@{endpoint}:{model}" if endpoint else f"{provider}:{model}"


def price_table(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """The operator's rates, from a named file or from the manifest.

    `ORRERY_PRICES` exists because the manifest is version-controlled and
    rates are local, dated and frequently wrong: an operator who keeps
    them in a file of their own does not have to carry a permanent local
    edit to a shipped file.
    """
    override = os.environ.get("ORRERY_PRICES")
    if override:
        try:
            table = json.loads(Path(override).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SpendError(f"ORRERY_PRICES is unreadable: {exc}") from exc
        if not isinstance(table, dict):
            raise SpendError("ORRERY_PRICES must hold a price table object")
        return table
    table = manifest.get("prices")
    return table if isinstance(table, dict) else None


def table_digest(table: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(table, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def table_age_days(table: dict[str, Any], today: date | None = None) -> int | None:
    raw = table.get("as_of")
    if not isinstance(raw, str):
        return None
    try:
        stamp = date.fromisoformat(raw)
    except ValueError:
        return None
    return ((today or datetime.now(timezone.utc).date()) - stamp).days


def price_refusal(table: dict[str, Any] | None, today: date | None = None) -> str | None:
    """Why money cannot be reported, or None when it can.

    Freshness is checked before any lookup, because a stale table that
    looks authoritative is the failure this guard exists for.
    """
    if table is None:
        return "the manifest carries no price table"
    if not isinstance(table.get("models"), dict) or not table["models"]:
        return "the price table carries no rates"
    if table.get("currency") != "USD":
        return "the price table does not declare USD"
    age = table_age_days(table, today)
    if age is None:
        return "the price table has no valid as_of date"
    if age < 0:
        return f"the price table is dated {table.get('as_of')}, in the future"
    limit = table.get("max_age_days")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return "the price table has no valid max_age_days"
    if age > limit:
        return (
            f"the price table is {age} days old, past its stated "
            f"maximum of {limit}; update as_of and the rates"
        )
    return None


def rate_for(
    table: dict[str, Any], provider: str, model: str, endpoint: str | None
) -> dict[str, float] | None:
    entry = table.get("models", {}).get(price_key(provider, model, endpoint))
    if not isinstance(entry, dict):
        return None
    rates: dict[str, float] = {}
    for name in TOKEN_CLASSES:
        value = entry.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return None
        rates[name] = float(value)
    return rates


def money_of(tokens: dict[str, int], rates: dict[str, float]) -> float:
    """USD for one attempt's tokens, at a million-token rate."""
    return sum(_integer(tokens.get(name)) * rates[name] / 1_000_000 for name in TOKEN_CLASSES)
