#!/usr/bin/env python3
"""Deterministic regression tests for the Órrery orchestration kit.

Covers the settings updater's atomicity, locking and ownership rules, and the
direct review wrapper's systemd cleanup, signal handling and CODEX_HOME
normalisation. The tests never call a real model and never touch the live
Claude or Codex configuration.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable

# Loading the entry points as modules would otherwise leave __pycache__
# directories inside the kit.
sys.dont_write_bytecode = True

KIT_DIR = Path(__file__).resolve().parent.parent
SETTINGS_SCRIPT = KIT_DIR / "scripts" / "apply-claude-settings.py"
REVIEW_SCRIPT = KIT_DIR / "scripts" / "orrery-review"
PRINCIPAL_SCRIPT = KIT_DIR / "scripts" / "orrery"
RUNTIME_SCRIPT = KIT_DIR / "scripts" / "orrery_runtime.py"
FALLBACK_SCRIPT = KIT_DIR / "scripts" / "orrery_fallback.py"
SESSION_START_SCRIPT = KIT_DIR / "scripts" / "orrery-session-start"
CONFIG_SCRIPT = KIT_DIR / "scripts" / "orrery-config"
INSTALL_SCRIPT = KIT_DIR / "scripts" / "install.sh"
DOCTOR_SCRIPT = KIT_DIR / "scripts" / "doctor.sh"
FAKE_CODEX = Path(__file__).resolve().parent / "fake-codex"
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake-claude"

UNIT_GLOB = "orrery-review-*"

TESTS: list[tuple[str, Callable[[], None]]] = []


def test(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def register(function: Callable[[], None]) -> Callable[[], None]:
        TESTS.append((name, function))
        return function

    return register


class Failure(AssertionError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def load_script(path: Path, name: str) -> types.ModuleType:
    # These entry points are executables without a .py suffix, so the loader
    # has to be named explicitly.
    spec = importlib.util.spec_from_file_location(
        name,
        path,
        loader=importlib.machinery.SourceFileLoader(name, str(path)),
    )
    if spec is None or spec.loader is None:
        raise Failure(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


settings_module = load_script(SETTINGS_SCRIPT, "kit_apply_claude_settings")
review_module = load_script(REVIEW_SCRIPT, "kit_orrery_review")
runtime_module = sys.modules["orrery_runtime"]
fallback_module = sys.modules["orrery_fallback"]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


CANONICAL = {
    "model": "opus",
    "enabledPlugins": {"codex@openai-codex": False},
    "hooks": {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            'python3 "$HOME/.claude/hooks/leave-no-trace.py" '
                            "hook-cleanup"
                        ),
                        "timeout": 30,
                    }
                ],
            }
        ]
    },
}


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def run_settings(
    *arguments: str,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SETTINGS_SCRIPT), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    if expect_success and result.returncode != 0:
        raise Failure(
            f"apply-claude-settings.py failed: {result.stderr.strip()}"
        )
    return result


def sidecar_residue(directory: Path, target_name: str) -> list[str]:
    """Same-directory temporary files the updater must never leave behind."""
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.name.startswith(f".{target_name}.")
        and not entry.name.endswith(".orrery.lock")
    )


def completed(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["systemctl"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def show_output(load_state: str, active_state: str) -> str:
    return f"LoadState={load_state}\nActiveState={active_state}\n"


def list_review_units() -> list[str]:
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "list-units",
            UNIT_GLOB,
            "--all",
            "--no-legend",
            "--no-pager",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=20,
        check=False,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def review_runtime_root() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or str(Path.home() / ".cache")
    return Path(base) / "orrery-review"


def runtime_residue() -> list[str]:
    root = review_runtime_root()
    if not root.exists():
        return []
    return sorted(entry.name for entry in root.iterdir())


def review_environment(mode: str, marker: Path | None = None) -> dict[str, str]:
    bin_dir = Path(tempfile.mkdtemp(prefix="kit-fake-bin."))
    shutil.copy2(FAKE_CODEX, bin_dir / "codex")
    shutil.copy2(FAKE_CLAUDE, bin_dir / "claude")
    (bin_dir / "codex").chmod(0o755)
    (bin_dir / "claude").chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}"
    environment["CODEX_FAKE_MODE"] = mode
    environment["CLAUDE_FAKE_MODE"] = mode
    environment["KIT_FAKE_BIN"] = str(bin_dir)
    if marker is not None:
        environment["CODEX_FAKE_MARKER"] = str(marker)
    return environment


def start_review(
    environment: dict[str, str],
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(REVIEW_SCRIPT), *arguments],
        env=environment,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def run_principal(
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [sys.executable, str(PRINCIPAL_SCRIPT), *arguments],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)


def finish_review(
    process: subprocess.Popen[str],
    environment: dict[str, str],
    timeout: float = 120.0,
) -> tuple[str, str]:
    """Collect a wrapper's output, reclaiming everything on every path.

    A test that fails or times out must not itself leak the wrapper, its
    transient unit, or the fake Codex directory: this suite polices exactly
    that behaviour in the code under test.
    """
    try:
        return process.communicate(timeout=timeout)
    finally:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.communicate(timeout=20)
        stop_stray_units(f"orrery-review-{process.pid}-")
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)


def wait_for_unit(process: subprocess.Popen[str], timeout: float = 20.0) -> str:
    """Return the transient unit name once systemd has registered it."""
    deadline = time.monotonic() + timeout
    prefix = f"orrery-review-{process.pid}-"

    while time.monotonic() < deadline:
        for line in list_review_units():
            name = line.strip().lstrip("● ").split()[0]
            if name.startswith(prefix):
                return name
        if process.poll() is not None:
            raise Failure("review wrapper exited before registering a unit")
        time.sleep(0.05)

    raise Failure("transient review unit never appeared")


def stop_stray_units(prefix: str) -> None:
    for line in list_review_units():
        name = line.strip().lstrip("● ").split()[0]
        if name.startswith(prefix):
            subprocess.run(
                ["systemctl", "--user", "stop", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )


def assert_no_review_residue(prefix: str) -> None:
    deadline = time.monotonic() + 10.0
    units: list[str] = []
    while time.monotonic() < deadline:
        units = [
            line
            for line in list_review_units()
            if line.strip().lstrip("● ").split()[0].startswith(prefix)
        ]
        if not units:
            break
        time.sleep(0.1)

    # Only this wrapper's own run directories. The runtime root is shared,
    # so reading it whole would blame this test for the state of an
    # unrelated review, or a second copy of this suite, running concurrently.
    owner = re.search(r"-(\d+)-$", prefix)
    marker = f"run.{owner.group(1)}." if owner else "run."
    residue = [name for name in runtime_residue() if name.startswith(marker)]
    if units or residue:
        stop_stray_units(prefix)
        raise Failure(f"residue left behind: units={units} runtime={residue}")


# ---------------------------------------------------------------------------
# Settings updater: ownership and preservation
# ---------------------------------------------------------------------------


@test("unrelated hooks, near-match commands and empty groups survive")
def test_hook_preservation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(
            target,
            {
                "model": "sonnet",
                "unrelatedSetting": {"keep": True},
                "enabledPlugins": {
                    "codex@openai-codex": True,
                    "other@example": True,
                },
                "hooks": {
                    "Notification": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "notify-send x"}
                            ],
                        }
                    ],
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/opt/not-leave-no-trace.py",
                                }
                            ]
                        },
                        {"hooks": []},
                    ],
                },
            },
        )

        run_settings(
            "--all",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        data = read_json(target)

        require(data["model"] == "opus", "model not applied")
        require(
            data["enabledPlugins"]["codex@openai-codex"] is False,
            "companion not disabled",
        )
        require(
            data["enabledPlugins"]["other@example"] is True,
            "unrelated plugin lost",
        )
        require(
            data["unrelatedSetting"] == {"keep": True},
            "unrelated setting lost",
        )

        commands = [
            handler.get("command")
            for group in data["hooks"]["Stop"]
            for handler in group.get("hooks", [])
        ]
        require(
            "/opt/not-leave-no-trace.py" in commands,
            "near-match hook command was deleted",
        )
        require(
            any(group.get("hooks") == [] for group in data["hooks"]["Stop"]),
            "unrelated empty hook group was deleted",
        )
        require("Notification" in data["hooks"], "Notification hooks lost")


@test("canonical hooks are replaced rather than duplicated")
def test_hooks_not_duplicated() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {})

        canonical_command = CANONICAL["hooks"]["Stop"][0]["hooks"][0]["command"]

        for _ in range(3):
            run_settings(
                "--all",
                "--source",
                str(source),
                "--target",
                str(target),
            )

        data = read_json(target)
        occurrences = [
            handler.get("command")
            for group in data["hooks"]["Stop"]
            for handler in group.get("hooks", [])
        ].count(canonical_command)

        require(
            occurrences == 1,
            f"canonical hook installed {occurrences} times, expected once",
        )


@test("permission rules are added without disturbing the user's own")
def test_permissions_merged() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(
            source,
            {
                "permissions": {
                    "allow": ["Bash(codex:*)", "Bash(orrery-review:*)"]
                }
            },
        )
        write_json(
            target,
            {
                "permissions": {
                    "allow": [
                        "Bash(npm test:*)",
                        "Bash(codex:*)",
                        f"Bash({'claude' + '-codex'}-review:*)",
                        f"Bash({'claude' + '-codex'}-doctor:*)",
                    ],
                    "deny": ["Bash(rm:*)"],
                    "defaultMode": "acceptEdits",
                }
            },
        )

        run_settings(
            "--permissions",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        permissions = read_json(target)["permissions"]

        require(
            "Bash(orrery-review:*)" in permissions["allow"],
            "the toolkit rule was not added",
        )
        require(
            "Bash(npm test:*)" in permissions["allow"],
            "an unrelated allow rule was lost",
        )
        require(
            permissions["allow"].count("Bash(codex:*)") == 1,
            f"an already-present rule was duplicated: {permissions['allow']}",
        )
        require(permissions["deny"] == ["Bash(rm:*)"], "deny rules were lost")
        require(
            permissions["defaultMode"] == "acceptEdits",
            "the default permission mode was lost",
        )
        retired = "claude" + "-codex"
        require(
            all(retired not in rule for rule in permissions["allow"]),
            f"retired permission rules survived: {permissions['allow']}",
        )

        result = run_settings(
            "--permissions",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        require(
            "already installed" in result.stdout,
            f"a second run was not a no-op: {result.stdout.strip()}",
        )


@test("Claude thinking uses the supported persisted and max representations")
def test_claude_thinking_merge() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"

        write_json(
            source,
            {
                "model": "fable",
                "env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"},
            },
        )
        write_json(
            target,
            {
                "model": "sonnet",
                "effortLevel": "high",
                "env": {
                    "CLAUDE_CODE_EFFORT_LEVEL": "low",
                    "UNRELATED": "keep",
                },
            },
        )
        run_settings(
            "--effort",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        data = read_json(target)
        require(
            data.get("model") == "sonnet"
            and "effortLevel" not in data
            and data.get("env", {}).get("CLAUDE_CODE_EFFORT_LEVEL") == "max"
            and data["env"].get("UNRELATED") == "keep",
            f"max thinking merge was wrong: {data}",
        )

        write_json(source, {"model": "fable", "effortLevel": "xhigh"})
        run_settings(
            "--effort",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        data = read_json(target)
        require(
            data.get("effortLevel") == "xhigh"
            and "CLAUDE_CODE_EFFORT_LEVEL" not in data.get("env", {})
            and data.get("env", {}).get("UNRELATED") == "keep",
            f"persisted thinking merge was wrong: {data}",
        )

        # Repository initialization uses model-only application; it must
        # preserve a personal thinking choice already in that repository.
        write_json(source, {"model": "opus"})
        run_settings(
            "--model",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        require(
            read_json(target).get("effortLevel") == "xhigh",
            "--model changed a personal thinking choice",
        )

        before = target.read_text()
        for invalid in (
            {"effortLevel": "max"},
            {
                "effortLevel": "high",
                "env": {"CLAUDE_CODE_EFFORT_LEVEL": "max"},
            },
        ):
            write_json(source, invalid)
            result = run_settings(
                "--effort",
                "--source",
                str(source),
                "--target",
                str(target),
                expect_success=False,
            )
            require(
                result.returncode != 0 and target.read_text() == before,
                f"invalid Claude thinking changed live state: {invalid}",
            )


@test("the canonical permissions unblock Codex delegation")
def test_canonical_permissions() -> None:
    canonical = read_json(KIT_DIR / "global" / "claude-settings.json")
    allow = canonical.get("permissions", {}).get("allow", [])
    require(
        "Bash(codex:*)" in allow,
        f"delegation would stop for approval on every call: {allow}",
    )


@test("canonical companion enablement fails closed")
def test_companion_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "settings.json"
        original = {"enabledPlugins": {"codex@openai-codex": False}}
        write_json(target, original)

        for bad_value in (True, "false", None, 0):
            source = root / "bad.json"
            write_json(source, {"enabledPlugins": {"codex@openai-codex": bad_value}})
            result = run_settings(
                "--companion",
                "--source",
                str(source),
                "--target",
                str(target),
                expect_success=False,
            )
            require(
                result.returncode != 0,
                f"companion value {bad_value!r} was accepted",
            )
            require(
                read_json(target) == original,
                f"target mutated while rejecting {bad_value!r}",
            )


@test("a settings symlink stays a symlink and its referent is updated")
def test_symlink_preserved() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        referent = root / "real-settings.json"
        link = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(referent, {"model": "sonnet"})
        link.symlink_to(referent)

        run_settings(
            "--model",
            "--source",
            str(source),
            "--target",
            str(link),
        )

        require(link.is_symlink(), "settings symlink was replaced by a file")
        require(
            link.resolve() == referent.resolve(),
            "settings symlink now points elsewhere",
        )
        require(read_json(referent)["model"] == "opus", "referent not updated")


@test("replacement preserves the file mode and leaves no temporary residue")
def test_mode_and_no_residue() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet"})
        target.chmod(0o640)

        run_settings(
            "--model",
            "--source",
            str(source),
            "--target",
            str(target),
        )

        mode = stat.S_IMODE(target.stat().st_mode)
        require(mode == 0o640, f"mode changed to {oct(mode)}")

        residue = sidecar_residue(root, target.name)
        require(not residue, f"temporary residue left: {residue}")

        backups = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.name.startswith("settings.json.backup-orrery-")
        )
        require(len(backups) == 1, f"expected one backup, found {backups}")
        require(
            read_json(root / backups[0])["model"] == "sonnet",
            "backup does not hold the previous content",
        )


@test("a rerun with nothing to change writes nothing")
def test_idempotent() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {})

        run_settings("--all", "--source", str(source), "--target", str(target))
        before = target.stat().st_ino

        result = run_settings(
            "--all",
            "--source",
            str(source),
            "--target",
            str(target),
        )
        require(
            "already installed" in result.stdout,
            f"expected a no-op, got: {result.stdout.strip()}",
        )
        require(target.stat().st_ino == before, "no-op rewrote the file")

        backups = [
            entry
            for entry in root.iterdir()
            if entry.name.startswith("settings.json.backup-orrery-")
        ]
        require(len(backups) == 1, "a no-op created an extra backup")


# ---------------------------------------------------------------------------
# Settings updater: concurrency
# ---------------------------------------------------------------------------


@test("a non-cooperating write before publication is retried, not discarded")
def test_foreign_write_retried() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet", "padding": "x" * 200000})

        original_write = settings_module.write_temporary
        injected: list[int] = []

        def inject(target_path: Path, data: Any, mode: int) -> Path:
            temporary = original_write(target_path, data, mode)
            if not injected:
                injected.append(1)
                # A writer that does not take the sidecar lock replaces the
                # file after this attempt read it.
                foreign = target_path.with_name("foreign.json")
                write_json(
                    foreign,
                    {
                        "model": "sonnet",
                        "padding": "x" * 200000,
                        "concurrent": True,
                    },
                )
                os.replace(foreign, target_path)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                status = settings_module.main()
        finally:
            settings_module.write_temporary = original_write

        require(status == 0, f"updater returned {status}")
        data = read_json(target)
        require(data["model"] == "opus", "the requested change was not applied")
        require(
            data.get("concurrent") is True,
            "the concurrent writer's update was silently overwritten",
        )
        require(len(injected) == 1, "the injection did not run")
        require(
            not sidecar_residue(root, target.name),
            "the losing attempt left a temporary file behind",
        )


@test("a non-cooperating create before publication is retried, not discarded")
def test_foreign_create_retried() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)

        original_write = settings_module.write_temporary
        injected: list[int] = []

        def inject(target_path: Path, data: Any, mode: int) -> Path:
            temporary = original_write(target_path, data, mode)
            if not injected:
                injected.append(1)
                foreign = target_path.with_name("foreign.json")
                write_json(foreign, {"concurrent": True})
                os.replace(foreign, target_path)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                status = settings_module.main()
        finally:
            settings_module.write_temporary = original_write

        require(status == 0, f"updater returned {status}")
        data = read_json(target)
        require(data["model"] == "opus", "the requested change was not applied")
        require(
            data.get("concurrent") is True,
            "the concurrent creator's file was silently overwritten",
        )
        require(
            not sidecar_residue(root, target.name),
            "the losing attempt left a temporary file behind",
        )


@test("a third writer racing the losing attempt is not deleted")
def test_third_writer_not_deleted() -> None:
    """A rollback swap would restore a stale version over a newer one."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet"})

        original_swap = settings_module.swap_into_place
        rounds: list[int] = []

        def racing_swap(temporary: Path, target_path: Path) -> Any:
            rounds.append(1)
            if len(rounds) == 1:
                # A second writer wins the race this attempt is about to
                # lose, landing between the exchange and any rollback.
                second = target_path.with_name("second.json")
                write_json(second, {"model": "sonnet", "second": True})
                os.replace(second, target_path)
                result = original_swap(temporary, target_path)
                third = target_path.with_name("third.json")
                write_json(
                    third,
                    {"model": "sonnet", "second": True, "third": True},
                )
                os.replace(third, target_path)
                return result
            return original_swap(temporary, target_path)

        settings_module.swap_into_place = racing_swap
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                status = settings_module.main()
        finally:
            settings_module.swap_into_place = original_swap

        require(status == 0, f"updater returned {status}")
        data = read_json(target)
        require(data["model"] == "opus", "the requested change was not applied")
        require(
            data.get("third") is True,
            "the newest concurrent update was deleted by the rollback",
        )
        require(
            not sidecar_residue(root, target.name),
            "a losing attempt left a temporary file behind",
        )


@test("an in-place concurrent write is detected despite the inode surviving")
def test_same_inode_write_detected() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet"})

        original_write = settings_module.write_temporary
        injected: list[int] = []

        def inject(target_path: Path, data: Any, mode: int) -> Path:
            temporary = original_write(target_path, data, mode)
            if not injected:
                injected.append(1)
                # Truncate and rewrite in place. The inode never changes.
                write_json(target_path, {"model": "sonnet", "inplace": True})
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                status = settings_module.main()
        finally:
            settings_module.write_temporary = original_write

        require(status == 0, f"updater returned {status}")
        data = read_json(target)
        require(data["model"] == "opus", "the requested change was not applied")
        require(
            data.get("inplace") is True,
            "an in-place concurrent write was silently overwritten",
        )


@test("a raced version already holding the setting is still reinstalled")
def test_raced_equal_version_reinstalled() -> None:
    """The no-op short circuit must not fire on a base that is not live."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet"})

        original_write = settings_module.write_temporary
        injected: list[int] = []

        def inject(target_path: Path, data: Any, mode: int) -> Path:
            temporary = original_write(target_path, data, mode)
            if not injected:
                injected.append(1)
                # The foreign version already satisfies the request, so the
                # next attempt computes no change and must still install it.
                foreign = target_path.with_name("foreign.json")
                write_json(foreign, {"model": "opus", "concurrent": True})
                os.replace(foreign, target_path)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                status = settings_module.main()
        finally:
            settings_module.write_temporary = original_write

        require(status == 0, f"updater returned {status}")
        data = read_json(target)
        require(data["model"] == "opus", "the requested setting is missing")
        require(
            data.get("concurrent") is True,
            "success was reported while the live file lacked the "
            "concurrent update",
        )


@test("an unreadable displaced version is preserved, not deleted")
def test_unreadable_displaced_preserved() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet"})
        original_inode = target.stat().st_ino

        original_write = settings_module.write_temporary
        injected: list[int] = []

        def inject(target_path: Path, data: Any, mode: int) -> Path:
            temporary = original_write(target_path, data, mode)
            if not injected:
                injected.append(1)
                # Rewritten in place, so the inode survives, then made
                # unreadable so folding it forward has to fail.
                write_json(target_path, {"model": "sonnet", "inplace": True})
                target_path.chmod(0)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            reported = False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    settings_module.main()
            except SystemExit as exc:
                reported = exc.code not in (0, None)
        finally:
            settings_module.write_temporary = original_write

        require(reported, "an unreadable displaced version was not reported")

        survivors = [
            entry
            for entry in root.iterdir()
            if entry.name.startswith("settings.json.backup-orrery-")
            and entry.stat().st_ino == original_inode
        ]
        require(
            survivors,
            "the concurrent writer's file was deleted instead of preserved",
        )
        for entry in survivors:
            entry.chmod(0o600)


@test("a FIFO at the settings path is refused rather than blocking")
def test_fifo_target_refused() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        os.mkfifo(target)

        result = subprocess.run(
            [
                sys.executable,
                str(SETTINGS_SCRIPT),
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )

        require(
            result.returncode != 0,
            "a FIFO settings path was accepted",
        )
        require(
            "not a regular file" in result.stderr,
            f"unexpected diagnostic: {result.stderr.strip()!r}",
        )


@test("a displaced non-regular file is preserved rather than deleted")
def test_displaced_symlink_preserved() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet"})

        original_write = settings_module.write_temporary
        injected: list[int] = []

        def inject(target_path: Path, data: Any, mode: int) -> Path:
            temporary = original_write(target_path, data, mode)
            if not injected:
                injected.append(1)
                dangling = target_path.with_name("dangling")
                dangling.symlink_to(target_path.with_name("nowhere.json"))
                os.replace(dangling, target_path)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            refused = False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    settings_module.main()
            except SystemExit as exc:
                refused = exc.code not in (0, None)
        finally:
            settings_module.write_temporary = original_write

        require(refused, "a non-regular settings path was not reported")
        backups = [
            entry
            for entry in root.iterdir()
            if entry.name.startswith("settings.json.backup-orrery-")
        ]
        require(
            any(entry.is_symlink() for entry in backups),
            f"the displaced symlink was deleted instead of preserved: "
            f"{[entry.name for entry in backups]}",
        )
        require(
            not sidecar_residue(root, target.name),
            "a temporary file was left behind",
        )


@test("exhausting every attempt leaves the settings as they were found")
def test_exhaustion_restores() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet", "foreign": 0})

        original_write = settings_module.write_temporary
        rounds: list[int] = []

        def inject(target_path: Path, payload: bytes, mode: int) -> Path:
            temporary = original_write(target_path, payload, mode)
            rounds.append(1)
            if len(rounds) <= settings_module.ATTEMPTS:
                foreign = target_path.with_name("foreign.json")
                write_json(
                    foreign,
                    {"model": "sonnet", "foreign": len(rounds)},
                )
                os.replace(foreign, target_path)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            message = ""
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    settings_module.main()
            except SystemExit as exc:
                message = str(exc.code)
        finally:
            settings_module.write_temporary = original_write

        require("No update was applied" in message, f"unexpected: {message!r}")

        data = read_json(target)
        require(
            data.get("model") == "sonnet",
            "a partial merge was left live after reporting no update",
        )
        require(
            data.get("foreign") == settings_module.ATTEMPTS,
            f"the last concurrent version was not restored: {data}",
        )
        require(
            not sidecar_residue(root, target.name),
            "the abandoned attempts left temporary files behind",
        )


@test("a writer landing during the restore stays live")
def test_restore_converges() -> None:
    """The restore is a compare-and-swap too, so it can lose and must retry.

    Losing means an even newer version was displaced, and that is what
    should end up live. Installing the older one anyway silently regresses
    the settings while reporting failure.
    """
    for extra in (1, 3):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            target = root / "settings.json"
            write_json(source, {"model": "opus"})
            write_json(target, {"model": "sonnet", "foreign": 0})

            original_write = settings_module.write_temporary
            rounds: list[int] = []

            def inject(target_path: Path, payload: bytes, mode: int) -> Path:
                temporary = original_write(target_path, payload, mode)
                rounds.append(1)
                # Keeps writing past exhaustion, into the restore itself.
                if len(rounds) <= settings_module.ATTEMPTS + extra:
                    foreign = target_path.with_name("foreign.json")
                    write_json(
                        foreign,
                        {"model": "sonnet", "foreign": len(rounds)},
                    )
                    os.replace(foreign, target_path)
                return temporary

            settings_module.write_temporary = inject
            try:
                sys.argv = [
                    "apply-claude-settings.py",
                    "--model",
                    "--source",
                    str(source),
                    "--target",
                    str(target),
                ]
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        settings_module.main()
                except SystemExit:
                    pass
            finally:
                settings_module.write_temporary = original_write

            newest = settings_module.ATTEMPTS + extra
            live = read_json(target).get("foreign")
            require(
                live == newest,
                f"{extra} writer(s) during the restore: live version is "
                f"{live}, but {newest} is newer and exists only in a backup",
            )


@test("a failed flush still leaves a backup of the displaced version")
def test_backup_precedes_flush() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        write_json(target, {"model": "sonnet", "keep": True})

        original_sync = settings_module.sync_directory
        calls: list[int] = []

        def failing_sync(path: Path) -> None:
            calls.append(1)
            raise OSError("simulated fsync failure")

        settings_module.sync_directory = failing_sync
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            reported = False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    settings_module.main()
            except SystemExit as exc:
                reported = exc.code not in (0, None)
        finally:
            settings_module.sync_directory = original_sync

        require(calls, "the flush was never attempted")
        require(reported, "a failed flush was not reported")

        backups = [
            entry
            for entry in root.iterdir()
            if entry.name.startswith("settings.json.backup-orrery-")
        ]
        require(
            backups,
            "a failed flush left the update live with no backup",
        )
        require(
            read_json(backups[0]) == {"model": "sonnet", "keep": True},
            "the backup does not hold the displaced version",
        )
        require(
            not sidecar_residue(root, target.name),
            "the displaced version was left under a temporary name",
        )


@test("a long target name still gets a backup within NAME_MAX")
def test_long_target_name_backup() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        write_json(source, CANONICAL)

        try:
            limit = int(os.pathconf(root, "PC_NAME_MAX"))
        except (OSError, ValueError):
            limit = 255

        # Both a plain name and a multi-byte one, because NAME_MAX bounds
        # bytes rather than characters, and names right up to the limit,
        # because the lock and temporary names are derived from them too.
        for filler in ("s", "é"):
            width = limit - 1
            name = filler * (width // len(os.fsencode(filler)))
            target = root / name
            require(
                len(os.fsencode(name)) <= limit,
                "the fixture name does not fit in a directory entry",
            )
            write_json(target, {"model": "sonnet", "keep": True})

            run_settings(
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            )

            require(read_json(target)["model"] == "opus", "update not applied")

            backups = [
                entry
                for entry in root.iterdir()
                if ".backup-orrery-" in entry.name
                and entry.name.startswith(name[:20])
            ]
            require(backups, f"a long {filler!r} name produced no backup")
            for entry in backups:
                require(
                    len(os.fsencode(entry.name)) <= limit,
                    f"the backup name is {len(os.fsencode(entry.name))} bytes",
                )
            require(
                read_json(backups[0]) == {"model": "sonnet", "keep": True},
                "the backup does not hold the displaced version",
            )
            require(
                not sidecar_residue(root, target.name),
                "the displaced version was left under a temporary name",
            )


@test("continuous contention destroys no version of the settings")
def test_unbounded_contention_preserves_every_version() -> None:
    """The guarantee a bounded compare-and-swap can actually make.

    If a writer lands in the window on every single attempt, the loop stops
    with an older version live and a newer one only in a backup. No bounded
    algorithm avoids that. What must hold regardless is that no version is
    ever destroyed, so the state is recoverable by hand.
    """
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, {"model": "opus"})
        write_json(target, {"model": "sonnet", "foreign": 0})

        original_write = settings_module.write_temporary
        rounds: list[int] = []

        def inject(target_path: Path, payload: bytes, mode: int) -> Path:
            temporary = original_write(target_path, payload, mode)
            rounds.append(1)
            # Never relents, including through every restore attempt.
            foreign = target_path.with_name("foreign.json")
            write_json(foreign, {"model": "sonnet", "foreign": len(rounds)})
            os.replace(foreign, target_path)
            return temporary

        settings_module.write_temporary = inject
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            reported = ""
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    settings_module.main()
            except SystemExit as exc:
                reported = str(exc.code)
        finally:
            settings_module.write_temporary = original_write

        backups = sorted(
            root.glob("settings.json.backup-orrery-*"),
            key=lambda path: path.stat().st_mtime,
        )
        preserved = {read_json(path).get("foreign") for path in backups}
        preserved.add(read_json(target).get("foreign"))

        require(reported, "continuous contention was not reported as failure")
        require(
            preserved >= set(range(1, len(rounds) + 1)),
            f"a version was destroyed: saw {sorted(preserved)} of "
            f"{len(rounds)} written",
        )
        require(
            "preserved" in reported and "Rerun" in reported,
            f"the report does not say how to recover: {reported!r}",
        )
        require(
            not sidecar_residue(root, target.name),
            "contention left temporary files behind",
        )


@test("an update is refused when RENAME_EXCHANGE is unsupported")
def test_no_unsafe_fallback() -> None:
    """There must be no check-then-replace path that can lose an update."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        target = root / "settings.json"
        write_json(source, CANONICAL)
        original = {"model": "sonnet"}
        write_json(target, original)

        def unsupported(first: Path, second: Path) -> None:
            raise settings_module.AtomicExchangeUnavailable("simulated")


        original_exchange = settings_module.exchange_paths
        settings_module.exchange_paths = unsupported
        try:
            sys.argv = [
                "apply-claude-settings.py",
                "--model",
                "--source",
                str(source),
                "--target",
                str(target),
            ]
            refused = False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    settings_module.main()
            except SystemExit as exc:
                refused = exc.code not in (0, None)
        finally:
            settings_module.exchange_paths = original_exchange

        require(refused, "the updater fell back to an unsafe replacement")
        require(read_json(target) == original, "the target was modified anyway")
        require(
            not sidecar_residue(root, target.name),
            "the refused attempt left a temporary file behind",
        )


@test("cooperating writers serialise on the sidecar lock without losing work")
def test_cooperating_writers_serialise() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        model_source = root / "model.json"
        hooks_source = root / "hooks.json"
        target = root / "settings.json"

        write_json(model_source, {"model": "opus"})
        write_json(hooks_source, CANONICAL)
        write_json(target, {"model": "sonnet", "padding": "y" * 400000})

        processes = []
        for _ in range(4):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SETTINGS_SCRIPT),
                        "--model",
                        "--source",
                        str(model_source),
                        "--target",
                        str(target),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SETTINGS_SCRIPT),
                        "--hooks",
                        "--source",
                        str(hooks_source),
                        "--target",
                        str(target),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        for process in processes:
            _, errors = process.communicate(timeout=120)
            require(
                process.returncode == 0,
                f"a concurrent writer failed: {errors.strip()}",
            )

        data = read_json(target)
        require(data["model"] == "opus", "the model update was lost")
        require(
            data.get("padding") == "y" * 400000,
            "an unrelated large setting was lost",
        )

        canonical_command = CANONICAL["hooks"]["Stop"][0]["hooks"][0]["command"]
        occurrences = [
            handler.get("command")
            for group in data["hooks"]["Stop"]
            for handler in group.get("hooks", [])
        ].count(canonical_command)
        require(
            occurrences == 1,
            f"concurrent hook writers produced {occurrences} copies",
        )
        require(
            not sidecar_residue(root, target.name),
            "concurrent writers left temporary files behind",
        )


# ---------------------------------------------------------------------------
# Review wrapper: systemd state handling
# ---------------------------------------------------------------------------


def with_stub_systemctl(
    responses: list[subprocess.CompletedProcess[str]],
    *,
    repeat_last: bool = False,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    queue = list(responses)

    def stub(*arguments: str, timeout: float = 20.0) -> Any:
        calls.append(arguments)
        if len(queue) == 1 and repeat_last:
            return queue[0]
        if not queue:
            raise Failure(f"unexpected extra systemctl call: {arguments}")
        return queue.pop(0)

    review_module.run_systemctl = stub
    return calls


@test("an absent unit is distinguished from a systemctl failure")
def test_absent_unit() -> None:
    original = review_module.run_systemctl
    try:
        calls = with_stub_systemctl(
            [completed(stdout=show_output("not-found", "inactive"))]
        )
        review_module.stop_unit("example.service")
        require(len(calls) == 1, "an absent unit should need only one query")
    finally:
        review_module.run_systemctl = original


@test("a systemctl bus failure is reported rather than treated as absence")
def test_bus_failure_fails_closed() -> None:
    original = review_module.run_systemctl
    try:
        with_stub_systemctl(
            [
                completed(
                    stderr=(
                        "Failed to connect to bus: "
                        "No such file or directory"
                    ),
                    returncode=1,
                )
            ],
            repeat_last=True,
        )
        raised = False
        try:
            review_module.stop_unit("example.service")
        except RuntimeError:
            raised = True
        require(raised, "a bus failure was silently treated as success")
    finally:
        review_module.run_systemctl = original


@test("a systemctl timeout is reported rather than treated as absence")
def test_systemctl_timeout_fails_closed() -> None:
    original = review_module.subprocess.run

    def slow(*_args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="systemctl", timeout=kwargs.get("timeout", 1))

    review_module.subprocess.run = slow
    try:
        raised = False
        try:
            review_module.stop_unit("example.service")
        except RuntimeError:
            raised = True
        require(raised, "a systemctl timeout was silently treated as success")
    finally:
        review_module.subprocess.run = original


@test("blank or missing state reporting is rejected")
def test_blank_state_rejected() -> None:
    original = review_module.run_systemctl
    try:
        with_stub_systemctl([completed(stdout="")], repeat_last=True)
        raised = False
        try:
            review_module.stop_unit("example.service")
        except RuntimeError:
            raised = True
        require(raised, "missing LoadState was accepted")

        with_stub_systemctl(
            [completed(stdout=show_output("loaded", ""))],
            repeat_last=True,
        )
        raised = False
        try:
            review_module.stop_unit("example.service")
        except RuntimeError:
            raised = True
        require(raised, "empty ActiveState was accepted")
    finally:
        review_module.run_systemctl = original


@test("terminal states are accepted without any stop attempt")
def test_terminal_states() -> None:
    original = review_module.run_systemctl
    try:
        for state in ("inactive", "failed"):
            calls = with_stub_systemctl(
                [completed(stdout=show_output("loaded", state))]
            )
            review_module.stop_unit("example.service")
            require(
                all(call[0] == "show" for call in calls),
                f"state {state} triggered an unnecessary stop",
            )
    finally:
        review_module.run_systemctl = original


@test("every loaded non-terminal state requires cleanup")
def test_non_terminal_states_require_cleanup() -> None:
    original = review_module.run_systemctl
    try:
        for state in (
            "active",
            "activating",
            "deactivating",
            "reloading",
            "maintenance",
            "refreshing",
            "some-future-state",
        ):
            calls = with_stub_systemctl(
                [
                    completed(stdout=show_output("loaded", state)),
                    completed(),
                    completed(stdout=show_output("loaded", "inactive")),
                ]
            )
            review_module.stop_unit("example.service")
            require(
                any(call[0] == "stop" for call in calls),
                f"state {state} returned success without cleanup",
            )
    finally:
        review_module.run_systemctl = original


@test("a failed stop is escalated to SIGKILL before being reported")
def test_stop_escalation() -> None:
    original_run = review_module.run_systemctl
    original_settle = review_module.STOP_SETTLE_SECONDS
    review_module.STOP_SETTLE_SECONDS = 0.0
    try:
        # A unit that stays active after a failed stop but yields to SIGKILL.
        calls = with_stub_systemctl(
            [
                completed(stdout=show_output("loaded", "active")),
                completed(stderr="stop failed", returncode=1),
                completed(stdout=show_output("loaded", "active")),
                completed(),
                completed(stdout=show_output("loaded", "failed")),
            ]
        )
        review_module.stop_unit("example.service")
        require(
            any(call[0] == "kill" for call in calls),
            "SIGKILL escalation was not attempted after a failed stop",
        )

        # A unit that survives both the stop and the SIGKILL must be reported.
        with_stub_systemctl(
            [
                completed(stdout=show_output("loaded", "active")),
                completed(stderr="stop failed", returncode=1),
                completed(stdout=show_output("loaded", "active")),
                completed(stderr="kill failed", returncode=1),
                completed(stdout=show_output("loaded", "active")),
            ]
        )
        raised = False
        try:
            review_module.stop_unit("example.service")
        except RuntimeError:
            raised = True
        require(raised, "a unit that never stopped was reported as clean")
    finally:
        review_module.run_systemctl = original_run
        review_module.STOP_SETTLE_SECONDS = original_settle


# ---------------------------------------------------------------------------
# Review wrapper: CODEX_HOME and argument handling
# ---------------------------------------------------------------------------


def shell_codex_home(script: Path, environment: dict[str, str]) -> str:
    """Resolve CODEX_HOME the way the Bash entry points do."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'probe="$(mktemp "${TMPDIR:-/tmp}/kit-probe.XXXXXX")" || exit 1; '
            "trap 'rm -f \"$probe\"' EXIT; "
            'sed -n "/^if ! CODEX_HOME=/,/^fi$/p" "$1" > "$probe"; '
            'set -euo pipefail; source "$probe"; printf "%s" "$CODEX_HOME"',
            "bash",
            str(script),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise Failure(f"shell CODEX_HOME resolution failed: {result.stderr}")
    return result.stdout


def python_codex_home(environment: dict[str, str]) -> str:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import importlib.machinery, importlib.util, sys\n"
            "loader = importlib.machinery.SourceFileLoader('m', sys.argv[1])\n"
            "spec = importlib.util.spec_from_file_location(\n"
            "    'm', sys.argv[1], loader=loader)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "print(module.codex_home(), end='')\n",
            str(REVIEW_SCRIPT),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise Failure(f"python CODEX_HOME resolution failed: {result.stderr}")
    return result.stdout


@test("a stop that times out still reaches SIGKILL escalation")
def test_stop_timeout_escalates() -> None:
    original_run = review_module.run_systemctl
    original_settle = review_module.STOP_SETTLE_SECONDS
    review_module.STOP_SETTLE_SECONDS = 0.0
    try:
        calls: list[tuple[str, ...]] = []
        queue = [
            completed(stdout=show_output("loaded", "active")),
            RuntimeError("systemctl timed out: stop"),
            completed(stdout=show_output("loaded", "active")),
            completed(),
            completed(stdout=show_output("loaded", "failed")),
        ]

        def stub(*arguments: str, timeout: float = 20.0) -> Any:
            calls.append(arguments)
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        review_module.run_systemctl = stub
        review_module.stop_unit("example.service")
        require(
            any(call[0] == "kill" for call in calls),
            "a timed-out stop skipped the SIGKILL escalation",
        )
    finally:
        review_module.run_systemctl = original_run
        review_module.STOP_SETTLE_SECONDS = original_settle


@test("an initial state query failure still reaches stop and SIGKILL")
def test_initial_query_failure_escalates() -> None:
    original_run = review_module.run_systemctl
    original_settle = review_module.STOP_SETTLE_SECONDS
    review_module.STOP_SETTLE_SECONDS = 0.0
    try:
        calls: list[tuple[str, ...]] = []
        queue: list[Any] = [
            RuntimeError("systemctl timed out: show"),
            completed(),
            completed(stdout=show_output("loaded", "active")),
            completed(),
            completed(stdout=show_output("loaded", "failed")),
        ]

        def stub(*arguments: str, timeout: float = 20.0) -> Any:
            calls.append(arguments)
            response = queue.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        review_module.run_systemctl = stub
        review_module.stop_unit("example.service")
        verbs = [call[0] for call in calls]
        require("stop" in verbs, "a failed initial query skipped the stop")
        require("kill" in verbs, "a failed initial query skipped the SIGKILL")
    finally:
        review_module.run_systemctl = original_run
        review_module.STOP_SETTLE_SECONDS = original_settle


@test("the handled signal set covers every asynchronous terminate signal")
def test_signal_coverage() -> None:
    covered = review_module.interrupting_signals()

    for name in (
        "SIGHUP",
        "SIGINT",
        "SIGQUIT",
        "SIGTERM",
        "SIGUSR1",
        "SIGUSR2",
        "SIGALRM",
        "SIGIO",
        "SIGPOLL",
        "SIGSTKFLT",
        "SIGPWR",
        "SIGPROF",
        "SIGVTALRM",
        "SIGXCPU",
        "SIGXFSZ",
    ):
        member = getattr(signal, name, None)
        if member is None:
            continue
        require(int(member) in covered, f"{name} is not handled")

    for name in ("SIGKILL", "SIGSTOP", "SIGSEGV", "SIGCHLD", "SIGWINCH"):
        member = getattr(signal, name, None)
        if member is None:
            continue
        require(int(member) not in covered, f"{name} must not be handled")

    realtime = getattr(signal, "SIGRTMIN", None)
    if realtime is not None:
        require(int(realtime) in covered, "real-time signals are not handled")


@test("a later run reclaims runtime state left by an uncatchable death")
def test_stale_runtime_swept() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)

    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=30)

    # A dead owner.
    stale = root / f"run.{victim.pid}.stale-test"
    stale.mkdir(exist_ok=True)
    (stale / "service-environment.json").write_text("{}")

    # A live but unrelated process that happens to hold the same identifier
    # the directory records, which is what process-identifier reuse looks
    # like from here.
    reused = root / f"run.{os.getpid()}.reused-test"
    reused.mkdir(exist_ok=True)
    review_module.record_runtime_owner(reused)
    text = (reused / "owner").read_text().split()
    (reused / "owner").write_text(f"{text[0]} {int(text[1]) + 1}\n")
    reused_name = reused.name

    # This wrapper's own directory.
    live = root / f"run.{os.getpid()}.live-test"
    live.mkdir(exist_ok=True)
    review_module.record_runtime_owner(live)

    try:
        review_module.sweep_stale_runtime(root)
        require(not stale.exists(), "stale runtime state was not reclaimed")
        require(
            not (root / reused_name).exists(),
            "a reused process identifier protected a dead owner's state",
        )
        require(live.exists(), "runtime state of a live wrapper was removed")
    finally:
        shutil.rmtree(stale, ignore_errors=True)
        shutil.rmtree(reused, ignore_errors=True)
        shutil.rmtree(live, ignore_errors=True)


@test("the sweep stops an orphaned unit before removing its state")
def test_sweep_stops_orphaned_unit() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)

    unit_base = f"orrery-review-sweep-test-{os.getpid()}"
    unit = f"{unit_base}.service"
    subprocess.run(
        [
            "systemd-run",
            "--user",
            "--quiet",
            "--collect",
            f"--unit={unit_base}",
            "--property=Type=exec",
            "/usr/bin/sleep",
            "600",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
        check=True,
    )

    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=30)
    entry = root / f"run.{victim.pid}.orphan-test"
    entry.mkdir(exist_ok=True)
    (entry / "owner").write_text(f"{victim.pid} 1 {unit}\n")

    try:
        review_module.sweep_stale_runtime(root)
        require(not entry.exists(), "orphaned runtime state was not reclaimed")

        state = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            stdout=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        ).stdout.strip()
        require(
            state in ("inactive", "failed", "unknown", ""),
            f"the orphaned unit was left running: {state}",
        )
    finally:
        shutil.rmtree(entry, ignore_errors=True)
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


@test("a unit that cannot be stopped keeps its runtime state")
def test_sweep_keeps_state_when_stop_fails() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)

    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=30)
    entry = root / f"run.{victim.pid}.unstoppable-test"
    entry.mkdir(exist_ok=True)
    (entry / "owner").write_text(f"{victim.pid} 1 stubborn.service\n")

    original = review_module.run_systemctl
    try:
        with_stub_systemctl(
            [
                completed(
                    stderr="Failed to connect to bus: Connection refused",
                    returncode=1,
                )
            ],
            repeat_last=True,
        )
        review_module.sweep_stale_runtime(root)
        require(
            entry.exists(),
            "runtime state was deleted although the unit could still be live",
        )
    finally:
        review_module.run_systemctl = original
        shutil.rmtree(entry, ignore_errors=True)


@test("an unknown recorded start time does not protect stale runtime state")
def test_unknown_start_time_not_a_match() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)

    # Names this live process, so only the recorded start time can decide.
    entry = root / f"run.{os.getpid()}.unknown-start-test"
    entry.mkdir(exist_ok=True)
    (entry / "owner").write_text(f"{os.getpid()} unknown stale-unknown.service\n")

    stopped: list[str] = []
    original_stop = review_module.stop_unit
    review_module.stop_unit = lambda unit: stopped.append(unit)
    try:
        review_module.sweep_stale_runtime(root)
        require(
            stopped == ["stale-unknown.service"],
            f"the orphaned unit was not stopped: {stopped}",
        )
        require(not entry.exists(), "an unknown start time protected the state")
    finally:
        review_module.stop_unit = original_stop
        shutil.rmtree(entry, ignore_errors=True)


@test("a wrapper that cannot confirm the stop keeps its runtime state")
def test_wrapper_keeps_state_when_stop_fails() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    pattern = f"run.{os.getpid()}.*"
    before = {entry.name for entry in root.glob(pattern)}

    environment = review_environment("success")
    original_stop = review_module.stop_unit
    original_program_name = review_module.PROGRAM_NAME
    original_argv = sys.argv
    saved_environment = os.environ.copy()

    # main() installs handlers for every interrupting signal and leaves them
    # blocked on exit, which would otherwise affect the rest of this run.
    saved_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    saved_handlers: dict[int, Any] = {}
    for number in review_module.interrupting_signals():
        try:
            saved_handlers[number] = signal.getsignal(number)
        except (ValueError, OSError):
            continue

    def refuse(unit: str) -> None:
        raise RuntimeError("simulated cleanup failure")

    try:
        os.environ["PATH"] = environment["PATH"]
        os.environ["CODEX_FAKE_MODE"] = "success"
        review_module.stop_unit = refuse
        review_module.PROGRAM_NAME = "orrery-review"
        sys.argv = ["orrery-review", "--timeout", "60", "--", "prompt"]

        reported = False
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                review_module.main()
        except review_module.CleanupError:
            reported = True

        require(reported, "a failed stop was not reported as cleanup failure")
        kept = {entry.name for entry in root.glob(pattern)} - before
        require(
            kept,
            "runtime state was removed although the unit could still be live",
        )
    finally:
        review_module.stop_unit = original_stop
        review_module.PROGRAM_NAME = original_program_name
        sys.argv = original_argv
        os.environ.clear()
        os.environ.update(saved_environment)
        for number, handler in saved_handlers.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError, TypeError):
                continue
        signal.pthread_sigmask(signal.SIG_SETMASK, saved_mask)
        for entry in root.glob(pattern):
            if entry.name not in before:
                shutil.rmtree(entry, ignore_errors=True)
        stop_stray_units(f"orrery-review-{os.getpid()}-")
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)


@test("an unreadable subdirectory does not block runtime reclamation")
def test_permission_blocked_removal() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)

    victim = subprocess.Popen([sys.executable, "-c", "pass"])
    victim.wait(timeout=30)
    entry = root / f"run.{victim.pid}.locked-test"
    locked = entry / "tmp" / "locked"
    locked.mkdir(parents=True, exist_ok=True)
    (locked / "file").write_text("x")
    (entry / "owner").write_text(f"{victim.pid} 1\n")
    locked.chmod(0)

    try:
        require(
            not shutil.rmtree(entry, ignore_errors=True) and entry.exists(),
            "the fixture did not actually block a plain removal",
        )
        review_module.sweep_stale_runtime(root)
        require(not entry.exists(), "an unreadable subdirectory blocked the sweep")
    finally:
        if locked.exists():
            locked.chmod(0o700)
        shutil.rmtree(entry, ignore_errors=True)


@test("runtime reclamation never chmods anything outside the runtime tree")
def test_force_remove_does_not_follow_symlinks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        outside = root / "outside"
        outside.mkdir()
        (outside / "file").write_text("keep me")
        outside.chmod(0o755)

        run_dir = root / "run.1.symlink-test"
        (run_dir / "tmp").mkdir(parents=True)
        (run_dir / "tmp" / "escape").symlink_to(outside, target_is_directory=True)

        removed = review_module.force_remove(run_dir)

        require(removed, "the run directory was not removed")
        require(outside.is_dir(), "the linked directory was removed")
        require(
            (outside / "file").read_text() == "keep me",
            "the linked directory's contents were disturbed",
        )
        require(
            stat.S_IMODE(outside.stat().st_mode) == 0o755,
            "the mode of a directory outside the runtime tree was changed",
        )

        # A run entry that is itself a symlink must be unlinked, and a
        # dangling one must not be reported as reclaimed while it survives.
        for name, referent in (
            ("run.1.live-link", outside),
            ("run.1.dangling-link", root / "nowhere"),
        ):
            link = root / name
            link.symlink_to(referent, target_is_directory=True)
            removed = review_module.force_remove(link)
            require(
                not os.path.lexists(link),
                f"{name} was left behind",
            )
            require(removed, f"{name} removal was reported as a failure")

        require(outside.is_dir(), "a symlink referent was removed")


@test("a zombie owner does not protect stale runtime state")
def test_zombie_owner_swept() -> None:
    root = review_runtime_root()
    root.mkdir(parents=True, exist_ok=True)

    # Left unreaped on purpose, so /proc still lists it as a zombie.
    zombie = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        try:
            text = Path(f"/proc/{zombie.pid}/stat").read_text()
        except OSError:
            break
        if text[text.rfind(")") + 2 :].split()[0] == "Z":
            break
        time.sleep(0.05)

    entry = root / f"run.{zombie.pid}.zombie-test"
    entry.mkdir(exist_ok=True)
    review_module.record_runtime_owner(entry)

    try:
        require(
            review_module.process_start_time(zombie.pid) is None,
            "a zombie was reported as able to run cleanup",
        )
        review_module.sweep_stale_runtime(root)
        require(not entry.exists(), "a zombie owner protected stale state")
    finally:
        shutil.rmtree(entry, ignore_errors=True)
        zombie.wait(timeout=30)


@test("CODEX_HOME normalises identically in Bash and Python entry points")
def test_codex_home_consistent() -> None:
    home = os.environ["HOME"]
    cases = [
        None,
        f"{home}/custom-codex",
        "~/custom-codex",
        f"{home}//custom-codex/",
        "./relative-codex",
    ]

    for case in cases:
        environment = os.environ.copy()
        if case is None:
            environment.pop("CODEX_HOME", None)
        else:
            environment["CODEX_HOME"] = case

        from_python = python_codex_home(environment)
        for script in (INSTALL_SCRIPT, DOCTOR_SCRIPT):
            from_shell = shell_codex_home(script, environment)
            require(
                from_shell == from_python,
                f"CODEX_HOME={case!r}: {script.name} gave {from_shell!r}, "
                f"the wrapper gave {from_python!r}",
            )


@test("an explicitly empty CODEX_HOME is rejected with status 2")
def test_empty_codex_home() -> None:
    environment = review_environment("success")
    environment["CODEX_HOME"] = ""

    try:
        result = subprocess.run(
            [sys.executable, str(REVIEW_SCRIPT), "--", "prompt"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)
    require(
        result.returncode == 2,
        f"expected status 2, got {result.returncode}: {result.stderr}",
    )
    require(
        "CODEX_HOME cannot be empty" in result.stderr,
        f"unexpected diagnostic: {result.stderr}",
    )

    # HOME is isolated so that a regression in the guard under test cannot
    # let the installer loose on the real home directory.
    with tempfile.TemporaryDirectory() as directory:
        isolated = os.environ.copy()
        isolated["CODEX_HOME"] = ""
        isolated["HOME"] = str(Path(directory) / "home")
        Path(isolated["HOME"]).mkdir()

        for script in (INSTALL_SCRIPT, DOCTOR_SCRIPT):
            shell = subprocess.run(
                ["bash", str(script)],
                env=isolated,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )
            require(
                shell.returncode == 2,
                f"{script.name} accepted an empty CODEX_HOME "
                f"(status {shell.returncode})",
            )


@test("an invalid manifest role is refused rather than silently substituted")
def test_invalid_manifest_role_refused() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        environment = review_environment("success")
        try:
            manifest_path = kit / "global" / "orchestration.json"
            manifest = read_json(manifest_path)
            reviewer = next(
                step for step in manifest["steps"] if step["id"] == "reviewer"
            )
            reviewer["model"] = ""
            write_json(manifest_path, manifest)
            result = subprocess.run(
                [
                    sys.executable,
                    str(kit / "scripts" / "orrery-review"),
                    "--timeout",
                    "60",
                    "--",
                    "prompt",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )
            require(
                result.returncode == 2,
                f"a model-less role was accepted (status {result.returncode})",
            )
            require(
                "has no model" in result.stderr,
                f"unexpected diagnostic: {result.stderr.strip()!r}",
            )

            reviewer["model"] = "gpt-5.6-sol"
            reviewer["provider"] = "anthropic"
            reviewer["thinking"] = "max"
            write_json(manifest_path, manifest)
            result = subprocess.run(
                [
                    sys.executable,
                    str(kit / "scripts" / "orrery-review"),
                    "--timeout",
                    "60",
                    "--",
                    "prompt",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )
            require(
                result.returncode == 2
                and "belongs to openai, not anthropic" in result.stderr,
                f"a cross-provider mismatch was accepted: {result.stderr!r}",
            )
        finally:
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)


@test("an empty or missing prompt is rejected")
def test_prompt_required() -> None:
    result = subprocess.run(
        [sys.executable, str(REVIEW_SCRIPT), "--", "   "],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    require(result.returncode == 2, "an empty prompt was accepted")


def fallback_environment(mode: str) -> dict[str, str]:
    """A review environment whose PATH has no systemd tools at all.

    The interpreter's own directory stays on PATH so the stand-in's
    `env python3` shebang still resolves; it contains no systemd tools.
    """
    environment = review_environment(mode)
    environment["PATH"] = os.pathsep.join(
        [environment["KIT_FAKE_BIN"], str(Path(sys.executable).parent)]
    )
    return environment


@test("without systemd the review degrades to a process group and completes")
def test_fallback_completion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "verdict.txt"
        environment = fallback_environment("success")
        process = start_review(
            environment,
            "--timeout",
            "60",
            "--output",
            str(output),
            "--",
            "prompt",
        )
        stdout, stderr = finish_review(process, environment)

        require(
            process.returncode == 0,
            f"fallback run failed ({process.returncode}): {stderr}",
        )
        require("# PASS" in stdout, f"verdict not printed: {stdout!r}")
        require(output.exists(), "verdict was not published")
        require(
            "without control-group containment" in stderr,
            f"the degraded mode was not announced: {stderr!r}",
        )
        own_units = [
            line
            for line in list_review_units()
            if line.strip()
            .lstrip("● ")
            .split()[0]
            .startswith(f"orrery-review-{process.pid}-")
        ]
        require(
            not own_units,
            "a transient unit appeared despite systemd being unavailable",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("without systemd a timeout still kills the run and leaves no residue")
def test_fallback_timeout() -> None:
    environment = fallback_environment("sleep")
    process = start_review(environment, "--timeout", "5", "--", "prompt")
    _, stderr = finish_review(process, environment, timeout=180)

    require(
        process.returncode == 124,
        f"expected status 124, got {process.returncode}: {stderr}",
    )
    survivors = subprocess.run(
        ["pgrep", "-f", "CODEX_FAKE_MODE"],
        stdout=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    ).stdout.strip()
    require(not survivors, f"the fake codex survived the timeout: {survivors}")
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("without systemd a same-group descendant dies with the run")
def test_fallback_group_descendant() -> None:
    marker = Path(tempfile.mkdtemp(prefix="kit-marker.")) / "child.pid"
    environment = fallback_environment("linger")
    environment["CODEX_FAKE_MARKER"] = str(marker)
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    try:
        stdout, stderr = finish_review(process, environment)

        require(
            process.returncode == 0,
            f"linger run failed ({process.returncode}): {stderr}",
        )
        child = int(marker.read_text().strip())
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(OSError):
                os.kill(child, signal.SIGKILL)
            raise Failure(
                "a same-group descendant survived the fallback cleanup"
            )
    finally:
        shutil.rmtree(marker.parent, ignore_errors=True)


@test("a missing procfs degrades liveness and scanning conservatively")
def test_proc_fallback() -> None:
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_no_proc",
    )
    with tempfile.TemporaryDirectory() as directory:
        hook.PROC_ROOT = Path(directory) / "no-proc"

        require(
            hook.live_same_process(os.getpid(), 12345),
            "a live process was reported dead without procfs",
        )
        reaped = subprocess.run(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            timeout=30,
            check=True,
        )
        # A start-time mismatch cannot be detected without procfs; only a
        # genuinely absent identifier may be reported dead.
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait()
        require(
            not hook.live_same_process(finished.pid, 12345),
            "a reaped process was reported alive without procfs",
        )
        del reaped
        require(
            hook.scan_session_processes("any-session") == [],
            "ownership was invented without procfs to attribute it",
        )
        require(
            hook.live_command_lines() == [],
            "command lines were invented without procfs",
        )
        # Verification requires a checkable start time. Without procfs a
        # signal must never be authorised by an identifier alone.
        require(
            not hook.live_verified_process(os.getpid(), 12345),
            "an unverifiable process was treated as verified without procfs",
        )


@test("Codex receives an explicit model, thinking, sandbox and private prompt")
def test_codex_invocation_contract() -> None:
    """The wrapper's central promises live in the argv it hands to Codex."""
    with tempfile.TemporaryDirectory() as directory:
        arguments_path = Path(directory) / "argv.txt"
        stdin_path = Path(directory) / "stdin.txt"
        environment_path = Path(directory) / "environment.json"
        environment = review_environment("success")
        environment["CODEX_FAKE_ARGS"] = str(arguments_path)
        environment["CODEX_FAKE_STDIN"] = str(stdin_path)
        environment["CODEX_FAKE_ENV"] = str(environment_path)
        environment["OPENAI_ORRERY_TEST"] = "openai-kept"
        environment["ANTHROPIC_ORRERY_TEST"] = "anthropic-dropped"

        process = start_review(
            environment, "--timeout", "60", "--", "-p", "the prompt"
        )
        _, stderr = finish_review(process, environment)

        require(
            process.returncode == 0,
            f"wrapper failed ({process.returncode}): {stderr}",
        )
        arguments = arguments_path.read_text().splitlines()

        for flag, value in (
            ("--model", "gpt-5.6-sol"),
            ("--sandbox", "read-only"),
            ("-c", 'model_reasoning_effort="ultra"'),
        ):
            require(flag in arguments, f"codex was not passed {flag}")
            require(
                arguments[arguments.index(flag) + 1] == value,
                f"codex was passed {flag} "
                f"{arguments[arguments.index(flag) + 1]!r}, not {value!r}",
            )
        for expected in (
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--output-last-message",
            "-",
        ):
            require(expected in arguments, f"codex was not passed {expected}")
        require(
            "-p the prompt" not in arguments
            and all("the prompt" not in argument for argument in arguments),
            f"the private prompt leaked into argv: {arguments!r}",
        )
        handoff = stdin_path.read_text()
        require(
            handoff.startswith("ORRERY ROLE HANDOFF\nRole: reviewer\n")
            and "bounded non-principal session" in handoff
            and "Read-only: do not modify files." in handoff
            and handoff.rstrip().endswith("-p the prompt"),
            f"the role handoff was incomplete or mangled: {handoff!r}",
        )
        provider_env = read_json(environment_path)
        require(
            provider_env.get("OPENAI_ORRERY_TEST") == "openai-kept"
            and "ANTHROPIC_ORRERY_TEST" not in provider_env,
            f"the OpenAI adapter leaked the other provider's environment: "
            f"{provider_env}",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("Claude receives an explicit model, maximum effort, cache flag and sandbox")
def test_claude_invocation_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        manifest_path = kit / "global" / "orchestration.json"
        manifest = read_json(manifest_path)
        reviewer = next(
            step for step in manifest["steps"] if step["id"] == "reviewer"
        )
        reviewer.update(
            provider="anthropic",
            model="sonnet",
            thinking="max",
        )
        write_json(manifest_path, manifest)

        arguments_path = root / "argv.txt"
        stdin_path = root / "stdin.txt"
        settings_path = root / "settings.json"
        environment_path = root / "environment.json"
        environment = review_environment("success")
        environment["CLAUDE_FAKE_ARGS"] = str(arguments_path)
        environment["CLAUDE_FAKE_STDIN"] = str(stdin_path)
        environment["CLAUDE_FAKE_SETTINGS"] = str(settings_path)
        environment["CLAUDE_FAKE_ENV"] = str(environment_path)
        environment["ANTHROPIC_ORRERY_TEST"] = "anthropic-kept"
        environment["OPENAI_ORRERY_TEST"] = "openai-dropped"

        process = subprocess.Popen(
            [
                sys.executable,
                str(kit / "scripts" / "orrery-review"),
                "--timeout",
                "60",
                "--",
                "private Claude assignment",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=120)
        finally:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            stop_stray_units(f"orrery-review-{process.pid}-")
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

        require(
            process.returncode == 0 and "# PASS" in stdout,
            f"Claude adapter failed ({process.returncode}): {stderr}",
        )
        arguments = arguments_path.read_text().splitlines()
        for flag, value in (
            ("--model", "sonnet"),
            ("--effort", "max"),
            ("--output-format", "json"),
            ("--permission-mode", "plan"),
        ):
            require(
                flag in arguments
                and arguments[arguments.index(flag) + 1] == value,
                f"Claude did not receive {flag} {value}: {arguments}",
            )
        for flag in (
            "--print",
            "--exclude-dynamic-system-prompt-sections",
            "--no-session-persistence",
            "--settings",
        ):
            require(flag in arguments, f"Claude was not passed {flag}")
        require(
            all("private Claude assignment" not in value for value in arguments)
            and stdin_path.read_text().rstrip().endswith(
                "private Claude assignment"
            ),
            "the Claude assignment was exposed in argv or lost from stdin",
        )

        settings = read_json(settings_path)
        sandbox = settings.get("sandbox", {})
        deny = settings.get("permissions", {}).get("deny", [])
        require(
            sandbox.get("enabled") is True
            and sandbox.get("failIfUnavailable") is True
            and sandbox.get("allowUnsandboxedCommands") is False
            and str(Path.cwd().resolve())
            in sandbox.get("filesystem", {}).get("denyWrite", [])
            and {"Edit", "Write", "NotebookEdit"} <= set(deny),
            f"the Claude reviewer sandbox is not fail-closed: {settings}",
        )
        provider_env = read_json(environment_path)
        require(
            provider_env.get("ANTHROPIC_ORRERY_TEST") == "anthropic-kept"
            and "OPENAI_ORRERY_TEST" not in provider_env,
            f"the Anthropic adapter leaked the other provider environment: "
            f"{provider_env}",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("every runtime role can use either provider without profiles")
def test_every_role_is_provider_neutral() -> None:
    manifest = read_json(KIT_DIR / "global" / "orchestration.json")
    original_which = runtime_module.shutil.which
    runtime_module.shutil.which = lambda command: f"/fake/{command}"
    try:
        for provider, model, thinking, executable in (
            ("anthropic", "fable", "max", "/fake/claude"),
            ("openai", "gpt-5.6-sol", "ultra", "/fake/codex"),
        ):
            configured = copy.deepcopy(manifest)
            for step in configured["steps"]:
                step.update(
                    provider=provider,
                    model=model,
                    thinking=thinking,
                )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "orchestration.json"
                write_json(path, configured)
                roles = {
                    role_id: runtime_module.load_role(role_id, path)
                    for role_id in (
                        "orchestrator",
                        "mechanic",
                        "implementer",
                        "plan-reviewer",
                        "reviewer",
                    )
                }
                principal = runtime_module.principal_command(
                    roles["orchestrator"], []
                )
                require(
                    principal[0] == executable
                    and model in principal
                    and any(thinking in argument for argument in principal),
                    f"{provider} could not act as principal: {principal}",
                )
                for role_id in (
                    "mechanic",
                    "implementer",
                    "plan-reviewer",
                    "reviewer",
                ):
                    settings = (
                        Path(directory) / "claude-settings.json"
                        if provider == "anthropic"
                        else None
                    )
                    command = runtime_module.delegated_command(
                        roles[role_id],
                        Path(directory) / "verdict.txt",
                        settings,
                    )
                    require(
                        command[0] == executable
                        and model in command
                        and any(thinking in argument for argument in command),
                        f"{provider} could not run {role_id}: {command}",
                    )
                    expected_access = (
                        "read-only"
                        if role_id in ("plan-reviewer", "reviewer")
                        else "workspace-write"
                    )
                    if provider == "openai":
                        require(
                            command[command.index("--sandbox") + 1]
                            == expected_access,
                            f"{role_id} lost its access contract: {command}",
                        )
                    else:
                        require(
                            command[command.index("--permission-mode") + 1]
                            == (
                                "plan"
                                if expected_access == "read-only"
                                else "acceptEdits"
                            ),
                            f"{role_id} lost its Claude permission mode",
                        )
    finally:
        runtime_module.shutil.which = original_which


@test("a repository can select Codex as its principal without changing defaults")
def test_repository_principal_override() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory)
        (repository / ".git").mkdir()
        write_json(
            repository / ".orrery.json",
            {
                "orchestrator": {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "thinking": "ultra",
                }
            },
        )
        role = runtime_module.load_role(
            "orchestrator",
            cwd=repository,
        )
        require(
            (role.provider, role.model, role.thinking)
            == ("openai", "gpt-5.6-sol", "ultra"),
            f"repository principal override was ignored: {role}",
        )
        global_role = runtime_module.load_role(
            "orchestrator",
            cwd=KIT_DIR,
        )
        require(
            (global_role.provider, global_role.model, global_role.thinking)
            == ("anthropic", "fable", "max"),
            "the repository override mutated global defaults",
        )


@test("fallback ranking selects the nearest role tier and thinking position")
def test_nearest_fallback_ranking() -> None:
    principal = runtime_module.load_role("orchestrator")
    cross_provider = fallback_module.nearest_fallback(
        principal,
        "test",
        excluded_providers={"anthropic"},
        assumed_ready={"openai"},
        discover_live=False,
    )
    require(cross_provider is not None, "no principal fallback was found")
    require(
        (
            cross_provider.candidate.provider,
            cross_provider.candidate.model,
            cross_provider.candidate.thinking,
        )
        == ("openai", "gpt-5.6-sol", "ultra"),
        f"Fable did not map to Sol at maximum thinking: {cross_provider}",
    )

    reviewer = runtime_module.load_role("reviewer")
    same_provider = fallback_module.nearest_fallback(
        reviewer,
        "test",
        excluded_providers={"anthropic"},
        excluded_models={("openai", "gpt-5.6-sol")},
        assumed_ready={"openai"},
        discover_live=False,
    )
    require(same_provider is not None, "no same-provider model fallback was found")
    require(
        (
            same_provider.candidate.provider,
            same_provider.candidate.model,
            same_provider.candidate.thinking,
        )
        == ("openai", "gpt-5.6-terra", "ultra"),
        f"a model-only failure did not preserve provider/proximity: {same_provider}",
    )

    require(
        fallback_module._picker_tier(0, 6) == 3
        and fallback_module._picker_tier(5, 6) == 1,
        "future picker models are not assigned deterministic proximity tiers",
    )


@test("fallback consent is exact, explicit, and non-interactive-safe")
def test_fallback_consent_contract() -> None:
    principal = runtime_module.load_role("orchestrator")
    proposal = fallback_module.nearest_fallback(
        principal,
        "authentication unavailable",
        excluded_providers={"anthropic"},
        assumed_ready={"openai"},
        discover_live=False,
    )
    require(proposal is not None, "the consent test has no proposal")

    output = io.StringIO()
    required = fallback_module.request_fallback_consent(
        proposal,
        approval=None,
        no_fallback=False,
        program_name="orrery",
        stream=output,
        tty_opener=lambda: None,
    )
    require(
        required is fallback_module.Consent.REQUIRED
        and "ORRERY FALLBACK APPROVAL REQUIRED" in output.getvalue()
        and "--approve-fallback openai:gpt-5.6-sol" in output.getvalue(),
        f"non-interactive consent was inferred or underspecified: {output.getvalue()}",
    )

    wrong = fallback_module.request_fallback_consent(
        proposal,
        approval=("openai", "gpt-5.6-terra"),
        no_fallback=False,
        program_name="orrery",
        stream=io.StringIO(),
        tty_opener=lambda: None,
    )
    approved = fallback_module.request_fallback_consent(
        proposal,
        approval=("openai", "gpt-5.6-sol"),
        no_fallback=False,
        program_name="orrery",
        stream=io.StringIO(),
        tty_opener=lambda: None,
    )
    require(
        wrong is fallback_module.Consent.REQUIRED
        and approved is fallback_module.Consent.APPROVED,
        "approval was not bound to the exact provider/model",
    )

    changed = io.StringIO()
    stale_approval = fallback_module.request_fallback_consent(
        proposal,
        approval=("openai", "gpt-5.6-sol"),
        no_fallback=False,
        program_name="orrery",
        context_warning=True,
        require_rerun_after_inspection=True,
        stream=changed,
        tty_opener=lambda: None,
    )
    require(
        stale_approval is fallback_module.Consent.REQUIRED
        and "workspace changed during the failed attempt" in changed.getvalue()
        and "did not start another write-capable process" in changed.getvalue(),
        "an approval predating partial writes was incorrectly accepted",
    )


@test("provider failure diagnostics separate model, account, and transient cases")
def test_failure_classification() -> None:
    require(
        fallback_module.classify_failure("model is unavailable or not found")
        is fallback_module.FailureScope.MODEL,
        "model unavailability was not isolated to the model",
    )
    require(
        fallback_module.classify_failure(
            "Model gpt-example is not supported for this account"
        )
        is fallback_module.FailureScope.MODEL,
        "a provider's common model-not-supported wording was misclassified",
    )
    require(
        fallback_module.classify_failure("usage limit reached; no credits remain")
        is fallback_module.FailureScope.PROVIDER,
        "credit exhaustion did not exclude the provider",
    )
    require(
        fallback_module.classify_failure("service unavailable: 503")
        is fallback_module.FailureScope.TRANSIENT,
        "a transient service failure was not recognized",
    )
    require(
        fallback_module.classify_failure("overloaded_error: ECONNRESET")
        is fallback_module.FailureScope.TRANSIENT,
        "a structured transient provider error was not recognized",
    )


@test("the supervised principal requires approval before auth fallback")
def test_principal_auth_fallback_requires_approval() -> None:
    with tempfile.TemporaryDirectory() as directory:
        codex_arguments = Path(directory) / "codex-args"
        environment = review_environment("success")
        environment["CLAUDE_FAKE_AUTH"] = "logged-out"
        environment["CODEX_FAKE_ARGS"] = str(codex_arguments)
        result = run_principal(environment)

        require(
            result.returncode == fallback_module.APPROVAL_REQUIRED,
            f"an unapproved principal fallback returned {result.returncode}",
        )
        require(
            "ORRERY FALLBACK APPROVAL REQUIRED" in result.stderr
            and "openai:gpt-5.6-sol" in result.stderr,
            f"the principal proposal was not reported: {result.stderr}",
        )
        require(
            not codex_arguments.exists(),
            "the principal fallback started without approval",
        )


@test("the principal detects a missing known model before inference")
def test_principal_model_visibility_fallback() -> None:
    environment = review_environment("success")
    environment["CLAUDE_FAKE_HIDE_MODEL"] = "fable"
    result = run_principal(environment)

    require(
        result.returncode == fallback_module.APPROVAL_REQUIRED,
        f"missing principal model returned {result.returncode}: {result.stderr}",
    )
    require(
        "not picker-visible" in result.stderr
        and "Nearest candidate: Anthropic / opus / thinking max" in result.stderr
        and "ORRERY FALLBACK APPROVAL REQUIRED" in result.stderr,
        f"the same-provider model fallback was not proposed: {result.stderr}",
    )


@test("fallback never re-adds a model omitted by a live provider picker")
def test_fallback_respects_live_model_visibility() -> None:
    environment = review_environment("success")
    environment["CLAUDE_FAKE_AUTH"] = "logged-out"
    environment["CODEX_FAKE_HIDE_MODEL"] = "gpt-5.6-sol"
    result = run_principal(environment)

    require(
        result.returncode == fallback_module.APPROVAL_REQUIRED,
        f"hidden cross-provider model returned {result.returncode}",
    )
    require(
        "Nearest candidate: OpenAI / gpt-5.6-terra / thinking ultra"
        in result.stderr,
        f"a picker-hidden model was reintroduced as a fallback: {result.stderr}",
    )


@test("the supervised principal crosses providers only after exact approval")
def test_principal_runtime_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        claude_arguments = Path(directory) / "claude-args"
        codex_arguments = Path(directory) / "codex-args"
        failed_environment = review_environment("success")
        failed_environment["CLAUDE_FAKE_MODE"] = "quota"
        failed_environment["CLAUDE_FAKE_ARGS"] = str(claude_arguments)
        failed = run_principal(failed_environment)
        require(
            failed.returncode == 8
            and "ORRERY FALLBACK APPROVAL REQUIRED" in failed.stderr,
            f"the failed principal did not wait for approval: {failed.stderr}",
        )
        original_arguments = claude_arguments.read_text()

        approved_environment = review_environment("success")
        approved_environment["CLAUDE_FAKE_ARGS"] = str(claude_arguments)
        approved_environment["CODEX_FAKE_ARGS"] = str(codex_arguments)
        result = run_principal(
            approved_environment,
            "--approve-fallback",
            "openai:gpt-5.6-sol",
            "--",
            "--claude-only-argument",
        )

        require(
            result.returncode == 0,
            f"approved principal fallback failed: {result.stderr}",
        )
        arguments = codex_arguments.read_text().splitlines()
        require(
            "--model" in arguments
            and arguments[arguments.index("--model") + 1] == "gpt-5.6-sol"
            and any("ultra" in argument for argument in arguments)
            and "--claude-only-argument" not in arguments,
            f"cross-provider principal arguments were unsafe: {arguments}",
        )
        require(
            "conversation state and provider-specific CLI arguments cannot migrate"
            in result.stderr
            and "Fallback approved for openai:gpt-5.6-sol" in result.stderr,
            f"principal fallback limitations were not disclosed: {result.stderr}",
        )
        require(
            claude_arguments.read_text() == original_arguments,
            "the approved rerun retried the failed principal provider",
        )


@test("an unwritable --output destination cannot lose a completed verdict")
def test_output_failure_preserves_verdict() -> None:
    with tempfile.TemporaryDirectory() as directory:
        blocker = Path(directory) / "blocker"
        blocker.write_text("")

        environment = review_environment("success")
        process = start_review(
            environment,
            "--timeout",
            "60",
            "--output",
            str(blocker / "verdict.txt"),
            "--",
            "prompt",
        )
        stdout, stderr = finish_review(process, environment)

        require(
            process.returncode == 1,
            f"expected status 1, got {process.returncode}: {stderr}",
        )
        require(
            "# PASS" in stdout,
            "the completed verdict was lost with the failed publication",
        )
        require(
            "could not be written" in stderr,
            f"the publication failure was not reported: {stderr!r}",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a closed stdout still publishes and never loses the verdict")
def test_closed_stdout_still_publishes() -> None:
    """Each destination is attempted independently of the others."""
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "verdict.txt"
        environment = review_environment("success")

        process = subprocess.Popen(
            [
                "bash",
                "-c",
                'exec "$1" "$2" --timeout 60 --output "$3" -- prompt 1>&-',
                "bash",
                sys.executable,
                str(REVIEW_SCRIPT),
                str(output),
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        _, stderr = finish_review(process, environment)

        require(
            output.exists() and "# PASS" in output.read_text(),
            "a closed stdout prevented publication of the verdict",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


# ---------------------------------------------------------------------------
# Review wrapper: end-to-end cleanup
# ---------------------------------------------------------------------------


@test("normal completion publishes the verdict and leaves no residue")
def test_normal_completion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "-verdict.txt"
        environment = review_environment("success")
        process = start_review(
            environment,
            "--timeout",
            "60",
            "--output",
            str(output),
            "--",
            "-a prompt beginning with a hyphen",
        )
        stdout, stderr = finish_review(process, environment)

        require(
            process.returncode == 0,
            f"wrapper failed ({process.returncode}): {stderr}",
        )
        require("# PASS" in stdout, f"verdict not printed: {stdout!r}")
        require(output.exists(), "verdict was not published")
        require("# PASS" in output.read_text(), "published verdict is wrong")
        require(
            "Reviewer · openai · gpt-5.6-sol · thinking ultra"
            in stderr
            and "control resumed" in stderr,
            f"handover messages missing: {stderr!r}",
        )
        require(
            not [
                entry.name
                for entry in Path(directory).iterdir()
                if entry.name.startswith(".-verdict.txt.")
            ],
            "publication left a temporary file behind",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a restrictive umask breaks neither the run nor its diagnostics")
def test_restrictive_umask() -> None:
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "verdict.txt"

        # Success, which reads the verdict back, and failure, which reads
        # the Codex log back. Both files are created by the wrapper itself,
        # so an inherited umask must not make them unreadable.
        for mode, expected in (("success", 0), ("fail", 7)):
            environment = review_environment(mode)
            environment["KIT_UMASK"] = "0777"
            process = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'umask 0777; exec "$@"',
                    "bash",
                    sys.executable,
                    str(REVIEW_SCRIPT),
                    "--timeout",
                    "60",
                    "--output",
                    str(output),
                    "--",
                    "prompt",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout, stderr = process.communicate(timeout=180)
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

            require(
                process.returncode == expected,
                f"umask 0777 {mode}: expected {expected}, got "
                f"{process.returncode}: {stderr}",
            )
            require(
                "Traceback" not in stderr,
                f"umask 0777 {mode} raised: {stderr}",
            )

            if mode == "success":
                require("# PASS" in stdout, "the verdict was not readable")
                require(output.exists(), "the verdict was not published")
                require(
                    stat.S_IMODE(output.stat().st_mode) == 0o600,
                    "the published verdict is not private",
                )
            else:
                require(
                    "simulated codex failure" in stderr,
                    "the Codex log was not readable for diagnostics",
                )

            assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a failing Codex run is reported and leaves no residue")
def test_failing_run() -> None:
    environment = review_environment("fail")
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    _, stderr = finish_review(process, environment)

    require(process.returncode == 7, f"expected status 7: {process.returncode}")
    require("exit status 7" in stderr, f"failure not reported: {stderr!r}")
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("an approved delegated quota fallback crosses providers once")
def test_delegated_quota_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        codex_arguments = Path(directory) / "codex-args"
        failed_environment = review_environment("success")
        failed_environment["CODEX_FAKE_MODE"] = "quota"
        failed_environment["CODEX_FAKE_ARGS"] = str(codex_arguments)
        failed = start_review(
            failed_environment,
            "--timeout",
            "60",
            "--",
            "prompt",
        )
        _, failed_stderr = finish_review(failed, failed_environment)
        require(
            failed.returncode == 7
            and "ORRERY FALLBACK APPROVAL REQUIRED" in failed_stderr,
            f"the failed reviewer did not wait for approval: {failed_stderr}",
        )
        original_arguments = codex_arguments.read_text()
        assert_no_review_residue(f"orrery-review-{failed.pid}-")

        approved_environment = review_environment("success")
        approved_environment["CODEX_FAKE_ARGS"] = str(codex_arguments)
        approved = start_review(
            approved_environment,
            "--timeout",
            "60",
            "--approve-fallback",
            "anthropic:fable",
            "--",
            "prompt",
        )
        stdout, stderr = finish_review(approved, approved_environment)

        require(
            approved.returncode == 0 and "fake Claude verdict" in stdout,
            f"approved delegated fallback failed: {stderr}",
        )
        require(
            "Nearest candidate: Anthropic / fable / thinking max" in stderr
            and "Fallback approved for anthropic:fable" in stderr
            and "↳ Fallback reviewer · anthropic · fable" in stderr,
            f"delegated fallback was not fully announced: {stderr}",
        )
        require(
            codex_arguments.read_text() == original_arguments,
            "the approved rerun retried the failed reviewer provider",
        )
        assert_no_review_residue(f"orrery-review-{approved.pid}-")


@test("a model-only failure proposes the nearest same-provider model")
def test_model_failure_prefers_same_provider() -> None:
    environment = review_environment("model-fail")
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    _, stderr = finish_review(process, environment)

    require(process.returncode == 7, f"model failure status changed: {stderr}")
    require(
        "Nearest candidate: OpenAI / gpt-5.6-terra / thinking ultra" in stderr
        and "ORRERY FALLBACK APPROVAL REQUIRED" in stderr,
        f"same-provider fallback was not proposed: {stderr}",
    )
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a transient delegated failure retries exactly once")
def test_transient_failure_retries_once() -> None:
    with tempfile.TemporaryDirectory() as directory:
        attempts = Path(directory) / "attempts"
        attempts.write_text("0")
        environment = review_environment("transient-once")
        environment["CODEX_FAKE_ATTEMPTS"] = str(attempts)
        process = start_review(environment, "--timeout", "60", "--", "prompt")
        stdout, stderr = finish_review(process, environment)

        require(
            process.returncode == 0 and "# PASS" in stdout,
            f"the transient retry did not recover: {stderr}",
        )
        require(
            attempts.read_text() == "2"
            and stderr.count("retrying it once") == 1
            and "ORRERY FALLBACK PROPOSED" not in stderr,
            f"transient retry count or reporting is wrong: {stderr}",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a failed writer requires inspection before fallback approval")
def test_partial_write_blocks_inline_fallback() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory)
        subprocess.run(
            ["git", "init", "--quiet", str(repository)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=True,
        )
        partial_edit = repository / "partial.txt"
        failed_environment = review_environment("transient")
        failed_environment["CODEX_FAKE_WRITE"] = str(partial_edit)
        failed = start_review(
            failed_environment,
            "--timeout",
            "60",
            "--role",
            "implementer",
            "--",
            "prompt",
            cwd=repository,
        )
        _, stderr = finish_review(failed, failed_environment)

        require(
            failed.returncode == 7 and partial_edit.exists(),
            f"the simulated partial writer did not fail as intended: {stderr}",
        )
        require(
            "workspace changed during the failed attempt" in stderr
            and "did not start another write-capable process" in stderr
            and "transient retry skipped" in stderr
            and "--approve-fallback anthropic:sonnet" in stderr,
            f"partial-write fallback was not gated for inspection: {stderr}",
        )
        assert_no_review_residue(f"orrery-review-{failed.pid}-")

        approved_environment = review_environment("success")
        approved = start_review(
            approved_environment,
            "--timeout",
            "60",
            "--role",
            "implementer",
            "--approve-fallback",
            "anthropic:sonnet",
            "--",
            "prompt",
            cwd=repository,
        )
        stdout, approved_stderr = finish_review(
            approved,
            approved_environment,
        )
        require(
            approved.returncode == 0 and "fake Claude verdict" in stdout,
            f"the inspected fallback could not resume: {approved_stderr}",
        )
        require(
            "Fallback approved for anthropic:sonnet" in approved_stderr,
            f"the resumed writer approval was not explicit: {approved_stderr}",
        )
        assert_no_review_residue(f"orrery-review-{approved.pid}-")


@test("fallback reports when no authenticated provider remains")
def test_no_fallback_candidate() -> None:
    environment = review_environment("success")
    environment["CODEX_FAKE_MODE"] = "quota"
    environment["CLAUDE_FAKE_AUTH"] = "logged-out"
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    _, stderr = finish_review(process, environment)

    require(process.returncode == 7, f"provider failure status changed: {stderr}")
    require(
        "no authenticated or potentially authenticated fallback candidate remains"
        in stderr
        and "ORRERY FALLBACK PROPOSED" not in stderr,
        f"absence of fallback was not explicit: {stderr}",
    )
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("--no-fallback preserves an explicitly pinned provider")
def test_no_fallback_option() -> None:
    environment = review_environment("quota")
    process = start_review(
        environment,
        "--timeout",
        "60",
        "--no-fallback",
        "--",
        "prompt",
    )
    _, stderr = finish_review(process, environment)

    require(process.returncode == 7, f"pinned provider status changed: {stderr}")
    require(
        "Fallback is disabled for this invocation" in stderr
        and "no substitution was made" in stderr,
        f"the explicit provider pin was not honored: {stderr}",
    )
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a successful run with no verdict is reported and leaves no residue")
def test_empty_verdict() -> None:
    environment = review_environment("empty")
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    _, stderr = finish_review(process, environment)

    require(process.returncode == 1, f"expected status 1: {process.returncode}")
    require("no final result" in stderr, f"not reported: {stderr!r}")
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("a timeout stops the control group and leaves no residue")
def test_timeout() -> None:
    environment = review_environment("sleep")
    process = start_review(environment, "--timeout", "5", "--", "prompt")
    _, stderr = finish_review(process, environment, timeout=180)

    require(
        process.returncode == 124,
        f"expected status 124, got {process.returncode}: {stderr}",
    )
    assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("detached SIGTERM-resistant descendants are killed with the cgroup")
def test_detached_descendant() -> None:
    environment = review_environment("detach")
    process = start_review(environment, "--timeout", "5", "--", "prompt")
    try:
        unit = wait_for_unit(process)
    finally:
        finish_review(process, environment, timeout=180)

    assert_no_review_residue(f"orrery-review-{process.pid}-")

    survivors = subprocess.run(
        ["pgrep", "-af", "signal.SIG_IGN"],
        stdout=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    ).stdout

    # A survivor is a finding, not a pet: it would otherwise sleep for an
    # hour after the suite that discovered it has exited.
    for line in survivors.splitlines():
        first = line.split()[0] if line.split() else ""
        if "3600" in line and first.isdigit():
            with contextlib.suppress(OSError):
                os.kill(int(first), signal.SIGKILL)

    require(
        "3600" not in survivors,
        f"a detached descendant of {unit} survived: {survivors}",
    )


@test("every handled signal terminates the run without residue")
def test_signals_clean_up() -> None:
    for signal_number in (
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGHUP,
        signal.SIGQUIT,
        signal.SIGUSR1,
    ):
        environment = review_environment("sleep")
        process = start_review(environment, "--timeout", "120", "--", "prompt")
        try:
            wait_for_unit(process)
            os.kill(process.pid, signal_number)
            _, stderr = process.communicate(timeout=120)
        finally:
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

        require(
            process.returncode == 128 + signal_number,
            f"signal {signal_number}: expected status "
            f"{128 + signal_number}, got {process.returncode}: {stderr}",
        )
        require(
            f"interrupted by signal {signal_number}" in stderr,
            f"signal {signal_number} was not reported: {stderr!r}",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("interruption during service registration leaves no residue")
def test_immediate_interruption() -> None:
    for delay in (0.0, 0.001, 0.003, 0.01, 0.03, 0.08, 0.2):
        environment = review_environment("sleep")
        process = start_review(environment, "--timeout", "120", "--", "prompt")
        try:
            time.sleep(delay)
            os.kill(process.pid, signal.SIGTERM)
            process.communicate(timeout=120)
        finally:
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("systemd bounds the unit even when the wrapper is killed uncatchably")
def test_runtime_max_backstop() -> None:
    marker = Path(tempfile.mkdtemp(prefix="kit-marker.")) / "codex.pid"
    environment = review_environment("sleep", marker=marker)
    process = start_review(environment, "--timeout", "1", "--", "prompt")
    unit = ""
    try:
        unit = wait_for_unit(process)
        properties = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=RuntimeMaxUSec",
            ],
            stdout=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        ).stdout.strip()
        require(
            properties.split("=", 1)[-1] not in ("", "infinity"),
            f"the transient unit has no runtime bound: {properties!r}",
        )
    finally:
        os.kill(process.pid, signal.SIGKILL)
        process.communicate(timeout=60)
        stop_stray_units(f"orrery-review-{process.pid}-")
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)
        shutil.rmtree(marker.parent, ignore_errors=True)
        # Only this wrapper's own directory. A broader glob would delete the
        # runtime state of an unrelated review running concurrently.
        for entry in review_runtime_root().glob(f"run.{process.pid}.*"):
            shutil.rmtree(entry, ignore_errors=True)


# ---------------------------------------------------------------------------
# Canonical configuration
# ---------------------------------------------------------------------------


@test("Leave No Trace cleanup keeps the session TMPDIR usable")
def test_lnt_preserves_tmpdir() -> None:
    """Removing the directory TMPDIR names breaks the rest of the session."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace",
    )

    # cleanup() sweeps the real temporary directories, which a test must
    # never do.
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-tmpdir"
    state = hook.state_dir(session)
    tmp_root = state / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    (tmp_root / "scratch").write_text("x")

    try:
        failures = hook.cleanup(
            session,
            detached_only=False,
            ignore_leases=True,
            run_registry=False,
        )

        require(not failures, f"cleanup reported failures: {failures}")
        require(
            tmp_root.is_dir(),
            "cleanup removed the directory TMPDIR points at, so every later "
            "mktemp in the session would fail",
        )
        require(
            not list(tmp_root.iterdir()),
            "cleanup left temporary files behind",
        )
        require(
            stat.S_IMODE(tmp_root.stat().st_mode) == 0o700,
            "the recreated temporary directory is not private",
        )

        # mkdir's mode argument is masked by the umask, so a restrictive
        # umask must not be able to leave TMPDIR unusable.
        (tmp_root / "scratch").write_text("x")
        saved_umask = os.umask(0o777)
        try:
            failures = hook.cleanup(
                session,
                detached_only=False,
                ignore_leases=True,
                run_registry=False,
            )
        finally:
            os.umask(saved_umask)

        require(not failures, f"cleanup reported failures: {failures}")
        require(
            stat.S_IMODE(tmp_root.stat().st_mode) == 0o700,
            "a restrictive umask left the temporary directory unusable",
        )
        with tempfile.NamedTemporaryFile(dir=tmp_root) as handle:
            require(bool(handle.name), "the temporary directory is unusable")
    finally:
        shutil.rmtree(state, ignore_errors=True)


@test("Leave No Trace state stays usable under a restrictive umask")
def test_lnt_umask() -> None:
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_umask",
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        saved_umask = os.umask(0o777)
        try:
            # Missing parents are created by private_dir, not by mkdir's
            # parents=True, so each level has to be secured in turn.
            deep = root / "a" / "b" / "c"
            hook.private_dir(deep)
            for level in (deep, deep.parent, deep.parent.parent):
                require(
                    stat.S_IMODE(level.stat().st_mode) == 0o700,
                    f"{level} was created with mode "
                    f"{oct(stat.S_IMODE(level.stat().st_mode))}",
                )
            with tempfile.NamedTemporaryFile(dir=deep) as handle:
                require(bool(handle.name), "the directory is unusable")

            # A pre-existing directory above the missing ones is untouched.
            outside = root / "outside"
            outside.mkdir(mode=0o755)
            os.chmod(outside, 0o755)
            hook.private_dir(outside / "child")
            require(
                stat.S_IMODE(outside.stat().st_mode) == 0o755,
                "an existing parent's mode was changed",
            )
        finally:
            os.umask(saved_umask)

        # A non-directory occupying the path must raise, so that
        # runtime_root falls through to its next candidate instead of
        # returning a root nothing can be created under.
        occupied = root / "occupied"
        occupied.write_text("not a directory")
        raised = False
        try:
            hook.private_dir(occupied)
        except OSError:
            raised = True
        require(raised, "a regular file was accepted as a directory")

        fallback = root / "fallback"
        fallback.mkdir()
        require(
            hook.private_dir(fallback).is_dir(),
            "a usable candidate was rejected",
        )

        # A log left unreadable cannot be opened, so the repair has to come
        # before the open rather than after it.
        unreadable = root / "unreadable.log"
        unreadable.touch()
        unreadable.chmod(0)
        hook.private_file(unreadable)
        with unreadable.open("a") as handle:
            handle.write("repaired\n")
        require(
            unreadable.read_text() == "repaired\n",
            "an unreadable log was not repaired before opening",
        )


@test("the transient review unit is given a private umask")
def test_service_umask() -> None:
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "marker"
        environment = review_environment("success", marker=marker)

        process = subprocess.Popen(
            [
                "bash",
                "-c",
                'umask 0777; exec "$@"',
                "bash",
                sys.executable,
                str(REVIEW_SCRIPT),
                "--timeout",
                "60",
                "--",
                "prompt",
            ],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        _, stderr = process.communicate(timeout=180)
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

        require(process.returncode == 0, f"the review failed: {stderr}")
        require(marker.exists(), "the stand-in Codex did not run")
        require(
            stat.S_IMODE(marker.stat().st_mode) == 0o600,
            "the transient unit did not run with a private umask; it is "
            "started by the user manager and does not inherit one",
        )
        assert_no_review_residue(f"orrery-review-{process.pid}-")


@test("an unexpired lease survives a turn boundary but not the session end")
def test_lease_survives_stop() -> None:
    """claude-lnt-start exists for work that spans tool calls."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_leases",
    )
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-lease"
    state = hook.private_dir(hook.state_dir(session))
    hook.private_dir(state / "tmp")
    hook.save_json(
        state / "meta.json",
        {"session_id": session, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
    )

    # Working data the leased process depends on. Surviving the turn without
    # it would not be a meaningful promise.
    sentinel = state / "tmp" / "sentinel"
    sentinel.write_text("working data")

    lease = "test-lease-identifier"
    environment = os.environ.copy()
    environment["CLAUDE_CODE_SESSION_ID"] = session
    environment["CLAUDE_CODE_CHILD_SESSION"] = "1"
    environment["CLAUDE_LNT_LEASE_ID"] = lease
    environment.pop("CLAUDE_LNT_INTERNAL", None)

    leased = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env=environment,
        start_new_session=True,
    )

    def run_cleanup(event: str) -> None:
        payload = json.dumps({"session_id": session, "hook_event_name": event})
        subprocess.run(
            [
                sys.executable,
                str(KIT_DIR / "global" / "hooks" / "leave-no-trace.py"),
                "hook-cleanup",
            ],
            input=payload,
            env={
                **os.environ,
                "CLAUDE_LNT_DISABLE_WATCHDOG": "1",
                # The hook runs in a fresh interpreter, so an in-process
                # stub cannot reach it. Without this the real sweep would
                # delete real profiles from the real /tmp.
                "CLAUDE_LNT_SKIP_PROFILE_SWEEP": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )

    try:
        hook.save_json(
            state / "leases.json",
            {
                lease: {
                    "pid": leased.pid,
                    "start": hook.proc_start(leased.pid),
                    "expires": time.time() + 600,
                    "command": ["sleep"],
                }
            },
        )

        run_cleanup("Stop")
        time.sleep(0.5)
        require(
            leased.poll() is None,
            "a leased process was killed at the end of a turn, which is "
            "exactly what claude-lnt-start promises will not happen",
        )
        require(
            sentinel.exists(),
            "the leased process survived but its temporary directory was "
            "cleared underneath it, so it lost its sockets and working data",
        )

        run_cleanup("SessionEnd")
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and leased.poll() is None:
            time.sleep(0.1)
        require(
            leased.poll() is not None,
            "a leased process survived the end of the session",
        )
        require(
            not sentinel.exists(),
            "session temporary data outlived the session",
        )
    finally:
        if leased.poll() is None:
            leased.kill()
        leased.wait(timeout=30)
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("a lease expiring mid-cleanup never strands a live process without data")
def test_lease_expiry_during_cleanup() -> None:
    """The process and its working directory must be decided together.

    Sampling the leases twice let an expiry between the two spare the
    process on the first check and delete its directory on the second,
    leaving it alive and broken.
    """
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_expiry",
    )
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-expiry"
    state = hook.private_dir(hook.state_dir(session))
    hook.private_dir(state / "tmp")
    hook.private_dir(state / "cleanup.d")
    hook.save_json(
        state / "meta.json",
        {"session_id": session, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
    )

    sentinel = state / "tmp" / "sentinel"
    sentinel.write_text("working data")

    lease = "expiring-lease"
    environment = os.environ.copy()
    environment["CLAUDE_CODE_SESSION_ID"] = session
    environment["CLAUDE_CODE_CHILD_SESSION"] = "1"
    environment["CLAUDE_LNT_LEASE_ID"] = lease
    environment.pop("CLAUDE_LNT_INTERNAL", None)

    leased = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        env=environment,
        start_new_session=True,
    )

    try:
        # Expires while the registered cleanup below is still running.
        hook.save_json(
            state / "leases.json",
            {
                lease: {
                    "pid": leased.pid,
                    "start": hook.proc_start(leased.pid),
                    "expires": time.time() + 0.5,
                    "command": ["sleep"],
                }
            },
        )
        # A rollback that destroys exactly what the leased process depends
        # on. It must not run while that process is still alive.
        hook.save_json(
            state / "cleanup.d" / "00000000000000000001-slow.json",
            {
                "argv": [
                    sys.executable,
                    "-c",
                    f"import os, time; time.sleep(2); os.unlink({str(sentinel)!r})",
                ]
            },
        )

        subprocess.run(
            [
                sys.executable,
                str(KIT_DIR / "global" / "hooks" / "leave-no-trace.py"),
                "hook-cleanup",
            ],
            input=json.dumps(
                {"session_id": session, "hook_event_name": "Stop"}
            ),
            env={
                **os.environ,
                "CLAUDE_LNT_DISABLE_WATCHDOG": "1",
                # The hook runs in a fresh interpreter, so an in-process
                # stub cannot reach it. Without this the real sweep would
                # delete real profiles from the real /tmp.
                "CLAUDE_LNT_SKIP_PROFILE_SWEEP": "1",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
        time.sleep(0.5)

        alive = leased.poll() is None
        kept = sentinel.exists()
        require(
            alive == kept,
            "the process and its working directory disagreed: alive="
            f"{alive} data_kept={kept}. A process must never be left running "
            "without the directory it depends on.",
        )
    finally:
        if leased.poll() is None:
            leased.kill()
        leased.wait(timeout=30)
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("a lease follows the work when its launcher daemonises")
def test_lease_survives_daemonising_launcher() -> None:
    """The lease belongs to the work, not to one process.

    Requiring the recorded process to be alive would kill a descendant that
    outlived the launcher which started it, well before its term expired.
    """
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_daemon",
    )

    session = f"kit-tests-{os.getpid()}-daemon"
    state = hook.private_dir(hook.state_dir(session))
    hook.save_json(
        state / "meta.json",
        {"session_id": session, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
    )

    environment = os.environ.copy()
    environment["CLAUDE_CODE_SESSION_ID"] = session
    environment["CLAUDE_CODE_CHILD_SESSION"] = "1"
    environment["CLAUDE_LNT_LEASE_ID"] = "daemon-lease"
    environment.pop("CLAUDE_LNT_INTERNAL", None)

    launcher = subprocess.Popen(
        ["sh", "-c", "sleep 120 & echo $!"],
        env=environment,
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = int(launcher.stdout.readline().strip())
    launcher.wait(timeout=30)

    children = [child]
    try:
        hook.save_json(
            state / "leases.json",
            {
                "daemon-lease": {
                    "pid": launcher.pid,
                    "start": 1,
                    "expires": time.time() + 300,
                    "command": ["sh"],
                }
            },
        )

        require(
            Path(f"/proc/{child}").exists(), "the fixture child did not survive"
        )
        require(
            "daemon-lease" in hook.active_lease_ids(session),
            "the lease lapsed when its launcher exited, so a descendant "
            "still doing the work would be killed before its term",
        )

        owned = hook.find_owned_processes(
            session, detached_only=False, ignore_leases=False
        )
        require(
            not any(pid == child for pid, _, _ in owned),
            "the surviving descendant was selected for termination",
        )

        # Repeated with no settling delay, which is the window in which a
        # child part way through exec has an unreadable environment. Reading
        # /proc twice would classify it as unleased and then as owned.
        #
        # Each iteration gets its own session and its own lease, so the only
        # thing that can make the raced child look leased is observing that
        # child. Sharing a session with an already-visible holder would make
        # the test pass under the old two-scan behaviour.
        for index in range(8):
            racing_session = f"{session}-race-{index}"
            racing_state = hook.private_dir(hook.state_dir(racing_session))
            hook.save_json(
                racing_state / "meta.json",
                {
                    "session_id": racing_session,
                    "cwd": str(KIT_DIR),
                    "claude_pid": os.getpid(),
                },
            )

            racing_environment = os.environ.copy()
            racing_environment["CLAUDE_CODE_SESSION_ID"] = racing_session
            racing_environment["CLAUDE_CODE_CHILD_SESSION"] = "1"
            racing_environment["CLAUDE_LNT_LEASE_ID"] = "raced-lease"
            racing_environment.pop("CLAUDE_LNT_INTERNAL", None)

            racing = subprocess.Popen(
                ["sh", "-c", "sleep 60 & echo $!"],
                env=racing_environment,
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            raced = int(racing.stdout.readline().strip())
            racing.wait(timeout=30)
            children.append(raced)

            hook.save_json(
                racing_state / "leases.json",
                {
                    "raced-lease": {
                        "pid": racing.pid,
                        "start": 1,
                        "expires": time.time() + 300,
                        "command": ["sh"],
                    }
                },
            )

            try:
                active = hook.active_lease_ids(racing_session)
                owned = hook.find_owned_processes(
                    racing_session,
                    detached_only=False,
                    ignore_leases=False,
                    active_ids=active,
                )
                require(
                    not any(pid == raced for pid, _, _ in owned),
                    f"iteration {index}: a leased descendant was selected "
                    "for termination while its exec was still in flight",
                )
            finally:
                shutil.rmtree(
                    hook.state_dir(racing_session), ignore_errors=True
                )
    finally:
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("cleanup of an unknown session is a no-op, not an error")
def test_cleanup_absent_session() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(KIT_DIR / "global" / "hooks" / "leave-no-trace.py"),
            "cleanup",
            f"absent-{os.getpid()}",
        ],
        env={**os.environ, "CLAUDE_LNT_SKIP_PROFILE_SWEEP": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        check=False,
    )
    require(
        result.returncode == 0,
        f"cleanup of an absent session failed: {result.stderr.strip()}",
    )
    require(
        "Clean" in result.stdout,
        f"unexpected output: {result.stdout.strip()!r}",
    )


@test("unlinking a lock cannot split mutual exclusion")
def test_lock_survives_unlink() -> None:
    """A lock held on an unlinked inode excludes nobody.

    Reclamation unlinks the pathname, so a waiter that was already blocked
    on the old inode must notice and start again on the file that replaced
    it, rather than believing it holds the lock.
    """
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_unlink",
    )

    session = f"kit-tests-{os.getpid()}-unlink"
    path = hook.session_lock_path(session)
    overlapped: list[bool] = []
    inside = threading.Event()

    def waiter() -> None:
        inside.wait(timeout=30)
        with hook.session_lock(session):
            # If exclusion had split, a second holder could enter here.
            entered: list[bool] = []

            def contender() -> None:
                with hook.session_lock(session):
                    entered.append(True)

            other = threading.Thread(target=contender)
            other.start()
            other.join(timeout=2.0)
            overlapped.append(bool(entered))

    thread = threading.Thread(target=waiter)
    try:
        thread.start()
        with hook.session_lock(session):
            inside.set()
            time.sleep(0.3)
            # Reclamation, exactly as cleanup performs it.
            path.unlink(missing_ok=True)
        thread.join(timeout=60)

        require(overlapped, "the waiter never acquired the lock")
        require(
            not overlapped[0],
            "two holders were inside the lock at once, so unlinking split "
            "mutual exclusion across two inodes",
        )
    finally:
        thread.join(timeout=30)
        path.unlink(missing_ok=True)


@test("lock files are reclaimed rather than accumulating per session")
def test_lock_files_reclaimed() -> None:
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_lockgc",
    )
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-lockgc"
    lock = hook.session_lock_path(session)

    try:
        with hook.session_lock(session):
            pass
        require(lock.exists(), "the fixture did not create a lock")

        # A crashed session leaves its lock behind; nothing holds it now.
        hook.sweep_orphan_locks()
        require(
            not lock.exists(),
            "an unheld lock for a session with no state was not reclaimed",
        )

        # A lock somebody is holding must survive the sweep.
        held = f"{session}-held"
        with hook.session_lock(held):
            hook.sweep_orphan_locks()
            require(
                hook.session_lock_path(held).exists(),
                "the sweep reclaimed a lock that was actively held",
            )
        hook.sweep_orphan_locks()

        # A normal end of session takes its own lock with it.
        ending = f"{session}-ending"
        hook.private_dir(hook.state_dir(ending))
        hook.save_json(
            hook.state_dir(ending) / "meta.json",
            {"session_id": ending, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
        )
        hook.scan_session_processes = lambda session_id: []
        hook.cleanup(
            ending,
            detached_only=False,
            ignore_leases=True,
            run_registry=False,
            remove_state=True,
        )
        require(
            not hook.session_lock_path(ending).exists(),
            "a completed session left its lock behind",
        )
    finally:
        for name in (session, f"{session}-held", f"{session}-ending"):
            hook.session_lock_path(name).unlink(missing_ok=True)
            shutil.rmtree(hook.state_dir(name), ignore_errors=True)


@test("the session lock outlives the directory it protects")
def test_lock_outside_session_state() -> None:
    """A lock inside the session directory cannot serialise its own removal.

    Two cases it has to survive: a first launch racing a cleanup before the
    directory exists at all, and a launch racing the removal at SessionEnd.
    """
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_lockfile",
    )
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    class Arguments:
        command = ["sleep", "60"]
        ttl = 300

    saved_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    launched: list[int] = []
    sessions: list[str] = []

    try:
        # The lock file must not live under the session directory.
        probe = f"kit-tests-{os.getpid()}-lockfile"
        sessions.append(probe)
        with hook.session_lock(probe):
            pass
        require(
            not (hook.state_dir(probe) / ".lock").exists(),
            "the lock is inside the directory whose removal it serialises",
        )

        # A first launch, with no session directory yet, racing a cleanup.
        fresh = f"kit-tests-{os.getpid()}-firstlaunch"
        sessions.append(fresh)
        started = threading.Event()
        original_records = hook.lease_records

        def slow_records(session_id: str) -> Any:
            started.set()
            time.sleep(1.0)
            return original_records(session_id)

        def launch() -> None:
            hook.lease_records = slow_records
            os.environ["CLAUDE_CODE_SESSION_ID"] = fresh
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    hook.start_process(Arguments())
            finally:
                hook.lease_records = original_records

        thread = threading.Thread(target=launch)
        thread.start()
        require(started.wait(timeout=30), "the launch never reached its lease")
        hook.cleanup(
            fresh,
            detached_only=False,
            ignore_leases=False,
            run_registry=False,
        )
        thread.join(timeout=60)

        records = hook.lease_records(fresh)
        require(records, "the first launch recorded no lease")
        pid = next(iter(records.values()))["pid"]
        launched.append(pid)
        require(
            Path(f"/proc/{pid}").exists(),
            "a cleanup ran unlocked because the session directory did not "
            "exist yet, and killed the launch it raced",
        )
    finally:
        if saved_session is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = saved_session
        for pid in launched:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
        for name in sessions:
            shutil.rmtree(hook.state_dir(name), ignore_errors=True)


@test("a launch in flight is not killed by a concurrent cleanup")
def test_launch_survives_concurrent_cleanup() -> None:
    """The window between Popen and recording the lease must be covered."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_launch",
    )
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-launch"
    state = hook.private_dir(hook.state_dir(session))
    hook.private_dir(state / "tmp")
    hook.save_json(
        state / "meta.json",
        {"session_id": session, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
    )
    hook.save_json(state / "leases.json", {})

    class Arguments:
        command = ["sleep", "60"]
        ttl = 300

    started = threading.Event()
    original_records = hook.lease_records

    def slow_records(session_id: str) -> Any:
        started.set()
        time.sleep(1.0)
        return original_records(session_id)

    def launch() -> None:
        hook.lease_records = slow_records
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                hook.start_process(Arguments())
        finally:
            hook.lease_records = original_records

    saved_session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    os.environ["CLAUDE_CODE_SESSION_ID"] = session
    thread = threading.Thread(target=launch)
    launched: int | None = None

    try:
        thread.start()
        require(started.wait(timeout=30), "the launch never reached its lease")

        # Concurrent with the launch, in the window before it is recorded.
        hook.cleanup(
            session,
            detached_only=False,
            ignore_leases=False,
            run_registry=False,
        )
        thread.join(timeout=60)

        records = hook.lease_records(session)
        require(records, "the launch recorded no lease")
        launched = next(iter(records.values()))["pid"]
        require(
            Path(f"/proc/{launched}").exists(),
            "a process was terminated between being launched and having its "
            "lease recorded, which is the window the lock has to cover",
        )
    finally:
        if saved_session is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = saved_session
        if launched:
            try:
                os.kill(launched, signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("a lease cannot be granted inside the teardown window")
def test_lease_creation_serialises_with_teardown() -> None:
    """Otherwise a new holder is left with the directory just removed."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_lock",
    )
    hook.run_registered_cleanup = lambda session: []
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None
    hook.scan_session_processes = lambda session: []

    session = f"kit-tests-{os.getpid()}-lock"
    state = hook.private_dir(hook.state_dir(session))
    hook.private_dir(state / "tmp")
    hook.save_json(
        state / "meta.json",
        {"session_id": session, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
    )
    hook.save_json(state / "leases.json", {})

    def record_lease() -> None:
        with hook.session_lock(session):
            records = hook.lease_records(session)
            records["new"] = {
                "pid": 4242,
                "start": 1,
                "expires": time.time() + 300,
                "command": ["worker"],
            }
            hook.save_json(state / "leases.json", records)

    writer = threading.Thread(target=record_lease)
    original_save = hook.save_json

    def save_and_race(path: Path, value: Any) -> None:
        original_save(path, value)
        # Start the competing writer exactly inside the teardown window.
        if path.name == "leases.json" and value == {}:
            writer.start()
            time.sleep(0.3)

    hook.save_json = save_and_race
    try:
        hook.cleanup(
            session,
            detached_only=False,
            ignore_leases=False,
            run_registry=True,
        )
    finally:
        hook.save_json = original_save
        writer.join(timeout=30)

    try:
        require(
            "new" in hook.lease_records(session),
            "the competing lease was lost entirely",
        )
        require(
            (state / "tmp").is_dir(),
            "a lease was granted whose temporary directory had already been "
            "removed, so its holder would run with no state",
        )
    finally:
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("teardown revokes the leases it invalidates")
def test_teardown_revokes_leases() -> None:
    """A lease protects state. Once that state is gone, so is the lease.

    A holder that only becomes visible after its directory was removed must
    not be spared on every later cleanup, which would leave it running
    without the state it depends on until its term expired.
    """
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_revoke",
    )
    hook.run_registered_cleanup = lambda session: []
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-revoke"
    state = hook.private_dir(hook.state_dir(session))
    tmp = hook.private_dir(state / "tmp")
    (tmp / "sentinel").write_text("working data")
    hook.save_json(
        state / "meta.json",
        {"session_id": session, "cwd": str(KIT_DIR), "claude_pid": os.getpid()},
    )
    hook.save_json(
        state / "leases.json",
        {
            "late": {
                "pid": 999999,
                "start": 1,
                "expires": time.time() + 600,
                "command": ["worker"],
            }
        },
    )

    holder = [
        (
            4242,
            "worker",
            {
                "CLAUDE_CODE_SESSION_ID": session,
                "CLAUDE_CODE_CHILD_SESSION": "1",
                "CLAUDE_LNT_LEASE_ID": "late",
            },
        )
    ]

    def sequence(scans: list[list[Any]]) -> Any:
        calls = {"n": 0}

        def scan(_session: str) -> list[Any]:
            index = min(calls["n"], len(scans) - 1)
            calls["n"] += 1
            return scans[index]

        return scan

    try:
        # The holder is invisible until after teardown has happened.
        hook.scan_session_processes = sequence([[], [], holder])
        first = hook.cleanup(
            session,
            detached_only=False,
            ignore_leases=False,
            run_registry=True,
        )
        require(
            not (tmp / "sentinel").exists(),
            "nothing was torn down, so this does not test revocation",
        )
        require(first, "the late holder was not reported after teardown")

        # Now it is visible from the start. Its lease must not come back.
        hook.scan_session_processes = sequence([holder])
        second = hook.cleanup(
            session,
            detached_only=False,
            ignore_leases=False,
            run_registry=True,
        )
        require(
            second,
            "a holder whose state was already removed was spared again, so "
            "it would survive untouched for the rest of its term",
        )
    finally:
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("a finished process's lease stops protecting the session directory")
def test_dead_lease_releases_state() -> None:
    """Lease records outlive their processes, so expiry alone is not enough."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_dead_lease",
    )

    session = f"kit-tests-{os.getpid()}-dead-lease"
    state = hook.private_dir(hook.state_dir(session))

    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=30)

    try:
        hook.save_json(
            state / "leases.json",
            {
                "long-ttl": {
                    "pid": finished.pid,
                    "start": 1,
                    # An hour of term remaining, but the process is gone.
                    "expires": time.time() + 3600,
                    "command": ["true"],
                }
            },
        )
        require(
            not hook.active_lease_ids(session),
            "a lease whose process has exited still counted as active, so it "
            "would protect the session directory for the rest of its term",
        )
    finally:
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("orphaned browser profiles are swept but live ones are spared")
def test_orphan_browser_profiles() -> None:
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_leave_no_trace_profiles",
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        session = f"kit-tests-{os.getpid()}-profiles"

        # Every driver and browser combination that creates an ephemeral
        # profile, so a missing prefix leaves one behind for ever.
        abandoned = [
            root / f"{prefix}-abandoned"
            for prefix in (
                "playwright_chromiumdev_profile",
                "playwright_firefoxdev_profile",
                "playwright_webkitdev_profile",
                "puppeteer_dev_chrome_profile",
                "puppeteer_dev_firefox_profile",
            )
        ]

        stale = root / "playwright_chromiumdev_profile-stale"
        recent = root / "playwright_chromiumdev_profile-recent"
        live = root / "playwright_chromiumdev_profile-live"
        unrelated = root / "important-data"

        # Names that merely begin with a driver's name are the user's, not
        # ephemeral profiles. This sweep deletes permanently, so the glob
        # must not reach them.
        bystanders = [
            root / "playwright_test-results",
            root / "playwright_report",
            root / "puppeteer_cache",
        ]

        for path in [stale, recent, live, unrelated, *bystanders, *abandoned]:
            path.mkdir()
            (path / "marker").write_text("x")

        old = time.time() - hook.ORPHAN_PROFILE_GRACE_SECONDS - 60
        for path in [stale, live, *bystanders, *abandoned]:
            os.utime(path, (old, old))

        # A browser whose command line still names its profile.
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; time.sleep(60)",
                f"--user-data-dir={live}",
            ]
        )

        try:
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                if str(live) in "\n".join(hook.live_command_lines()):
                    break
                time.sleep(0.05)

            hook.sweep_orphan_browser_profiles(session, roots={root})

            require(not stale.exists(), "an abandoned profile was not removed")
            for path in abandoned:
                require(
                    not path.exists(),
                    f"{path.name} was not swept: its driver prefix is "
                    "missing from BROWSER_PROFILE_PATTERNS",
                )
            require(
                recent.exists(),
                "a profile inside the grace period was removed, which could "
                "race a browser that has just started",
            )
            require(live.exists(), "a live browser's profile was removed")
            require(unrelated.exists(), "an unrelated directory was removed")
            for path in bystanders:
                require(
                    path.exists(),
                    f"{path.name} was deleted: the glob is matching the "
                    "driver name rather than its profile prefix",
                )
        finally:
            holder.kill()
            holder.wait(timeout=30)
            shutil.rmtree(hook.state_dir(session), ignore_errors=True)


@test("the watchdog lapses rather than tearing down a live session")
def test_watchdog_deadline_lapse() -> None:
    """Reaching the 24h watch bound must not be treated as Claude dying."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_watch_lapse",
    )
    calls: list[dict[str, Any]] = []
    hook.cleanup = lambda session_id, **kwargs: (calls.append(kwargs), [])[1]
    hook.append_log = lambda *args, **kwargs: None

    hook.live_same_process = lambda pid, start: True
    hook.MAX_WATCH_SECONDS = 0
    status = hook.watch("kit-tests-watch", 12345, 67890)
    require(status == 0, f"watch returned {status}")
    require(
        calls == [],
        f"a live session was cleaned up at the deadline: {calls}",
    )

    # Claude exiting in the window between the loop test and the deadline
    # test is a crash to clean up after, not a term to lapse.
    answers = iter([True, False])
    hook.live_same_process = lambda pid, start: next(answers, False)
    hook.watch("kit-tests-watch", 12345, 67890)
    require(
        calls and calls[-1].get("remove_state") is True,
        f"a death at the deadline was treated as a lapse: {calls}",
    )
    calls.clear()

    # When Claude has genuinely died the terminal cleanup must still run.
    hook.live_same_process = lambda pid, start: False
    hook.watch("kit-tests-watch", 12345, 67890)
    require(
        calls and calls[-1].get("remove_state") is True,
        f"a dead session was not torn down: {calls}",
    )


@test("a pre-existing browser profile is preserved, not deleted")
def test_persistent_profile_preserved() -> None:
    """Recency of writes must never be read as proof of session creation."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_profiles",
    )
    hook.append_log = lambda *args, **kwargs: None
    session = f"kit-tests-{os.getpid()}-profile"

    with tempfile.TemporaryDirectory() as directory:
        # A persistent profile a session browser merely opened: it is the
        # user's own uid and freshly modified, exactly what the old
        # recency heuristic deleted.
        profile = Path(directory) / "chromium-work"
        (profile / "Default").mkdir(parents=True)
        (profile / "Default" / "Cookies").write_text("precious")

        require(
            hook.safe_remove_profile(profile, session),
            "preservation must not be reported as a failure",
        )
        require(profile.is_dir(), "a pre-existing profile was deleted")
        require(
            (profile / "Default" / "Cookies").read_text() == "precious",
            "a pre-existing profile was damaged",
        )

    # A profile under the session's own temporary directory is removed.
    state_tmp = hook.state_dir(session) / "tmp"
    state_tmp.mkdir(parents=True, exist_ok=True)
    session_profile = state_tmp / "profile-x"
    session_profile.mkdir()
    try:
        require(
            hook.safe_remove_profile(session_profile, session),
            "removing a session-created profile failed",
        )
        require(
            not session_profile.exists(),
            "a session-created profile was not removed",
        )

        # The session temporary directory itself is what TMPDIR names, so
        # a browser pointed straight at it must not cost the session its
        # own working directory.
        require(
            hook.safe_remove_profile(state_tmp, session),
            "preserving the session TMPDIR must not be a failure",
        )
        require(
            state_tmp.is_dir(),
            "the session temporary directory itself was removed",
        )
    finally:
        shutil.rmtree(hook.state_dir(session), ignore_errors=True)

    # An ephemeral automation profile in the system temporary directory is
    # removed too.
    ephemeral = Path(tempfile.gettempdir()) / (
        f"playwright_chromiumdev_profile-kittest{os.getpid()}"
    )
    ephemeral.mkdir()
    try:
        require(
            hook.safe_remove_profile(ephemeral, session),
            "removing an ephemeral automation profile failed",
        )
        require(
            not ephemeral.exists(),
            "an ephemeral automation profile was not removed",
        )
    finally:
        shutil.rmtree(ephemeral, ignore_errors=True)


def guard_decision(command: str) -> bool:
    """True when hook-guard denies the command."""
    result = subprocess.run(
        [
            sys.executable,
            str(KIT_DIR / "global" / "hooks" / "leave-no-trace.py"),
            "hook-guard",
        ],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    return '"deny"' in result.stdout


@test("the detach guard ignores heredoc bodies and quoted text")
def test_guard_precision() -> None:
    blocked = [
        "sleep 30 &",
        "nohup ./server >log 2>&1 &",
        "./worker & disown",
        "setsid ./daemon",
        "make; nohup ./x",
        "cmd1\nnohup ./y",
        # Indentation and prefix commands do not stop a detach from being
        # the command that runs.
        "  nohup sh -c 'sleep 999 &' >/tmp/log 2>&1",
        "env nohup ./server",
        "FOO=1 nohup ./server",
        # A substitution runs wherever it appears, including inside a
        # double-quoted string or an expanding heredoc body.
        'echo "$(nohup sleep 999 >/tmp/log 2>&1 &)"',
        "cat <<EOF\n$(nohup sleep 999 &)\nEOF",
        "echo `setsid ./daemon`",
        # A quoted `<<EOF` is text, so it cannot hide the next line.
        "printf '%s\\n' '<<EOF'\nnohup sh -c 'sleep 999 &' >/tmp/log 2>&1",
    ]
    allowed = [
        "git grep -n disown",
        "cat docs/disown.md",
        "grep -rn setsid .",
        "cat > entry.sh <<'EOF'\n#!/bin/sh\nnginx &\nexec app\nEOF",
        # A tab-indented delimiter only ends a `<<-` heredoc; for a plain
        # `<<` it is body text and the backgrounding line is data too.
        "cat <<EOF\n\tEOF\nsleep 30 &\nEOF",
        "cat <<-EOF\n\tnginx &\n\tEOF",
        # Only an exact delimiter line ends a heredoc, so a line with
        # trailing spaces leaves the rest of the body as data.
        "cat <<EOF\nEOF   \nserver &\nEOF",
        # Delimiters are not restricted to word characters.
        "cat <<'END-MARK'\nserver &\nEND-MARK",
        # A single-quoted delimiter suppresses expansion, so even a
        # substitution in that body is literal text.
        "cat <<'EOF'\n$(nohup sleep 999 &)\nEOF",
        "echo 'a &\nb'",
        "echo 'first\nsleep 30 &'",
        'printf "%s" "x & y"',
        "make && make test",
        "cmd > out 2>&1",
        "grep -c '&' file",
    ]
    for command in blocked:
        require(
            guard_decision(command),
            f"a genuine detach was allowed: {command!r}",
        )
    for command in allowed:
        require(
            not guard_decision(command),
            f"a legitimate command was denied: {command!r}",
        )


@test("session state and TMPDIR exist even when the stale sweep fails")
def test_hook_start_survives_sweep_failure() -> None:
    """A slow or failing sweep must not cost the session its own setup."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_start_order",
    )
    session = f"kit-tests-{os.getpid()}-order"

    def explode() -> None:
        raise RuntimeError("sweep failed")

    hook.sweep_stale_states = explode
    hook.start_watchdog = lambda *args, **kwargs: None
    hook.read_json_stdin = lambda: {"session_id": session, "cwd": os.getcwd()}

    saved = {
        name: os.environ.get(name)
        for name in ("CLAUDE_ENV_FILE", "CLAUDE_PID")
    }
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / "environment"
        os.environ["CLAUDE_ENV_FILE"] = str(env_file)
        os.environ["CLAUDE_PID"] = str(os.getpid())
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                try:
                    hook.hook_start()
                    raise Failure("the stubbed sweep did not run")
                except RuntimeError:
                    pass
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        state = hook.state_dir(session)
        try:
            require(
                "TMPDIR=" in env_file.read_text(),
                "TMPDIR redirection was not written before the sweep",
            )
            require(
                (state / "tmp").is_dir(),
                "the session temporary directory was not created first",
            )
            require(
                (state / "meta.json").exists(),
                "session metadata was not written before the sweep",
            )
            require(
                "Leave No Trace automation is active" in captured.getvalue(),
                "the hook context was not printed before the sweep",
            )
            context = json.loads(captured.getvalue())["hookSpecificOutput"][
                "additionalContext"
            ]
            require(
                "at most 2 reviewer rounds" in context
                and "ask the user to decide" in context,
                f"SessionStart omitted the live plan-review cap: {context}",
            )
        finally:
            shutil.rmtree(state, ignore_errors=True)


@test("SessionStart injects the bounded plan-review contract")
def test_plan_review_session_context() -> None:
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_plan_review_context",
    )
    with tempfile.TemporaryDirectory() as directory:
        manifest_path = Path(directory) / "orchestration.json"
        hook.ORCHESTRATION_MANIFEST = manifest_path

        def configure(value: Any) -> None:
            write_json(
                manifest_path,
                {"settings": {"plan_review_rounds": {"value": value}}},
            )

        for value in (1, 2, 4):
            configure(value)
            require(
                hook.configured_plan_review_rounds() == value,
                f"the hook did not read a {value}-round cap",
            )
            context = hook.plan_review_context(value)
            require(
                f"at most {value} reviewer round" in context
                and "blocking or advisory" in context
                and "stop before implementation" in context
                and "Do not iterate merely to obtain agreement" in context,
                f"the {value}-round context omits its safety contract: {context}",
            )
            if value == 1:
                require(
                    "cannot receive an independent confirmation round" in context,
                    "the one-round escalation rule is absent",
                )
            else:
                require(
                    "the original blocking objections" in context,
                    "confirmation rounds are not narrowly scoped",
                )

        for invalid in (0, 5, True, "2", None):
            configure(invalid)
            require(
                hook.configured_plan_review_rounds()
                == hook.DEFAULT_PLAN_REVIEW_ROUNDS,
                f"invalid cap did not fall back safely: {invalid!r}",
            )
        manifest_path.write_text("{broken")
        require(
            hook.configured_plan_review_rounds()
            == hook.DEFAULT_PLAN_REVIEW_ROUNDS,
            "malformed JSON disabled the safe plan-review default",
        )


@test("an aged state directory without metadata is reclaimed")
def test_unattributed_state_reclaimed() -> None:
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_unattributed",
    )
    hook.append_log = lambda *args, **kwargs: None
    root = hook.runtime_root()

    aged = root / f"kit-tests-unattr-{os.getpid()}-aged"
    aged.mkdir(parents=True, exist_ok=True)
    hook.save_json(aged / "leases.json", {})
    old = time.time() - hook.MAX_WATCH_SECONDS - 60
    os.utime(aged, (old, old))
    try:
        hook.reclaim_unattributed_state(aged)
        require(not aged.exists(), "aged unattributed state was not reclaimed")
    finally:
        shutil.rmtree(aged, ignore_errors=True)

    fresh = root / f"kit-tests-unattr-{os.getpid()}-fresh"
    fresh.mkdir(parents=True, exist_ok=True)
    try:
        hook.reclaim_unattributed_state(fresh)
        require(fresh.exists(), "fresh state was reclaimed prematurely")
    finally:
        shutil.rmtree(fresh, ignore_errors=True)

    # Reclamation takes the same lock a launch takes, so a launch holding
    # it is never raced.
    contended = root / f"kit-tests-unattr-{os.getpid()}-locked"
    contended.mkdir(parents=True, exist_ok=True)
    os.utime(contended, (old, old))
    lock = root / f".{contended.name}.lock"
    holder = lock.open("a+b")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        finished = threading.Event()
        threading.Thread(
            target=lambda: (
                hook.reclaim_unattributed_state(contended),
                finished.set(),
            ),
            daemon=True,
        ).start()
        require(
            not finished.wait(1.0),
            "reclamation proceeded while a launch held the lock",
        )
        require(contended.is_dir(), "locked state was removed anyway")
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        require(
            finished.wait(10.0),
            "reclamation never completed after the lock was released",
        )
        require(not contended.exists(), "released state was not reclaimed")
    finally:
        holder.close()
        shutil.rmtree(contended, ignore_errors=True)
        lock.unlink(missing_ok=True)


@test("token usage is aggregated, deduplicated and cumulative-aware")
def test_usage_tracker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        project = home / ".claude" / "projects" / "-x"
        project.mkdir(parents=True)

        def assistant(identifier, request, tokens, when, model="m-1"):
            return json.dumps(
                {
                    "type": "assistant",
                    "timestamp": when,
                    "requestId": request,
                    "message": {
                        "id": identifier,
                        "model": model,
                        "usage": {
                            "input_tokens": tokens,
                            "cache_read_input_tokens": tokens * 10,
                            "cache_creation_input_tokens": 0,
                            "output_tokens": tokens * 2,
                        },
                    },
                }
            )

        recent = "2026-07-29T00:00:00Z"
        (project / "a.jsonl").write_text(
            "\n".join(
                [
                    # A zero-usage synthetic twin precedes the real row with
                    # the same identity: it must not swallow the count.
                    assistant("msg_1", "req_1", 0, recent),
                    assistant("msg_1", "req_1", 100, recent),
                    # The same response replayed into a resumed transcript.
                    assistant("msg_1", "req_1", 100, recent),
                    assistant("msg_2", "req_2", 50, recent),
                    # Too old to count.
                    assistant("msg_3", "req_3", 999, "2020-01-01T00:00:00Z"),
                    "not json at all",
                ]
            )
            + "\n"
        )

        codex = home / "codex" / "sessions" / "2026" / "07" / "29"
        codex.mkdir(parents=True)

        def token_count(total, when):
            return json.dumps(
                {
                    "timestamp": when,
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": total,
                                "cached_input_tokens": total // 2,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 10,
                                "total_tokens": total + 10,
                            }
                        },
                    },
                }
            )

        (codex / "rollout-2026-07-29T00-00-00-x.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session_meta",
                            "payload": {
                                "type": "session_meta",
                                "model_provider": "openai",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {
                                "type": "turn_context",
                                "model": "gpt-5.6-terra",
                            },
                        }
                    ),
                    # Cumulative: only the final count may be charged.
                    token_count(1000, recent),
                    token_count(4000, recent),
                    # A model switch after the last count must not steal
                    # the attribution of tokens it never produced.
                    json.dumps(
                        {
                            "type": "turn_context",
                            "payload": {
                                "type": "turn_context",
                                "model": "gpt-9-imaginary",
                            },
                        }
                    ),
                ]
            )
            + "\n"
        )

        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["CODEX_HOME"] = str(home / "codex")

        result = subprocess.run(
            [
                sys.executable,
                str(KIT_DIR / "scripts" / "orrery-usage"),
                "--since",
                "2026-07-28",
                "--json",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        require(result.returncode == 0, f"usage tracker failed: {result.stderr}")
        report = json.loads(result.stdout)
        usage = {
            (row["provider"], row["model"]): row for row in report["usage"]
        }

        claude = usage[("claude", "m-1")]
        require(
            claude["fresh_in"] == 150
            and claude["cache_read"] == 1500
            and claude["output"] == 300
            and claude["events"] == 2,
            f"claude usage was miscounted: {claude}",
        )

        codex_row = usage[("openai", "gpt-5.6-terra")]
        require(
            codex_row["total"] == 4010
            and codex_row["cache_read"] == 2000
            and codex_row["fresh_in"] == 2000
            and codex_row["events"] == 1,
            f"codex cumulative usage was miscounted: {codex_row}",
        )


@test("the orchestration manifest matches the live configuration")
def test_orchestration_manifest() -> None:
    manifest = read_json(KIT_DIR / "global" / "orchestration.json")
    catalogue = read_json(KIT_DIR / "global" / "model-catalogue.json")
    known = {
        provider: {
            entry["id"]: entry
            for entry in entries
        }
        for provider, entries in catalogue["providers"].items()
    }
    expected_ids = [
        "orchestrator",
        "mechanic",
        "implementer",
        "plan-reviewer",
        "reviewer",
    ]
    require(
        [step.get("id") for step in manifest["steps"]] == expected_ids,
        "manifest roles are missing, duplicated, or reordered",
    )
    steps = {step["id"]: step for step in manifest["steps"]}
    require(
        set(expected_ids) == set(steps),
        f"manifest is missing core steps: {sorted(steps)}",
    )
    expected_access = {
        "orchestrator": "principal",
        "mechanic": "workspace-write",
        "implementer": "workspace-write",
        "plan-reviewer": "read-only",
        "reviewer": "read-only",
    }
    for role_id, step in steps.items():
        require(
            bool(step.get("summary")) and bool(step.get("selects")),
            f"step {step['id']} lacks its explanatory text",
        )
        require(
            step.get("provider") in {"anthropic", "openai"}
            and step.get("access") == expected_access[role_id],
            f"{role_id} has an invalid provider or access contract",
        )
        choice = known.get(step["provider"], {}).get(step.get("model"))
        if choice is not None:
            levels = choice["thinking_levels"]
            require(
                (step.get("thinking") in levels)
                if levels
                else step.get("thinking") is None,
                f"{step['id']} has unsupported thinking "
                f"{step.get('thinking')!r} for {step.get('model')!r}",
            )

    require(
        (
            steps["orchestrator"]["provider"],
            steps["orchestrator"]["model"],
            steps["orchestrator"]["thinking"],
        )
        == ("anthropic", "fable", "max"),
        "the principal default is not Fable at maximum thinking",
    )
    for role_id in ("plan-reviewer", "reviewer"):
        require(
            (
                steps[role_id]["provider"],
                steps[role_id]["model"],
                steps[role_id]["thinking"],
            )
            == ("openai", "gpt-5.6-sol", "ultra"),
            f"{role_id} does not default to Sol at maximum thinking",
        )
    claude_settings = read_json(KIT_DIR / "global" / "claude-settings.json")
    require(
        "model" not in claude_settings
        and "effortLevel" not in claude_settings
        and "CLAUDE_CODE_EFFORT_LEVEL"
        not in claude_settings.get("env", {}),
        "Claude settings duplicate the provider-neutral role manifest",
    )

    settings = manifest.get("settings")
    require(isinstance(settings, dict), "manifest settings are not an object")
    plan_rounds = settings.get("plan_review_rounds", {})
    require(
        isinstance(plan_rounds, dict)
        and all(
            not isinstance(plan_rounds.get(key), bool)
            and isinstance(plan_rounds.get(key), int)
            for key in ("value", "minimum", "maximum")
        )
        and plan_rounds.get("minimum") == 1
        and plan_rounds.get("maximum") == 4
        and 1 <= plan_rounds["value"] <= 4,
        f"invalid plan-review round setting: {plan_rounds}",
    )
    require(
        all(
            isinstance(plan_rounds.get(key), str)
            and bool(plan_rounds[key].strip())
            for key in ("node", "label", "description")
        ),
        f"plan-review setting lacks explanatory metadata: {plan_rounds}",
    )


@test("every step offers a dropdown covering its provider's models")
def test_model_catalogue() -> None:
    """Nobody can type an identifier they have no way of knowing."""
    catalogue = read_json(KIT_DIR / "global" / "model-catalogue.json")
    providers = catalogue["providers"]
    require(
        {"anthropic", "openai"} <= set(providers),
        f"the catalogue omits a provider in use: {sorted(providers)}",
    )
    expected = {
        "anthropic": {
            "fable": ["low", "medium", "high", "xhigh", "max"],
            "opus": ["low", "medium", "high", "xhigh", "max"],
            "sonnet": ["low", "medium", "high", "xhigh", "max"],
            "haiku": [],
        },
        "openai": {
            "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
            "gpt-5.6-terra": [
                "low", "medium", "high", "xhigh", "max", "ultra"
            ],
            "gpt-5.6-sol": [
                "low", "medium", "high", "xhigh", "max", "ultra"
            ],
            "gpt-5.5": ["low", "medium", "high", "xhigh"],
        },
    }
    for provider, entries in providers.items():
        require(bool(entries), f"{provider} has no models to offer")
        actual: dict[str, list[str]] = {}
        for entry in entries:
            require(
                isinstance(entry.get("id"), str) and entry["id"].strip(),
                f"{provider} has an entry with no identifier: {entry}",
            )
            require(
                entry.get("label") == entry["id"],
                f"{provider} entry {entry['id']} has a characterised label",
            )
            levels = entry.get("thinking_levels")
            require(
                isinstance(levels, list)
                and len(levels) == len(set(levels)),
                f"{provider} entry {entry['id']} has invalid thinking levels",
            )
            default = entry.get("default_thinking")
            require(
                default is None or default in levels,
                f"{provider} entry {entry['id']} has an invalid default",
            )
            actual[entry["id"]] = levels
        require(
            actual == expected[provider],
            f"{provider} capabilities drifted: {actual}",
        )

    serialised = json.dumps(catalogue).lower()
    for characterisation in ("fast", "balanced", "strongest"):
        require(
            characterisation not in serialised,
            f"catalogue still characterises a model as {characterisation}",
        )

    all_identities = {
        (provider, entry["id"])
        for provider, entries in providers.items()
        for entry in entries
    }
    all_labels = [
        entry["label"]
        for entries in providers.values()
        for entry in entries
    ]
    require(
        len(all_labels) == len(set(all_labels)),
        f"the ordinary dropdown contains duplicate labels: {all_labels}",
    )

    # Every role gets the complete provider-neutral menu, and opening the
    # page cannot silently change its current selection.
    module = load_script(
        KIT_DIR / "scripts" / "orrery-config", "kit_config_choices"
    )
    for state in module.snapshot():
        offered = {
            (choice["provider"], choice["id"])
            for choice in state["choices"]
            if choice["known"]
        }
        require(
            offered == all_identities,
            f"{state['id']} does not offer both providers completely",
        )
        require(
            (state["provider"], state["model"]) in offered,
            f"{state['id']}'s current provider/model is not offered",
        )
        current = next(
            choice
            for choice in state["choices"]
            if choice["provider"] == state["provider"]
            and choice["id"] == state["model"]
        )
        if current["known"]:
            levels = current["thinking_levels"]
            require(
                (state["thinking"] in levels)
                if levels
                else not state["thinking"],
                f"{state['id']} exposes an impossible thinking choice",
            )

    manifest_steps = {
        step["id"]: step
        for step in read_json(
            KIT_DIR / "global" / "orchestration.json"
        )["steps"]
    }
    require(
        manifest_steps["orchestrator"]["model"] == "fable"
        and manifest_steps["orchestrator"]["thinking"] == "max",
        "the orchestrator does not default to Fable 5 at max",
    )
    for role_id in ("plan-reviewer", "reviewer"):
        require(
            manifest_steps[role_id]["model"] == "gpt-5.6-sol"
            and manifest_steps[role_id]["thinking"] == "ultra",
            f"{role_id} does not default to maximum Sol thinking",
        )

    defaults = {
        entry["id"]: entry.get("default_thinking")
        for provider in catalogue["providers"].values()
        for entry in provider
    }
    for model in (
        "fable",
        "sonnet",
    ):
        require(
            defaults.get(model) == "max",
            f"{model} does not default to maximum thinking: {defaults.get(model)!r}",
        )
    require(
        defaults.get("gpt-5.6-sol") == "ultra",
        "gpt-5.6-sol does not default to its maximum thinking level, ultra",
    )


@test("model thinking levels are discovered per model from both CLIs")
def test_live_model_catalogue_discovery() -> None:
    config = load_script(
        CONFIG_SCRIPT,
        f"kit_config_discovery_{time.time_ns()}",
    )
    discovery = sys.modules["orrery_model_catalogue"]
    environment = review_environment("success")
    try:
        result = discovery.discover_catalogue(
            config.bundled_catalogue(),
            timeout=5,
            environment=environment,
        )
        broken_environment = dict(environment)
        broken_environment["CLAUDE_FAKE_MODE"] = "fail"
        partial = discovery.discover_catalogue(
            config.bundled_catalogue(),
            timeout=5,
            environment=broken_environment,
        )
    finally:
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

    require(
        result.sources
        == {"anthropic": "installed CLI", "openai": "installed CLI"},
        f"live provider catalogues were not used: {result}",
    )
    require(not result.warnings, f"model discovery warned: {result.warnings}")
    anthropic = {
        entry["id"]: entry for entry in result.providers["anthropic"]
    }
    openai = {
        entry["id"]: entry for entry in result.providers["openai"]
    }
    require(
        set(anthropic)
        == {"fable", "opus", "sonnet", "haiku", "nova"},
        f"Claude aliases were duplicated or future models vanished: {anthropic}",
    )
    require(
        "default" not in anthropic
        and "opus[1m]" not in anthropic
        and "claude-fable-5[1m]" not in anthropic,
        f"Claude's equivalent rows were not collapsed: {anthropic}",
    )
    require(
        anthropic["fable"]["thinking_levels"]
        == ["low", "medium", "high", "xhigh", "max"]
        and anthropic["haiku"]["thinking_levels"] == []
        and anthropic["nova"]["thinking_levels"] == ["low", "high"],
        f"Claude thinking choices were flattened across models: {anthropic}",
    )
    require(
        "gpt-6-future" in openai
        and "gpt-hidden" not in openai
        and openai["gpt-6-future"]["thinking_levels"]
        == ["minimal", "standard", "deep"]
        and openai["gpt-6-future"]["default_thinking"] == "standard",
        f"Codex pagination or future effort discovery failed: {openai}",
    )
    require(
        openai["gpt-5.6-sol"]["default_thinking"] == "ultra",
        "Orrery's maxed-out Sol default was replaced by the CLI suggestion",
    )
    require(
        partial.sources
        == {"anthropic": "fallback", "openai": "installed CLI"}
        and "gpt-6-future"
        in {
            entry["id"] for entry in partial.providers["openai"]
        }
        and {
            entry["id"] for entry in partial.providers["anthropic"]
        }
        == {"fable", "opus", "sonnet", "haiku"}
        and any("anthropic:" in warning for warning in partial.warnings),
        f"one provider failure discarded the other live catalogue: {partial}",
    )


@test("the chart reproduces the documented pipeline and names real roles")
def test_manifest_chart() -> None:
    manifest = read_json(KIT_DIR / "global" / "orchestration.json")
    roles = {step["id"] for step in manifest["steps"]}
    chart = manifest["chart"]
    nodes = {node["id"]: node for node in chart["nodes"]}

    for node in nodes.values():
        require(bool(node.get("label")), f"node {node['id']} has no label")
        for role in node["roles"]:
            require(role in roles, f"node {node['id']} names an unknown role: {role}")
        left = node["x"] - node["w"] / 2
        right = node["x"] + node["w"] / 2
        top = node["y"] - node["h"] / 2
        bottom = node["y"] + node["h"] / 2
        require(
            0 <= left < right <= chart["width"]
            and 0 <= top < bottom <= chart["height"],
            f"node {node['id']} sits outside the canvas",
        )

    used = {role for node in nodes.values() for role in node["roles"]}
    require(
        used == roles,
        f"roles missing from the chart would be unconfigurable: {roles - used}",
    )

    for edge in chart["edges"]:
        require(
            edge["from"] in nodes and edge["to"] in nodes,
            f"edge names an unknown node: {edge}",
        )
    reached = {edge["to"] for edge in chart["edges"]}
    entry = chart["nodes"][0]["id"]
    require(
        set(nodes) - reached == {entry},
        f"unreachable chart nodes: {set(nodes) - reached - {entry}}",
    )

    edge_pairs = {(edge["from"], edge["to"]) for edge in chart["edges"]}
    require(
        ("plan", "plan-review-step") in edge_pairs
        and ("plan-review-step", "plan") in edge_pairs,
        "the chart does not show the bounded plan-review cycle",
    )
    return_edge = next(
        edge
        for edge in chart["edges"]
        if edge["from"] == "plan-review-step" and edge["to"] == "plan"
    )
    require(
        not isinstance(return_edge.get("offset"), bool)
        and isinstance(return_edge.get("offset"), (int, float))
        and return_edge["offset"] != 0,
        "the chart paints both directions of the plan-review cycle together",
    )
    require(
        ("plan-review-step", "plan-escalation") in edge_pairs,
        "the chart hides plan-review deadlock escalation",
    )
    require(
        ("plan-review-step", "complex-implementation") in edge_pairs
        and nodes["complex-implementation"]["roles"] == ["implementer"],
        "complex work does not continue downward through the implementer",
    )

    classifier = [
        edge for edge in chart["edges"] if edge["from"] == "classify"
    ]
    expected_branches = [
        ("investigation", "investigation"),
        ("trivial", "trivial"),
        ("mechanical", "mechanical"),
        ("standard", "standard"),
        ("complex", "plan"),
    ]
    require(
        [(edge.get("label"), edge["to"]) for edge in classifier]
        == expected_branches,
        f"classifier branches are not in the required order: {classifier}",
    )
    targets = [nodes[target] for _label, target in expected_branches]
    require(
        [target["x"] for target in targets]
        == sorted(target["x"] for target in targets)
        and all(target["y"] > nodes["classify"]["y"] for target in targets),
        "classifier results are not a left-to-right downward rank",
    )
    for first, second in zip(targets, targets[1:]):
        gap = (
            second["x"] - second["w"] / 2
            - (first["x"] + first["w"] / 2)
        )
        require(gap >= 24, f"classifier nodes are only {gap}px apart")

    # Sample the same cubic geometry the browser uses and ensure no wire
    # travels through an unrelated node. A generous eight-pixel moat keeps
    # arrowheads and labels from reading as tangled with a box.
    def boundary(node: dict[str, Any], towards: dict[str, Any]) -> tuple[float, float]:
        dx = towards["x"] - node["x"]
        dy = towards["y"] - node["y"]
        a = node["w"] / 2
        b = node["h"] / 2
        if node.get("shape") == "diamond":
            scale = 1 / (abs(dx) / a + abs(dy) / b)
        else:
            scale = min(
                a / abs(dx) if abs(dx) > 1e-6 else float("inf"),
                b / abs(dy) if abs(dy) > 1e-6 else float("inf"),
            )
        return node["x"] + dx * scale, node["y"] + dy * scale

    def segment_distance(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        dx, dy = end[0] - start[0], end[1] - start[1]
        squared = dx * dx + dy * dy
        along = (
            max(
                0.0,
                min(
                    1.0,
                    (
                        (point[0] - start[0]) * dx
                        + (point[1] - start[1]) * dy
                    )
                    / squared,
                ),
            )
            if squared
            else 0.0
        )
        return (
            (point[0] - start[0] - along * dx) ** 2
            + (point[1] - start[1] - along * dy) ** 2
        ) ** 0.5

    def shape_distance(
        point: tuple[float, float],
        node: dict[str, Any],
    ) -> float:
        dx = abs(point[0] - node["x"])
        dy = abs(point[1] - node["y"])
        half_width, half_height = node["w"] / 2, node["h"] / 2
        if node.get("shape") != "diamond":
            if dx <= half_width and dy <= half_height:
                return 0.0
            return (
                max(dx - half_width, 0) ** 2
                + max(dy - half_height, 0) ** 2
            ) ** 0.5
        if dx / half_width + dy / half_height <= 1:
            return 0.0
        corners = (
            (node["x"], node["y"] - half_height),
            (node["x"] + half_width, node["y"]),
            (node["x"], node["y"] + half_height),
            (node["x"] - half_width, node["y"]),
        )
        return min(
            segment_distance(point, corner, corners[(index + 1) % 4])
            for index, corner in enumerate(corners)
        )

    def move_away(
        point: tuple[float, float],
        node: dict[str, Any],
        minimum: float,
    ) -> tuple[float, float]:
        dx, dy = point[0] - node["x"], point[1] - node["y"]
        length = (dx * dx + dy * dy) ** 0.5
        travel = minimum
        while True:
            moved = (
                point[0] + dx / length * travel,
                point[1] + dy / length * travel,
            )
            if shape_distance(moved, node) >= minimum:
                return moved
            travel *= 1.5

    for edge in chart["edges"]:
        source = nodes[edge["from"]]
        target = nodes[edge["to"]]
        start = boundary(source, target)
        end = move_away(boundary(target, source), target, 20)
        lift = max(26, abs(end[1] - start[1]) * 0.4)
        way = 1 if end[1] >= start[1] else -1
        offset = edge.get("offset", 0)
        c1 = (start[0] + offset, start[1] + lift * way)
        c2 = (end[0] + offset, end[1] - lift * way)
        for index in range(1, 500):
            t = index / 500
            point = tuple(
                (1 - t) ** 3 * start[axis]
                + 3 * (1 - t) ** 2 * t * c1[axis]
                + 3 * (1 - t) * t ** 2 * c2[axis]
                + t ** 3 * end[axis]
                for axis in (0, 1)
            )
            for node_id, node in nodes.items():
                if node_id in (edge["from"], edge["to"]):
                    continue
                if (
                    node["x"] - node["w"] / 2 - 8 < point[0]
                    < node["x"] + node["w"] / 2 + 8
                    and node["y"] - node["h"] / 2 - 8 < point[1]
                    < node["y"] + node["h"] / 2 + 8
                ):
                    raise Failure(
                        f"{edge['from']} → {edge['to']} tangles with {node_id}"
                    )

        # The complete fixed-size arrow marker, not merely its tip, must
        # remain visibly outside the opaque target node.
        tangent = (end[0] - c2[0], end[1] - c2[1])
        tangent_length = (tangent[0] ** 2 + tangent[1] ** 2) ** 0.5
        along = (
            tangent[0] / tangent_length,
            tangent[1] / tangent_length,
        )
        across = (-along[1], along[0])
        triangle = (
            end,
            (
                end[0] - 11 * along[0] - 5.5 * across[0],
                end[1] - 11 * along[1] - 5.5 * across[1],
            ),
            (
                end[0] - 11 * along[0] + 5.5 * across[0],
                end[1] - 11 * along[1] + 5.5 * across[1],
            ),
        )
        marker_gap = min(
            shape_distance(
                (
                    a * triangle[0][0]
                    + b * triangle[1][0]
                    + (1 - a - b) * triangle[2][0],
                    a * triangle[0][1]
                    + b * triangle[1][1]
                    + (1 - a - b) * triangle[2][1],
                ),
                target,
            )
            for row in range(11)
            for column in range(11 - row)
            for a, b in ((row / 10, column / 10),)
        )
        require(
            marker_gap >= 8,
            f"{edge['from']} → {edge['to']} hides its arrowhead "
            f"{marker_gap:.1f}px from the target",
        )

    config_source = CONFIG_SCRIPT.read_text()
    require(
        'const ARROW_CLEARANCE = 20;' in config_source
        and 'markerUnits: "userSpaceOnUse"' in config_source
        and 'class: "edge", "marker-end": "url(#arrow)"' in config_source
        and ".wires marker path { fill: var(--wire); stroke: none; }"
        in config_source,
        "the config no longer renders fixed-size, fully visible arrowheads",
    )
    plan_rounds = manifest["settings"]["plan_review_rounds"]
    require(
        plan_rounds["node"] == "plan-review-step",
        "the round control is not attached to the plan-review node",
    )

    # The chart is the README's flowchart, so its shape must not drift
    # from the documented one.
    readme = (KIT_DIR / "README.md").read_text()
    diagram_start = readme.index("flowchart TB")
    diagram = readme[diagram_start:readme.index("```", diagram_start)]
    diagram_lower = diagram.lower()
    for token in (
        "classify",
        "findings",
        "investigation",
        "mechanical",
        "trivial",
        "blocking",
        "round cap",
    ):
        require(
            token in diagram_lower,
            f"the README flowchart no longer mentions {token}",
        )
        require(
            any(token in node["label"] or token in node["id"] for node in nodes.values())
            or any(token in edge.get("label", "") for edge in chart["edges"]),
            f"the chart omits {token}, which the README documents",
        )
    require(
        "direction LR" in diagram
        and 'I0["investigation"] ~~~ T0["trivial"]' in diagram
        and 'S0["standard"] ~~~ X0["complex"]' in diagram,
        "the README does not constrain the classifier results left to right",
    )
    readme_branch_positions = [
        diagram.index(f"C ---->|{label}|")
        for label, _target in expected_branches
    ]
    require(
        readme_branch_positions == sorted(readme_branch_positions),
        "the README classifier branches are not declared in the required order",
    )


@test("the plan-review cap accepts only bounded integer rounds")
def test_plan_review_setting_validation() -> None:
    module = load_script(
        KIT_DIR / "scripts" / "orrery-config",
        "kit_config_plan_rounds",
    )
    settings = {state["id"]: state for state in module.settings_snapshot()}
    state = settings["plan_review_rounds"]
    require(
        state["value"] == 2
        and state["choices"] == [1, 2, 3, 4]
        and state["node"] == "plan-review-step",
        f"the plan-review setting state is wrong: {state}",
    )
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        require(module.print_state() == 0, "--print state failed")
    require(
        "review rounds" in printed.getvalue()
        and "1–4; global/orchestration.json" in printed.getvalue(),
        f"--print hid the plan-review cap: {printed.getvalue()!r}",
    )

    for value in (1, 4):
        edits = module.plan({"plan_review_rounds": {"value": value}})
        require(len(edits) == 1, f"{value} rounds did not produce one edit")
        after = json.loads(edits[0]["after"])
        require(
            after["settings"]["plan_review_rounds"]["value"] == value,
            f"{value} rounds was not planned correctly",
        )

    def refused(change: Any, expected: str) -> None:
        try:
            module.plan({"plan_review_rounds": change})
        except module.ConfigError as exc:
            require(
                expected in str(exc),
                f"wrong refusal for {change!r}: {exc}",
            )
        else:
            raise Failure(f"invalid plan-review change was accepted: {change!r}")

    for value in (0, 5):
        refused({"value": value}, "between 1 and 4")
    for value in (True, False, "2", 2.0, None):
        refused({"value": value}, "must be an integer")
    refused({"value": 2, "maximum": 99}, "only the value")
    refused([], "invalid change")

    with tempfile.TemporaryDirectory() as directory:
        kit = Path(directory) / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        manifest_path = kit / "global" / "orchestration.json"
        manifest = read_json(manifest_path)
        manifest["settings"]["plan_review_rounds"]["maximum"] = 99
        write_json(manifest_path, manifest)
        module.KIT_DIR = kit
        try:
            module.settings_snapshot()
        except module.ConfigError as exc:
            require(
                "bounded from 1 to 4" in str(exc),
                f"a mutated bound gave the wrong error: {exc}",
            )
        else:
            raise Failure("a mutable 99-round cap was accepted")

        manifest["settings"]["plan_review_rounds"]["maximum"] = 4
        manifest["settings"]["plan_review_rounds"]["node"] = "missing"
        write_json(manifest_path, manifest)
        try:
            module.settings_snapshot()
        except module.ConfigError as exc:
            require(
                "missing chart node" in str(exc),
                f"a missing setting node gave the wrong error: {exc}",
            )
        else:
            raise Failure("a setting attached to a missing node was accepted")


@test("policy, skill and SessionStart agree on bounded plan review")
def test_plan_review_contract_alignment() -> None:
    policy = (KIT_DIR / "global" / "AGENTS.md").read_text()
    skill = (
        KIT_DIR / "global" / "skills" / "development-orchestrator" / "SKILL.md"
    ).read_text()
    hook = (KIT_DIR / "global" / "hooks" / "leave-no-trace.py").read_text()
    for name, text in (("policy", policy), ("skill", skill)):
        text_lower = " ".join(text.lower().split())
        for phrase in (
            "blocking",
            "advisory",
            "stop early",
            "stop before implementation",
            "ask the user to choose",
        ):
            require(
                phrase in text_lower,
                f"the {name} omits the plan-review rule {phrase!r}",
            )
    require(
        "With a one-round cap" in skill,
        "the skill leaves one-round blocking objections ambiguous",
    )
    require(
        "configured_plan_review_rounds()" in hook
        and "plan_review_context" in hook
        and "plan_context" in hook,
        "SessionStart does not inject the manifest cap",
    )


@test("the agent runner accepts a role and refuses an unsafe one")
def test_agent_role_option() -> None:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        environment = review_environment("success")
        environment["CODEX_HOME"] = str(home)

        arguments_path = home / "argv.txt"
        environment["CODEX_FAKE_ARGS"] = str(arguments_path)

        try:
            process = start_review(
                environment,
                "--role",
                "plan-reviewer",
                "--timeout",
                "60",
                "--",
                "prompt",
            )
            _, stderr = finish_review(process, environment)
            require(
                process.returncode == 0,
                f"a plan-reviewer review failed ({process.returncode}): {stderr}",
            )
            require(
                "Plan reviewer · openai · gpt-5.6-sol · thinking ultra"
                in stderr,
                f"the handover did not name the configured role: {stderr!r}",
            )
            arguments = arguments_path.read_text().splitlines()
            require(
                arguments[arguments.index("--model") + 1] == "gpt-5.6-sol"
                and arguments[arguments.index("--sandbox") + 1] == "read-only",
                "the plan reviewer did not receive its static adapter",
            )
            assert_no_review_residue(f"orrery-review-{process.pid}-")

            # A role identifier can select only one manifest entry.
            for hostile in ("../reviewer", "a/b", ""):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(REVIEW_SCRIPT),
                        f"--role={hostile}",
                        "--",
                        "prompt",
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                )
                require(
                    result.returncode == 2
                    and (
                        "invalid role name" in result.stderr
                        or "--role is required" in result.stderr
                    ),
                    f"an unsafe role was accepted: {hostile!r}",
                )
        finally:
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)


def exercise_provider_neutral_config_surface() -> None:
    """Exercise the HTTP surface against an isolated checkout."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        home = root / "home"
        home.mkdir()
        environment = review_environment("success")
        environment["HOME"] = str(home)
        environment["CODEX_HOME"] = str(home / ".codex")

        process = subprocess.Popen(
            [
                sys.executable,
                str(kit / "scripts" / "orrery-config"),
                "--port",
                "0",
                "--timeout",
                "120",
                "--no-browser",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            url = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if line.startswith("CONFIG_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
            require(bool(url), "the server never announced its URL")

            import urllib.error
            import urllib.request

            page = urllib.request.urlopen(url, timeout=10).read().decode()
            for forbidden in (
                "No model runs here",
                "· fast",
                "· balanced",
                "· strongest",
                "claude-fable-5[1m]",
                "claude-sonnet-5",
            ):
                require(
                    forbidden not in page,
                    f"the configuration page still contains {forbidden!r}",
                )
            require(
                'data-kind="thinking"' in page
                and '["anthropic", "openai"].map(provider =>' in page
                and 'provider === "anthropic" ? "Anthropic" : "OpenAI"'
                in page
                and "const kin = Boolean(chosen && !self" in page
                and 'box.classList.toggle("self", self)' in page,
                "the page lacks provider-neutral controls or tandem hover logic",
            )
            require(
                "Models and thinking levels were discovered from the installed "
                "Claude and Codex CLIs." in page,
                "the page did not report its live capability source",
            )
            require(
                ".cnode.self { box-shadow:" in page
                and ".cnode.kin { box-shadow:" not in page,
                "a tandem node receives the hovered node's outline",
            )

            state_match = re.search(
                r"const STATE = (.*);\nconst SETTINGS =",
                page,
            )
            require(state_match is not None, "the page omitted its role state")
            live_states = json.loads(state_match.group(1))
            identities = {
                f"{choice['provider']}::{choice['id']}"
                for choice in live_states[0]["choices"]
                if choice["known"]
            }
            require(
                "anthropic::nova" in identities
                and "openai::gpt-6-future" in identities,
                f"future CLI models were not added automatically: {identities}",
            )
            for state in live_states:
                values = [
                    f"{choice['provider']}::{choice['id']}"
                    for choice in state["choices"]
                    if choice["known"]
                ]
                require(
                    set(values) == identities
                    and len(values) == len(set(values)),
                    f"a model menu is incomplete or duplicated: {values}",
                )

            bad = url.replace("/t/", "/t/deadbeef", 1)
            try:
                urllib.request.urlopen(bad, timeout=10)
                raise Failure("a wrong URL token was accepted")
            except urllib.error.HTTPError as error:
                require(error.code == 404, f"wrong token gave {error.code}")

            def post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
                request = urllib.request.Request(
                    url + endpoint,
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with urllib.request.urlopen(request, timeout=60) as reply:
                        return json.loads(reply.read())
                except urllib.error.HTTPError as error:
                    return json.loads(error.read())

            future = {
                "reviewer": {
                    "provider": "openai",
                    "model": "gpt-6-future",
                    "thinking": "deep",
                }
            }
            future_preview = post("preview", future)
            require(
                len(future_preview.get("edits", [])) == 1
                and '"model": "gpt-6-future"'
                in future_preview["edits"][0]["diff"]
                and '"thinking": "deep"'
                in future_preview["edits"][0]["diff"],
                f"a newly discovered model was not configurable: {future_preview}",
            )
            wrong_future_level = post(
                "preview",
                {
                    "reviewer": {
                        "provider": "openai",
                        "model": "gpt-6-future",
                        "thinking": "ultra",
                    }
                },
            )
            require(
                "thinking must be one of: minimal, standard, deep"
                in wrong_future_level.get("error", ""),
                f"a future model inherited another model's levels: "
                f"{wrong_future_level}",
            )

            all_anthropic = {
                "mechanic": {
                    "provider": "anthropic",
                    "model": "opus",
                    "thinking": "max",
                },
                "implementer": {
                    "provider": "anthropic",
                    "model": "sonnet",
                    "thinking": "max",
                },
                "plan-reviewer": {
                    "provider": "anthropic",
                    "model": "sonnet",
                    "thinking": "max",
                },
                "reviewer": {
                    "provider": "anthropic",
                    "model": "opus",
                    "thinking": "max",
                },
                "plan_review_rounds": {"value": 4},
            }
            preview = post("preview", all_anthropic)
            require(
                len(preview.get("edits", [])) == 1
                and preview["edits"][0]["file"]
                == "global/orchestration.json"
                and '"provider": "anthropic"' in preview["edits"][0]["diff"],
                f"multi-role preview was not one manifest edit: {preview}",
            )
            unchanged = read_json(kit / "global" / "orchestration.json")
            require(
                next(
                    step
                    for step in unchanged["steps"]
                    if step["id"] == "reviewer"
                )["provider"]
                == "openai",
                "preview modified the manifest",
            )

            applied = post("apply", all_anthropic)
            require(
                applied.get("applied") == ["global/orchestration.json"],
                f"the atomic apply wrote unexpected files: {applied}",
            )
            manifest = read_json(kit / "global" / "orchestration.json")
            require(
                {step["provider"] for step in manifest["steps"]}
                == {"anthropic"}
                and manifest["settings"]["plan_review_rounds"]["value"] == 4,
                "the all-Anthropic configuration was not applied",
            )

            # A Codex principal and Claude reviewers are equally valid.
            codex_principal = {
                "orchestrator": {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "thinking": "ultra",
                }
            }
            post("preview", codex_principal)
            switched = post("apply", codex_principal)
            require(
                switched.get("applied") == ["global/orchestration.json"],
                f"the Codex-principal switch failed: {switched}",
            )
            manifest = read_json(kit / "global" / "orchestration.json")
            by_id = {step["id"]: step for step in manifest["steps"]}
            require(
                by_id["orchestrator"]["provider"] == "openai"
                and by_id["reviewer"]["provider"] == "anthropic",
                "principal and reviewer providers remain artificially coupled",
            )

            for body, fragment in (
                (
                    {"reviewer": {"model": "vendor/custom-model"}},
                    "custom model change must include its provider",
                ),
                (
                    {
                        "reviewer": {
                            "provider": "anthropic",
                            "model": "gpt-5.6-sol",
                            "thinking": "max",
                        }
                    },
                    "belongs to openai",
                ),
                (
                    {
                        "reviewer": {
                            "provider": "openai",
                            "model": "gpt-5.5",
                            "thinking": "ultra",
                        }
                    },
                    "thinking must be one of",
                ),
                (
                    {
                        "reviewer": {
                            "provider": "openai",
                            "model": "vendor/custom-model",
                            "thinking": "high",
                        }
                    },
                    "available thinking levels are unknown",
                ),
            ):
                refused = post("preview", body)
                require(
                    fragment in refused.get("error", ""),
                    f"unsafe model/provider combination was accepted: {refused}",
                )

            custom = {
                "reviewer": {
                    "provider": "openai",
                    "model": "vendor/custom-model",
                }
            }
            custom_preview = post("preview", custom)
            require(
                len(custom_preview.get("edits", [])) == 1
                and '"model": "vendor/custom-model"'
                in custom_preview["edits"][0]["diff"]
                and '"thinking": null' in custom_preview["edits"][0]["diff"],
                f"a provider-qualified custom model was not planned: {custom_preview}",
            )

            for hostile in (
                "</script><img src=x>",
                'gpt"; rm -rf /',
                "gpt\nmodel=evil",
                "x" * 121,
                "",
            ):
                refused = post(
                    "preview",
                    {
                        "reviewer": {
                            "provider": "openai",
                            "model": hostile,
                        }
                    },
                )
                require(
                    "error" in refused,
                    f"a hostile model identifier was accepted: {hostile!r}",
                )

            # Preview is an exact-content contract.
            post("preview", custom)
            substituted = post(
                "apply",
                {
                    "reviewer": {
                        "provider": "openai",
                        "model": "vendor/other-model",
                    }
                },
            )
            require(
                "not the one that was previewed"
                in substituted.get("error", ""),
                f"an unpreviewed substitution was applied: {substituted}",
            )

            stale_change = {
                "mechanic": {
                    "provider": "anthropic",
                    "model": "fable",
                    "thinking": "max",
                }
            }
            post("preview", stale_change)
            manifest_path = kit / "global" / "orchestration.json"
            externally_changed = read_json(manifest_path)
            externally_changed["external"] = "preserve"
            write_json(manifest_path, externally_changed)
            stale = post("apply", stale_change)
            require(
                "changed since" in stale.get("error", "")
                and read_json(manifest_path).get("external") == "preserve",
                f"a concurrent manifest edit was overwritten: {stale}",
            )
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                process.stdout.close()
            process.wait(timeout=20)
            shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)


@test("the configuration surface previews and applies model changes")
def test_config_surface() -> None:
    exercise_provider_neutral_config_surface()
    return

@test("the model alias map is valid and covers the documented aliases")
def test_model_alias_map() -> None:
    table = read_json(KIT_DIR / "global" / "model-catalogue.json")
    providers = table.get("providers")
    require(
        isinstance(providers, dict)
        and set(providers) == {"anthropic", "openai"},
        "the provider-neutral catalogue is missing",
    )
    identities = [
        (provider, entry.get("id"))
        for provider, entries in providers.items()
        for entry in entries
    ]
    require(
        len(identities) == len(set(identities))
        and all(
            isinstance(model, str) and model.strip()
            for _provider, model in identities
        ),
        f"invalid or duplicate catalogue identities: {identities}",
    )
    aliases = {model for _provider, model in identities}
    require(
        {"fable", "opus", "sonnet", "haiku"} <= aliases,
        "the documented Anthropic aliases are incomplete",
    )


@test("residual reporting counts only survivors of this cleanup's own kills")
def test_residual_reports_survivors_only() -> None:
    """A poller's next short-lived child must not block completion."""
    hook = load_script(
        KIT_DIR / "global" / "hooks" / "leave-no-trace.py",
        "kit_lnt_residual",
    )
    hook.append_log = lambda *args, **kwargs: None
    hook.stop_snap_scopes = lambda *args, **kwargs: None
    hook.terminate_processes = lambda *args, **kwargs: None
    hook.sweep_orphan_browser_profiles = lambda *args, **kwargs: None

    session = f"kit-tests-{os.getpid()}-residual"
    env = {
        "CLAUDE_CODE_SESSION_ID": session,
        "CLAUDE_CODE_CHILD_SESSION": "1",
    }

    def scripted(snapshots: list[list[tuple[int, str, dict[str, str]]]]):
        queue = list(snapshots)
        return lambda _session: queue.pop(0) if len(queue) > 1 else queue[0]

    # The terminated poll child is replaced by a fresh one mid-cleanup: the
    # newcomer is the next sweep's business, not this cleanup's failure.
    hook.scan_session_processes = scripted(
        [
            [(100, "sleep 2", env)],
            [(100, "sleep 2", env)],
            [(200, "sleep 2", env)],
            [(200, "sleep 2", env)],
        ]
    )
    failures = hook.cleanup_locked(
        session,
        detached_only=False,
        ignore_leases=False,
        run_registry=False,
    )
    require(
        failures == [],
        f"a process spawned during cleanup was reported as residual: {failures}",
    )

    # A process that survives its own termination is a genuine failure.
    hook.scan_session_processes = scripted([[(300, "stubborn", env)]])
    failures = hook.cleanup_locked(
        session,
        detached_only=False,
        ignore_leases=False,
        run_registry=False,
    )
    require(
        any("300" in failure for failure in failures),
        f"a kill survivor was not reported as residual: {failures}",
    )


@test("the canonical settings disable the companion with a JSON Boolean")
def test_canonical_companion_boolean() -> None:
    canonical = read_json(KIT_DIR / "global" / "claude-settings.json")
    value = canonical.get("enabledPlugins", {}).get("codex@openai-codex")
    require(
        value is False and isinstance(value, bool),
        f"canonical companion value is {value!r}, expected Boolean false",
    )


@test("the canonical hooks only reference real Claude Code hook events")
def test_canonical_hook_events() -> None:
    known = {
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "UserPromptSubmit",
        "SessionStart",
        "SessionEnd",
        "Stop",
        "StopFailure",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PostCompact",
    }
    canonical = read_json(KIT_DIR / "global" / "claude-settings.json")
    unknown = sorted(set(canonical.get("hooks", {})) - known)
    require(not unknown, f"unknown hook events: {unknown}")


@test("direct Codex sessions visibly require principal fallback approval")
def test_codex_session_start_fallback_notice() -> None:
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(KIT_DIR),
        "model": "gpt-5.6-sol",
    }
    result = subprocess.run(
        [sys.executable, str(SESSION_START_SCRIPT), "openai"],
        input=json.dumps(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    require(result.returncode == 0, f"Codex SessionStart failed: {result.stderr}")
    notice = json.loads(result.stdout)
    context = notice.get("hookSpecificOutput", {}).get("additionalContext", "")
    require(
        "configured Anthropic / fable" in notice.get("systemMessage", "")
        and "thinking max" in notice.get("systemMessage", "")
        and "active OpenAI / gpt-5.6-sol" in notice.get("systemMessage", "")
        and "ORRERY PRINCIPAL FALLBACK APPROVAL REQUIRED" in context
        and "Do not infer approval" in context
        and "does not report the active thinking level" in context,
        f"the direct-session warning is incomplete: {notice}",
    )


@test("a direct session matching the repository principal emits no warning")
def test_matching_session_start_is_quiet() -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory)
        (repository / ".git").mkdir()
        write_json(
            repository / ".orrery.json",
            {
                "orchestrator": {
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "thinking": "ultra",
                }
            },
        )
        payload = {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "cwd": str(repository),
            "model": "gpt-5.6-sol",
        }
        result = subprocess.run(
            [sys.executable, str(SESSION_START_SCRIPT), "openai"],
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        require(
            result.returncode == 0 and result.stdout == "",
            f"a matching principal produced a fallback warning: {result.stdout}",
        )


@test("direct sessions distinguish the active model from the nearest candidate")
def test_session_start_recommends_nearest_model() -> None:
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "cwd": str(KIT_DIR),
        "model": "gpt-5.6-terra",
    }
    result = subprocess.run(
        [sys.executable, str(SESSION_START_SCRIPT), "openai"],
        input=json.dumps(payload),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    notice = json.loads(result.stdout)
    require(
        "active OpenAI / gpt-5.6-terra" in notice["systemMessage"]
        and "nearest potential fallback is OpenAI / gpt-5.6-sol"
        in notice["systemMessage"],
        f"the active extension model was mistaken for the nearest: {notice}",
    )


@test("the canonical Codex hook uses the supported SessionStart contract")
def test_canonical_codex_hook() -> None:
    canonical = read_json(KIT_DIR / "global" / "codex-hooks.json")
    groups = canonical.get("hooks", {}).get("SessionStart", [])
    require(len(groups) == 1, f"unexpected Codex SessionStart groups: {groups}")
    handlers = groups[0].get("hooks", [])
    require(
        groups[0].get("matcher") == "startup|resume|clear|compact"
        and len(handlers) == 1
        and handlers[0].get("type") == "command"
        and "orrery-session-start.py" in handlers[0].get("command", "")
        and handlers[0].get("timeout") == 15,
        f"the Codex SessionStart contract is invalid: {groups}",
    )


@test("the installer links every command the doctor checks")
def test_installer_covers_doctor_commands() -> None:
    installer = INSTALL_SCRIPT.read_text()
    for command in (
        "orrery-init",
        "orrery-doctor",
        "orrery-review",
        "orrery-usage",
        "orrery-config",
    ):
        require(
            f"$HOME/.local/bin/{command}" in installer,
            f"install.sh does not install {command}",
        )


@test("the installer refuses to link a canonical file over its own source")
def test_installer_refuses_self_source() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        home = root / "home"
        home.mkdir()

        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["CODEX_HOME"] = str(kit / "global" / "codex")

        result = subprocess.run(
            ["bash", str(kit / "scripts" / "install.sh")],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )

        require(
            result.returncode != 0,
            "the installer accepted a target inside its own source tree",
        )
        for relative in (
            "global/AGENTS.md",
            "global/orchestration.json",
            "global/model-catalogue.json",
        ):
            path = kit / relative
            require(
                path.is_file() and not path.is_symlink(),
                f"the canonical source was destroyed: {relative}",
            )


def exercise_green_path_install() -> None:
    """Install and initialize using only isolated user and repository state."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        home = root / "home"
        codex_home_path = root / "codex"
        (home / ".claude").mkdir(parents=True)
        codex_home_path.mkdir()
        (home / ".claude" / "AGENTS.md").write_text("claude-owned\n")
        (codex_home_path / "AGENTS.md").write_text("codex-owned\n")
        write_json(
            codex_home_path / "hooks.json",
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "user-owned-stop-hook",
                                }
                            ]
                        }
                    ]
                }
            },
        )

        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["CODEX_HOME"] = str(codex_home_path)
        environment["PATH"] = (
            f"{home / '.local' / 'bin'}{os.pathsep}"
            f"{environment.get('PATH', '')}"
        )

        def install() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["bash", str(kit / "scripts" / "install.sh")],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
                check=False,
            )

        first = install()
        require(first.returncode == 0, f"fresh install failed: {first.stderr}")
        require(
            (home / ".claude" / "AGENTS.md").resolve()
            == (kit / "global" / "AGENTS.md").resolve()
            and (codex_home_path / "AGENTS.md").resolve()
            == (kit / "global" / "AGENTS.md").resolve()
            and (home / ".claude" / "CLAUDE.md").resolve()
            == (kit / "global" / "CLAUDE.md").resolve(),
            "the canonical shared instruction chain was not linked",
        )
        require(
            (home / ".claude" / "skills" / "development-orchestrator").is_symlink()
            and (home / ".agents" / "skills" / "development-orchestrator").is_symlink(),
            "the provider-neutral skill was not installed for both CLIs",
        )
        require(
            (home / ".claude" / "hooks" / "orrery-session-start.py").resolve()
            == (kit / "scripts" / "orrery-session-start").resolve()
            and (
                codex_home_path / "hooks" / "orrery-session-start.py"
            ).resolve()
            == (kit / "scripts" / "orrery-session-start").resolve(),
            "the direct-session check was not linked for both providers",
        )
        for name in (
            "orrery",
            "orrery-agent",
            "orrery-review",
            "orrery-init",
            "orrery-doctor",
            "orrery-config",
            "orrery-usage",
        ):
            require(
                (home / ".local" / "bin" / name).is_symlink(),
                f"{name} was not installed",
            )

        settings = read_json(home / ".claude" / "settings.json")
        require(
            settings.get("enabledPlugins", {}).get("codex@openai-codex")
            is False
            and "SessionStart" in settings.get("hooks", {})
            and "model" not in settings
            and "effortLevel" not in settings
            and "CLAUDE_CODE_EFFORT_LEVEL"
            not in settings.get("env", {}),
            "installation duplicated role model settings into Claude settings",
        )
        codex_hooks = read_json(codex_home_path / "hooks.json")
        codex_commands = {
            hook.get("command")
            for groups in codex_hooks.get("hooks", {}).values()
            for group in groups
            for hook in group.get("hooks", [])
            if isinstance(hook, dict)
        }
        require(
            "user-owned-stop-hook" in codex_commands
            and any(
                command and "orrery-session-start.py" in command
                for command in codex_commands
            ),
            f"Codex hook installation lost or omitted handlers: {codex_hooks}",
        )

        backups = sorted((home / ".orrery-backups").glob("*"))
        require(len(backups) == 1, f"expected one backup set: {backups}")
        backup = backups[0]
        require(
            (
                backup
                / "home"
                / ".claude"
                / "AGENTS.md"
            ).read_text()
            == "claude-owned\n"
            and (
                backup
                / "absolute"
                / str(codex_home_path).lstrip("/")
                / "AGENTS.md"
            ).read_text()
            == "codex-owned\n",
            "same-named AGENTS backups collided",
        )

        second = install()
        require(second.returncode == 0, f"idempotent install failed: {second.stderr}")
        require(
            sorted((home / ".orrery-backups").glob("*")) == backups,
            "an idempotent install created another backup set",
        )

        repository = root / "repo"
        repository.mkdir()

        def initialize(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "bash",
                    str(kit / "scripts" / "init-project.sh"),
                    *arguments,
                    str(repository),
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )

        initialized = initialize()
        require(
            initialized.returncode == 0
            and (repository / ".git").is_dir()
            and (repository / "AGENTS.md").is_file()
            and (repository / "CLAUDE.md").read_text().strip()
            == "@AGENTS.md",
            f"plain-directory initialization failed: {initialized.stderr}",
        )
        exclude = (repository / ".git" / "info" / "exclude").read_text()
        require(
            "/.orrery.json" in exclude
            and "/CLAUDE.local.md" in exclude,
            "private project artefacts were not excluded",
        )

        chosen = initialize("fable")
        require(chosen.returncode == 0, f"Fable override failed: {chosen.stderr}")
        override = read_json(repository / ".orrery.json")
        require(
            override["orchestrator"]
            == {
                "provider": "anthropic",
                "model": "fable",
                "thinking": "max",
            },
            f"Fable did not use maximum thinking: {override}",
        )
        override["personal"] = {"keep": True}
        write_json(repository / ".orrery.json", override)
        switched = initialize("gpt-5.6-sol")
        override = read_json(repository / ".orrery.json")
        require(
            switched.returncode == 0
            and override["orchestrator"]
            == {
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "thinking": "ultra",
            }
            and override["personal"] == {"keep": True},
            f"Sol override or unrelated JSON preservation failed: {override}",
        )
        require(
            initialize("no-such-model").returncode != 0,
            "an unknown model or directory argument was accepted",
        )


@test("a fresh install succeeds, reruns idempotently and migrates a new repository")
def test_green_path_install() -> None:
    exercise_green_path_install()
    return

@test("project initialization respects Git worktree boundaries")
def test_initializer_git_boundaries() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )
        environment = os.environ.copy()

        def invoke(
            target: Path,
            *,
            env: dict[str, str] | None = None,
            explicit: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            command = ["bash", str(kit / "scripts" / "init-project.sh")]
            if explicit:
                command.append(str(target))
            return subprocess.run(
                command,
                cwd=target,
                env=env or environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )

        # With no directory argument, the current plain directory becomes a
        # repository before any templates are installed.
        plain = root / "plain"
        plain.mkdir()
        initialized = invoke(plain, explicit=False)
        require(
            initialized.returncode == 0
            and (plain / ".git").is_dir()
            and (plain / "CLAUDE.md").is_file(),
            f"default-directory initialization failed: {initialized.stderr}",
        )

        # A nested target reuses its enclosing worktree and must never grow a
        # nested .git directory.
        parent = root / "parent"
        nested = parent / "a" / "b"
        nested.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", str(parent)],
            timeout=60,
            check=True,
        )
        nested_result = invoke(nested)
        require(
            nested_result.returncode == 0
            and (parent / "CLAUDE.md").is_file()
            and not (nested / ".git").exists(),
            f"nested worktree handling failed: {nested_result.stderr}",
        )

        # Linked worktrees are already valid repositories even though .git
        # is a file rather than a directory.
        seed = root / "seed"
        linked = root / "linked"
        seed.mkdir()
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        (seed / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(seed), "add", "seed.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(seed),
                "-c",
                "user.name=kit",
                "-c",
                "user.email=kit@example.invalid",
                "commit",
                "-qm",
                "seed",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(seed),
                "worktree",
                "add",
                "-q",
                "-b",
                "linked-test",
                str(linked),
            ],
            check=True,
        )
        linked_marker = (linked / ".git").read_text()
        linked_result = invoke(linked)
        require(
            linked_result.returncode == 0
            and (linked / ".git").is_file()
            and (linked / ".git").read_text() == linked_marker
            and (linked / "CLAUDE.md").is_file(),
            f"linked worktree was reinitialized: {linked_result.stderr}",
        )

        # Git plumbing inherited from a caller must not redirect discovery
        # or installation into some other repository.
        foreign = root / "foreign"
        foreign.mkdir()
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        isolated = root / "isolated"
        isolated.mkdir()
        poisoned = environment.copy()
        poisoned["GIT_DIR"] = str(foreign / ".git")
        poisoned["GIT_WORK_TREE"] = str(foreign)
        isolated_result = invoke(isolated, env=poisoned)
        require(
            isolated_result.returncode == 0
            and (isolated / ".git").is_dir()
            and not (foreign / "CLAUDE.md").exists(),
            "inherited Git plumbing redirected initialization",
        )

        # Existing unusable metadata is never overwritten by a nested fresh
        # repository.
        malformed = root / "malformed"
        malformed.mkdir()
        (malformed / ".git").write_text("not a gitdir\n")
        malformed_result = invoke(malformed)
        require(
            malformed_result.returncode != 0
            and (malformed / ".git").read_text() == "not a gitdir\n"
            and not (malformed / "CLAUDE.md").exists(),
            "malformed Git metadata was overwritten",
        )

        bare = root / "bare.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        bare_result = invoke(bare)
        require(
            bare_result.returncode != 0
            and "bare Git repository" in bare_result.stderr
            and not (bare / "CLAUDE.md").exists(),
            "a bare repository was treated as a worktree",
        )

        # One release-compatibility pass upgrades the old managed markers
        # without leaving the retired namespace in the migrated file.
        legacy = root / "legacy"
        legacy.mkdir()
        subprocess.run(["git", "init", "-q", str(legacy)], check=True)
        retired = "claude" + "-codex"
        (legacy / "CLAUDE.local.md").write_text(
            "personal prefix\n\n"
            f"<!-- {retired}-kit:start -->\nold block\n"
            f"<!-- {retired}-kit:end -->\n\npersonal suffix\n"
        )
        legacy_result = invoke(legacy)
        migrated = (legacy / "CLAUDE.local.md").read_text()
        require(
            legacy_result.returncode == 0
            and retired not in migrated
            and "personal prefix" in migrated
            and "personal suffix" in migrated,
            f"legacy managed block migration failed: {legacy_result.stderr}",
        )


@test("project instruction migration preserves every existing-file combination")
def test_initializer_instruction_combinations() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kit = root / "kit"
        shutil.copytree(
            KIT_DIR,
            kit,
            ignore=shutil.ignore_patterns(".git", "__pycache__"),
        )

        def repository(name: str) -> Path:
            path = root / name
            path.mkdir()
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            return path

        def initialize(path: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [
                    "bash",
                    str(kit / "scripts" / "init-project.sh"),
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
                check=False,
            )

        agents_only = repository("agents-only")
        (agents_only / "AGENTS.md").write_text("personal agents\n")
        result = initialize(agents_only)
        require(
            result.returncode == 0
            and (agents_only / "AGENTS.md").read_text()
            == "personal agents\n"
            and (agents_only / "CLAUDE.md").read_text().strip()
            == "@AGENTS.md",
            "an existing AGENTS.md was not preserved",
        )

        claude_only = repository("claude-only")
        (claude_only / "CLAUDE.md").write_text("personal Claude policy\n")
        result = initialize(claude_only)
        require(
            result.returncode == 0
            and (claude_only / "CLAUDE.md").read_text()
            == "personal Claude policy\n"
            and (claude_only / "AGENTS.md").read_text()
            == "personal Claude policy\n"
            and "manual deduplication" in result.stdout,
            "Claude-only instructions were not safely mirrored",
        )

        both = repository("both")
        (both / "AGENTS.md").write_text("agents original\n")
        (both / "CLAUDE.md").write_text("claude original\n")
        result = initialize(both)
        require(
            result.returncode == 0
            and (both / "AGENTS.md").read_text() == "agents original\n"
            and (both / "CLAUDE.md").read_text() == "claude original\n"
            and "does not import @AGENTS.md" in result.stdout,
            "two arbitrary instruction files were overwritten or hidden",
        )

        broken_wrapper = repository("broken-wrapper")
        (broken_wrapper / "CLAUDE.md").write_text("@AGENTS.md\n")
        result = initialize(broken_wrapper)
        require(
            result.returncode == 0
            and (broken_wrapper / "AGENTS.md").read_text()
            == (kit / "project-template" / "AGENTS.md").read_text()
            and (broken_wrapper / "CLAUDE.md").read_text() == "@AGENTS.md\n",
            "a Claude import with a missing canonical target was not repaired",
        )


@test("the doctor rejects a hook installed under the wrong matcher or timeout")
def test_doctor_hook_fidelity() -> None:
    """A right command under a wrong matcher never runs for its tool."""
    canonical = read_json(KIT_DIR / "global" / "claude-settings.json")

    def check(live: dict[str, Any]) -> bool:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude").mkdir()
            write_json(home / ".claude" / "settings.json", live)

            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment["CODEX_HOME"] = str(home / ".codex")

            result = subprocess.run(
                ["bash", str(DOCTOR_SCRIPT)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=180,
                check=False,
            )
            return (
                "PASS  Live Claude settings contain canonical hooks and permissions"
                in result.stdout
            )

    require(
        check(copy.deepcopy(canonical)),
        "the canonical settings themselves failed the hook check",
    )

    wrong_matcher = copy.deepcopy(canonical)
    wrong_matcher["hooks"]["PreToolUse"][0]["matcher"] = "Read"
    require(
        not check(wrong_matcher),
        "a hook moved to another matcher passed the check",
    )

    wrong_timeout = copy.deepcopy(canonical)
    wrong_timeout["hooks"]["SessionStart"][0]["hooks"][0]["timeout"] = 10
    require(
        not check(wrong_timeout),
        "a hook with a shortened timeout passed the check",
    )


@test("the setup guide documents every file in the kit")
def test_setup_guide_current() -> None:
    """The previous guide drifted because it duplicated file contents.

    It now references files instead, so this only has to hold the inventory
    honest: a script nobody documented is a script nobody will find.
    """
    guide_path = KIT_DIR / "docs" / "setup-guide.md"
    require(guide_path.exists(), "docs/setup-guide.md is missing")
    guide = guide_path.read_text()

    # Whole path tokens, not substrings: "scripts/install" must not be
    # satisfied by the documented "scripts/install.sh".
    mentioned = set(re.findall(r"[A-Za-z0-9_.@/-]+", guide))

    # Git's tracked set, not the filesystem: ignored bytecode is not part of
    # the kit and must not make this fail.
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(KIT_DIR),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        stdout=subprocess.PIPE,
        text=True,
        timeout=60,
        check=True,
    ).stdout.split()

    documented: list[str] = []
    undocumented: list[str] = []

    for relative in sorted(tracked):
        if not (KIT_DIR / relative).exists():
            # A renamed path remains in the index until the final logical
            # commit; document the live path, not a deleted index entry.
            continue
        if Path(relative).name in (".gitignore", "README.md"):
            continue
        if KIT_DIR / relative == guide_path:
            continue

        (documented if relative in mentioned else undocumented).append(relative)

    require(
        not undocumented,
        f"not mentioned in docs/setup-guide.md: {undocumented}",
    )
    require(len(documented) > 15, f"the guide covers too little: {documented}")

    # Anything the guide names must still exist.
    for claim in ("scripts/orrery-review", "tests/run-tests.py"):
        require((KIT_DIR / claim).exists(), f"the guide names a missing {claim}")


@test("the retired namespace is absent from current files and paths")
def test_orrery_namespace_complete() -> None:
    retired = "claude" + "-codex"
    offenders: list[str] = []
    for path in KIT_DIR.rglob("*"):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(KIT_DIR))
        if retired in relative.lower():
            offenders.append(relative)
            continue
        if not path.is_file() or path.suffix in {".png", ".pyc"}:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if retired in text.lower():
            offenders.append(relative)
    require(
        not offenders,
        f"retired namespace remains in current artefacts: {sorted(offenders)}",
    )


@test("every command the policy names is pre-approved")
def test_policy_commands_allowed() -> None:
    """A documented command that stops for approval cannot be used headlessly."""
    canonical = read_json(KIT_DIR / "global" / "claude-settings.json")
    allow = canonical.get("permissions", {}).get("allow", [])
    prefixes = [
        rule[len("Bash(") : -len(":*)")]
        for rule in allow
        if rule.startswith("Bash(") and rule.endswith(":*)")
    ]

    policy = (KIT_DIR / "global" / "AGENTS.md").read_text()

    commands = re.findall(r"`((?:npx|codex|orrery-review)[^`]*)`", policy)

    # Fenced blocks too. The screenshot command lives in one, and shell line
    # continuations have to be folded before it can be matched. The
    # pre-completion battery commands are as mandatory as any other: one
    # that stops for approval blocks every completion in a headless run.
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", policy, re.S):
        for line in re.sub(r"\\\n\s*", " ", block).splitlines():
            line = line.strip()
            if line.startswith(
                (
                    "npx ",
                    "codex ",
                    "orrery-review ",
                    "pgrep ",
                    "ss ",
                    "ps ",
                    "ls ",
                    "grep ",
                    "command -v ",
                    "nvidia-smi ",
                    "git status",
                )
            ):
                commands.append(line)

    gaps = []
    for command in commands:
        command = " ".join(command.split())
        if not any(command.startswith(prefix) for prefix in prefixes):
            gaps.append(command)

    require(
        any(command.startswith("npx ") for command in commands),
        "the extraction found no npx command, so it is not testing anything",
    )

    require(not gaps, f"named in the policy but not pre-approved: {gaps}")


@test("no call site pins a system browser")
def test_no_system_browser() -> None:
    forbidden = (
        "/snap/bin/chromium",
        "executablePath",
        'channel: "chrome"',
        "channel='chrome'",
    )
    offenders: list[str] = []
    for path in KIT_DIR.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix in {".pyc"}:
            continue
        # The global policy and this test name these in order to forbid them.
        if path.name == "CLAUDE.md" or "tests" in path.parts:
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(KIT_DIR)}: {needle}")
    require(not offenders, f"system browser references: {offenders}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    if not FAKE_CODEX.exists():
        print(f"missing test helper: {FAKE_CODEX}", file=sys.stderr)
        return 2

    selected = sys.argv[1:]
    failures = 0
    skipped = 0

    for name, function in TESTS:
        if selected and not any(token in name for token in selected):
            skipped += 1
            continue

        started = time.monotonic()
        try:
            function()
        except Failure as exc:
            failures += 1
            print(f"FAIL  {name}\n      {exc}")
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - report any test error
            # BaseException so that a SystemExit escaping a script under test
            # is reported rather than silently ending the run.
            failures += 1
            print(f"ERROR {name}\n      {type(exc).__name__}: {exc}")
        else:
            elapsed = time.monotonic() - started
            print(f"PASS  {name} ({elapsed:.1f}s)")

    total = len(TESTS) - skipped
    print()
    if failures:
        print(f"KIT_TESTS_FAILED: {failures} of {total} failed")
        return 1

    print(f"KIT_TESTS_PASSED: {total} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
