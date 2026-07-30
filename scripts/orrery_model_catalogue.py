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


def _claude_alias(value: str, resolved: str, seen: set[str]) -> str:
    """Return a stable family alias while preserving distinct version rows."""
    without_context = re.sub(r"\[[^\]]+\]$", "", value)
    if without_context in CLAUDE_ALIASES:
        return without_context

    # Current Claude Code exposes Fable as a full identifier while the other
    # current families have aliases. Preserve Orrery's documented Fable alias
    # for the first resolved family row, but leave any additional explicit
    # versions visible by their exact identifiers.
    if "fable" not in seen and re.search(r"(?:^|-)fable(?:-|$)", resolved):
        return "fable"
    return value


def _normalise_claude_models(raw_models: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_models, list):
        raise CatalogueDiscoveryError(
            "Claude initialization did not return a model list"
        )
    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_resolved: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        value = raw.get("value")
        resolved = raw.get("resolvedModel")
        if not isinstance(value, str) or not value or value == "default":
            continue
        if not isinstance(resolved, str):
            resolved = value
        model = _claude_alias(value, resolved, seen_ids)
        if (
            not EFFORT_NAME.fullmatch(model)
            and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:@/+\-\[\]]{0,119}",
                model,
            )
        ):
            raise CatalogueDiscoveryError(
                f"Claude reported an unsafe model identifier: {model!r}"
            )
        resolved_identity = re.sub(r"\[[^\]]+\]$", "", resolved)
        if model in seen_ids or resolved_identity in seen_resolved:
            continue
        supports_effort = raw.get("supportsEffort") is True
        levels = _safe_levels(
            raw.get("supportedEffortLevels"),
            f"Claude model {model}",
        ) if supports_effort else []
        entries.append(
            {
                "id": model,
                "label": model,
                "thinking_levels": levels,
                "default_thinking": levels[-1] if levels else None,
            }
        )
        seen_ids.add(model)
        seen_resolved.add(resolved_identity)
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
        if not isinstance(efforts, list):
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
                "thinking_levels": levels,
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
