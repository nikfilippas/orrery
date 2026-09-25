#!/usr/bin/env python3
"""Discover provider model capabilities without running a model.

Claude Code exposes its model catalogue in the same initialization response
used by the Claude Agent SDK. Codex exposes its catalogue through the
app-server ``model/list`` method. Orrery reads those two local interfaces so
new picker-visible models and their effort levels appear in the configuration
surface without a hand-maintained release.
"""

from __future__ import annotations

import copy
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_TIMEOUT_SECONDS = 15.0
EFFORT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CLAUDE_ALIASES = ("fable", "opus", "sonnet", "haiku")
# Identities are retained and later printed by the doctor, which reads
# its report line by line: an identifier carrying a newline could forge
# a verdict, so nothing that is not a plain identifier is kept at all.
# It has to accept exactly what orrery_runtime.MODEL_ID accepts, `~`
# included, or a legitimate identifier would abort the whole catalogue.
_IDENTIFIER = re.compile(r"[A-Za-z0-9~][A-Za-z0-9._:@/+~\-\[\]]{0,119}")


class CatalogueDiscoveryError(Exception):
    """One provider's local catalogue could not be discovered safely."""


@dataclass(frozen=True)
class DiscoveryResult:
    """An effective provider catalogue and how each half was obtained."""

    providers: dict[str, list[dict[str, Any]]]
    sources: dict[str, str]
    warnings: tuple[str, ...]


def _safe_levels(value: Any, context: str) -> list[str]:
    if not isinstance(value, list):
        return []
    levels: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not EFFORT_NAME.fullmatch(raw):
            raise CatalogueDiscoveryError(
                f"{context} reported an unsafe thinking level: {raw!r}"
            )
        if raw not in levels:
            levels.append(raw)
    return levels


def _terminate(process: subprocess.Popen[bytes]) -> None:
    """Reclaim a discovery subprocess and its descendants."""
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def _messages_until(
    process: subprocess.Popen[bytes],
    deadline: float,
    accept: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    """Read JSONL without text-buffer/select races until a message matches."""
    if process.stdout is None:
        raise CatalogueDiscoveryError("provider catalogue stdout is unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffered = b""
    try:
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            events = selector.select(min(0.25, remaining))
            if not events:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffered += chunk
            while b"\n" in buffered:
                raw, buffered = buffered.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(message, dict) and accept(message):
                    return message
    finally:
        selector.close()
    if process.poll() is None:
        raise CatalogueDiscoveryError("provider catalogue discovery timed out")
    raise CatalogueDiscoveryError(
        f"provider catalogue process exited with status {process.returncode}"
    )


def _start(
    command: list[str],
    *,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CatalogueDiscoveryError(
            f"could not start {command[0]}: {exc}"
        ) from exc


def _send(process: subprocess.Popen[bytes], message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise CatalogueDiscoveryError("provider catalogue stdin is unavailable")
    try:
        process.stdin.write(
            json.dumps(message, separators=(",", ":")).encode() + b"\n"
        )
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        raise CatalogueDiscoveryError(
            "provider catalogue process closed its input"
        ) from exc


def _claude_family(resolved: str) -> str | None:
    """The alias family a resolved identifier belongs to, if any."""
    for family in CLAUDE_ALIASES:
        if re.search(rf"(?:^|-){family}(?:-|$)", resolved):
            return family
    return None


def _family_version(resolved: str, family: str) -> tuple[int, ...]:
    """Numeric components following a family name, for ordering rows.

    `claude-fable-5-1` yields (5, 1) and `claude-opus-5` yields (5,), so
    two rows of one family order by version rather than by the order the
    provider happened to list them in. A row carrying no version yields
    the empty tuple and therefore loses to any versioned row.
    """
    match = re.search(rf"(?:^|-){re.escape(family)}((?:-\d+)*)", resolved)
    if match is None or not match.group(1):
        return ()
    return tuple(int(part) for part in match.group(1).split("-") if part)


def _strip_context(value: str) -> str:
    return re.sub(r"\[[^\]]+\]$", "", value)


def _bare_claude_id(value: str) -> str:
    """A Claude id with its packaging removed, leaving family and version.

    The context suffix goes first because it sits outermost, after any
    date: `claude-haiku-4-5-20251001[1m]` would otherwise keep its date.
    Then the Bedrock `us.anthropic.` prefix and `-v1:0` suffix, a Vertex
    `@20260901` with any `-v2` before it, and a snapshot's `-20251001`,
    so a date is never read as a version component and every form of
    one model compares equal.
    """
    bare = _strip_context(value).lower()
    bare = re.sub(r"^(?:[a-z0-9-]+\.)?anthropic\.", "", bare)
    bare = re.sub(r"-v\d+(?::\d+)?$", "", bare)
    bare = re.sub(r"(?:-v\d+)?@\d{8}$", "", bare)
    return re.sub(r"-\d{8}$", "", bare)


def _identity_version(bare: str, family: str) -> tuple[int, ...] | None:
    """The version a bare Claude id carries, or None where it has none.

    Read after the family, `claude-opus-5-5`, or where nothing follows
    it, before, as the legacy `claude-3-5-haiku` puts it. A run ending
    in `.`, `claude-opus-5.5`, is not a version this can read, so the
    id stays a literal rather than truncating to `claude-opus-5`.
    """
    after = re.search(rf"(?:^|-){re.escape(family)}((?:-\d+)*)(\.)?", bare)
    if after is None or after.group(2):
        return None
    run = after.group(1)
    if not run:
        before = re.search(
            rf"(?:^|-)((?:\d+-)+){re.escape(family)}(?:-|$)", bare
        )
        run = before.group(1) if before else ""
    version = tuple(int(part) for part in run.split("-") if part)
    return version or None


@dataclass(frozen=True)
class ModelIdentity:
    """Which underlying model an identifier names, for comparison only.

    Three shapes, never mixed. A versioned Claude id carries its family
    and version tuple. A bare alias nothing has resolved carries its
    family and no version, and stands for every version of it, since
    the CLI decides which one it runs. Anything else, every OpenAI id
    and any custom one, carries only its suffix-stripped literal, so two
    such ids can never meet through an empty version tuple.
    """

    provider: str
    family: str | None
    version: tuple[int, ...] | None
    literal: str | None

    def matches(self, other: ModelIdentity) -> bool:
        if self.provider != other.provider:
            return False
        if self.literal is not None or other.literal is not None:
            return self.literal == other.literal
        if self.family != other.family:
            return False
        # An unresolved alias matches its whole family: that is all
        # anything knows about it, and treating it as distinct from a
        # version it may be running would let a failed or excluded model
        # come back under its other name.
        return (
            self.version is None
            or other.version is None
            or self.version == other.version
        )


def model_identity(
    provider: str,
    model: str,
    resolved: str | None = None,
) -> ModelIdentity:
    """The identity every comparison of two model ids goes through.

    `resolved` is discovery's statement of what an alias runs now, and
    is read first where a caller has it. No prefix rule: `claude-opus-5`
    and `claude-opus-5-5` differ in their version tuple, and a context
    suffix such as `[1m]` is the same model with a larger window.
    """
    if provider == "anthropic":
        for name in (resolved, model):
            if not name:
                continue
            bare = _bare_claude_id(name)
            family = _claude_family(bare)
            if family is None:
                continue
            version = _identity_version(bare, family)
            if version:
                return ModelIdentity(provider, family, version, None)
        bare = _strip_context(model).lower()
        if bare in CLAUDE_ALIASES:
            return ModelIdentity(provider, bare, None, None)
    return ModelIdentity(provider, None, None, _strip_context(model))


def _normalise_claude_models(raw_models: Any) -> list[dict[str, Any]]:
    """Selectable Claude models, with identity retained.

    Each entry keeps three things rather than one: `id`, the alias a
    manifest is written against; `selectable`, the exact value the picker
    offers; and `resolved`, the provider's own resolved identifier. The
    alias used to be assigned to whichever family row appeared first,
    which let a reordering silently move `fable` onto a different
    version and could collapse an exactly pinned identifier away. It is
    now assigned to the highest version of its family, and every other
    row of that family keeps its exact identifier.
    """
    if not isinstance(raw_models, list):
        raise CatalogueDiscoveryError(
            "Claude initialization did not return a model list"
        )

    rows: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        value = raw.get("value")
        resolved = raw.get("resolvedModel")
        if not isinstance(value, str) or not value or value == "default":
            continue
        if not isinstance(resolved, str) or not resolved:
            resolved = value
        native = _strip_context(value)
        family = native if native in CLAUDE_ALIASES else _claude_family(resolved)
        declared = raw.get("supportsEffort")
        offered = raw.get("supportedEffortLevels")
        supports_effort = declared is True
        levels = _safe_levels(
            offered,
            f"Claude model {value}",
        ) if supports_effort else []
        # An empty level list means two different things: the provider
        # said this model has no effort levels, or it said nothing
        # usable. Only the first justifies calling a configured level
        # withdrawn, so which one it was is recorded rather than
        # guessed. `supportsEffort: true` carrying no level list is the
        # second case however loudly it asserts the first: it says
        # effort exists without saying which levels, so the levels are
        # unknown, not withdrawn.
        stated = declared is False or (
            supports_effort and isinstance(offered, list)
        )
        for identity in (value, resolved):
            if not _IDENTIFIER.fullmatch(identity):
                raise CatalogueDiscoveryError(
                    f"Claude reported an unsafe model identifier: {identity!r}"
                )
        rows.append(
            {
                "value": value,
                "resolved": resolved,
                "resolved_identity": _strip_context(resolved),
                "native": native if native in CLAUDE_ALIASES else None,
                "family": family,
                "levels": levels,
                "thinking_stated": stated,
            }
        )

    # A family's alias goes to its highest version, deterministically,
    # and only among rows that do not already carry a native alias.
    alias_winner: dict[str, str] = {}
    reserved = {row["native"] for row in rows if row["native"] is not None}
    for family in CLAUDE_ALIASES:
        # A provider that offers the alias itself owns it. Letting an
        # exact row also claim it produced two rows with one id, and
        # de-duplication then dropped whichever arrived second, which is
        # order-dependence wearing a deterministic hat.
        if family in reserved:
            continue
        contenders = [
            row for row in rows
            if row["native"] is None and row["family"] == family
        ]
        if not contenders:
            continue
        best = max(
            contenders,
            key=lambda row: (
                _family_version(row["resolved_identity"], family),
                row["value"],
            ),
        )
        alias_winner[family] = best["value"]

    def rank(row: dict[str, Any]) -> tuple[int, str]:
        """How strong a claim a row has on its resolved identity."""
        if row["native"] is not None:
            return (0, row["value"])
        if alias_winner.get(row["family"]) == row["value"]:
            return (1, row["value"])
        return (2, row["value"])

    # Two picker rows can resolve to one underlying model, and only one
    # of them is kept. Reserving the alias was not enough on its own:
    # the survivor was still whichever row the provider happened to
    # list first, so a native `sonnet` sitting behind an exact
    # `claude-sonnet-5[1m]` with the same resolved identity vanished,
    # and a role configured on `sonnet` was then reported unavailable.
    # The winner is now chosen before anything is emitted.
    resolved_winner: dict[str, str] = {}
    for row in rows:
        identity = row["resolved_identity"]
        current = resolved_winner.get(identity)
        if current is None:
            resolved_winner[identity] = row["value"]
            continue
        held = next(item for item in rows if item["value"] == current)
        if rank(row) < rank(held):
            resolved_winner[identity] = row["value"]

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        if row["native"] is not None:
            model = row["native"]
        elif alias_winner.get(row["family"]) == row["value"]:
            model = row["family"]
        else:
            model = row["value"]
        if (
            not EFFORT_NAME.fullmatch(model)
            and not _IDENTIFIER.fullmatch(model)
        ):
            raise CatalogueDiscoveryError(
                f"Claude reported an unsafe model identifier: {model!r}"
            )
        if resolved_winner[row["resolved_identity"]] != row["value"]:
            continue
        if model in seen_ids:
            continue
        levels = row["levels"]
        entries.append(
            {
                "id": model,
                "label": model,
                "selectable": row["value"],
                "resolved": row["resolved_identity"],
                "thinking_levels": levels,
                "thinking_stated": row["thinking_stated"],
                "default_thinking": levels[-1] if levels else None,
            }
        )
        seen_ids.add(model)
    if not entries:
        raise CatalogueDiscoveryError("Claude returned no selectable models")
    return entries


def discover_claude_models(
    executable: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    environment: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read Claude Code's Agent-SDK initialization model metadata."""
    env = dict(os.environ if environment is None else environment)
    env["CLAUDE_CODE_ENTRYPOINT"] = "sdk-ts"
    env.pop("NODE_OPTIONS", None)
    process = _start(
        [
            executable,
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
            "--no-session-persistence",
            "--tools",
            "",
            "--setting-sources=",
            "--strict-mcp-config",
        ],
        environment=env,
    )
    request_id = f"orrery-{uuid.uuid4().hex}"
    try:
        _send(
            process,
            {
                "request_id": request_id,
                "type": "control_request",
                "request": {"subtype": "initialize"},
            },
        )
        response = _messages_until(
            process,
            time.monotonic() + timeout,
            lambda message: (
                message.get("type") == "control_response"
                and message.get("response", {}).get("request_id") == request_id
            ),
        ).get("response", {})
        if response.get("subtype") != "success":
            raise CatalogueDiscoveryError(
                f"Claude catalogue request failed: {response.get('error', 'error')}"
            )
        payload = response.get("response")
        if not isinstance(payload, dict):
            raise CatalogueDiscoveryError(
                "Claude catalogue response has no initialization payload"
            )
        return _normalise_claude_models(payload.get("models"))
    finally:
        _terminate(process)


def _normalise_codex_models(raw_models: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_models, list):
        raise CatalogueDiscoveryError("Codex model/list returned no data list")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, dict) or raw.get("hidden") is True:
            continue
        model = raw.get("model") or raw.get("id")
        if not isinstance(model, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,119}",
            model,
        ):
            raise CatalogueDiscoveryError(
                f"Codex reported an unsafe model identifier: {model!r}"
            )
        if model in seen:
            continue
        efforts = raw.get("supportedReasoningEfforts")
        stated = isinstance(efforts, list)
        if not stated:
            efforts = []
        levels = _safe_levels(
            [
                effort.get("reasoningEffort")
                for effort in efforts
                if isinstance(effort, dict)
            ],
            f"Codex model {model}",
        )
        default = raw.get("defaultReasoningEffort")
        if default not in levels:
            default = levels[0] if levels else None
        entries.append(
            {
                "id": model,
                "label": model,
                # Codex exposes exact identifiers rather than aliases, so
                # all three identities coincide; they are recorded anyway
                # so consumers need not special-case the provider.
                "selectable": model,
                "resolved": model,
                "thinking_levels": levels,
                "thinking_stated": stated,
                "default_thinking": default,
            }
        )
        seen.add(model)
    if not entries:
        raise CatalogueDiscoveryError("Codex returned no selectable models")
    return entries


def discover_codex_models(
    executable: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    environment: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read every picker-visible Codex model through app-server."""
    env = dict(os.environ if environment is None else environment)
    process = _start([executable, "app-server"], environment=env)
    deadline = time.monotonic() + timeout
    request_id = 2
    models: list[Any] = []
    cursor: str | None = None
    try:
        _send(
            process,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "orrery",
                        "title": "Orrery",
                        "version": "1",
                    }
                },
            },
        )
        initialized = _messages_until(
            process,
            deadline,
            lambda message: message.get("id") == 1,
        )
        if "error" in initialized:
            raise CatalogueDiscoveryError(
                f"Codex initialize failed: {initialized['error']}"
            )
        _send(process, {"method": "initialized", "params": {}})

        while True:
            params: dict[str, Any] = {
                "limit": 100,
                "includeHidden": False,
            }
            if cursor is not None:
                params["cursor"] = cursor
            _send(
                process,
                {
                    "method": "model/list",
                    "id": request_id,
                    "params": params,
                },
            )
            response = _messages_until(
                process,
                deadline,
                lambda message, expected=request_id: (
                    message.get("id") == expected
                ),
            )
            if "error" in response:
                raise CatalogueDiscoveryError(
                    f"Codex model/list failed: {response['error']}"
                )
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(
                result.get("data"), list
            ):
                raise CatalogueDiscoveryError(
                    "Codex model/list returned an invalid result"
                )
            models.extend(result["data"])
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
            request_id += 1
        return _normalise_codex_models(models)
    finally:
        _terminate(process)


def _ordered_with_fallback(
    live: list[dict[str, Any]],
    fallback: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep familiar aliases first, then append newly discovered models."""
    by_id = {entry["id"]: dict(entry) for entry in live}
    ordered: list[dict[str, Any]] = []
    for seed in fallback:
        model = seed.get("id")
        if model not in by_id:
            continue
        entry = by_id.pop(model)
        seeded_default = seed.get("default_thinking")
        if seeded_default in entry.get("thinking_levels", []):
            entry["default_thinking"] = seeded_default
        seeded_tier = seed.get("fallback_tier")
        if (
            isinstance(seeded_tier, int)
            and not isinstance(seeded_tier, bool)
            and 1 <= seeded_tier <= 3
        ):
            entry["fallback_tier"] = seeded_tier
        ordered.append(entry)
    ordered.extend(
        entry for entry in live if entry["id"] in by_id
    )
    return ordered


def discover_catalogue(
    fallback: dict[str, list[dict[str, Any]]],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    environment: dict[str, str] | None = None,
) -> DiscoveryResult:
    """Discover both providers concurrently, falling back independently."""
    env = dict(os.environ if environment is None else environment)
    providers = copy.deepcopy(fallback)
    sources = {provider: "fallback" for provider in fallback}
    disabled = env.get("ORRERY_MODEL_DISCOVERY", "").strip().lower()
    if disabled in {"0", "false", "no", "off"}:
        return DiscoveryResult(providers, sources, ())

    path = env.get("PATH")
    commands = {
        "anthropic": shutil.which("claude", path=path),
        "openai": shutil.which("codex", path=path),
    }
    discoverers: dict[
        str,
        tuple[Callable[..., list[dict[str, Any]]], str],
    ] = {}
    if commands["anthropic"]:
        discoverers["anthropic"] = (
            discover_claude_models,
            commands["anthropic"],
        )
    if commands["openai"]:
        discoverers["openai"] = (
            discover_codex_models,
            commands["openai"],
        )

    warnings = [
        f"{provider}: {provider} CLI is unavailable; using bundled fallback"
        for provider, executable in commands.items()
        if executable is None
    ]
    with ThreadPoolExecutor(max_workers=max(1, len(discoverers))) as pool:
        futures = {
            pool.submit(
                discoverer,
                executable,
                timeout=timeout,
                environment=env,
            ): provider
            for provider, (discoverer, executable) in discoverers.items()
        }
        for future in as_completed(futures):
            provider = futures[future]
            try:
                live = future.result()
            except Exception as exc:  # noqa: BLE001 - provider fallback boundary
                warnings.append(
                    f"{provider}: {exc}; using bundled fallback"
                )
                continue
            providers[provider] = _ordered_with_fallback(
                live,
                fallback.get(provider, []),
            )
            sources[provider] = "installed CLI"

    return DiscoveryResult(
        providers=providers,
        sources=sources,
        warnings=tuple(warnings),
    )
