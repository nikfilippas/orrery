#!/usr/bin/env python3
"""The name every surface gives a model, with the version it means.

A stored model id says less than a reader needs: `opus` meant Opus 5 on
one CLI release and Opus 5.5 on the next, and nothing that printed
`opus` said which. Every surface that names a model therefore names it
here, the way Claude Code's own picker does: the CLI's name for an
exact version ("Opus 5.5"), and for a floating alias the version the
installed CLI resolves it to now ("Opus (latest) · Opus 5.5").

Nothing here guesses a version. Where the CLI lists no row for an
alias (the shipped `fable` on a CLI listing only Fable's versions), the
alias is named by what it was last observed to run as, and when, from
a delegated run's own report of that alias; with no observation it
says so. Where discovery is unavailable an alias is
"unverified". Everything is fail-soft: a surface that cannot name a
model still prints the id it was given.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from orrery_model_catalogue import (  # noqa: E402
    DISCOVERY_CACHE_SECONDS,
    cached_models,
    derived_label,
    discover_models,
    model_identity,
)

MOVING = re.compile(r"(?:^|-)latest$")

_OBSERVED: dict[tuple[str, str], tuple[float, tuple[str, str] | None]] = {}


def _command(provider: str) -> str:
    return "claude" if provider == "anthropic" else "codex"


def live_entries(
    provider: str,
    *,
    discover: bool = True,
    environment: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    """The installed CLI's picker entries, or None where they are unknown.

    `discover=False` reads only what this run already discovered, for a
    surface that must not spawn the CLI itself: the dispatch banner
    follows a check that has just discovered, and a fallback prompt
    follows a ranking that has.
    """
    env = dict(os.environ if environment is None else environment)
    if env.get("ORRERY_MODEL_DISCOVERY", "").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return None
    executable = shutil.which(_command(provider), path=env.get("PATH"))
    if executable is None:
        return None
    if not discover:
        return cached_models(provider, executable, env)
    try:
        return discover_models(provider, executable, environment=env)
    except Exception:  # noqa: BLE001 - naming must never fail a surface
        return None


def _bundled(provider: str) -> list[dict[str, Any]]:
    try:
        from orrery_runtime import load_catalogue

        return load_catalogue().get(provider, [])
    except Exception:  # noqa: BLE001 - naming must never fail a surface
        return []


def _when(timestamp: str) -> str:
    """A day a reader recognises: `26 Sep` this year, `Dec '25` before.

    An earlier year gives its month, not its day, so the label still
    fits a role card's select whole: "Sonnet (latest) · Sonnet 4.6,
    30 Dec 2025" does not, and the part cut would be the date.
    """
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.year != datetime.now(timezone.utc).year:
        return f"{moment:%b} '{moment:%y}"
    return f"{moment.day} {moment:%b}"


def last_observed(provider: str, model: str) -> tuple[str, str] | None:
    """The model an alias last ran as, and when, or None where unseen.

    Only from a delegated run's own report of the alias: that record
    names the alias it was dispatched as. A principal session writes no
    spend record, and no transcript says which configured alias a
    session was started as, so a principal alias the CLI lists no row
    for stays unobserved rather than named from another session.
    Remembered for the run, since several surfaces may ask about one
    alias, and for no longer than a discovery is, so a long-lived page
    learns of a newer run.
    """
    key = (provider, model)
    held = _OBSERVED.get(key)
    if held is not None and time.monotonic() - held[0] < DISCOVERY_CACHE_SECONDS:
        return held[1]
    try:
        from orrery_incidents import last_reported_model

        observed = last_reported_model(provider, model)
    except Exception:  # noqa: BLE001 - naming must never fail a surface
        observed = None
    _OBSERVED[key] = (time.monotonic(), observed)
    return observed


def _label_of(
    provider: str, model: str, entries: list[dict[str, Any]]
) -> str:
    """An exact id's name: the CLI's where it lists the id, else Orrery's."""
    for entry in entries:
        if entry.get("id") == model and isinstance(entry.get("label"), str):
            return entry["label"]
    for entry in _bundled(provider):
        if entry.get("id") == model and isinstance(entry.get("label"), str):
            return entry["label"]
    return derived_label(provider, model)


def model_name(
    provider: str,
    model: str,
    entries: list[dict[str, Any]] | None,
    *,
    observe: bool = True,
) -> str:
    """How a model is named on every surface.

    `entries` is the installed CLI's picker, or None where discovery is
    unavailable. An exact id reads as its name; a floating alias as
    "Opus (latest)" and the version it means now, or was last observed
    to run as; a moving name with no identity says it moves.
    """
    rows = [entry for entry in entries or [] if isinstance(entry, dict)]
    identity = model_identity(provider, model)
    if identity.family is not None and identity.version is None:
        bare = re.sub(r"\[[^\]]+\]$", "", model)
        base = derived_label(provider, bare)
        suffix = re.search(r"\[([^\]]+)\]$", model)
        if suffix:
            base += f", {suffix.group(1).upper()} context"
        own = next((row for row in rows if row.get("id") == bare), None)
        # Only a resolution naming a version says what the alias runs;
        # a row the CLI gave no resolvedModel resolves to itself.
        resolved = own.get("resolved") if own is not None else None
        if (
            own is not None
            and isinstance(resolved, str)
            and model_identity(provider, resolved).version is not None
            and own.get("resolved_label")
        ):
            return f"{base} · {own['resolved_label']}"
        seen = last_observed(provider, model) if observe else None
        # The version and the day of the last run that reported it, with
        # no verb: the label must fit a role card's select whole, at
        # most 37 characters, and the day already says it is a past
        # run, not what the alias means now. Where the CLI could not be
        # asked the observation stands alone: with no row for the alias
        # its current version is unknown whether or not it was asked.
        if seen is not None:
            observed = f"{_label_of(provider, seen[0], rows)}, {_when(seen[1])}"
            return f"{base} · {observed}"
        if entries is None:
            return f"{base} · unverified"
        return f"{base} · not yet observed"
    label = _label_of(provider, model, rows)
    if identity.literal is not None and MOVING.search(
        re.sub(r"\[[^\]]+\]$", "", model)
    ):
        label += " · moves, no version"
    return label


def name(
    provider: str,
    model: str,
    *,
    discover: bool = True,
    observe: bool = True,
    environment: dict[str, str] | None = None,
) -> str:
    """`model_name` against this run's discovery of the provider's CLI."""
    try:
        return model_name(
            provider,
            model,
            live_entries(provider, discover=discover, environment=environment),
            observe=observe,
        )
    except Exception:  # noqa: BLE001 - naming must never fail a surface
        return model


def with_name(provider: str, model: str, **options: Any) -> str:
    """The id a user types, followed by what it means, where they differ."""
    named = name(provider, model, **options)
    return model if named == model else f"{model} ({named})"


def cached_name(provider: str, model: str) -> str:
    """`name` from what this run already discovered, never spawning a CLI.

    For a launcher's own lines, such as a standing approval's: they are
    printed before or instead of a dispatch, and must not add one.
    """
    return name(provider, model, discover=False)


def banner_name(role: Any) -> str:
    """How a dispatch banner names the model a role is about to run.

    From what this run's pre-dispatch check already discovered, never
    by asking the CLI again: the banner is printed on every dispatch.
    Where nothing was discovered, an alias says it is unverified. An
    endpoint serves its own models, which the first-party picker cannot
    name, so a routed role keeps its id.
    """
    if getattr(role, "endpoint", None) is not None:
        return role.model
    return name(role.provider, role.model, discover=False)


def spend_name(provider: str, model: str) -> str:
    """How a usage or spend report names the model a figure is filed under.

    Never from discovery: a figure is history, and what an alias means
    now says nothing about what it meant then. An exact id reads as its
    version, whether or not the catalogue lists that version. An alias
    key holds only spend whose version is unknown: delegated runs that
    recorded no reported models, and runs that reported no single model
    of the alias's family (a side-call model of another family does not
    count). It may mix versions, so it says so.
    `provider` may be a transcript's own name for it, `claude`.
    """
    provider = {"claude": "anthropic", "codex": "openai"}.get(provider, provider)
    try:
        identity = model_identity(provider, model)
        if identity.family is not None and identity.version is None:
            return f"{identity.family.capitalize()}, version not recorded"
        return _label_of(provider, model, [])
    except Exception:  # noqa: BLE001 - naming must never fail a report
        return model


def _resolution(
    alias: str, entries: list[dict[str, Any]]
) -> tuple[str, str] | None:
    """What one CLI's picker says an alias runs, or None where it has no row."""
    bare = re.sub(r"\[[^\]]+\]$", "", alias)
    for entry in entries:
        if entry.get("id") == bare and isinstance(entry.get("resolved"), str):
            label = entry.get("resolved_label")
            if not isinstance(label, str):
                label = entry["resolved"]
            return entry["resolved"], label
    return None


def sibling_divergences(
    provider: str,
    models: Any,
    dispatched: list[dict[str, Any]],
    installs: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    """Doctor verdicts for aliases another installed CLI resolves differently.

    The CLI Orrery dispatches decides what an alias means, and an
    editor's own copy (the VS Code extension's) can be a different
    release that decides differently: sessions the user starts there
    then run another model than the delegates configured under the same
    alias, with nothing on either side saying so. Each sibling is asked
    once, only when a configured model is an alias, and only where the
    dispatched CLI's own answer (`dispatched`) is known. `installs` is
    `provider_installs(provider)` where the caller already has it, so
    each install's version is not asked twice.
    """
    aliases = sorted(
        model
        for model in models
        if model_identity(provider, model).family is not None
        and model_identity(provider, model).version is None
    )
    if not aliases:
        return []
    from orrery_runtime import RuntimeConfigError, provider_installs

    if installs is None:
        try:
            installs = provider_installs(provider)
        except RuntimeConfigError:
            return []
    ours = next((item for item in installs if item["origin"] == "PATH"), None)
    where = (
        f"{ours['version'] or 'unknown version'} at {ours['path']}"
        if ours is not None
        else "the one on PATH"
    )
    verdicts: list[tuple[str, str]] = []
    for item in installs:
        if item["origin"] != "sibling" or item["refused"]:
            continue
        try:
            sibling = discover_models(provider, item["path"])
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            verdicts.append((
                "SKIP",
                f"{provider}: the CLI at {item['path']} could not be asked "
                f"what its aliases mean: {exc}",
            ))
            continue
        for alias in aliases:
            here = _resolution(alias, dispatched)
            there = _resolution(alias, sibling)
            if (
                here is None
                or there is None
                or model_identity(provider, here[0])
                == model_identity(provider, there[0])
            ):
                continue
            verdicts.append((
                "WARN",
                f"{provider}: {alias} runs {here[1]} ({here[0]}) in the CLI "
                f"Orrery dispatches ({where}) but {there[1]} ({there[0]}) in "
                f"the one installed at {item['path']} "
                f"({item['version'] or 'unknown version'}), so sessions "
                "started there run a different model from delegates "
                "configured as the same alias. Update the older CLI"
                + (
                    " (claude update for the one on PATH)."
                    if provider == "anthropic"
                    else "."
                ),
            ))
    return verdicts
