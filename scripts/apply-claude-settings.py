#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import ctypes
import ctypes.util
import errno
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


PLUGIN = "codex@openai-codex"
CLAUDE_EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
CLAUDE_PERSISTED_EFFORTS = {"low", "medium", "high", "xhigh"}
CLAUDE_ENV_EFFORTS = CLAUDE_PERSISTED_EFFORTS | {"max"}

# How many times the update is recomputed on top of a version installed by a
# writer that does not take the sidecar lock.
ATTEMPTS = 5

AT_FDCWD = -100
RENAME_EXCHANGE = 1 << 1


def load_renameat2() -> Any:
    try:
        library = ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.so.6",
            use_errno=True,
        )
        entry = library.renameat2
    except (AttributeError, OSError):
        return None

    entry.restype = ctypes.c_int
    entry.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    return entry


# Darwin's atomic swap. RENAME_SWAP per <sys/stdio.h>; the semantics match
# RENAME_EXCHANGE. This path follows Apple's manpage and is exercised only
# where renameat2 is absent; on Linux it is never reached.
DARWIN_RENAME_SWAP = 0x00000002


def load_renamex_np() -> Any:
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL(
            ctypes.util.find_library("c") or "libc.dylib",
            use_errno=True,
        )
        entry = library.renamex_np
    except (AttributeError, OSError):
        return None

    entry.restype = ctypes.c_int
    entry.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    return entry


RENAMEAT2 = load_renameat2()
RENAMEX_NP = load_renamex_np()


UNSUPPORTED_EXCHANGE = (
    errno.ENOSYS,
    errno.EINVAL,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
)


class AtomicExchangeUnavailable(RuntimeError):
    pass


def exchange_paths(first: Path, second: Path) -> None:
    """Atomically swap two directory entries.

    There is no weaker fallback. Without an atomic exchange the update
    would have to check the target and then replace it in a separate
    syscall, which silently discards anything a writer outside the sidecar
    lock installs in between, so an update is refused instead. Linux
    provides renameat2 with RENAME_EXCHANGE; Darwin provides renamex_np
    with RENAME_SWAP.
    """
    if RENAMEAT2 is not None:
        status = RENAMEAT2(
            AT_FDCWD,
            os.fsencode(str(first)),
            AT_FDCWD,
            os.fsencode(str(second)),
            RENAME_EXCHANGE,
        )
    elif RENAMEX_NP is not None:
        status = RENAMEX_NP(
            os.fsencode(str(first)),
            os.fsencode(str(second)),
            DARWIN_RENAME_SWAP,
        )
    else:
        raise AtomicExchangeUnavailable(
            "no atomic exchange syscall is available in libc"
        )

    if status == 0:
        return

    code = ctypes.get_errno()
    if code in UNSUPPORTED_EXCHANGE:
        raise AtomicExchangeUnavailable(
            f"atomic exchange is unsupported here: {os.strerror(code)}"
        )

    raise OSError(code, os.strerror(code), str(first), None, str(second))


def parse_args() -> argparse.Namespace:
    script = Path(__file__).resolve()
    kit = script.parent.parent

    parser = argparse.ArgumentParser(
        description=(
            "Apply selected canonical agent settings in one atomic update "
            "while preserving unrelated live settings."
        )
    )
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--effort", action="store_true")
    parser.add_argument("--companion", action="store_true")
    parser.add_argument("--hooks", action="store_true")
    parser.add_argument("--permissions", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--source",
        type=Path,
        default=kit / "global" / "claude-settings.json",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
    )

    args = parser.parse_args()

    if args.all:
        args.model = True
        args.effort = True
        args.companion = True
        args.hooks = True
        args.permissions = True

    if not (
        args.model
        or args.effort
        or args.companion
        or args.hooks
        or args.permissions
    ):
        parser.error(
            "select --model, --effort, --companion, --hooks, "
            "--permissions, or --all"
        )

    return args


def load_object(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise SystemExit(f"Settings file does not exist: {path}")

    try:
        value = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in settings file {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise SystemExit(f"Settings root must be a JSON object: {path}")

    return value


def actual_target(path: Path) -> Path:
    if path.is_symlink():
        return path.resolve(strict=False)
    return path


def validate_hook_groups(
    hooks: dict[str, Any],
    *,
    source_name: str,
) -> None:
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            raise SystemExit(
                f"Invalid hooks structure in {source_name}: event groups "
                "must be lists."
            )

        for group in groups:
            if not isinstance(group, dict):
                raise SystemExit(
                    f"Invalid hooks structure in {source_name}: groups "
                    "must be objects."
                )

            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                raise SystemExit(
                    f"Invalid hooks structure in {source_name}: group hooks "
                    "must be lists."
                )


def canonical_hook_commands(
    canonical_hooks: dict[str, Any],
) -> set[str]:
    commands: set[str] = set()

    for groups in canonical_hooks.values():
        for group in groups:
            for handler in group.get("hooks", []):
                if not isinstance(handler, dict):
                    continue

                command = handler.get("command")
                if isinstance(command, str):
                    commands.add(command)

    return commands


def merge_hooks(
    current: dict[str, Any],
    canonical: dict[str, Any],
) -> None:
    existing_hooks = current.get("hooks", {})
    canonical_hooks = canonical.get("hooks", {})

    if not isinstance(existing_hooks, dict):
        raise SystemExit("Live hooks setting must be a JSON object.")
    if not isinstance(canonical_hooks, dict):
        raise SystemExit("Canonical hooks setting must be a JSON object.")

    validate_hook_groups(existing_hooks, source_name="live settings")
    validate_hook_groups(canonical_hooks, source_name="canonical settings")

    owned_commands = canonical_hook_commands(canonical_hooks)
    cleaned: dict[str, list[dict[str, Any]]] = {}

    for event, groups in existing_hooks.items():
        cleaned_groups: list[dict[str, Any]] = []

        for group in groups:
            original_handlers = group.get("hooks", [])
            kept_handlers = [
                handler
                for handler in original_handlers
                if not (
                    isinstance(handler, dict)
                    and handler.get("command") in owned_commands
                )
            ]

            removed_owned_handler = (
                len(kept_handlers) != len(original_handlers)
            )

            if removed_owned_handler and not kept_handlers:
                # Drop a group that consisted only of toolkit-owned handlers.
                # Preserve genuinely unrelated groups, including empty groups.
                continue

            updated_group = copy.deepcopy(group)
            updated_group["hooks"] = kept_handlers
            cleaned_groups.append(updated_group)

        cleaned[event] = cleaned_groups

    for event, groups in canonical_hooks.items():
        cleaned.setdefault(event, []).extend(copy.deepcopy(groups))

    current["hooks"] = cleaned


def merge_permissions(
    current: dict[str, Any],
    canonical: dict[str, Any],
) -> None:
    """Install toolkit permission rules without touching user-owned rules.

    Delegation is unusable without these: Claude Code otherwise stops to ask
    before every `codex` call, which blocks any non-interactive run. Rules
    Canonical rules are added, and the two exact rules owned by the retired
    command namespace are removed during migration. Nothing else is lost;
    `deny`, `ask` and the default mode are left alone.
    """
    canonical_permissions = canonical.get("permissions", {})
    if not isinstance(canonical_permissions, dict):
        raise SystemExit("Canonical permissions setting must be a JSON object.")

    canonical_allow = canonical_permissions.get("allow", [])
    if not isinstance(canonical_allow, list):
        raise SystemExit("Canonical permissions.allow must be a JSON array.")

    permissions = current.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SystemExit("Live permissions setting must be a JSON object.")

    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        raise SystemExit("Live permissions.allow must be a JSON array.")

    # Releases before the Órrery rename installed two command rules under
    # the former namespace. Remove only those exact toolkit-owned entries;
    # constructing the legacy spelling keeps the retired name out of the
    # current source and generated documentation.
    legacy_prefix = "claude" + "-codex"
    retired_rules = {
        f"Bash({legacy_prefix}-review:*)",
        f"Bash({legacy_prefix}-doctor:*)",
    }
    allow[:] = [rule for rule in allow if rule not in retired_rules]

    for rule in canonical_allow:
        if rule not in allow:
            allow.append(rule)


def merge_effort(
    current: dict[str, Any],
    canonical: dict[str, Any],
) -> None:
    """Install Claude's effective thinking level without losing other env.

    Claude persists low through xhigh in ``effortLevel``. ``max`` is
    session-only there, but is valid through CLAUDE_CODE_EFFORT_LEVEL, so
    the canonical file uses that owned environment key for max. Exactly one
    representation may be present to avoid a hidden precedence conflict.
    """
    persisted = canonical.get("effortLevel")
    canonical_env = canonical.get("env", {})
    if not isinstance(canonical_env, dict):
        raise SystemExit("Canonical env setting must be a JSON object.")
    environment = canonical_env.get(CLAUDE_EFFORT_ENV)

    if persisted is not None and persisted not in CLAUDE_PERSISTED_EFFORTS:
        raise SystemExit(
            "Canonical effortLevel must be low, medium, high, or xhigh."
        )
    if environment is not None and environment not in CLAUDE_ENV_EFFORTS:
        raise SystemExit(
            f"Canonical {CLAUDE_EFFORT_ENV} must be low, medium, high, "
            "xhigh, or max."
        )
    if persisted is not None and environment is not None:
        raise SystemExit(
            "Canonical Claude thinking must use either effortLevel or "
            f"{CLAUDE_EFFORT_ENV}, not both."
        )

    current.pop("effortLevel", None)
    if persisted is not None:
        current["effortLevel"] = persisted

    live_env = current.get("env", {})
    if not isinstance(live_env, dict):
        raise SystemExit("Live env setting must be a JSON object.")
    live_env = copy.deepcopy(live_env)
    live_env.pop(CLAUDE_EFFORT_ENV, None)
    if environment is not None:
        live_env[CLAUDE_EFFORT_ENV] = environment
    if live_env:
        current["env"] = live_env
    else:
        current.pop("env", None)


def sync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def render(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2) + "\n").encode()


def sibling_name(
    target: Path,
    suffix: str,
    *,
    prefix: str = "",
    reserve: int = 0,
) -> str:
    """A name derived from the target that fits in a directory entry.

    Every file this updater creates beside the settings lives or dies by
    this: a name over NAME_MAX fails the syscall, and for the backup that
    would mean a live update with no preserved previous version.

    NAME_MAX bounds bytes, not characters, so the budget is measured on the
    encoded form. Whole characters are dropped rather than encoded bytes,
    because cutting through a multi-byte sequence leaves a lone surrogate
    that later fails to print. `reserve` covers bytes a caller appends
    afterwards, such as the random suffix `tempfile` adds.
    """
    try:
        limit = int(os.pathconf(target.parent, "PC_NAME_MAX"))
    except (OSError, ValueError, AttributeError):
        limit = 255

    fixed = len(os.fsencode(prefix)) + len(os.fsencode(suffix)) + reserve
    available = max(1, limit - fixed)

    stem = target.name
    while len(stem) > 1 and len(os.fsencode(stem)) > available:
        stem = stem[:-1]

    return f"{prefix}{stem}{suffix}"


def write_temporary(target: Path, payload: bytes, mode: int) -> Path:
    """Put the update in a durable same-directory temporary file."""
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=target.parent,
        # tempfile appends eight random characters to the prefix.
        prefix=sibling_name(target, ".", prefix=".", reserve=8),
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())

    temporary.chmod(mode)
    return temporary


def backup_path(target: Path) -> Path:
    return target.with_name(
        sibling_name(
            target,
            f".backup-orrery-{time.time_ns()}-{os.getpid()}",
        )
    )


def lock_path(target: Path) -> Path:
    return target.with_name(
        sibling_name(target, ".orrery.lock", prefix=".")
    )


def swap_into_place(temporary: Path, target: Path) -> os.stat_result:
    """Atomically swap `temporary` into `target` and describe what moved out.

    This is the compare-and-swap primitive: on return `temporary` names
    whichever entry `target` held at the instant of the swap, so the caller
    can tell from that entry alone whether the update landed on the version
    it read. Nothing is rolled back, because a rollback swap is itself racy:
    a third writer landing during it would be overwritten by a version that
    is already stale.

    The caller must preserve the displaced entry and must never unlink it.
    `lstat` is used so that a displaced symlink, including a dangling one,
    is described rather than raising.
    """
    exchange_paths(temporary, target)
    return os.lstat(temporary)


def install(
    target: Path,
    payload: bytes,
    mode: int,
    identity: tuple[int, int],
    expected_bytes: bytes,
) -> tuple[bool, bytes, os.stat_result, tuple[int, int]]:
    """Make one compare-and-swap attempt at installing `payload`.

    Returns whether the swap landed on the version the caller expected, the
    displaced content, the displaced entry's metadata, and the identity of
    the file this attempt installed. The displaced entry is always preserved
    as a backup before anything else can fail, so no content is discarded
    whatever happens afterwards.
    """
    temporary = write_temporary(target, payload, mode)

    try:
        installed = os.stat(temporary)
        displaced = swap_into_place(temporary, target)
    except AtomicExchangeUnavailable as exc:
        temporary.unlink(missing_ok=True)
        raise SystemExit(
            f"Cannot update agent settings safely: {exc}."
        ) from exc
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    # The swap has happened. From here `temporary` names the displaced
    # entry, which may belong to another writer, so it is renamed to a
    # backup before anything that can fail. Nothing below may unlink it.
    backup = backup_path(target)
    try:
        os.replace(temporary, backup)
    except OSError as exc:
        raise SystemExit(
            "The settings update was applied but the previous version could "
            f"not be renamed to a backup: {exc}. It is at:\n{temporary}"
        ) from exc

    # Flushed only once both the swap and the preservation are in place, so
    # a failure here can never mean a live update with no backup. Nothing
    # fallible, including reporting, comes between them.
    try:
        sync_directory(target.parent)
    except OSError as exc:
        raise SystemExit(
            "The settings update was applied but could not be flushed to "
            f"disk: {exc}. The previous version is at:\n{backup}"
        ) from exc

    print(f"Backed up agent settings to:\n{backup}")

    if not stat.S_ISREG(displaced.st_mode):
        raise SystemExit(
            "The agent settings path was replaced by something other than "
            f"a regular file. It was preserved at:\n{backup}"
        )

    try:
        displaced_bytes = backup.read_bytes()
    except OSError as exc:
        raise SystemExit(
            "Could not read the settings version this update displaced: "
            f"{exc}. It was preserved at:\n{backup}"
        ) from exc

    accepted = (
        (displaced.st_dev, displaced.st_ino) == identity
        and displaced_bytes == expected_bytes
    )
    return (
        accepted,
        displaced_bytes,
        displaced,
        (installed.st_dev, installed.st_ino),
    )


def object_from_bytes(
    data: bytes,
    *,
    path: Path,
    missing_ok: bool,
) -> dict[str, Any]:
    if not data:
        if missing_ok:
            return {}
        raise SystemExit(f"Settings file is empty: {path}")

    try:
        value = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid JSON in settings file {path}: {exc}") from exc

    if not isinstance(value, dict):
        raise SystemExit(f"Settings root must be a JSON object: {path}")

    return value


def current_snapshot(
    path: Path,
) -> tuple[bool, bytes, int, tuple[int, int] | None]:
    """Read the live settings and the identity of the exact inode read.

    The content and the identity come from one descriptor so that they can
    never describe two different files.
    """
    try:
        # O_NONBLOCK so that a FIFO left at the settings path is rejected
        # below instead of blocking forever waiting for a writer.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False, b"", 0o600, None

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(
                f"Agent settings path is not a regular file: {path}"
            )

        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)

    return (
        True,
        b"".join(chunks),
        info.st_mode & 0o777,
        (info.st_dev, info.st_ino),
    )


def apply_selected_settings(
    current: dict[str, Any],
    canonical: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    updated = copy.deepcopy(current)

    if args.model:
        model = canonical.get("model")
        if not isinstance(model, str) or not model.strip():
            raise SystemExit(
                "Canonical model must be a non-empty JSON string."
            )
        updated["model"] = model

    if args.effort:
        merge_effort(updated, canonical)

    if args.companion:
        companion = canonical.get("enabledPlugins", {}).get(PLUGIN)
        if companion is not False or not isinstance(companion, bool):
            raise SystemExit(
                f"Canonical {PLUGIN} setting must be the JSON Boolean false."
            )

        enabled_plugins = updated.setdefault("enabledPlugins", {})
        if not isinstance(enabled_plugins, dict):
            raise SystemExit(
                "Live enabledPlugins setting must be a JSON object."
            )
        enabled_plugins[PLUGIN] = False

    if args.hooks:
        merge_hooks(updated, canonical)

    if args.permissions:
        merge_permissions(updated, canonical)

    return updated


def main() -> int:
    # Settings paths can carry bytes that are not valid in the locale
    # encoding. Reporting one must never crash in place of doing the work.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, OSError, ValueError):
            pass

    args = parse_args()
    canonical = load_object(args.source, missing_ok=False)
    target_path = actual_target(args.target)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    lock = lock_path(target_path)

    with lock.open("a+b") as lock_handle:
        lock.chmod(0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)

        existed, base_bytes, mode, identity = current_snapshot(target_path)

        # What the update is computed from and what the target is expected
        # to hold start out identical, but diverge after a losing swap: the
        # base becomes the version a foreign writer installed, while the
        # target holds this process's own previous attempt.
        expected_bytes = base_bytes
        base_is_live = True

        for _attempt in range(ATTEMPTS):
            current = object_from_bytes(
                base_bytes,
                path=target_path,
                missing_ok=not existed,
            )
            updated = apply_selected_settings(current, canonical, args)

            if updated == current and base_is_live:
                print("Selected agent settings are already installed.")
                return 0

            payload = render(updated)

            if not existed:
                # Create only if the target is still absent. A failure means
                # somebody created it first, so adopt their file as the base
                # for the next attempt.
                temporary = write_temporary(target_path, payload, mode)
                try:
                    os.link(temporary, target_path)
                except FileExistsError:
                    temporary.unlink(missing_ok=True)
                    existed, base_bytes, mode, identity = current_snapshot(
                        target_path
                    )
                    expected_bytes = base_bytes
                    base_is_live = True
                    continue
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise

                temporary.unlink(missing_ok=True)
                sync_directory(target_path.parent)
                print("Applied selected agent settings atomically.")
                return 0

            try:
                accepted, displaced_bytes, displaced, installed = install(
                    target_path,
                    payload,
                    mode,
                    identity,
                    expected_bytes,
                )
            except FileNotFoundError:
                # `exchange_paths` refused because the target no longer
                # exists. Nothing was swapped.
                existed, base_bytes, mode, identity = current_snapshot(
                    target_path
                )
                expected_bytes = base_bytes
                base_is_live = True
                continue

            if accepted:
                print("Applied selected agent settings atomically.")
                return 0

            # A writer outside the sidecar lock installed the displaced
            # file, either by replacing it or by rewriting it in place. Fold
            # its content into the next attempt rather than discarding it,
            # and expect the file this attempt installed.
            base_bytes = displaced_bytes
            expected_bytes = payload
            mode = displaced.st_mode & 0o777
            identity = installed
            existed = True
            base_is_live = False

        # Every attempt collided. Put the version displaced by the last one
        # back, so the settings end as they were found instead of holding a
        # merge that omits it, and only then report that nothing was applied.
        #
        # The restore is a compare-and-swap like any other, so it can lose
        # to a writer that lands during it. Losing means an even newer
        # version was displaced, which is then what should be live, so the
        # restore follows the same convergence as the main loop rather than
        # leaving an older version installed.
        payload = base_bytes
        restored = False

        for _restore_attempt in range(ATTEMPTS):
            try:
                restored, displaced_bytes, displaced, installed = install(
                    target_path,
                    payload,
                    mode,
                    identity,
                    expected_bytes,
                )
            except FileNotFoundError:
                restored = False
                break

            if restored:
                break

            # Order matters: the target now holds the payload this attempt
            # installed, and the next attempt installs what was displaced.
            expected_bytes = payload
            payload = displaced_bytes
            mode = displaced.st_mode & 0o777
            identity = installed

        if restored:
            raise SystemExit(
                f"Agent settings changed during each of {ATTEMPTS} "
                "attempts. No update was applied and the settings were left "
                "as they were found."
            )

    # Every bounded compare-and-swap has this end: if a writer lands in the
    # window on every attempt, the loop stops with the version it last
    # installed live and a newer one only in a backup. Under sustained
    # contention no bounded algorithm avoids that, so the guarantee this
    # code does make is the one that matters: no version is ever destroyed.
    # Each is preserved as a backup, and the newest is the most recent of
    # them, so the state is always recoverable by hand.
    raise SystemExit(
        f"Agent settings changed during each of {ATTEMPTS} attempts and "
        f"again during {ATTEMPTS} attempts to restore them, so another "
        "writer is updating them continuously. No update was applied. Every "
        "version that was displaced is preserved as a "
        f"{target_path.name}.backup-orrery-* file, newest last by "
        "modification time, and one of those is more recent than what is "
        "currently live. Rerun once the other writer has stopped."
    )


if __name__ == "__main__":
    raise SystemExit(main())
