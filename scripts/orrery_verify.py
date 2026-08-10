"""Running one repository command under the containment ladder.

Shared because two callers need exactly the same behaviour: a contract's
acceptance criteria, and the command that checks a memory fact. A second
implementation of this would be a second answer to how far a command may
reach, which is not a question that should have two answers.
"""

from __future__ import annotations

import contextlib
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


# Distinct from any exit status a command could plausibly return, so
# "the host could not contain this" is never read as "the check failed".
UNCONTAINED_STATUS = 125


def verification_timeout() -> float:
    """The per-command verification budget, overridable for tests."""
    try:
        value = float(os.environ.get("ORRERY_VERIFY_TIMEOUT_SECONDS", "600"))
    except ValueError:
        return 600.0
    return value if value > 0 else 600.0


def listed(path: str) -> str:
    """One entry in a systemd path-list property, safely.

    These properties take a space-separated list, so a path containing a
    space would be read as two entries. Quoting alone is not enough: a
    path holding a quote of its own closes the string early and the rest
    is parsed as further entries, so a delegate able to make a workdir
    resolve through such a name could widen its own grant. Escaping
    matches what the delegate runner already does.
    """
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def writable_paths(workdir: Path) -> list[str]:
    """Where a verification command may write.

    The worktree it was pointed at, and the temporary directories a
    build or a test suite reaches for by habit. Anything else is the
    operator's to grant: `ORRERY_VERIFY_WRITABLE` takes a colon-
    separated list, which is how a suite that writes to a shared cache
    keeps working without every command regaining the whole home
    directory.
    """
    granted = [str(workdir.resolve()), "/tmp", "/var/tmp"]
    extra = os.environ.get("ORRERY_VERIFY_WRITABLE", "")
    granted.extend(
        str(Path(entry).expanduser()) for entry in extra.split(":") if entry.strip()
    )
    return granted


# Granted to every contained run because the provider CLIs build their
# sandbox mount points there. A repository living under one of them has
# its control store inside a write grant, so a delegate can forge the
# ledger, the evidence packets and the memory facts that later runs are
# handed. Everywhere else the kernel refuses that write.
BROAD_GRANTS = ("/tmp", "/var/tmp")


def under_broad_grant(path: Path) -> str | None:
    """The granted directory a path sits beneath, if any."""
    resolved = Path(os.path.realpath(path))
    for grant in BROAD_GRANTS:
        base = Path(grant)
        if resolved == base or base in resolved.parents:
            return grant
    return None


def session_control_paths() -> list[str]:
    """The sockets through which contained code could ask for more."""
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    return [
        str(path) for path in (runtime / "systemd", runtime / "bus") if path.exists()
    ]


def protected_paths(workdir: Path) -> list[str]:
    """What stays read-only inside the granted worktree.

    Granting the worktree is not enough on its own: a memory fact's
    command defaults to running at the repository root, so the store the
    whole design protects sat inside the writable grant, along with the
    git directory whose hooks are executed later. A deeper read-only
    mapping beats a grant containing it, which is how these are carved
    back out.

    Every worktree's control store, not only this one's. A linked
    worktree holds no `.orrery` of its own, so the store that matters
    belongs to the primary checkout and lives somewhere else entirely;
    stopping at the worktree's own `.git` file left it writable whenever
    that checkout sat under a granted path.
    """
    protected: list[Path] = []

    def plumbing(*arguments: str) -> str:
        # Tolerant of git being absent or failing: this runs on the way
        # into every verification, and a missing plumbing tool must
        # narrow what is protected, never abort the run.
        try:
            return subprocess.run(
                ["git", "-C", str(workdir), *arguments],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=60, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    common = plumbing("rev-parse", "--git-common-dir")
    if common:
        protected.append(workdir / common)
    for line in plumbing("worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            entry = Path(line[len("worktree "):])
            protected.append(entry / ".orrery")
            protected.append(entry / ".git")
    # Outside a repository, or with git unavailable, fall back to what is
    # visible from the workdir upwards.
    if not protected:
        for candidate in (workdir, *workdir.parents):
            for name in (".orrery", ".git"):
                protected.append(candidate / name)

    resolved: list[str] = []
    for path in protected:
        try:
            if path.exists():
                resolved.append(str(path.resolve()))
        except OSError:
            continue
    return sorted(set(resolved))


_CONTAINMENT_ENFORCED: bool | None = None
# Absolute, and from the system's own directories. Resolving these
# through PATH would let anything earlier on it answer the probe.
_SYSTEMD_RUN = "/usr/bin/systemd-run"
_TRUSTED_PREFIXES = ("/usr/bin/", "/bin/", "/usr/sbin/", "/sbin/")


def _trusted_tool(fixed: str, name: str, override: str = "") -> str | None:
    """An absolute, canonical path to a system tool, or nothing.

    Canonical because a lexical prefix check accepts
    /usr/bin/../../tmp/attacker/systemd-run, which is neither in /usr/bin
    nor trustworthy. The realpath is what the kernel will execute, so it
    is what gets checked.
    """
    # An explicit override is honoured before anything else, and only
    # from the runner's own environment. That is a different trust level
    # from PATH: the runner's environment is the operator's, while PATH
    # can carry a relative component and the launch runs with the
    # delegate's workspace as its working directory, so a delegate could
    # plant a `systemd-run` of its own and have the next attempt start
    # outside containment.
    # An explicit override is authoritative, including when it names
    # nothing: a caller that has deliberately pointed this at an absent
    # tool is saying the tool is unavailable, and falling back to the
    # real one would silently overrule them.
    chosen = os.environ.get(override, "") if override else ""
    if chosen:
        resolved = os.path.realpath(chosen)
        return resolved if os.path.isfile(resolved) else None
    for candidate in (fixed, shutil.which(name) or ""):
        if not candidate:
            continue
        resolved = os.path.realpath(candidate)
        if resolved.startswith(_TRUSTED_PREFIXES) and os.path.isfile(resolved):
            return resolved
    return None


def systemd_run_path() -> str | None:
    return _trusted_tool(_SYSTEMD_RUN, "systemd-run", "ORRERY_SYSTEMD_RUN")


def systemctl_path() -> str | None:
    return _trusted_tool("/usr/bin/systemctl", "systemctl", "ORRERY_SYSTEMCTL")


def containment_enforced() -> bool:
    """Whether this host actually enforces the sandbox, measured not assumed.

    A runner that restricts unprivileged user namespaces accepts the
    unit and silently drops its protections, so the presence of
    systemd-run proves nothing.

    A non-zero exit proves nothing either: a unit that failed to start,
    for any reason at all, exits non-zero without restricting anything.
    So the probe asserts both directions in one run, under the same
    property composition the real thing uses. The permitted write must
    succeed and the forbidden write must fail; either result alone is
    consistent with no containment whatsoever.
    """
    global _CONTAINMENT_ENFORCED
    if _CONTAINMENT_ENFORCED is not None:
        return _CONTAINMENT_ENFORCED
    executable = systemd_run_path()
    if executable is None:
        _CONTAINMENT_ENFORCED = False
        return False
    probe = Path(tempfile.mkdtemp(prefix="orrery-verify-probe.", dir=Path.home()))
    allowed = Path(tempfile.mkdtemp(prefix="orrery-verify-grant."))
    try:
        # The composition the real run uses, not a reduced one, so a host
        # that honours ProtectHome but drops explicit mappings cannot
        # pass here and fail there. Paths are quoted for the same reason
        # the real unit quotes them: one containing a space would make
        # systemd refuse the unit, and a refused unit exits non-zero
        # without restricting anything.
        result = subprocess.run(
            [
                executable, "--user", "--wait", "--collect", "--pipe",
                f"--unit=orrery-probe-{os.getpid()}-{random.randrange(1 << 30):x}",
                "--property=ProtectSystem=strict",
                "--property=ProtectHome=read-only",
                "--property=NoNewPrivileges=yes",
                *(
                    f"--property=InaccessiblePaths={listed(path)}"
                    for path in session_control_paths()
                ),
                "--property=Environment=XDG_RUNTIME_DIR=",
                "--property=Environment=DBUS_SESSION_BUS_ADDRESS=",
                f"--property=ReadWritePaths={listed(str(allowed))}",
                "--property=RuntimeMaxSec=30",
                "/bin/sh", "-c",
                'permitted="$1"; forbidden="$2"; '
                'touch "$permitted" || exit 1; '
                'if touch "$forbidden" 2>/dev/null; then exit 1; fi; exit 0',
                "probe", str(allowed / "written"), str(probe / "written"),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=60, check=False,
        )
        # All three, because any one alone is consistent with no
        # containment at all: a unit that never started exits non-zero
        # and creates neither file.
        _CONTAINMENT_ENFORCED = (
            result.returncode == 0
            and (allowed / "written").exists()
            and not (probe / "written").exists()
        )
    except (OSError, subprocess.SubprocessError):
        _CONTAINMENT_ENFORCED = False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
        shutil.rmtree(allowed, ignore_errors=True)
    return _CONTAINMENT_ENFORCED


def containment_diagnosis(output: str, workdir: Path) -> str:
    """Turn a permission failure into an instruction, not a mystery.

    A suite that writes outside the worktree fails with a bare EROFS or
    EACCES, which reads as a broken test rather than as a boundary doing
    its job. Naming the boundary and how to widen it is the difference
    between a five-minute fix and an afternoon.
    """
    signatures = ("Read-only file system", "Permission denied", "errno 30", "EROFS")
    if not any(signature in output for signature in signatures):
        return ""
    return (
        "\n[orrery] This command may write only to "
        + ", ".join(writable_paths(workdir))
        + ". A verification command runs contained because a repository's "
        "own code decides what it does. Grant more with "
        "ORRERY_VERIFY_WRITABLE=/path/one:/path/two, or point the command "
        "at a path inside the worktree.\n"
    )


def run_verification(command: str, workdir: Path) -> tuple[int, str]:
    """Run one contract verification command under the containment ladder.

    Contained rather than merely time-bounded. The command is chosen by
    an operator but its behaviour is decided by whatever code the
    repository holds, which a delegate may have just written, so a
    verification run is exactly as trusted as a delegated run and gets
    the same posture: the home directory readable and not writable, the
    system read-only, and writes confined to the worktree.
    """
    budget = verification_timeout()
    # Measured, not assumed. A host that accepts the unit and drops its
    # protections would otherwise look identical to one that enforces
    # them, and every fact verified there would be evidence produced
    # with no boundary at all.
    use_systemd = containment_enforced()
    if use_systemd:
        unit = f"orrery-verify-{os.getpid()}-{random.randrange(1 << 30):x}"
        wrapped = [
            systemd_run_path() or "systemd-run",
            "--user",
            "--wait",
            "--collect",
            "--pipe",
            f"--unit={unit}",
            f"--property=RuntimeMaxSec={int(budget) + 60}",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=read-only",
            "--property=NoNewPrivileges=yes",
            # Without these every mapping below is decoration: contained
            # code can ask the user manager for a sibling unit that
            # inherits none of them. Measured on this host, then closed.
            *(
                f"--property=InaccessiblePaths={listed(path)}"
                for path in session_control_paths()
            ),
            "--property=Environment=XDG_RUNTIME_DIR=",
            "--property=Environment=DBUS_SESSION_BUS_ADDRESS=",
            # Quoted, because these properties take space-separated
            # lists and a repository path with a space in it would
            # otherwise make systemd reject the unit before the command
            # ran, which reads as a refuted fact rather than as a
            # configuration problem.
            *(
                f"--property=ReadWritePaths={listed(path)}"
                for path in writable_paths(workdir)
            ),
            # After the grants, and deeper than them, so they win.
            *(
                f"--property=ReadOnlyPaths={listed(path)}"
                for path in protected_paths(workdir)
            ),
            f"--working-directory={workdir}",
            "/bin/sh",
            "-c",
            command,
        ]
        timed_out = False
        try:
            result = subprocess.run(
                wrapped,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=budget + 30,
                check=False,
            )
            output = result.stdout
            if result.returncode != 0:
                output += containment_diagnosis(output, workdir)
            return result.returncode, output
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            captured = exc.stdout or ""
            if isinstance(captured, bytes):
                captured = captured.decode(errors="replace")
            return 124, captured
        finally:
            if timed_out:
                subprocess.run([systemctl_path() or "systemctl", "--user", "stop", f"{unit}.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=20)
                subprocess.run([systemctl_path() or "systemctl", "--user", "kill", "--kill-who=all", "--signal=KILL", f"{unit}.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=20)
    # Fail closed. Without systemd there is no filesystem confinement at
    # all, only a process group, and a verification command runs code the
    # repository supplies: on a host that cannot contain it, the honest
    # answer is to refuse rather than to run it over the whole home
    # directory and call the result evidence.
    if os.environ.get("ORRERY_ALLOW_UNCONFINED") != "1":
        # 125, not 1. A fact whose command never ran has not been
        # refuted, and recording it as refuted would drop a previously
        # current fact out of every later handoff because the host
        # could not contain a command.
        return UNCONTAINED_STATUS, (
            "orrery: refusing to verify without containment. systemd-run and "
            "systemctl are required, because a verification command runs "
            "whatever code this repository holds and would otherwise reach "
            "the whole home directory. Set ORRERY_ALLOW_UNCONFINED=1 to "
            "accept that risk deliberately.\n"
        )
    print("orrery: systemd is unavailable; verification uses process-group containment.", file=sys.stderr)
    process: subprocess.Popen[str] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            cwd=workdir,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        output, _ = process.communicate(timeout=budget)
        return process.returncode, output
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        captured = exc.stdout or ""
        if isinstance(captured, bytes):
            captured = captured.decode(errors="replace")
        return 124, captured
    finally:
        if timed_out and process is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
