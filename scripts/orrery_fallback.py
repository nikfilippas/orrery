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

import os
import re
import shutil
import subprocess
import sys
from hashlib import sha256
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import IO, Any, Callable, Iterable


sys.path.insert(0, str(Path(__file__).resolve().parent))

from orrery_model_catalogue import (  # noqa: E402
    CatalogueDiscoveryError,
    discover_claude_models,
    discover_codex_models,
)
from orrery_runtime import (  # noqa: E402
    MODEL_ID,
    PROVIDERS,
    Role,
    RuntimeConfigError,
    load_catalogue,
    load_manifest,
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


def configured_model_tiers() -> dict[tuple[str, str], int]:
    tiers: dict[tuple[str, str], int] = {}
    for step in load_manifest().get("steps", []):
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

    seed_by_id = {entry.get("id"): entry for entry in bundled}
    merged: list[dict[str, Any]] = []
    for entry in live:
        candidate = dict(entry)
        seed = seed_by_id.get(candidate.get("id"), {})
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
) -> tuple[Availability, str]:
    """Check a bundled-known model against a live picker without inference."""
    bundled = load_catalogue().get(role.provider, [])
    if not any(entry.get("id") == role.model for entry in bundled):
        return (
            Availability.UNKNOWN,
            f"{role.provider}/{role.model} is a custom model identifier",
        )
    env = dict(os.environ if environment is None else environment)
    entries, source = _catalogue_entries(
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
    return (
        Availability.UNAVAILABLE,
        f"{role.provider}/{role.model} is not picker-visible in the installed CLI",
    )


def _thinking_for(
    original: Role,
    candidate: dict[str, Any],
    source_entries: Iterable[dict[str, Any]],
) -> str | None:
    levels = candidate.get("thinking_levels")
    if not isinstance(levels, list) or not levels:
        return None

    source = next(
        (
            entry
            for entry in source_entries
            if entry.get("id") == original.model
        ),
        None,
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
) -> FallbackProposal | None:
    """Return the closest potentially usable role without authorising it."""
    env = dict(os.environ if environment is None else environment)
    excluded_provider_set = set(excluded_providers)
    excluded_model_set = set(excluded_models)
    assumed = set(assumed_ready)
    supplied_statuses = {} if statuses is None else dict(statuses)
    additions = {} if additional_models is None else additional_models
    configured_tiers = configured_model_tiers()
    bundled = load_catalogue()
    source_entries = bundled.get(original.provider, [])
    source_seed = next(
        (
            entry
            for entry in source_entries
            if entry.get("id") == original.model
        ),
        None,
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
        tuple[tuple[int, int, int, int, str, str], Role, str]
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

        ordered = list(by_id.values())
        for index, entry in enumerate(ordered):
            model = entry["id"]
            identity = (provider, model)
            if identity == (original.provider, original.model):
                continue
            if identity in excluded_model_set:
                continue
            tier = entry.get("fallback_tier")
            if not isinstance(tier, int) or isinstance(tier, bool):
                tier = configured_tiers.get(
                    identity,
                    _picker_tier(index, len(ordered)),
                )
            thinking = _thinking_for(original, entry, source_entries)
            candidate = replace(
                original,
                provider=provider,
                model=model,
                thinking=thinking,
            )
            configured_distance = (
                abs(configured_tiers[identity] - target_tier)
                if identity in configured_tiers
                else 4
            )
            score = (
                0 if provider == original.provider else 1,
                abs(tier - target_tier),
                configured_distance,
                0 if status.state is Availability.READY else 1,
                provider,
                f"{index:05d}:{model}",
            )
            ranked.append((score, candidate, source))

    if not ranked:
        return None
    _score, candidate, catalogue_source = min(ranked, key=lambda item: item[0])
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


def proposal_for_approval(
    original: Role,
    approval: tuple[str, str],
    *,
    environment: dict[str, str] | None = None,
) -> tuple[FallbackProposal | None, set[str], set[tuple[str, str]]]:
    """Resolve an approval from a previous failed invocation.

    A cross-provider approval means the configured provider already failed;
    a same-provider approval means only the configured model failed. This
    lets a rerun start the exact approved candidate without needlessly
    retrying the known-bad process.
    """
    excluded_providers: set[str] = set()
    excluded_models: set[tuple[str, str]] = set()
    if approval[0] == original.provider:
        excluded_models.add((original.provider, original.model))
    else:
        excluded_providers.add(original.provider)

    proposal = nearest_fallback(
        original,
        "the user approved a candidate proposed by an earlier failed attempt",
        excluded_providers=excluded_providers,
        excluded_models=excluded_models,
        environment=environment,
    )
    return proposal, excluded_providers, excluded_models


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
                return None
            outputs.append(result.stdout)
            digest.update(b"\0command\0")
            digest.update(result.stdout)

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
    try:
        return open("/dev/tty", "r+", encoding="utf-8", buffering=1)
    except OSError:
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
    _safe_print(
        f"Rerun with: --approve-fallback {proposal.approval_key} "
        f"--approval-scope {'|'.join(scope_entries)}",
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
