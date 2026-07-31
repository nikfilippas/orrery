#!/usr/bin/env python3
"""Validated role loading and static provider adapters for Orrery."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


KIT_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = KIT_DIR / "global" / "orchestration.json"
CATALOGUE_PATH = KIT_DIR / "global" / "model-catalogue.json"
PROVIDERS = frozenset({"anthropic", "openai"})
ROLE_IDS = frozenset(
    {"orchestrator", "mechanic", "implementer", "plan-reviewer", "reviewer"}
)
ACCESS_LEVELS = frozenset({"principal", "workspace-write", "read-only"})
THINKING_LEVEL = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MODEL_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-\[\]]{0,119}$"
)


class RuntimeConfigError(Exception):
    """The canonical runtime configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class Role:
    id: str
    title: str
    provider: str
    model: str
    thinking: str | None
    access: str

    @property
    def read_only(self) -> bool:
        return self.access == "read-only"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(
            f"the orchestration manifest is unreadable: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise RuntimeConfigError("the orchestration manifest must be an object")
    return manifest


def load_catalogue(
    path: Path = CATALOGUE_PATH,
) -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(path.read_text())
        providers = data["providers"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError(
            f"the model catalogue is unreadable: {exc}"
        ) from exc
    if not isinstance(providers, dict):
        raise RuntimeConfigError(
            "the model catalogue providers must be an object"
        )
    parsed: dict[str, list[dict[str, Any]]] = {}
    for provider, entries in providers.items():
        if provider not in PROVIDERS or not isinstance(entries, list):
            raise RuntimeConfigError(
                f"the model catalogue has an invalid provider: {provider!r}"
            )
        parsed[provider] = [
            entry for entry in entries if isinstance(entry, dict)
        ]
    return parsed


def project_override(cwd: Path) -> dict[str, Any] | None:
    current = cwd.resolve(strict=False)
    for directory in (current, *current.parents):
        candidate = directory / ".orrery.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeConfigError(
                    f"the repository override is unreadable: {exc}"
                ) from exc
            if not isinstance(data, dict):
                raise RuntimeConfigError(
                    "the repository override must be a JSON object"
                )
            override = data.get("orchestrator")
            if override is None:
                return None
            if not isinstance(override, dict):
                raise RuntimeConfigError(
                    "the repository orchestrator override must be an object"
                )
            return override
        if (directory / ".git").exists():
            break
    return None


def load_role(
    role_id: str,
    path: Path = MANIFEST_PATH,
    *,
    cwd: Path | None = None,
) -> Role:
    if role_id not in ROLE_IDS:
        raise RuntimeConfigError(f"unknown Orrery role: {role_id}")
    steps = load_manifest(path).get("steps")
    if not isinstance(steps, list):
        raise RuntimeConfigError("the orchestration manifest has no role list")
    matches = [
        step for step in steps
        if isinstance(step, dict) and step.get("id") == role_id
    ]
    if len(matches) != 1:
        raise RuntimeConfigError(
            f"the orchestration manifest must define {role_id} exactly once"
        )
    step = dict(matches[0])
    if role_id == "orchestrator" and path == MANIFEST_PATH:
        override = project_override(cwd or Path.cwd())
        if override is not None:
            unknown = set(override) - {"provider", "model", "thinking"}
            if unknown:
                raise RuntimeConfigError(
                    "the repository orchestrator override contains unknown "
                    f"fields: {sorted(unknown)}"
                )
            step.update(override)
    title = step.get("title")
    provider = step.get("provider")
    model = step.get("model")
    thinking = step.get("thinking")
    access = step.get("access")
    if not isinstance(title, str) or not title.strip():
        raise RuntimeConfigError(f"{role_id} has no title")
    if provider not in PROVIDERS:
        raise RuntimeConfigError(f"{role_id} has invalid provider: {provider!r}")
    if not isinstance(model, str) or not model.strip():
        raise RuntimeConfigError(f"{role_id} has no model")
    if not MODEL_ID.fullmatch(model):
        raise RuntimeConfigError(
            f"{role_id} has an invalid model identifier"
        )
    if (
        thinking is not None
        and (
            not isinstance(thinking, str)
            or not THINKING_LEVEL.fullmatch(thinking)
        )
    ):
        raise RuntimeConfigError(
            f"{role_id} has invalid thinking level: {thinking!r}"
        )
    if access not in ACCESS_LEVELS:
        raise RuntimeConfigError(f"{role_id} has invalid access: {access!r}")
    if role_id == "orchestrator" and access != "principal":
        raise RuntimeConfigError("the orchestrator must use principal access")
    if role_id != "orchestrator" and access == "principal":
        raise RuntimeConfigError(f"{role_id} cannot use principal access")
    catalogue = load_catalogue()
    known_providers = [
        known_provider
        for known_provider, entries in catalogue.items()
        if any(entry.get("id") == model for entry in entries)
    ]
    if known_providers and provider not in known_providers:
        raise RuntimeConfigError(
            f"{model} belongs to {known_providers[0]}, not {provider}"
        )
    known = next(
        (
            entry
            for entry in catalogue.get(provider, [])
            if entry.get("id") == model
        ),
        None,
    )
    if known is not None:
        levels = known.get("thinking_levels")
        if not isinstance(levels, list):
            raise RuntimeConfigError(
                f"{model} has no valid thinking-level catalogue"
            )
        if thinking is not None and thinking not in levels:
            available = ", ".join(str(level) for level in levels) or "none"
            raise RuntimeConfigError(
                f"{model} does not support thinking {thinking}; "
                f"available: {available}"
            )
    return Role(
        id=role_id,
        title=title.strip(),
        provider=provider,
        model=model.strip(),
        thinking=thinking,
        access=access,
    )


def provider_executable(provider: str) -> str:
    if provider not in PROVIDERS:
        raise RuntimeConfigError(f"unknown provider: {provider}")
    command = "claude" if provider == "anthropic" else "codex"
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeConfigError(
            f"required command unavailable for {provider}: {command}"
        )
    return resolved


def thinking_override(thinking: str) -> str:
    """A TOML-safe Codex `-c` value."""
    return f"model_reasoning_effort={json.dumps(thinking)}"


def principal_command(role: Role, extra: list[str]) -> list[str]:
    if role.id != "orchestrator":
        raise RuntimeConfigError("only the orchestrator can start a principal")
    executable = provider_executable(role.provider)
    if role.provider == "anthropic":
        command = [
            executable,
            "--model",
            role.model,
            "--exclude-dynamic-system-prompt-sections",
        ]
        if role.thinking:
            command.extend(["--effort", role.thinking])
    else:
        command = [
            executable,
            "--model",
            role.model,
        ]
        if role.thinking:
            command.extend(["-c", thinking_override(role.thinking)])
        command.extend(["--sandbox", "workspace-write"])
    return [*command, *extra]


def role_handoff(role: Role, assignment: str) -> str:
    access = (
        "Read-only: do not modify files."
        if role.read_only
        else "Workspace-write: modify only what the assignment requires."
    )
    return (
        "ORRERY ROLE HANDOFF\n"
        f"Role: {role.id}\n"
        "This is a bounded non-principal session. Do not delegate, spawn "
        "another agent, or re-enter the orchestration workflow.\n"
        f"{access}\n\n"
        "Assignment:\n"
        f"{assignment.strip()}\n"
    )


def delegated_command(
    role: Role,
    verdict_path: Path,
    settings_path: Path | None,
) -> list[str]:
    if role.id == "orchestrator":
        raise RuntimeConfigError(
            "use the orrery launcher for the principal orchestrator"
        )
    executable = provider_executable(role.provider)
    if role.provider == "openai":
        command = [executable, "--model", role.model]
        if role.thinking:
            command.extend(["-c", thinking_override(role.thinking)])
        command.extend(
            [
                "exec",
                "--sandbox",
                role.access,
                "--ephemeral",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--output-last-message",
                str(verdict_path),
                "-",
            ]
        )
        return command

    if settings_path is None:
        raise RuntimeConfigError("Claude roles require a sandbox settings file")
    # CLAUDE_CODE_SUBPROCESS_ENV_SCRUB forces the default permission mode
    # unless the allowed tools are declared explicitly; without this list a
    # non-interactive delegated run aborts before its first real turn. One
    # comma-joined token, because the variadic flag would swallow whatever
    # argument follows it.
    allowed_tools = (
        "Read,Grep,Glob,Bash"
        if role.read_only
        else "Edit,Write,NotebookEdit,Read,Grep,Glob,Bash"
    )
    command = [
        executable,
        "--print",
        "--model",
        role.model,
        "--exclude-dynamic-system-prompt-sections",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--settings",
        str(settings_path),
        "--allowedTools",
        allowed_tools,
        "--permission-mode",
        "plan" if role.read_only else "acceptEdits",
    ]
    if role.thinking:
        command.extend(["--effort", role.thinking])
    return command


def claude_sandbox_settings(role: Role, cwd: Path) -> dict[str, Any]:
    sandbox: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": not role.read_only,
    }
    permissions: dict[str, Any] = {}
    if role.read_only:
        sandbox["filesystem"] = {"denyWrite": [str(cwd.resolve())]}
        permissions["deny"] = [
            "Edit",
            "Write",
            "NotebookEdit",
            "Bash(git commit *)",
            "Bash(git push *)",
        ]
    return {
        "sandbox": sandbox,
        "permissions": permissions,
    }


def provider_environment(provider: str, tmp_dir: Path) -> dict[str, str]:
    if provider not in PROVIDERS:
        raise RuntimeConfigError(f"unknown provider: {provider}")
    environment = {
        "HOME": str(Path.home()),
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": str(tmp_dir),
    }
    exact_names = {
        "ALL_PROXY",
        "CURL_CA_BUNDLE",
        "DBUS_SESSION_BUS_ADDRESS",
        "GIT_ASKPASS",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LOGNAME",
        "NO_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "SHELL",
        "SSH_ASKPASS",
        "SSH_AUTH_SOCK",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "USER",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    prefixes = (
        ("AZURE_OPENAI_", "CODEX_", "OPENAI_")
        if provider == "openai"
        else (
            "ANTHROPIC_",
            "AWS_",
            "CLAUDE_",
            "CLOUD_ML_",
            "GOOGLE_",
            "VERTEX_",
        )
    )
    for name, value in os.environ.items():
        if name in exact_names or name.startswith(prefixes) or name.startswith("LC_"):
            environment[name] = value
    if provider == "openai":
        environment["CODEX_HOME"] = str(codex_home())
    else:
        environment["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
    # A delegated role is an independent context contained by this runner,
    # not a child of the invoking Claude Code session. Forwarding the
    # parent's identity markers makes the parent's lifecycle tooling treat
    # the delegated process tree as its own unregistered residue and reap
    # it mid-run, and invites nested-session behaviour in the provider CLI.
    for marker in (
        "CLAUDECODE",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_SSE_PORT",
    ):
        environment.pop(marker, None)
    return environment


def codex_home() -> Path:
    """Resolve CODEX_HOME without treating an explicit empty value as absent."""
    raw = os.environ.get("CODEX_HOME")
    if raw is not None and not raw.strip():
        raise RuntimeConfigError("CODEX_HOME cannot be empty")
    return (
        Path(raw).expanduser().resolve(strict=False)
        if raw is not None
        else Path.home() / ".codex"
    )
