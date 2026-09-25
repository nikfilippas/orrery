#!/usr/bin/env python3
"""Provider availability, nearest-model selection, and fallback consent.

The resolver deliberately separates three claims:

* an installed CLI can be checked for authentication without running a model;
* a picker-visible model is only *potentially* available until inference starts;
* a fallback is never authorised merely because Orrery found a candidate.

Both interactive launchers use this module, and SessionStart hooks use the
offline ranking half to explain direct-provider principal mismatches.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import IO, Any, Callable, Iterable


sys.path.insert(0, str(Path(__file__).resolve().parent))

from orrery_incidents import read_events  # noqa: E402
from orrery_model_catalogue import (  # noqa: E402
    CatalogueDiscoveryError,
    discover_claude_models,
    discover_codex_models,
    ModelIdentity,
    known_entry,
    model_identity,
    visible_entries,
)
from orrery_runtime import (  # noqa: E402
    MODEL_ID,
    PROVIDERS,
    Role,
    RuntimeConfigError,
    _git_root,
    effective_manifest,
    load_catalogue,
    load_role,
)
from orrery_standing import (  # noqa: E402
    RUN_SCOPE,
    SESSION_SCOPE,
    UNTIL_SCOPE,
    available_scopes,
)


APPROVAL_REQUIRED = 75
AUTH_TIMEOUT_SECONDS = 8.0
DISCOVERY_TIMEOUT_SECONDS = 12.0

# A substitution may be authorised only by a failure the incident log
# actually recorded. These are the kinds handle_failed_attempt writes,
# minus `config-error`: a broken configuration must be repaired rather
# than routed around, and it reaches that handler like the rest. Two
# failures recorded elsewhere are excluded too: `interrupted`, because
# a run the user stopped says nothing about the provider, and
# `output-failure`, which follows a run that completed and so is not a
# provider failure at all.
AUTHORISING_FAILURE_KINDS = frozenset(
    {
        "provider-failure",
        "provider-unavailable",
        "model-unavailable",
        "timeout",
        "stalled-loop",
        "no-result",
    }
)

# What closes a standing failure. A run that reached the provider writes
# `spend` on the way out, receipted or not, before any failure it ends in
# is recorded. The exception is a timed-out or stalled run whose unit
# would not stop: it may still be spending, so it writes no `spend` and
# its failure stands alone. A transient failure that was retried leaves
# `transient-retry`. `dispatch-closed` is what a receipted run wrote
# before it wrote `spend`, kept so such a record still in the window
# closes what it closed then. The consent bookkeeping kinds a failure is
# always followed by are neither: they are neutral, so a rerun after a
# documented consent stop still finds its failure.
CANCELLING_INCIDENT_KINDS = frozenset(
    {"spend", "transient-retry", "dispatch-closed"}
)

# How long a recorded failure keeps authorising. Bounded again by the
# time since boot, so a machine left up for weeks cannot let last
# week's failure authorise today's substitution. Hours, not days: the
# window only has to cover a consent stop and the user's rerun.
FAILURE_WINDOW_SECONDS = 6 * 3600.0
UPTIME_PATH = Path("/proc/uptime")

# These numbers are internal distance anchors, never picker labels. A model
# discovered in the future gets a tier from its configured role or provider
# picker position, so new releases do not require a source edit before they can
# be proposed.
ROLE_TIER = {
    "orchestrator": 3,
    "plan-reviewer": 3,
    "reviewer": 3,
    "implementer": 2,
    "mechanic": 1,
}

# A same-provider candidate is preferred only while its capability gap
# stays below this many tiers; from here on, a near-tier model on the
# other provider is the better substitute. On the 1-3 tier scale this
# means one step down keeps the provider and two steps crosses.
LARGE_TIER_GAP = 2

# What a delegate substitution may not reach. The ranking had no cost or
# allowance term at all, so every delegate's nearest candidate when its
# own provider was down was whatever the principal runs, and a worker
# outage converted itself into consumption of the one allowance the
# split exists to protect.
#
# The default is provider-scoped because the measured constraint is
# account-wide: Anthropic's rate-limit telemetry reports one five-hour
# and one seven-day window for the account, with no per-model breakdown,
# so excluding the principal's model alone would protect nothing.
# `principal-model` stays available for a user with evidence of
# genuinely separate per-model budgets.
PRINCIPAL_MODEL_SCOPE = "principal-model"
PRINCIPAL_PROVIDER_SCOPE = "principal-provider"
DELEGATE_FALLBACK_SCOPES = (PRINCIPAL_PROVIDER_SCOPE, PRINCIPAL_MODEL_SCOPE)
DEFAULT_DELEGATE_FALLBACK_SCOPE = PRINCIPAL_PROVIDER_SCOPE

# The named thinking scale, cheapest first. The cross-provider cap is
# stated over these names rather than over scale positions: `ultra` sits
# at the top of the OpenAI scale and a positional rule maps it onto the
# top of the Anthropic one, which is exactly the substitution the cap
# exists to prevent.
THINKING_ORDER = ("low", "medium", "high", "xhigh", "max", "ultra")
DEFAULT_DELEGATE_THINKING_CEILING = "high"


class Availability(str, Enum):
    READY = "ready"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class FailureScope(str, Enum):
    MODEL = "model"
    PROVIDER = "provider"
    TRANSIENT = "transient"


class Consent(str, Enum):
    APPROVED = "approved"
    DECLINED = "declined"
    REQUIRED = "required"


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    state: Availability
    executable: str | None
    reason: str


@dataclass(frozen=True)
class FallbackProposal:
    original: Role
    candidate: Role
    reason: str
    rationale: str
    catalogue_source: str

    @property
    def approval_key(self) -> str:
        return role_key(self.candidate)

    @property
    def crosses_provider(self) -> bool:
        return self.original.provider != self.candidate.provider


def provider_label(provider: str) -> str:
    return {"anthropic": "Anthropic", "openai": "OpenAI"}.get(
        provider,
        provider,
    )


def role_key(role: Role) -> str:
    return f"{role.provider}:{role.model}"


def parse_approval(value: str) -> tuple[str, str]:
    provider, separator, model = value.partition(":")
    if (
        not separator
        or provider not in PROVIDERS
        or not model
        or not MODEL_ID.fullmatch(model)
    ):
        raise RuntimeConfigError(
            "--approve-fallback must be PROVIDER:MODEL, where provider is "
            "anthropic or openai"
        )
    return provider, model


def _provider_command(provider: str) -> tuple[str, list[str]]:
    if provider == "anthropic":
        return "claude", ["auth", "status"]
    if provider == "openai":
        return "codex", ["login", "status"]
    raise RuntimeConfigError(f"unknown provider: {provider}")


def provider_status(
    provider: str,
    *,
    environment: dict[str, str] | None = None,
    timeout: float = AUTH_TIMEOUT_SECONDS,
) -> ProviderStatus:
    """Check command presence and login without exposing credential output."""
    env = dict(os.environ if environment is None else environment)
    command_name, arguments = _provider_command(provider)
    executable = shutil.which(command_name, path=env.get("PATH"))
    if executable is None:
        return ProviderStatus(
            provider,
            Availability.UNAVAILABLE,
            None,
            f"{command_name} is not installed or is not on PATH",
        )

    try:
        result = subprocess.run(
            [executable, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ProviderStatus(
            provider,
            Availability.UNKNOWN,
            executable,
            f"{command_name} authentication status timed out",
        )
    except OSError as exc:
        return ProviderStatus(
            provider,
            Availability.UNKNOWN,
            executable,
            f"{command_name} authentication status could not run: {exc}",
        )

    if result.returncode == 0:
        return ProviderStatus(
            provider,
            Availability.READY,
            executable,
            f"{provider_label(provider)} authentication is active",
        )
    return ProviderStatus(
        provider,
        Availability.UNAVAILABLE,
        executable,
        f"{provider_label(provider)} authentication is unavailable",
    )


def configured_model_tiers(
    manifest: dict[str, Any] | None = None,
) -> dict[tuple[str, str], int]:
    if manifest is None:
        manifest = effective_manifest()
    tiers: dict[tuple[str, str], int] = {}
    for step in manifest.get("steps", []):
        if not isinstance(step, dict):
            continue
        provider = step.get("provider")
        model = step.get("model")
        role_id = step.get("id")
        if (
            provider in PROVIDERS
            and isinstance(model, str)
            and role_id in ROLE_TIER
        ):
            identity = (provider, model)
            tiers[identity] = max(tiers.get(identity, 0), ROLE_TIER[role_id])
    return tiers


def delegate_fallback_scope(manifest: dict[str, Any] | None = None) -> str:
    """How much of the principal a delegate substitution may not reach."""
    if manifest is None:
        manifest = effective_manifest()
    value = manifest.get(
        "delegate_fallback_scope", DEFAULT_DELEGATE_FALLBACK_SCOPE
    )
    if value not in DELEGATE_FALLBACK_SCOPES:
        raise RuntimeConfigError(
            "the manifest delegate_fallback_scope must be "
            f"{' or '.join(DELEGATE_FALLBACK_SCOPES)}"
        )
    return str(value)


def delegate_thinking_ceiling(manifest: dict[str, Any] | None = None) -> str:
    """The highest thinking level a cross-provider delegate may be given."""
    if manifest is None:
        manifest = effective_manifest()
    value = manifest.get(
        "delegate_fallback_thinking_ceiling",
        DEFAULT_DELEGATE_THINKING_CEILING,
    )
    if value not in THINKING_ORDER:
        raise RuntimeConfigError(
            "the manifest delegate_fallback_thinking_ceiling must be one of "
            f"{', '.join(THINKING_ORDER)}"
        )
    return str(value)


def principal_identity(
    manifest: dict[str, Any] | None = None,
) -> Role | None:
    """The configured principal, or None where it cannot be identified.

    `manifest` is the document the command read when it loaded its own
    role or exited, so with it in hand no configuration is read here and
    nothing that read could raise. What is left to raise is the adoption
    marker, the trust store, or the repository override, and
    `apply_override=False` bypasses all three. It is safe to fall back
    on: where the override cannot be read the repository is not adopted,
    so the global principal is the one that would have applied anyway.
    """
    for apply_override in (True, False):
        try:
            return load_role(
                "orchestrator",
                manifest=manifest,
                apply_override=apply_override,
            )
        except RuntimeConfigError:
            continue
    return None


def principal_exclusions(
    role: Role,
    manifest: dict[str, Any] | None = None,
) -> tuple[set[str], set[tuple[str, str]]]:
    """What a substitution for this role may not reach, as exclusions.

    The principal itself is not governed: it is the allowance holder,
    and an interactive session whose cost the user can see. A delegate
    loses the principal's provider, or under `principal-model` only the
    principal's exact identity at any thinking level. The pair is
    returned as configured and every consumer compares it through
    `model_identity`, so the principal's model is excluded under any of
    its ids: `opus` and `claude-opus-5-5` are one allowance.

    Where the principal cannot be identified at all, no provider has
    been named, so the refusal is confined to crossing providers: an
    openai to openai substitution cannot touch an allowance that is by
    construction somewhere else.
    """
    if role.id == "orchestrator":
        return set(), set()
    principal = principal_identity(manifest)
    if principal is None:
        return {
            provider for provider in PROVIDERS if provider != role.provider
        }, set()
    if delegate_fallback_scope(manifest) == PRINCIPAL_MODEL_SCOPE:
        return set(), {(principal.provider, principal.model)}
    return {principal.provider}, set()


def _allowance_bars(
    identity: tuple[str, str],
    exclusions: tuple[set[str], set[tuple[str, str]]],
) -> bool:
    """Whether `principal_exclusions` output bars this candidate.

    By identity, not by literal pair, and with no discovery resolution:
    a standing approval is started without re-ranking, so this is its
    only guard, and ranking applies the same test so that nothing it
    offers a delegate is refused here. An unresolved principal alias
    therefore bars its whole family: nothing here says which version
    the principal runs.
    """
    providers, models = exclusions
    candidate = model_identity(*identity)
    return identity[0] in providers or any(
        candidate.matches(model_identity(*excluded)) for excluded in models
    )


def delegate_allowance_refusal(
    role: Role,
    identity: tuple[str, str],
    manifest: dict[str, Any] | None = None,
) -> str | None:
    """Why the principal-allowance rule bars this candidate, or None.

    A named candidate that the rule excluded must not be reported as no
    longer potentially available, which is true but says nothing about
    why, and sends the user looking at their provider rather than at the
    setting that decided it. Returned unpunctuated, so each caller can
    compose it into its own sentence.
    """
    if not _allowance_bars(identity, principal_exclusions(role, manifest)):
        return None
    principal = principal_identity(manifest)
    if principal is None:
        return (
            f"{identity[0]}/{identity[1]} would cross providers for "
            f"{role.id}, and Orrery could not identify the configured "
            "principal, so it cannot show that the substitution stays "
            "clear of the principal's allowance"
        )
    return (
        f"{identity[0]}/{identity[1]} is excluded for {role.id} by the "
        f"principal-allowance rule: the principal runs "
        f"{principal.provider}/{principal.model}, and "
        f"delegate_fallback_scope is {delegate_fallback_scope(manifest)}"
    )


def _picker_tier(index: int, count: int) -> int:
    if count <= 1:
        return 2
    return max(1, 3 - min(2, (index * 3) // count))


def _catalogue_entries(
    provider: str,
    *,
    status: ProviderStatus | None,
    environment: dict[str, str],
    discover_live: bool,
) -> tuple[list[dict[str, Any]], str]:
    bundled = [dict(entry) for entry in load_catalogue().get(provider, [])]
    if (
        not discover_live
        or status is None
        or status.executable is None
        or status.state is Availability.UNAVAILABLE
    ):
        return bundled, "bundled catalogue"

    discoverer = (
        discover_claude_models
        if provider == "anthropic"
        else discover_codex_models
    )
    try:
        live = discoverer(
            status.executable,
            timeout=DISCOVERY_TIMEOUT_SECONDS,
            environment=environment,
        )
    except (CatalogueDiscoveryError, OSError, RuntimeError):
        return bundled, "bundled catalogue"

    merged: list[dict[str, Any]] = []
    for entry in live:
        candidate = dict(entry)
        seed = known_entry(provider, str(candidate.get("id")), bundled) or {}
        tier = seed.get("fallback_tier")
        if isinstance(tier, int) and not isinstance(tier, bool):
            candidate["fallback_tier"] = tier
        merged.append(candidate)
    return merged, "installed CLI catalogue"


def model_status(
    role: Role,
    provider: ProviderStatus,
    *,
    environment: dict[str, str] | None = None,
    live: tuple[list[dict[str, Any]], str] | None = None,
) -> tuple[Availability, str]:
    """Check a bundled-known model against a live picker without inference.

    `live` lets a caller that has already discovered pass the result in,
    so a diagnostic checking several roles spawns one CLI per provider
    rather than one per question. Omitted, the behaviour is unchanged.

    Known means known to the bundled catalogue or, where a caller passed
    the live one, to the picker itself. A custom id is not discovered
    for here: the verdict for it could only be UNKNOWN or READY, and
    neither stops a dispatch, so the spawn would buy nothing.
    """
    bundled = load_catalogue().get(role.provider, [])
    seed = known_entry(role.provider, role.model, bundled)
    if seed is None:
        if (
            live is not None
            and live[1] == "installed CLI catalogue"
            and visible_entries(role.provider, role.model, live[0])
        ):
            return (
                Availability.READY,
                f"{role.provider}/{role.model} is picker-visible",
            )
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} is a custom model identifier",
        )
    env = dict(os.environ if environment is None else environment)
    entries, source = live if live is not None else _catalogue_entries(
        role.provider,
        status=provider,
        environment=env,
        discover_live=True,
    )
    if source != "installed CLI catalogue":
        return (
            Availability.UNKNOWN,
            f"{role.provider} model visibility could not be confirmed",
        )
    if any(entry.get("id") == role.model for entry in entries):
        return (
            Availability.READY,
            f"{role.provider}/{role.model} is picker-visible",
        )
    offered = visible_entries(role.provider, role.model, entries)
    if offered:
        # A stored alias the CLI lists no row for, or an exact id listed
        # under another form of itself: the CLI still accepts it.
        if model_identity(role.provider, role.model).version is None:
            return (
                Availability.READY,
                f"{role.provider}/{role.model} has no row of its own; the "
                "installed CLI resolves it within its picker-visible family",
            )
        return (
            Availability.READY,
            f"{role.provider}/{role.model} is picker-visible as "
            f"{offered[0]['id']}",
        )
    if (
        seed.get("id") != role.model
        and model_identity(role.provider, str(seed.get("id"))).version is None
    ):
        # Known only through its family's alias: the bundle does not name
        # this version, and Claude Code accepts full model names its
        # picker does not list, so absence from the picker proves nothing.
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} is not listed by the installed "
            "CLI, which may still accept it",
        )
    return (
        Availability.UNAVAILABLE,
        f"{role.provider}/{role.model} is not picker-visible in the installed CLI",
    )


def thinking_status(
    role: Role,
    provider: ProviderStatus,
    *,
    environment: dict[str, str] | None = None,
    live: tuple[list[dict[str, Any]], str] | None = None,
) -> tuple[Availability, str]:
    """Check a configured thinking level against the live picker.

    `model_status` answers whether the model is still offered; nothing
    answered whether the level configured against it still exists. A
    provider that renames or withdraws a level therefore stayed PASS in
    the doctor and failed at dispatch instead, which is the worst place
    to learn it.

    Deliberately conservative, and never a source of new failures on its
    own: an endpoint-routed role, a custom identifier, a provider whose
    live catalogue could not be read, and a model that is itself no
    longer picker-visible all return UNKNOWN. Only a known (bundled or
    picker-listed), picker-visible model whose live levels genuinely
    lack the configured one is reported UNAVAILABLE.
    """
    if role.endpoint is not None:
        return (
            Availability.UNKNOWN,
            f"{role.id} is routed at endpoint {role.endpoint.id}, which "
            "serves its own thinking levels",
        )
    if role.thinking is None:
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} has no configured thinking level",
        )
    bundled = load_catalogue().get(role.provider, [])
    discovered = live is not None and live[1] == "installed CLI catalogue"
    if known_entry(role.provider, role.model, bundled) is None and not (
        discovered and visible_entries(role.provider, role.model, live[0])
    ):
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} is a custom model identifier",
        )
    env = dict(os.environ if environment is None else environment)
    entries, source = live if live is not None else _catalogue_entries(
        role.provider,
        status=provider,
        environment=env,
        discover_live=True,
    )
    if source != "installed CLI catalogue":
        return (
            Availability.UNKNOWN,
            f"{role.provider} thinking levels could not be confirmed",
        )
    offered = visible_entries(role.provider, role.model, entries)
    if not offered:
        # model_status already reports this; do not fail it twice.
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} is not picker-visible",
        )
    if len(offered) > 1:
        # Offered through several rows, a stored alias through each
        # version of its family: which one the CLI picks is its own
        # decision, so a level is confirmed only where every row offers
        # it, and nothing is failed on a guess about which row runs.
        if all(
            role.thinking in (item.get("thinking_levels") or [])
            for item in offered
        ):
            return (
                Availability.READY,
                f"{role.provider}/{role.model} supports thinking {role.thinking}",
            )
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} is offered through rows whose "
            "thinking levels differ",
        )
    entry = offered[0]
    levels = entry.get("thinking_levels")
    if not isinstance(levels, list):
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} reports no thinking levels",
        )
    if not levels:
        # The provider saying "this model has no effort levels" is a
        # verdict about a configured level; the provider saying nothing
        # is not. Only the first may fail.
        if entry.get("thinking_stated") is True:
            return (
                Availability.UNAVAILABLE,
                f"{role.provider}/{role.model} no longer supports thinking "
                f"{role.thinking}; available: none",
            )
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} reports no thinking levels",
        )
    if role.thinking in levels:
        return (
            Availability.READY,
            f"{role.provider}/{role.model} supports thinking {role.thinking}",
        )
    available = ", ".join(str(level) for level in levels)
    return (
        Availability.UNAVAILABLE,
        f"{role.provider}/{role.model} no longer supports thinking "
        f"{role.thinking}; available: {available}",
    )


def _thinking_for(
    original: Role,
    candidate: dict[str, Any],
    source_entries: Iterable[dict[str, Any]],
) -> str | None:
    levels = candidate.get("thinking_levels")
    if not isinstance(levels, list) or not levels:
        return None

    source = known_entry(
        original.provider, original.model, list(source_entries)
    )
    source_levels = source.get("thinking_levels") if source else None
    if (
        original.thinking
        and isinstance(source_levels, list)
        and original.thinking in source_levels
    ):
        if len(source_levels) == 1:
            position = 1.0
        else:
            position = source_levels.index(original.thinking) / (
                len(source_levels) - 1
            )
        index = round(position * (len(levels) - 1))
        return str(levels[index])

    if original.thinking in levels:
        return original.thinking
    if original.thinking in {"max", "ultra", "deep"}:
        return str(levels[-1])
    default = candidate.get("default_thinking")
    if default in levels:
        return str(default)
    return str(levels[0])


def _thinking_rank(level: Any) -> int | None:
    return THINKING_ORDER.index(level) if level in THINKING_ORDER else None


def _capped_thinking(
    original: Role,
    candidate: dict[str, Any],
    source_entries: Iterable[dict[str, Any]],
    *,
    ceiling: str,
) -> str | None:
    """A cross-provider delegate's level: the configured name, capped.

    The candidate is given the configured level's own name where it
    offers one, and otherwise the nearest lower name it does offer,
    never above the ceiling. Positional mapping is what put an `ultra`
    reviewer on Anthropic's `max`, since `ultra` is the top of its own
    scale and `max` the top of the other; by name, `ultra` is not
    offered at all and the ceiling decides.

    This governs delegates only. The principal keeps `_thinking_for`:
    its cost is visible in the session the user is watching, and
    quietly substituting it down would change the quality of the work
    in front of them.
    """
    levels = candidate.get("thinking_levels")
    if not isinstance(levels, list) or not levels:
        return None
    ranked = sorted(
        (rank, level)
        for rank, level in (
            (_thinking_rank(level), level) for level in levels
        )
        if rank is not None
    )
    limit = _thinking_rank(ceiling)
    if not ranked or limit is None:
        # A level this catalogue cannot place on the named scale cannot
        # be capped by name either, so the positional mapping stands
        # rather than a guess being made about where it sits.
        return _thinking_for(original, candidate, source_entries)
    wanted = _thinking_rank(original.thinking)
    if wanted is None:
        # No configured name to match against this candidate's own, so
        # the positional mapping still chooses; the ceiling still binds
        # what it returns.
        positional = _thinking_for(original, candidate, source_entries)
        wanted = _thinking_rank(positional)
        if wanted is None:
            return positional
    allowed = [level for rank, level in ranked if rank <= min(wanted, limit)]
    # Nothing at or below the cap leaves the cheapest the candidate
    # offers: exceeding the ceiling is the one outcome this prevents.
    return allowed[-1] if allowed else ranked[0][1]


def _ranked_candidates(
    original: Role,
    *,
    excluded_providers: Iterable[str] = (),
    excluded_models: Iterable[tuple[str, str]] = (),
    environment: dict[str, str] | None = None,
    statuses: dict[str, ProviderStatus] | None = None,
    assumed_ready: Iterable[str] = (),
    additional_models: dict[str, list[str]] | None = None,
    discover_live: bool = True,
    manifest: dict[str, Any] | None = None,
) -> list[tuple[tuple[int, int, int, int, int, str, str], Role, str]]:
    """Every potentially usable candidate, nearest first."""
    env = dict(os.environ if environment is None else environment)
    excluded_provider_set = set(excluded_providers)
    excluded_model_set = set(excluded_models)
    assumed = set(assumed_ready)
    supplied_statuses = {} if statuses is None else dict(statuses)
    additions = {} if additional_models is None else additional_models
    # Read once, and only for a delegate: an invalid ceiling must not
    # fail the principal's own fallback, which the cap never governs.
    ceiling = (
        None
        if original.id == "orchestrator"
        else delegate_thinking_ceiling(manifest)
    )
    configured_tiers = configured_model_tiers(manifest)
    allowance = principal_exclusions(original, manifest)
    bundled = load_catalogue()
    source_entries = bundled.get(original.provider, [])
    source_seed = known_entry(
        original.provider, original.model, source_entries
    )
    seeded_source_tier = (
        source_seed.get("fallback_tier")
        if isinstance(source_seed, dict)
        else None
    )
    target_tier = (
        seeded_source_tier
        if isinstance(seeded_source_tier, int)
        else ROLE_TIER.get(original.id, 2)
    )

    ranked: list[
        tuple[tuple[int, int, int, int, int, str, str], Role, str]
    ] = []
    for provider in sorted(PROVIDERS):
        if provider in excluded_provider_set:
            continue
        status = supplied_statuses.get(provider)
        if provider in assumed:
            command_name, _ = _provider_command(provider)
            status = ProviderStatus(
                provider,
                Availability.READY,
                shutil.which(command_name, path=env.get("PATH")),
                f"{provider_label(provider)} is active in this session",
            )
        elif status is None:
            status = provider_status(provider, environment=env)
        if status.state is Availability.UNAVAILABLE:
            continue

        entries, source = _catalogue_entries(
            provider,
            status=status,
            environment=env,
            discover_live=discover_live,
        )
        by_id = {
            entry.get("id"): dict(entry)
            for entry in entries
            if isinstance(entry.get("id"), str)
        }
        if source != "installed CLI catalogue":
            for (configured_provider, model), tier in configured_tiers.items():
                if configured_provider == provider and model not in by_id:
                    by_id[model] = {
                        "id": model,
                        "label": model,
                        "thinking_levels": [],
                        "default_thinking": None,
                        "fallback_tier": tier,
                    }
        for model in additions.get(provider, []):
            if model not in by_id:
                by_id[model] = {
                    "id": model,
                    "label": model,
                    "thinking_levels": [],
                    "default_thinking": None,
                }

        # Discovery says what each alias runs now; where it does, an
        # alias is compared as that version, and where it does not, as
        # its whole family. Either way a failed or excluded model cannot
        # come back under its other id. The principal-allowance rule is
        # applied separately, without resolution, by the very test that
        # guards a standing approval, so ranking never offers a delegate
        # a candidate that guard would refuse.
        resolutions = {
            entry["id"]: entry["resolved"]
            for entry in by_id.values()
            if isinstance(entry.get("resolved"), str)
        }

        def identity_of(model: str) -> ModelIdentity:
            return model_identity(provider, model, resolutions.get(model))

        barred = [
            identity_of(model)
            for excluded_provider, model in excluded_model_set
            if excluded_provider == provider
        ]
        if provider == original.provider:
            barred.append(identity_of(original.model))

        ordered = list(by_id.values())
        # A configured bare alias the CLI lists no row for (the shipped
        # `fable` on a CLI listing only Fable's versions) is configured
        # for the rows it is offered through, so they keep its distance.
        configured_here = {
            (provider, model): tier
            for (configured_provider, model), tier in configured_tiers.items()
            if configured_provider == provider
        }
        for (configured_provider, alias), tier in configured_tiers.items():
            if (
                configured_provider != provider
                or alias in by_id
                or model_identity(provider, alias).version is not None
            ):
                continue
            for row in visible_entries(provider, alias, ordered):
                key = (provider, row["id"])
                configured_here[key] = max(configured_here.get(key, 0), tier)
        for index, entry in enumerate(ordered):
            model = entry["id"]
            identity = (provider, model)
            if any(identity_of(model).matches(item) for item in barred):
                continue
            if _allowance_bars(identity, allowance):
                continue
            tier = entry.get("fallback_tier")
            if not isinstance(tier, int) or isinstance(tier, bool):
                tier = configured_here.get(
                    identity,
                    _picker_tier(index, len(ordered)),
                )
            thinking = (
                _capped_thinking(
                    original, entry, source_entries, ceiling=ceiling
                )
                if ceiling is not None and provider != original.provider
                else _thinking_for(original, entry, source_entries)
            )
            candidate = replace(
                original,
                provider=provider,
                model=model,
                thinking=thinking,
                # Candidates are ranked from first-party catalogues, so a
                # substitute never inherits the failed role's custom
                # endpoint: that would send this model's name, and the
                # third-party credential, to a service that never served
                # it.
                endpoint=None,
            )
            configured_distance = (
                abs(configured_here[identity] - target_tier)
                if identity in configured_here
                else 4
            )
            tier_gap = abs(tier - target_tier)
            score = (
                # Providers often limit one model rather than the whole
                # account, so the nearest same-provider model comes
                # first, but only while the capability gap stays small:
                # a distant same-provider model ranks behind a near-tier
                # model on the other provider.
                1 if tier_gap >= LARGE_TIER_GAP else 0,
                0 if provider == original.provider else 1,
                tier_gap,
                configured_distance,
                0 if status.state is Availability.READY else 1,
                provider,
                f"{index:05d}:{model}",
            )
            ranked.append((score, candidate, source))

    ranked.sort(key=lambda item: item[0])
    return ranked


def nearest_fallback(
    original: Role,
    reason: str,
    *,
    excluded_providers: Iterable[str] = (),
    excluded_models: Iterable[tuple[str, str]] = (),
    environment: dict[str, str] | None = None,
    statuses: dict[str, ProviderStatus] | None = None,
    assumed_ready: Iterable[str] = (),
    additional_models: dict[str, list[str]] | None = None,
    discover_live: bool = True,
    manifest: dict[str, Any] | None = None,
) -> FallbackProposal | None:
    """Return the closest potentially usable role without authorising it."""
    principal_providers, principal_models = principal_exclusions(
        original, manifest
    )
    ranked = _ranked_candidates(
        original,
        excluded_providers=set(excluded_providers) | principal_providers,
        excluded_models=set(excluded_models) | principal_models,
        environment=environment,
        statuses=statuses,
        assumed_ready=assumed_ready,
        additional_models=additional_models,
        discover_live=discover_live,
        manifest=manifest,
    )
    if not ranked:
        return None
    _score, candidate, catalogue_source = ranked[0]
    rationale = (
        "closest role/model capability match among authenticated or "
        "potentially authenticated models"
    )
    return FallbackProposal(
        original=original,
        candidate=candidate,
        reason=reason,
        rationale=rationale,
        catalogue_source=catalogue_source,
    )


def same_provider_ladder(role: Role, *, limit: int = 2) -> list[str]:
    """Nearest same-provider models, for the CLI's own fallback setting.

    Deliberately pure: it reads only the bundled first-party catalogue,
    so it spawns no process, performs no picker discovery, and makes no
    authentication probe. `discover_live=False` is not offline
    (`_ranked_candidates` still calls `provider_status`), so the
    ranking path must not be reused here.

    Configured custom identifiers are excluded: one may belong to
    another role's third-party endpoint, and a name that only exists
    there must never be armed against a first-party account. An
    endpoint-backed role gets no ladder at all, because its process
    carries that endpoint's base URL and credentials.
    """
    if role.endpoint is not None:
        return []
    try:
        entries = load_catalogue().get(role.provider, [])
    except RuntimeConfigError:
        return []
    tiers = {
        entry["id"]: entry["fallback_tier"]
        for entry in entries
        if isinstance(entry.get("id"), str)
        and isinstance(entry.get("fallback_tier"), int)
        and not isinstance(entry.get("fallback_tier"), bool)
    }
    seed = known_entry(role.provider, role.model, entries)
    source_tier = (
        seed.get("fallback_tier")
        if seed is not None and seed.get("id") in tiers
        else None
    )
    if source_tier is None:
        # A model outside the first-party catalogue cannot be placed on
        # the tier scale, so no ladder can be justified for it.
        return []
    source = model_identity(role.provider, role.model)
    ranked = sorted(
        (
            (abs(tier - source_tier), index, model)
            for index, (model, tier) in enumerate(tiers.items())
            if abs(tier - source_tier) <= 1
            and not model_identity(role.provider, model).matches(source)
        )
    )
    # One rung per family: with an alias and its exact versions listed
    # side by side, a ladder by tier alone names one model twice, and
    # an overloaded service is then retried on itself.
    ladder: list[str] = []
    families: set[str | None] = set()
    for _distance, _index, model in ranked:
        identity = model_identity(role.provider, model)
        family = identity.family or identity.literal
        if family in families:
            continue
        families.add(family)
        ladder.append(model)
    return ladder[:limit]


def seconds_since_boot() -> float | None:
    """Seconds since this boot, or None where the clock cannot be read."""
    try:
        return float(UPTIME_PATH.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _same_identity(event: dict[str, Any], role: Role) -> bool:
    """Whether one incident event was written against this exact role.

    Deliberately four fields: role id, provider, model and thinking. An
    incident carries less than the standing store's ten-field
    fingerprint, and the endpoint id it does carry is not compared,
    because a substitution candidate never inherits one.
    """
    return (
        event.get("role") == role.id
        and event.get("provider") == role.provider
        and event.get("model") == role.model
        and event.get("thinking") == (role.thinking or None)
    )


def _within_repository(root: Path, workdir: Any) -> bool:
    """Whether a recorded working directory lies inside this repository.

    Containment rather than equality: the log records the process
    working directory, so a run started from a subdirectory would never
    match its own repository root.
    """
    if not isinstance(workdir, str) or not workdir:
        return False
    try:
        recorded = Path(workdir).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return recorded == root or root in recorded.parents


def authorising_failure(
    role: Role,
    *,
    cwd: Path | None = None,
) -> dict[str, Any] | None:
    """The recorded failure that authorises a substitution, or None.

    The precondition is the newest authorising failure for this exact
    identity in this repository, not followed by a cancelling event. It
    is stated over failures rather than over all events on purpose: the
    documented rerun flow always writes consent bookkeeping after the
    failure, so a rule over the newest event of any kind would refuse
    every rerun it exists to permit.
    """
    window = FAILURE_WINDOW_SECONDS
    since_boot = seconds_since_boot()
    if since_boot is not None:
        window = min(window, since_boot)
    working = Path.cwd() if cwd is None else Path(cwd)
    root = _git_root(working)
    if root is None:
        # Fail closed rather than degrade to the process directory. A
        # bare directory as the containment root matches every incident
        # recorded anywhere beneath it, so a failure in one repository
        # would authorise a substitution in a sibling whenever the
        # command is run from a parent that is not itself a checkout.
        # Refusing costs only a run started outside any repository.
        return None
    since = datetime.now(timezone.utc) - timedelta(seconds=window)

    found: dict[str, Any] | None = None
    for event in read_events(since=since):
        if not _same_identity(event, role):
            continue
        if not _within_repository(root, event.get("workdir")):
            continue
        kind = event.get("kind")
        if kind in AUTHORISING_FAILURE_KINDS:
            found = event
        elif kind in CANCELLING_INCIDENT_KINDS:
            found = None
    return found


def proposal_for_approval(
    original: Role,
    approval: tuple[str, str],
    *,
    environment: dict[str, str] | None = None,
    manifest: dict[str, Any] | None = None,
) -> tuple[FallbackProposal | None, set[str], set[tuple[str, str]]]:
    """Resolve an approval from a previous failed invocation.

    A cross-provider approval means the configured provider already
    failed; a same-provider approval means at least the configured
    model failed. That premise is not assumed here: `authorising_failure`
    proves it against the incident log before a caller resolves an
    approval. The approved identity may sit deeper than the
    freshly ranked nearest candidate: the exclusions a failing
    invocation accumulates while walking the ladder do not survive its
    exit, so a rerun cannot rebuild them. An explicitly named identity
    is therefore accepted from anywhere in the current ranking, and
    every candidate ranking nearer is excluded as already ruled out,
    so a later failure of the approved candidate proposes a strictly
    deeper rung instead of walking back up the ladder.

    A delegate's ranking starts from the principal-allowance exclusions,
    so an approval naming a candidate that rule bars resolves to no
    proposal. Callers report that with `delegate_allowance_refusal`,
    which says which rule decided it.
    """
    excluded_providers, excluded_models = principal_exclusions(
        original, manifest
    )
    if approval[0] == original.provider:
        excluded_models.add((original.provider, original.model))
    else:
        excluded_providers.add(original.provider)

    ranked = _ranked_candidates(
        original,
        excluded_providers=excluded_providers,
        excluded_models=excluded_models,
        environment=environment,
        manifest=manifest,
    )
    for _score, candidate, catalogue_source in ranked:
        identity = (candidate.provider, candidate.model)
        if identity == approval:
            proposal = FallbackProposal(
                original=original,
                candidate=candidate,
                reason=(
                    "the user approved a candidate proposed by an "
                    "earlier failed attempt"
                ),
                rationale=(
                    "explicitly approved candidate from the current "
                    "ranking; nearer candidates are excluded as "
                    "already ruled out"
                ),
                catalogue_source=catalogue_source,
            )
            return proposal, excluded_providers, excluded_models
        excluded_models.add(identity)
    return None, excluded_providers, excluded_models


def git_workspace_fingerprint(
    cwd: Path | None = None,
    *,
    timeout: float = 15.0,
) -> str | None:
    """Fingerprint tracked changes and non-ignored untracked content.

    None is deliberately treated as "cannot prove unchanged" by callers.
    The fingerprint never leaves the process and does not include ignored
    files, matching Git's definition of the workspace Orrery hands off.
    """
    working_directory = Path.cwd() if cwd is None else cwd
    try:
        root_result = subprocess.run(
            ["git", "-C", str(working_directory), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if root_result.returncode != 0:
        return None

    try:
        root = Path(os.fsdecode(root_result.stdout.rstrip(b"\n")))
    except (TypeError, ValueError):
        return None
    digest = sha256()
    commands = (
        ["rev-parse", "HEAD"],
        ["symbolic-ref", "--quiet", "HEAD"],
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        ["diff", "--no-ext-diff", "--binary"],
        ["diff", "--no-ext-diff", "--binary", "--cached"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    outputs: list[bytes] = []
    try:
        for arguments in commands:
            result = subprocess.run(
                ["git", "-C", str(root), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                if arguments[0] == "rev-parse":
                    output = b"unborn\n"
                elif arguments[0] == "symbolic-ref":
                    output = b"detached\n"
                else:
                    return None
            else:
                output = result.stdout
            outputs.append(output)
            digest.update(b"\0command\0")
            digest.update(" ".join(arguments).encode())
            digest.update(b"\0")
            digest.update(output)

        for encoded_name in outputs[-1].split(b"\0"):
            if not encoded_name:
                continue
            path = root / os.fsdecode(encoded_name)
            try:
                metadata = path.lstat()
                digest.update(b"\0untracked\0")
                digest.update(encoded_name)
                digest.update(
                    f":{metadata.st_mode}:{metadata.st_size}".encode()
                )
                if path.is_symlink():
                    digest.update(os.fsencode(os.readlink(path)))
                elif path.is_file():
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
            except OSError:
                # A concurrent removal or unreadable path means unchanged
                # state cannot be established safely.
                return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    return digest.hexdigest()


MODEL_FAILURE_PATTERNS = (
    r"\bmodel\b.{0,100}\b(?:not found|does not exist|doesn't exist|"
    r"not available|unavailable|not supported|unsupported)\b",
    r"\b(?:unknown|invalid)\s+model\b",
    r"\bno access to (?:the )?model\b",
    # A limit announced for one model is a model failure, not an
    # account failure: the provider still serves its other models.
    # Generic limit wording stays provider scope below.
    r"\b(?:usage|rate)\s+limit\b[^\n]{0,80}\bmodel\b",
    r"\bmodel\b[^\n]{0,80}\b(?:usage|rate)\s+limit\b",
)
PROVIDER_FAILURE_PATTERNS = (
    r"\bnot logged in\b",
    r"\bauthentication\b",
    r"\bunauthori[sz]ed\b",
    r"\binvalid api key\b",
    r"\bquota\b",
    r"\bbilling\b",
    r"\bcredit(?:s| balance)?\b",
    r"\busage limit\b",
    r"\bsubscription\b",
    r"\bentitlement\b",
    r"\brate limit\b",
)
TRANSIENT_FAILURE_PATTERNS = (
    r"\btimed? out\b",
    r"\bconnection (?:reset|refused|closed)\b",
    r"\bnetwork (?:error|failure|unavailable)\b",
    r"\bservice unavailable\b",
    r"\boverloaded(?:_error)?\b",
    r"\b(?:etimedout|econnreset|econnrefused|enotfound)\b",
    r"\b(?:502|503|504)\b",
)


# A reset time is only trusted when it follows wording that announces one,
# so an arbitrary date elsewhere in the diagnostics can never become an
# approval lifetime.
_RESET_CONTEXT = r"(?:try again|resets?|renews?|available(?: again)?)"
_RESET_ISO_PATTERN = re.compile(
    _RESET_CONTEXT
    + r"[^\n]{0,40}?"
    + r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?"
    + r"(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)
_RESET_PROSE_PATTERN = re.compile(
    _RESET_CONTEXT
    + r"[^\n]{0,40}?"
    + r"([A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4},?\s*"
    + r"(?:at\s+)?\d{1,2}:\d{2}\s*(?:[APap]\.?[Mm]\.?)?)",
    re.IGNORECASE,
)
_RESET_PROSE_FORMATS = (
    "%b %d %Y %I:%M %p",
    "%B %d %Y %I:%M %p",
    "%b %d %Y %H:%M",
    "%B %d %Y %H:%M",
)


def parse_reset_time(diagnostics: str) -> datetime | None:
    """The provider-stated reset moment in the diagnostics, if any.

    Returns an aware datetime, treating naive provider wording as local
    time. A moment that is not in the future returns None: it could not
    bound a standing approval.
    """
    if not diagnostics:
        return None

    parsed: datetime | None = None
    iso_match = _RESET_ISO_PATTERN.search(diagnostics)
    if iso_match is not None:
        raw = iso_match.group(1).replace("Z", "+00:00")
        if "T" not in raw:
            raw = raw.replace(" ", "T", 1)
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None

    if parsed is None:
        prose_match = _RESET_PROSE_PATTERN.search(diagnostics)
        if prose_match is None:
            return None
        text = prose_match.group(1)
        text = re.sub(r"(\d{1,2})(?:st|nd|rd|th)", r"\1", text)
        text = text.replace(",", " ").replace(".", "")
        text = re.sub(r"\bat\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        for format_string in _RESET_PROSE_FORMATS:
            try:
                parsed = datetime.strptime(text, format_string)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    if parsed.timestamp() <= datetime.now().astimezone().timestamp():
        return None
    return parsed


def _event_error_messages(event: dict[str, Any]) -> list[str]:
    kind = event.get("type")
    messages: list[Any] = []
    if kind == "error":
        messages.append(event.get("message"))
        # Anthropic's API error body nests its words and its code.
        error = event.get("error")
        if isinstance(error, dict):
            messages.append(error.get("message"))
            code = error.get("type")
            if isinstance(code, str):
                messages.append(code.replace("_", " "))
    elif kind == "turn.failed":
        error = event.get("error")
        if isinstance(error, dict):
            messages.append(error.get("message"))
    elif kind == "assistant" and event.get("is_api_error_message") is True:
        # The CLI's own error code, as words: its prose for a missing
        # model ("may not exist") matches no model pattern, while
        # `model_not_found`, `billing_error` and `rate_limit` say it
        # exactly.
        code = event.get("error")
        if isinstance(code, str):
            messages.append(code.replace("_", " "))
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            messages.extend(
                block.get("text")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    elif kind == "result" and event.get("is_error") is True:
        messages.append(event.get("result"))
        errors = event.get("errors")
        if isinstance(errors, list):
            messages.extend(errors)
    return [message for message in messages if isinstance(message, str)]


def provider_error_text(log_text: str) -> str:
    """Only the provider's own error content from a delegate's output.

    A run log interleaves the CLI's stderr with its JSON event stream,
    and that stream carries everything the delegate read and wrote:
    command output, tool results, assistant prose. Classified whole, a
    delegate that read the orchestrator skill's "model ... unavailable"
    wording, or a file mentioning billing, decided its own failure
    scope and reset time. Kept: lines that are not JSON events, which
    are stderr; Codex's fatal `error` and `turn.failed` events; Claude's
    API-error assistant message and erroring result. Codex `error`
    items are dropped with the other items: they are non-fatal notices,
    and a live probe of the installed CLI showed one reading "Model
    metadata ... not found" on a run that failed for another reason.
    """
    kept: list[str] = []
    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("{"):
            kept.append(line)
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            # Most likely an event torn by a kill, which still carries
            # its payload; losing a malformed stderr line is the cheaper
            # error.
            continue
        if not isinstance(event, dict) or "type" not in event:
            # A bare JSON error body the CLI printed to stderr.
            kept.append(line)
            continue
        kept.extend(_event_error_messages(event))
    return "\n".join(kept)


def classify_failure(
    diagnostics: str,
    *,
    timed_out: bool = False,
) -> FailureScope:
    text = diagnostics.lower()
    if any(re.search(pattern, text, re.DOTALL) for pattern in MODEL_FAILURE_PATTERNS):
        return FailureScope.MODEL
    if any(
        re.search(pattern, text, re.DOTALL)
        for pattern in PROVIDER_FAILURE_PATTERNS
    ):
        return FailureScope.PROVIDER
    if timed_out or any(
        re.search(pattern, text, re.DOTALL)
        for pattern in TRANSIENT_FAILURE_PATTERNS
    ):
        return FailureScope.TRANSIENT
    # Unknown non-zero provider exits are isolated from that provider rather
    # than cycling its models, which is the safe behavior for hidden quota and
    # entitlement failures.
    return FailureScope.PROVIDER


def _safe_print(message: str, *, stream: IO[str] | None = None) -> None:
    destination = sys.stderr if stream is None else stream
    try:
        print(message, file=destination, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def _open_tty() -> IO[str] | None:
    """The controlling terminal, opened the way getpass does.

    open("/dev/tty", "r+") builds a BufferedRandom, which requires a
    seekable file; a terminal never is, and the UnsupportedOperation it
    raises subclasses OSError, so the failure is silent. Wrapping the raw
    descriptor directly avoids the seekability requirement entirely.
    """
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return None
    try:
        return io.TextIOWrapper(
            io.FileIO(descriptor, "w+"),
            encoding="utf-8",
            line_buffering=True,
        )
    except (OSError, ValueError):
        os.close(descriptor)
        return None


@dataclass(frozen=True)
class ConsentDecision:
    """A consent outcome together with the approved lifetime, if any."""

    consent: Consent
    scope: str = RUN_SCOPE
    expires_at: float | None = None


def _scope_phrase(scope: str, expires_at: float | None) -> str:
    if scope == SESSION_SCOPE:
        return "for every project in this login session"
    if scope == UNTIL_SCOPE and expires_at is not None:
        moment = datetime.fromtimestamp(expires_at).astimezone()
        return f"for every project until {moment:%Y-%m-%d %H:%M %Z}"
    return "for this run only"


def _candidate_label(candidate: Role) -> str:
    return f"{candidate.provider}:{candidate.model}" + (
        f" (thinking {candidate.thinking})" if candidate.thinking else ""
    )


def request_fallback_consent(
    proposal: FallbackProposal,
    *,
    approval: tuple[str, str] | None,
    no_fallback: bool,
    program_name: str,
    original_status: int | None = None,
    context_warning: bool = False,
    require_rerun_after_inspection: bool = False,
    stream: IO[str] | None = None,
    tty_opener: Callable[[], IO[str] | None] = _open_tty,
) -> Consent:
    """Compatibility entry point preserving the Consent-only contract."""
    return request_fallback_decision(
        proposal,
        approval=approval,
        no_fallback=no_fallback,
        program_name=program_name,
        original_status=original_status,
        context_warning=context_warning,
        require_rerun_after_inspection=require_rerun_after_inspection,
        stream=stream,
        tty_opener=tty_opener,
    ).consent


def request_fallback_decision(
    proposal: FallbackProposal,
    *,
    approval: tuple[str, str] | None,
    no_fallback: bool,
    program_name: str,
    original_status: int | None = None,
    context_warning: bool = False,
    require_rerun_after_inspection: bool = False,
    stream: IO[str] | None = None,
    tty_opener: Callable[[], IO[str] | None] = _open_tty,
    reset_time: datetime | None = None,
    approval_scope: tuple[str, float | None] | None = None,
    extra_disclosures: Iterable[str] = (),
) -> ConsentDecision:
    """Notify, bind consent to the exact candidate, and never infer approval."""
    original = proposal.original
    candidate = proposal.candidate
    _safe_print("", stream=stream)
    _safe_print("ORRERY FALLBACK PROPOSED", stream=stream)
    _safe_print(
        f"Configured: {provider_label(original.provider)} / {original.model}"
        + (f" / thinking {original.thinking}" if original.thinking else ""),
        stream=stream,
    )
    _safe_print(f"Reason: {proposal.reason}", stream=stream)
    _safe_print(
        f"Nearest candidate: {provider_label(candidate.provider)} / "
        f"{candidate.model}"
        + (f" / thinking {candidate.thinking}" if candidate.thinking else ""),
        stream=stream,
    )
    _safe_print(
        f"Basis: {proposal.rationale}; {proposal.catalogue_source}.",
        stream=stream,
    )
    if proposal.crosses_provider:
        _safe_print(
            "A cross-provider fallback starts a fresh context; conversation "
            "state and provider-specific CLI arguments cannot migrate.",
            stream=stream,
        )
    if original.endpoint is not None and candidate.endpoint is None:
        # Approving this moves the assignment to a different company's
        # service, billing, and data handling, which the user must see.
        _safe_print(
            f"This candidate does not use the configured endpoint "
            f"{original.endpoint.label} ({original.endpoint.base_url}); it "
            f"runs on {provider_label(candidate.provider)}'s own service "
            "with that account's credentials.",
            stream=stream,
        )
    if context_warning:
        _safe_print(
            "The failed process may have changed the workspace. Inspect its "
            "state before approving another write-capable process.",
            stream=stream,
        )
    if original_status is not None:
        _safe_print(
            f"The failed attempt's exit status was {original_status}.",
            stream=stream,
        )
    for disclosure in extra_disclosures:
        _safe_print(disclosure, stream=stream)

    if no_fallback:
        _safe_print(
            "Fallback is disabled for this invocation; no substitution was "
            "made.",
            stream=stream,
        )
        return ConsentDecision(Consent.DECLINED)

    if require_rerun_after_inspection:
        _safe_print("ORRERY FALLBACK APPROVAL REQUIRED", stream=stream)
        _safe_print(
            "The workspace changed during the failed attempt (or Orrery "
            "could not prove that it stayed unchanged). Inspect it first, "
            "then rerun the same command with "
            f"`--approve-fallback {proposal.approval_key}` before `--`.",
            stream=stream,
        )
        _safe_print(
            f"{program_name} did not start another write-capable process.",
            stream=stream,
        )
        return ConsentDecision(Consent.REQUIRED)

    scopes = available_scopes(reset_time)
    expected = (candidate.provider, candidate.model)
    if approval is not None:
        if approval == expected:
            scope, expires_at = (
                approval_scope
                if approval_scope is not None
                else (RUN_SCOPE, None)
            )
            # A non-interactive rerun carries no first-run diagnostics to
            # bind a scope against, so it may not mint a multi-day standing
            # approval: `until` is refused here and can be granted only from
            # the interactive menu, which is bound to the offered scopes. A
            # `session` scope is accepted only where it was actually
            # offerable, so an unoffered session cannot be forged either.
            if scope == UNTIL_SCOPE:
                _safe_print(
                    "An 'until' standing approval cannot be granted "
                    "non-interactively; approve it from the interactive menu, "
                    "or approve run or session scope here.",
                    stream=stream,
                )
                return ConsentDecision(Consent.REQUIRED)
            if scope == SESSION_SCOPE and SESSION_SCOPE not in scopes:
                _safe_print(
                    "A session-scope approval is not available here; approve "
                    "run scope, or set the scope from the interactive menu.",
                    stream=stream,
                )
                return ConsentDecision(Consent.REQUIRED)
            _safe_print(
                f"Fallback approved for {proposal.approval_key} "
                f"{_scope_phrase(scope, expires_at)}.",
                stream=stream,
            )
            return ConsentDecision(Consent.APPROVED, scope, expires_at)
        _safe_print(
            "The supplied approval names a different candidate and was not "
            "accepted.",
            stream=stream,
        )

    tty = tty_opener()
    if tty is not None and tty.isatty():
        label = _candidate_label(candidate)
        expires_at = (
            reset_time.timestamp() if reset_time is not None else None
        )
        numbered: list[tuple[str, float | None]] = [
            (scope, expires_at if scope == UNTIL_SCOPE else None)
            for scope in scopes
        ]
        try:
            tty.write("Choose how to continue:\n")
            for index, (scope, scope_expiry) in enumerate(numbered, start=1):
                tty.write(
                    f"  {index}) Fall back to {label} "
                    f"{_scope_phrase(scope, scope_expiry)}\n"
                )
            stop_number = len(numbered) + 1
            tty.write(f"  {stop_number}) Stop here\n")
            tty.write(f"Choice [1-{stop_number}, Enter stops]: ")
            tty.flush()
            answer = tty.readline().strip().lower()
        finally:
            tty.close()

        chosen: tuple[str, float | None] | None = None
        if answer in {"y", "yes"}:
            chosen = (RUN_SCOPE, None)
        elif answer.isdigit() and 1 <= int(answer) <= len(numbered):
            chosen = numbered[int(answer) - 1]
        if chosen is not None:
            _safe_print(
                f"Fallback approved for {proposal.approval_key} "
                f"{_scope_phrase(chosen[0], chosen[1])}.",
                stream=stream,
            )
            return ConsentDecision(Consent.APPROVED, chosen[0], chosen[1])
        _safe_print(
            "Fallback declined; no substitution was made.", stream=stream
        )
        return ConsentDecision(Consent.DECLINED)

    _safe_print("ORRERY FALLBACK APPROVAL REQUIRED", stream=stream)
    scope_entries = [
        f"{UNTIL_SCOPE}:{reset_time.isoformat()}"
        if scope == UNTIL_SCOPE and reset_time is not None
        else scope
        for scope in scopes
    ]
    _safe_print(
        f"Candidate: {_candidate_label(candidate)}",
        stream=stream,
    )
    _safe_print(f"Scopes: {', '.join(scope_entries)}", stream=stream)
    # A non-interactive rerun may not mint an `until` standing approval, so
    # it is not advised as a rerun scope even when it is offered; it stays
    # in the Scopes line above because the interactive menu can still grant
    # it.
    rerun_entries = [
        entry
        for entry in scope_entries
        if entry != UNTIL_SCOPE and not entry.startswith(f"{UNTIL_SCOPE}:")
    ]
    _safe_print(
        f"Rerun with: --approve-fallback {proposal.approval_key} "
        f"--approval-scope {'|'.join(rerun_entries)}",
        stream=stream,
    )
    if len(rerun_entries) != len(scope_entries):
        _safe_print(
            "An 'until' standing approval is available only from the "
            "interactive menu, not from a non-interactive rerun.",
            stream=stream,
        )
    _safe_print(
        "Ask the user for explicit approval, then rerun the same command with "
        f"`--approve-fallback {proposal.approval_key}` before `--`.",
        stream=stream,
    )
    _safe_print(
        f"{program_name} did not start the proposed fallback.",
        stream=stream,
    )
    return ConsentDecision(Consent.REQUIRED)
