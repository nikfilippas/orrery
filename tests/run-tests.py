#!/usr/bin/env python3
"""Deterministic regression tests for the Claude-Codex orchestration kit.

Covers the settings updater's atomicity, locking and ownership rules, and the
direct review wrapper's systemd cleanup, signal handling and CODEX_HOME
normalisation. The tests never call a real model and never touch the live
Claude or Codex configuration.
"""

from __future__ import annotations

import contextlib
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
REVIEW_SCRIPT = KIT_DIR / "scripts" / "claude-codex-review"
INSTALL_SCRIPT = KIT_DIR / "scripts" / "install.sh"
DOCTOR_SCRIPT = KIT_DIR / "scripts" / "doctor.sh"
FAKE_CODEX = Path(__file__).resolve().parent / "fake-codex"

UNIT_GLOB = "claude-codex-review-*"

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
review_module = load_script(REVIEW_SCRIPT, "kit_claude_codex_review")


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
        and not entry.name.endswith(".claude-codex.lock")
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
    return Path(base) / "claude-codex-review"


def runtime_residue() -> list[str]:
    root = review_runtime_root()
    if not root.exists():
        return []
    return sorted(entry.name for entry in root.iterdir())


def review_environment(mode: str, marker: Path | None = None) -> dict[str, str]:
    bin_dir = Path(tempfile.mkdtemp(prefix="kit-fake-bin."))
    shutil.copy2(FAKE_CODEX, bin_dir / "codex")
    (bin_dir / "codex").chmod(0o755)

    environment = os.environ.copy()
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment.get('PATH', '')}"
    environment["CODEX_FAKE_MODE"] = mode
    environment["KIT_FAKE_BIN"] = str(bin_dir)
    if marker is not None:
        environment["CODEX_FAKE_MARKER"] = str(marker)
    return environment


def start_review(
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(REVIEW_SCRIPT), *arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def wait_for_unit(process: subprocess.Popen[str], timeout: float = 20.0) -> str:
    """Return the transient unit name once systemd has registered it."""
    deadline = time.monotonic() + timeout
    prefix = f"claude-codex-review-{process.pid}-"

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

    residue = [name for name in runtime_residue() if name.startswith("run.")]
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
                    "allow": ["Bash(codex:*)", "Bash(claude-codex-review:*)"]
                }
            },
        )
        write_json(
            target,
            {
                "permissions": {
                    "allow": ["Bash(npm test:*)", "Bash(codex:*)"],
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
            "Bash(claude-codex-review:*)" in permissions["allow"],
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
            if entry.name.startswith("settings.json.backup-claude-codex-")
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
            if entry.name.startswith("settings.json.backup-claude-codex-")
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
            if entry.name.startswith("settings.json.backup-claude-codex-")
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
            if entry.name.startswith("settings.json.backup-claude-codex-")
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
            if entry.name.startswith("settings.json.backup-claude-codex-")
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
                if ".backup-claude-codex-" in entry.name
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
            'sed -n "/^if ! CODEX_HOME=/,/^fi$/p" "$1" > /tmp/.kit-probe-$$ ; '
            'set -euo pipefail; source /tmp/.kit-probe-$$; '
            'rm -f /tmp/.kit-probe-$$; printf "%s" "$CODEX_HOME"',
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

    unit_base = f"claude-codex-review-sweep-test-{os.getpid()}"
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
        sys.argv = ["claude-codex-review", "--timeout", "60", "--", "prompt"]

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
        stop_stray_units(f"claude-codex-review-{os.getpid()}-")
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
    environment = os.environ.copy()
    environment["CODEX_HOME"] = ""

    result = subprocess.run(
        [sys.executable, str(REVIEW_SCRIPT), "--", "prompt"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
        check=False,
    )
    require(
        result.returncode == 2,
        f"expected status 2, got {result.returncode}: {result.stderr}",
    )
    require(
        "CODEX_HOME cannot be empty" in result.stderr,
        f"unexpected diagnostic: {result.stderr}",
    )

    for script in (INSTALL_SCRIPT, DOCTOR_SCRIPT):
        shell = subprocess.run(
            ["bash", str(script)],
            env=environment,
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


@test("a missing Codex profile is refused rather than silently substituted")
def test_missing_profile_refused() -> None:
    """Codex exits 0 on an unknown --profile, using its default model."""
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)

        environment = review_environment("success")
        environment["CODEX_HOME"] = str(home)

        result = subprocess.run(
            [sys.executable, str(REVIEW_SCRIPT), "--timeout", "60", "--", "prompt"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

        require(
            result.returncode == 2,
            f"a missing profile was accepted (status {result.returncode})",
        )
        require(
            "not readable" in result.stderr,
            f"unexpected diagnostic: {result.stderr.strip()!r}",
        )

        # A profile that exists but sets no model is equally unusable.
        (home / "sol.config.toml").write_text('model_reasoning_effort = "high"\n')
        result = subprocess.run(
            [sys.executable, str(REVIEW_SCRIPT), "--timeout", "60", "--", "prompt"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )
        require(
            result.returncode == 2 and "does not set a model" in result.stderr,
            f"a model-less profile was accepted: {result.stderr.strip()!r}",
        )


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
        stdout, stderr = process.communicate(timeout=120)
        shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

        require(
            process.returncode == 0,
            f"wrapper failed ({process.returncode}): {stderr}",
        )
        require("# PASS" in stdout, f"verdict not printed: {stdout!r}")
        require(output.exists(), "verdict was not published")
        require("# PASS" in output.read_text(), "published verdict is wrong")
        require(
            "Codex Sol" in stderr and "control resumed" in stderr,
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
        assert_no_review_residue(f"claude-codex-review-{process.pid}-")


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

            assert_no_review_residue(f"claude-codex-review-{process.pid}-")


@test("a failing Codex run is reported and leaves no residue")
def test_failing_run() -> None:
    environment = review_environment("fail")
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    _, stderr = process.communicate(timeout=120)
    shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

    require(process.returncode == 7, f"expected status 7: {process.returncode}")
    require("exit status 7" in stderr, f"failure not reported: {stderr!r}")
    assert_no_review_residue(f"claude-codex-review-{process.pid}-")


@test("a successful run with no verdict is reported and leaves no residue")
def test_empty_verdict() -> None:
    environment = review_environment("empty")
    process = start_review(environment, "--timeout", "60", "--", "prompt")
    _, stderr = process.communicate(timeout=120)
    shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

    require(process.returncode == 1, f"expected status 1: {process.returncode}")
    require("no final verdict" in stderr, f"not reported: {stderr!r}")
    assert_no_review_residue(f"claude-codex-review-{process.pid}-")


@test("a timeout stops the control group and leaves no residue")
def test_timeout() -> None:
    environment = review_environment("sleep")
    process = start_review(environment, "--timeout", "5", "--", "prompt")
    _, stderr = process.communicate(timeout=180)
    shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

    require(
        process.returncode == 124,
        f"expected status 124, got {process.returncode}: {stderr}",
    )
    assert_no_review_residue(f"claude-codex-review-{process.pid}-")


@test("detached SIGTERM-resistant descendants are killed with the cgroup")
def test_detached_descendant() -> None:
    environment = review_environment("detach")
    process = start_review(environment, "--timeout", "5", "--", "prompt")
    unit = wait_for_unit(process)
    process.communicate(timeout=180)
    shutil.rmtree(environment["KIT_FAKE_BIN"], ignore_errors=True)

    assert_no_review_residue(f"claude-codex-review-{process.pid}-")

    survivors = subprocess.run(
        ["pgrep", "-af", "signal.SIG_IGN"],
        stdout=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    ).stdout
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
        assert_no_review_residue(f"claude-codex-review-{process.pid}-")


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

        assert_no_review_residue(f"claude-codex-review-{process.pid}-")


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
        stop_stray_units(f"claude-codex-review-{process.pid}-")
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
        assert_no_review_residue(f"claude-codex-review-{process.pid}-")


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


@test("the installer links every command the doctor checks")
def test_installer_covers_doctor_commands() -> None:
    installer = INSTALL_SCRIPT.read_text()
    for command in (
        "claude-codex-init",
        "claude-codex-doctor",
        "claude-codex-review",
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
        for profile in ("luna", "terra", "sol"):
            path = kit / "global" / "codex" / f"{profile}.config.toml"
            require(
                path.is_file() and not path.is_symlink(),
                f"the canonical {profile} profile was destroyed",
            )
            require(
                "model" in path.read_text(),
                f"the canonical {profile} profile was emptied",
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
    for claim in ("scripts/claude-codex-review", "tests/run-tests.py"):
        require((KIT_DIR / claim).exists(), f"the guide names a missing {claim}")


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

    policy = (KIT_DIR / "global" / "CLAUDE.md").read_text()

    commands = re.findall(r"`((?:npx|codex|claude-codex-review)[^`]*)`", policy)

    # Fenced blocks too. The screenshot command lives in one, and shell line
    # continuations have to be folded before it can be matched.
    for block in re.findall(r"```(?:bash|sh)?\n(.*?)```", policy, re.S):
        for line in re.sub(r"\\\n\s*", " ", block).splitlines():
            line = line.strip()
            if line.startswith(("npx ", "codex ", "claude-codex-review ")):
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
