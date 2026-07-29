#!/usr/bin/env bash

set -u

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
if ! CODEX_HOME="$(
    python3 - <<'PY'
import os
from pathlib import Path

raw = os.environ.get("CODEX_HOME")

if raw is None:
    raw = str(Path.home() / ".codex")

if not raw.strip():
    raise SystemExit("CODEX_HOME cannot be empty")

print(Path(raw).expanduser().resolve(strict=False))
PY
)"; then
    printf 'Could not resolve CODEX_HOME.\n' >&2
    exit 2
fi
export CODEX_HOME
FAILURES=0

KERNEL="$(uname -s)"

pass() {
    printf "PASS  %s\n" "$1"
}

fail() {
    printf "FAIL  %s\n" "$1" >&2
    FAILURES=$((FAILURES + 1))
}

skip() {
    printf "SKIP  %s\n" "$1"
}

check_command() {
    if command -v "$1" >/dev/null 2>&1; then
        pass "Command available: $1"
    else
        fail "Command missing: $1"
    fi
}

check_link() {
    local target="$1"
    local expected="$2"

    if [ ! -L "$target" ]; then
        fail "Not a symbolic link: $target"
        return
    fi

    if [ "$(readlink -f "$target")" = "$(readlink -f "$expected")" ]; then
        pass "Correct link: $target"
    else
        fail "Incorrect link target: $target"
    fi
}

printf "\n=== Required commands ===\n"
for command in claude codex git jq python3; do
    check_command "$command"
done

printf "\n=== Configuration links ===\n"
check_link \
    "$HOME/.claude/CLAUDE.md" \
    "$KIT_DIR/global/CLAUDE.md"

check_link \
    "$HOME/.claude/skills/development-orchestrator" \
    "$KIT_DIR/global/skills/development-orchestrator"

for profile in luna terra sol; do
    check_link \
        "$CODEX_HOME/$profile.config.toml" \
        "$KIT_DIR/global/codex/$profile.config.toml"
done

printf "\n=== Global policy ===\n"
if [ -r "$HOME/.claude/CLAUDE.md" ]; then
    pass "Global CLAUDE.md is readable"
else
    fail "Global CLAUDE.md is not readable"
fi

printf "\n=== Orchestration skill ===\n"
SKILL="$HOME/.claude/skills/development-orchestrator/SKILL.md"

if grep -Fxq "name: development-orchestrator" "$SKILL" &&
   grep -Fxq "user-invocable: false" "$SKILL"; then
    pass "Skill metadata is valid"
else
    fail "Skill metadata is invalid"
fi

printf "\n=== Codex profiles ===\n"

validate_codex_profile() {
    local profile_name="$1"
    local expected_effort="$2"
    local profile_path="$CODEX_HOME/${profile_name}.config.toml"

    if python3 - "$profile_path" "$expected_effort" <<'PYPROFILE'
from pathlib import Path
import sys
import tomllib

path = Path(sys.argv[1])
expected_effort = sys.argv[2]

try:
    with path.open("rb") as handle:
        config = tomllib.load(handle)
except (OSError, tomllib.TOMLDecodeError):
    raise SystemExit(1)

model = config.get("model")
effort = config.get("model_reasoning_effort")

if not isinstance(model, str) or not model.strip():
    raise SystemExit(1)

if effort != expected_effort:
    raise SystemExit(1)
PYPROFILE
    then
        pass "${profile_name^} profile is valid"
    else
        fail "${profile_name^} profile is invalid"
    fi
}

validate_codex_profile "luna" "low"
validate_codex_profile "terra" "medium"
validate_codex_profile "sol" "high"

printf "\n=== Claude model aliases ===\n"
if python3 - "$KIT_DIR/global/claude-models.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    table = json.loads(Path(sys.argv[1]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

if not isinstance(table, dict) or not table:
    raise SystemExit(1)

for alias, model in table.items():
    if not isinstance(alias, str) or not alias.strip():
        raise SystemExit(1)
    if not isinstance(model, str) or not model.strip():
        raise SystemExit(1)

if "opus" not in table:
    raise SystemExit(1)
PY
then
    pass "Model alias map is valid"
else
    fail "Model alias map is missing, invalid, or lacks the opus default"
fi

printf "\n=== Authentication ===\n"
if codex login status 2>&1 | grep -q "Logged in"; then
    pass "Codex authentication is active"
else
    fail "Codex authentication is unavailable"
fi

printf "\n=== Claude Codex companion ===\n"
if python3 - \
    "$KIT_DIR/global/claude-settings.json" \
    "$HOME/.claude/settings.json" <<'PY'
from pathlib import Path
import json
import sys

plugin = "codex@openai-codex"
source = json.loads(Path(sys.argv[1]).read_text())
target = json.loads(Path(sys.argv[2]).read_text())

expected = source.get("enabledPlugins", {}).get(plugin)
actual = target.get("enabledPlugins", {}).get(plugin)

if expected is not False or not isinstance(expected, bool):
    raise SystemExit(1)

if actual is not False or not isinstance(actual, bool):
    raise SystemExit(1)
PY
then
    pass "Codex companion plugin is disabled with a JSON Boolean"
else
    fail "Codex companion plugin is not correctly disabled"
fi

printf "\n=== Script syntax ===\n"
for script in \
    "$KIT_DIR/scripts/install.sh" \
    "$KIT_DIR/scripts/init-project.sh" \
    "$KIT_DIR/scripts/doctor.sh"
do
    if bash -n "$script"; then
        pass "Valid Bash syntax: $(basename "$script")"
    else
        fail "Invalid Bash syntax: $(basename "$script")"
    fi
done

printf "\n=== Direct review syntax ===\n"
if python3 - "$KIT_DIR/scripts/claude-codex-review" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    compile(path.read_text(), str(path), "exec")
except (OSError, SyntaxError):
    raise SystemExit(1)
PY
then
    pass "Valid Python syntax: claude-codex-review"
else
    fail "Invalid Python syntax: claude-codex-review"
fi

for python_script in \
    "$KIT_DIR/scripts/apply-claude-settings.py" \
    "$KIT_DIR/scripts/claude-codex-usage" \
    "$KIT_DIR/scripts/claude-codex-config" \
    "$KIT_DIR/global/hooks/leave-no-trace.py" \
    "$KIT_DIR/tests/run-tests.py" \
    "$KIT_DIR/tests/fake-codex"
do
    if python3 - "$python_script" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    compile(path.read_text(), str(path), "exec")
except (OSError, SyntaxError):
    raise SystemExit(1)
PY
    then
        pass "Valid Python syntax: $(basename "$python_script")"
    else
        fail "Invalid Python syntax: $(basename "$python_script")"
    fi
done

for script in \
    "$KIT_DIR/scripts/set-claude-model.sh" \
    "$KIT_DIR/scripts/set-codex-companion-state.sh" \
    "$KIT_DIR/scripts/install-lnt-hooks.sh"
do
    if bash -n "$script"; then
        pass "Valid Bash syntax: $(basename "$script")"
    else
        fail "Invalid Bash syntax: $(basename "$script")"
    fi
done

for required_command in systemd-run systemctl; do
    if command -v "$required_command" >/dev/null 2>&1; then
        pass "Command available: $required_command"
    elif [ "$KERNEL" != "Linux" ]; then
        skip "$required_command not applicable on $KERNEL: reviews run without control-group containment"
    else
        fail "Command unavailable: $required_command"
    fi
done

# The settings updater refuses to run without an atomic exchange syscall,
# so a home directory on a filesystem without one (some NFS and eCryptfs
# setups) is worth discovering before the first update, not during it.
if python3 - "$KIT_DIR/scripts/apply-claude-settings.py" "$HOME/.claude" <<'PY'
import importlib.machinery
import importlib.util
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
script = Path(sys.argv[1])
target = Path(sys.argv[2])
target.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location(
    "kit_settings_probe",
    script,
    loader=importlib.machinery.SourceFileLoader("kit_settings_probe", str(script)),
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory(dir=target) as directory:
    first = Path(directory) / "first"
    second = Path(directory) / "second"
    first.write_text("a")
    second.write_text("b")
    try:
        module.exchange_paths(first, second)
    except module.AtomicExchangeUnavailable as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)
    if first.read_text() != "b" or second.read_text() != "a":
        raise SystemExit(1)
PY
then
    pass "Atomic settings exchange works on the settings filesystem"
else
    fail "Atomic settings exchange is unavailable on the settings filesystem"
fi

if [ "$KERNEL" != "Linux" ]; then
    skip "Transient systemd user services not applicable on $KERNEL"
    PYTHON3_PATH="$(command -v python3)"
elif PYTHON3_PATH="$(command -v python3)" && python3 - "$PYTHON3_PATH" <<'PY'
import os
import subprocess
import sys
import uuid

python_path = sys.argv[1]
unit = f"claude-codex-doctor-{os.getpid()}-{uuid.uuid4().hex[:8]}"
command = [
    "systemd-run",
    "--user",
    "--quiet",
    "--wait",
    "--collect",
    f"--unit={unit}",
    "--property=Type=exec",
    python_path,
    "-c",
    "pass",
]

try:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
        text=True,
    )
except (OSError, subprocess.TimeoutExpired) as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(1)
finally:
    subprocess.run(
        ["systemctl", "--user", "stop", f"{unit}.service"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )

if result.returncode != 0:
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    raise SystemExit(1)
PY
then
    pass "Transient systemd user services are usable"
else
    fail "Transient systemd user services are unavailable"
fi

SETTINGS_TEST_DIR=""
if SETTINGS_TEST_DIR="$(mktemp -d 2>/dev/null)"; then
    trap 'rm -rf "$SETTINGS_TEST_DIR"' EXIT

    cat > "$SETTINGS_TEST_DIR/source.json" <<'JSON'
{
  "model": "opus",
  "enabledPlugins": {
    "codex@openai-codex": false
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/example/leave-no-trace.py cleanup"
          }
        ]
      }
    ]
  }
}
JSON

    cat > "$SETTINGS_TEST_DIR/source-enabled.json" <<'JSON'
{
  "enabledPlugins": {
    "codex@openai-codex": true
  }
}
JSON

    cat > "$SETTINGS_TEST_DIR/target-real.json" <<'JSON'
{
  "model": "sonnet",
  "enabledPlugins": {
    "codex@openai-codex": true,
    "other@example": true
  },
  "hooks": {
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/example/notify"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/opt/not-leave-no-trace.py"
          }
        ]
      },
      {
        "hooks": []
      }
    ]
  }
}
JSON

    ln -s \
        "$SETTINGS_TEST_DIR/target-real.json" \
        "$SETTINGS_TEST_DIR/target.json"

    if "$KIT_DIR/scripts/apply-claude-settings.py" \
          --all \
          --source "$SETTINGS_TEST_DIR/source.json" \
          --target "$SETTINGS_TEST_DIR/target.json" \
          >/dev/null &&
       python3 - \
          "$SETTINGS_TEST_DIR/target.json" \
          "$SETTINGS_TEST_DIR/target-real.json" <<'PY'
from pathlib import Path
import json
import sys

link = Path(sys.argv[1])
referent = Path(sys.argv[2])
data = json.loads(referent.read_text())

if not link.is_symlink():
    raise SystemExit(1)

if data.get("model") != "opus":
    raise SystemExit(1)

plugins = data.get("enabledPlugins", {})
value = plugins.get("codex@openai-codex")

if value is not False or not isinstance(value, bool):
    raise SystemExit(1)

if plugins.get("other@example") is not True:
    raise SystemExit(1)

hooks = data.get("hooks", {})
if "Notification" not in hooks or "Stop" not in hooks:
    raise SystemExit(1)

stop_groups = hooks["Stop"]
commands = [
    handler.get("command")
    for group in stop_groups
    for handler in group.get("hooks", [])
    if isinstance(handler, dict)
]

if "/opt/not-leave-no-trace.py" not in commands:
    raise SystemExit(1)

if not any(group.get("hooks") == [] for group in stop_groups):
    raise SystemExit(1)
PY
    then
        pass "Atomic settings updater preserves settings and symlinks"
    else
        fail "Atomic settings updater behavioural test failed"
    fi

    if "$KIT_DIR/scripts/apply-claude-settings.py" \
          --companion \
          --source "$SETTINGS_TEST_DIR/source-enabled.json" \
          --target "$SETTINGS_TEST_DIR/target.json" \
          >/dev/null 2>&1
    then
        fail "Atomic settings updater accepts canonical companion enablement"
    else
        pass "Atomic settings updater fails closed on companion enablement"
    fi

    rm -rf "$SETTINGS_TEST_DIR"
    trap - EXIT
else
    fail "Could not create a safe temporary settings test directory"
fi

printf "\n=== Installed commands ===\n"
if [ "$(readlink -f "$HOME/.local/bin/claude-codex-init")" = \
     "$(readlink -f "$KIT_DIR/scripts/init-project.sh")" ]; then
    pass "claude-codex-init is correctly installed"
else
    fail "claude-codex-init is not correctly installed"
fi

if [ "$(readlink -f "$HOME/.local/bin/claude-codex-doctor")" = \
     "$(readlink -f "$KIT_DIR/scripts/doctor.sh")" ]; then
    pass "claude-codex-doctor is correctly installed"
else
    fail "claude-codex-doctor is not correctly installed"
fi

if [ "$(readlink -f "$HOME/.local/bin/claude-codex-usage")" = \
     "$(readlink -f "$KIT_DIR/scripts/claude-codex-usage")" ]; then
    pass "claude-codex-usage is correctly installed"
else
    fail "claude-codex-usage is not correctly installed"
fi

if [ "$(readlink -f "$HOME/.local/bin/claude-codex-config")" = \
     "$(readlink -f "$KIT_DIR/scripts/claude-codex-config")" ]; then
    pass "claude-codex-config is correctly installed"
else
    fail "claude-codex-config is not correctly installed"
fi

if python3 - "$KIT_DIR" <<'PY'
import json
import sys
import tomllib
from pathlib import Path

kit = Path(sys.argv[1])
manifest = json.loads((kit / "global" / "orchestration.json").read_text())
steps = manifest.get("steps")

if not isinstance(steps, list) or not steps:
    raise SystemExit(1)

identifiers = {step.get("id") for step in steps}
if not {"orchestrator", "luna", "terra", "sol"} <= identifiers:
    raise SystemExit(1)

for step in steps:
    path = kit / step["file"]
    if not path.is_file():
        print(f"missing: {step['file']}", file=sys.stderr)
        raise SystemExit(1)
    if step["kind"] == "codex-profile":
        with path.open("rb") as handle:
            profile = tomllib.load(handle)
        if profile.get("model_reasoning_effort") != step.get("expected_effort"):
            print(f"effort drift: {step['id']}", file=sys.stderr)
            raise SystemExit(1)
    elif step["kind"] == "claude-settings":
        model = json.loads(path.read_text()).get("model")
        if not isinstance(model, str) or not model.strip():
            raise SystemExit(1)
    else:
        raise SystemExit(1)
PY
then
    pass "Orchestration manifest matches the live configuration files"
else
    fail "Orchestration manifest is missing, invalid, or drifted"
fi

printf "\n=== Direct Codex review ===\n"
if [ -x "$HOME/.local/bin/claude-codex-review" ] &&
   [ "$(readlink -f "$HOME/.local/bin/claude-codex-review")" = \
     "$(readlink -f "$KIT_DIR/scripts/claude-codex-review")" ]; then
    pass "claude-codex-review is correctly installed"
else
    fail "claude-codex-review is not correctly installed"
fi

case ":$PATH:" in
    *":$HOME/.local/bin:"* | *":$HOME/.local/bin/:"*)
        pass "~/.local/bin is on PATH"
        ;;
    *)
        fail "~/.local/bin is not on PATH, so no installed command is invocable by name"
        ;;
esac

printf "\n=== Leave No Trace layer ===\n"
check_link \
    "$HOME/.claude/hooks/leave-no-trace.py" \
    "$KIT_DIR/global/hooks/leave-no-trace.py"

for lnt_command in start register cleanup status; do
    check_link \
        "$HOME/.local/bin/claude-lnt-$lnt_command" \
        "$KIT_DIR/scripts/claude-lnt-$lnt_command"
done

if python3 - \
    "$KIT_DIR/global/claude-settings.json" \
    "$HOME/.claude/settings.json" <<'PY'
from pathlib import Path
import json
import sys

try:
    canonical = json.loads(Path(sys.argv[1]).read_text())
    live = json.loads(Path(sys.argv[2]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)


def handlers(hooks):
    """Every hook handler as an exactly comparable entry.

    The command alone is not enough: a handler installed under a different
    matcher never runs for the tool it was meant to guard, and one with a
    shortened timeout is cancelled before it finishes. Both would leave the
    cleanup guarantee broken while the command string still looked right.
    """
    entries = set()
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher", "")
            for handler in group.get("hooks", []):
                if not isinstance(handler, dict):
                    continue
                if not isinstance(handler.get("command"), str):
                    continue
                entries.add(
                    (
                        event,
                        matcher if isinstance(matcher, str) else "",
                        handler.get("type"),
                        handler["command"],
                        handler.get("timeout"),
                    )
                )
    return entries


missing = sorted(
    handlers(canonical.get("hooks", {})) - handlers(live.get("hooks", {}))
)

if missing:
    for event, matcher, kind, command, timeout in missing:
        print(
            f"{event}[matcher={matcher!r}, type={kind!r}, "
            f"timeout={timeout!r}]: {command}",
            file=sys.stderr,
        )
    raise SystemExit(1)
PY
then
    pass "Live settings contain every canonical Leave No Trace hook"
else
    fail "Live settings are missing canonical Leave No Trace hooks"
fi

printf "\n=== Claude default model ===\n"
# Compared against the canonical settings, not a hard-coded name: changing
# the model is a documented flow, and the doctor validates consistency.
CANONICAL_MODEL="$(
    jq -r '.model // empty' "$KIT_DIR/global/claude-settings.json" 2>/dev/null
)"
if [ -z "$CANONICAL_MODEL" ]; then
    fail "Canonical settings do not define a model"
elif [ -r "$HOME/.claude/settings.json" ] &&
     jq -e --arg model "$CANONICAL_MODEL" '.model == $model' \
         "$HOME/.claude/settings.json" >/dev/null 2>&1
then
    pass "Claude default model matches the canonical settings ($CANONICAL_MODEL)"
else
    fail "Claude default model does not match the canonical settings ($CANONICAL_MODEL)"
fi

printf "\n=== Kit repository ===\n"
if [ -z "$(git -C "$KIT_DIR" status --porcelain)" ]; then
    pass "Kit repository is clean"
else
    fail "Kit repository has uncommitted changes"
    git -C "$KIT_DIR" status --short
fi

printf "\n"
if [ "$FAILURES" -eq 0 ]; then
    echo "CLAUDE_CODEX_KIT_READY"
    exit 0
fi

printf "CLAUDE_CODEX_KIT_FAILED: %d check(s) failed\n" "$FAILURES" >&2
exit 1
