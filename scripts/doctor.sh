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
    printf 'PASS  %s\n' "$1"
}

fail() {
    printf 'FAIL  %s\n' "$1" >&2
    FAILURES=$((FAILURES + 1))
}

skip() {
    printf 'SKIP  %s\n' "$1"
}

warn() {
    # Visible but never counted as a failure.
    printf 'WARN  %s\n' "$1"
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
    elif [ "$(readlink -f "$target")" = "$(readlink -f "$expected")" ]; then
        pass "Correct link: $target"
    else
        fail "Incorrect link target: $target"
    fi
}

printf '\n=== Base commands ===\n'
for command in git jq python3; do
    check_command "$command"
done

printf '\n=== Adoption trust ===\n'
if TRUST_WARNING="$(python3 - "$KIT_DIR/scripts" "$PWD" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from orrery_runtime import RuntimeConfigError, _read_trust, _trusted_marker, _git_root

try:
    root = _git_root(Path(sys.argv[2]))
    if root is not None and _trusted_marker(root) is not None and _read_trust(root) is None:
        print(f"Run orrery-init {root} to record trusted adoption.")
except RuntimeConfigError as exc:
    print(str(exc))
PY
)"; then
    if [ -n "$TRUST_WARNING" ]; then
        warn "$TRUST_WARNING"
    else
        pass "Adoption trust is valid for this repository"
    fi
else
    fail "Could not inspect adoption trust"
fi

if python3 - <<'PY'
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
    pass "Python is 3.11 or newer"
else
    fail "Python 3.11 or newer is required"
fi

printf '\n=== Canonical manifest and catalogue ===\n'
if python3 - "$KIT_DIR" <<'PY'
import json
import re
import sys
from pathlib import Path

kit = Path(sys.argv[1])
manifest = json.loads((kit / "global/orchestration.json").read_text())
catalogue = json.loads((kit / "global/model-catalogue.json").read_text())
providers = catalogue.get("providers")
if not isinstance(providers, dict) or set(providers) != {"anthropic", "openai"}:
    raise SystemExit("catalogue providers must be anthropic and openai")

effort_name = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
known = {}
for provider, entries in providers.items():
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"{provider} catalogue is empty")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise SystemExit("catalogue entries must be objects")
        model = entry.get("id")
        label = entry.get("label")
        levels = entry.get("thinking_levels")
        default = entry.get("default_thinking")
        fallback_tier = entry.get("fallback_tier")
        if not isinstance(model, str) or not model:
            raise SystemExit("catalogue model has no id")
        if model in seen:
            raise SystemExit(f"duplicate dropdown model: {provider}/{model}")
        if not isinstance(label, str) or not label:
            raise SystemExit(f"{model} has no label")
        if (
            not isinstance(levels, list)
            or len(levels) != len(set(levels))
            or any(
                not isinstance(level, str) or not effort_name.fullmatch(level)
                for level in levels
            )
        ):
            raise SystemExit(f"{model} has invalid thinking levels")
        if default is not None and default not in levels:
            raise SystemExit(f"{model} has an invalid thinking default")
        if (
            isinstance(fallback_tier, bool)
            or not isinstance(fallback_tier, int)
            or not 1 <= fallback_tier <= 3
        ):
            raise SystemExit(f"{model} has an invalid fallback tier")
        seen.add(model)
        known[(provider, model)] = entry

steps = manifest.get("steps")
expected = {
    "orchestrator": "principal",
    "mechanic": "workspace-write",
    "implementer": "workspace-write",
    "plan-reviewer": "read-only",
    "reviewer": "read-only",
}
if not isinstance(steps, list):
    raise SystemExit("manifest steps must be a list")
if [step.get("id") for step in steps] != list(expected):
    raise SystemExit("manifest roles are missing, duplicated, or reordered")
for step in steps:
    role = step["id"]
    provider = step.get("provider")
    model = step.get("model")
    thinking = step.get("thinking")
    if provider not in providers:
        raise SystemExit(f"{role} has invalid provider")
    if not isinstance(model, str) or not model:
        raise SystemExit(f"{role} has no model")
    if step.get("access") != expected[role]:
        raise SystemExit(f"{role} has the wrong access contract")
    entry = known.get((provider, model))
    if entry is not None:
        levels = entry["thinking_levels"]
        if levels and thinking not in levels:
            raise SystemExit(f"{role} thinking is unsupported")
        if not levels and thinking is not None:
            raise SystemExit(f"{role} should have no thinking level")
    elif (
        thinking is not None
        and (
            not isinstance(thinking, str)
            or not effort_name.fullmatch(thinking)
        )
    ):
        raise SystemExit(f"{role} has invalid custom thinking")

settings = manifest.get("settings", {})
rounds = settings.get("plan_review_rounds")
if not isinstance(rounds, dict):
    raise SystemExit("plan-review setting is missing")
value, minimum, maximum = (
    rounds.get("value"), rounds.get("minimum"), rounds.get("maximum")
)
if (
    any(isinstance(item, bool) or not isinstance(item, int)
        for item in (value, minimum, maximum))
    or (minimum, maximum) != (1, 4)
    or not minimum <= value <= maximum
):
    raise SystemExit("plan-review cap must be 1–4")

verbosity = manifest.get("verbosity", 1)
if (
    isinstance(verbosity, bool)
    or not isinstance(verbosity, int)
    or verbosity not in (1, 2, 3)
):
    raise SystemExit("manifest verbosity must be 1, 2, or 3")

endpoints = manifest.get("endpoints", {})
if not isinstance(endpoints, dict):
    raise SystemExit("manifest endpoints must be an object")
sys.path.insert(0, str(kit / "scripts"))
from orrery_runtime import load_endpoint  # noqa: E402

for name in endpoints:
    load_endpoint(manifest, name)

for step in manifest["steps"]:
    if step.get("endpoint") is not None:
        endpoint = load_endpoint(manifest, step["endpoint"])
        if endpoint.adapter != step["provider"]:
            raise SystemExit(
                f"{step['id']} provider does not match its endpoint adapter"
            )
    step_timeout = step.get("timeout_seconds")
    if step_timeout is not None and (
        isinstance(step_timeout, bool)
        or not isinstance(step_timeout, int)
        or not 30 <= step_timeout <= 7200
    ):
        raise SystemExit(
            "role timeout_seconds must be an integer between 30 and 7200"
        )
    hard_timeout = step.get("hard_timeout_seconds")
    if hard_timeout is not None:
        if (
            isinstance(hard_timeout, bool)
            or not isinstance(hard_timeout, int)
            or not 30 <= hard_timeout <= 14400
        ):
            raise SystemExit(
                "role hard_timeout_seconds must be an integer between "
                "30 and 14400"
            )
        if step_timeout is None or hard_timeout < step_timeout:
            raise SystemExit(
                "role hard_timeout_seconds requires timeout_seconds "
                "and must not be smaller"
            )

chart = manifest.get("chart", {})
nodes = chart.get("nodes")
edges = chart.get("edges")
if not isinstance(nodes, list) or not isinstance(edges, list):
    raise SystemExit("chart nodes and edges must be lists")
node_by_id = {node.get("id"): node for node in nodes}
if len(node_by_id) != len(nodes):
    raise SystemExit("chart node ids are duplicated")
for edge in edges:
    if edge.get("from") not in node_by_id or edge.get("to") not in node_by_id:
        raise SystemExit("chart edge names a missing node")
labels = [
    edge.get("label")
    for edge in edges
    if edge.get("from") == "classify"
]
required_labels = [
    "investigation", "trivial", "mechanical", "standard", "complex"
]
if labels != required_labels:
    raise SystemExit("classifier branches are not in the required order")
targets = [
    edge["to"] for edge in edges if edge.get("from") == "classify"
]
if [node_by_id[target]["y"] for target in targets] != sorted(
    node_by_id[target]["y"] for target in targets
) or any(
    node_by_id[target]["x"] <= node_by_id["classify"]["x"]
    for target in targets
):
    raise SystemExit(
        "classifier results are not a top-to-bottom rank right of classify"
    )
pairs = {(edge.get("from"), edge.get("to")) for edge in edges}
if (
    ("plan", "plan-review-step") not in pairs
    or ("plan-review-step", "plan") not in pairs
):
    raise SystemExit("chart omits the bounded plan-review cycle")
# The cycle is drawn as a bounded loop rather than as two arrows, so what
# has to hold is that both directions are named and the loop is marked.
cycle = [
    edge for edge in edges
    if {edge.get("from"), edge.get("to")} == {"plan", "plan-review-step"}
]
if len(cycle) != 2 or not all(
    isinstance(edge.get("label"), str) and edge["label"].strip()
    for edge in cycle
):
    raise SystemExit("the plan-review cycle does not name both directions")
if not chart.get("loopMark"):
    raise SystemExit("the chart does not mark the plan-review loop")
required_nodes = {
    "investigation-result", "review-gate", "review", "findings",
    "correct", "done", "plan-escalation",
}
if not required_nodes <= node_by_id.keys():
    raise SystemExit("chart is missing a workflow outcome")
for node in nodes:
    label = str(node.get("label", "")).lower()
    if "claude" in label or "codex" in label:
        raise SystemExit("chart hard-codes a provider into a role")
if rounds.get("node") not in node_by_id:
    raise SystemExit("plan-review setting names a missing node")

claude_settings = json.loads(
    (kit / "global/claude-settings.json").read_text()
)
if (
    "model" in claude_settings
    or "effortLevel" in claude_settings
    or "fallbackModel" in claude_settings
    or "CLAUDE_CODE_EFFORT_LEVEL" in claude_settings.get("env", {})
):
    raise SystemExit("Claude settings duplicate the role manifest")
PY
then
    pass "Manifest, model catalogue, access contracts, and chart are valid"
else
    fail "Manifest, model catalogue, access contracts, or chart is invalid"
fi

printf '\n=== Configured providers ===\n'
CONFIGURED_PROVIDERS="$(
    python3 - "$KIT_DIR/global/orchestration.json" <<'PY'
import json
import sys
from pathlib import Path
manifest = json.loads(Path(sys.argv[1]).read_text())
print("\n".join(sorted({step["provider"] for step in manifest["steps"]})))
PY
)"
while IFS= read -r provider; do
    [ -n "$provider" ] || continue
    if [ "$provider" = "anthropic" ]; then
        if command -v claude >/dev/null 2>&1; then
            pass "Configured provider command available: claude"
            if claude auth status >/dev/null 2>&1; then
                pass "Anthropic authentication is active"
            else
                fail "Anthropic authentication is unavailable"
            fi
        else
            fail "Configured provider command missing: claude"
        fi
    elif [ "$provider" = "openai" ]; then
        if command -v codex >/dev/null 2>&1; then
            pass "Configured provider command available: codex"
            if codex login status >/dev/null 2>&1; then
                pass "OpenAI authentication is active"
            else
                fail "OpenAI authentication is unavailable"
            fi
        else
            fail "Configured provider command missing: codex"
        fi
    fi
done <<< "$CONFIGURED_PROVIDERS"

if command -v codex >/dev/null 2>&1; then
    VALIDATED_CODEX="$(
        python3 -c "import sys; sys.path.insert(0, '$KIT_DIR/scripts'); \
from orrery_runtime import VALIDATED_CODEX_CLI; print(VALIDATED_CODEX_CLI)" \
            2>/dev/null
    )"
    INSTALLED_CODEX="$(codex --version 2>/dev/null | awk '{print $2}')"
    if [ -z "$VALIDATED_CODEX" ] || [ -z "$INSTALLED_CODEX" ]; then
        skip "Codex CLI version could not be compared with the baseline"
    elif [ "$INSTALLED_CODEX" = "$VALIDATED_CODEX" ]; then
        pass "Codex CLI matches the validated baseline ($VALIDATED_CODEX)"
    else
        warn "Codex CLI $INSTALLED_CODEX differs from the validated baseline $VALIDATED_CODEX; re-run the delegated probe (docs/setup-guide.md) and update VALIDATED_CODEX_CLI"
    fi
fi

printf '\n=== Potential fallback providers ===\n'
for provider in anthropic openai; do
    if grep -Fxq "$provider" <<< "$CONFIGURED_PROVIDERS"; then
        continue
    fi
    if [ "$provider" = "anthropic" ]; then
        if ! command -v claude >/dev/null 2>&1; then
            skip "Anthropic is not installed as a cross-provider fallback"
        elif claude auth status >/dev/null 2>&1; then
            pass "Anthropic is authenticated as a potential fallback"
        else
            skip "Anthropic is not authenticated as a cross-provider fallback"
        fi
    elif ! command -v codex >/dev/null 2>&1; then
        skip "OpenAI is not installed as a cross-provider fallback"
    elif codex login status >/dev/null 2>&1; then
        pass "OpenAI is authenticated as a potential fallback"
    else
        skip "OpenAI is not authenticated as a cross-provider fallback"
    fi
done

printf '\n=== Shared instruction chain ===\n'
check_link "$HOME/.claude/AGENTS.md" "$KIT_DIR/global/AGENTS.md"
check_link "$CODEX_HOME/AGENTS.md" "$KIT_DIR/global/AGENTS.md"
check_link "$HOME/.claude/CLAUDE.md" "$KIT_DIR/global/CLAUDE.md"
check_link \
    "$HOME/.claude/skills/development-orchestrator" \
    "$KIT_DIR/global/skills/development-orchestrator"
check_link \
    "$HOME/.agents/skills/development-orchestrator" \
    "$KIT_DIR/global/skills/development-orchestrator"
check_link \
    "$HOME/.claude/hooks/orrery-session-start.py" \
    "$KIT_DIR/scripts/orrery-session-start"
check_link \
    "$CODEX_HOME/hooks/orrery-session-start.py" \
    "$KIT_DIR/scripts/orrery-session-start"

if [ "$(tr -d '\r' < "$KIT_DIR/global/CLAUDE.md")" = "@AGENTS.md" ] &&
   [ "$(tr -d '\r' < "$KIT_DIR/project-template/CLAUDE.md")" = "@AGENTS.md" ]
then
    pass "Claude files are exact one-line AGENTS imports"
else
    fail "Claude files duplicate or fail to import AGENTS.md"
fi

if grep -Fq "ORRERY ROLE HANDOFF" \
       "$KIT_DIR/global/skills/development-orchestrator/SKILL.md" &&
   grep -Fq "does not re-enter the orchestration workflow" \
       "$KIT_DIR/global/AGENTS.md"
then
    pass "Non-principal role recursion guard is installed"
else
    fail "Non-principal role recursion guard is missing"
fi

if [ -e "$CODEX_HOME/AGENTS.override.md" ] ||
   [ -L "$CODEX_HOME/AGENTS.override.md" ]
then
    skip "$CODEX_HOME/AGENTS.override.md shadows the installed Codex policy"
else
    pass "No Codex AGENTS override shadows Orrery"
fi

printf '\n=== Script syntax ===\n'
for script in \
    "$KIT_DIR/scripts/install.sh" \
    "$KIT_DIR/scripts/init-project.sh" \
    "$KIT_DIR/scripts/doctor.sh" \
    "$KIT_DIR/scripts/install-lnt-hooks.sh"
do
    if bash -n "$script"; then
        pass "Valid Bash syntax: $(basename "$script")"
    else
        fail "Invalid Bash syntax: $(basename "$script")"
    fi
done

for script in \
    "$KIT_DIR/scripts/orrery" \
    "$KIT_DIR/scripts/orrery_fallback.py" \
    "$KIT_DIR/scripts/orrery-session-start" \
    "$KIT_DIR/scripts/orrery_model_catalogue.py" \
    "$KIT_DIR/scripts/orrery_runtime.py" \
    "$KIT_DIR/scripts/orrery-review" \
    "$KIT_DIR/scripts/orrery-config" \
    "$KIT_DIR/scripts/orrery-usage" \
    "$KIT_DIR/scripts/apply-claude-settings.py" \
    "$KIT_DIR/global/hooks/leave-no-trace.py" \
    "$KIT_DIR/tests/run-tests.py" \
    "$KIT_DIR/tests/fake-codex" \
    "$KIT_DIR/tests/fake-claude"
do
    if python3 - "$script" <<'PY'
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    compile(path.read_text(), str(path), "exec")
except (OSError, SyntaxError):
    raise SystemExit(1)
PY
    then
        pass "Valid Python syntax: $(basename "$script")"
    else
        fail "Invalid Python syntax: $(basename "$script")"
    fi
done

printf '\n=== Installed commands ===\n'
for entry in \
    "orrery:scripts/orrery" \
    "orrery-agent:scripts/orrery-review" \
    "orrery-review:scripts/orrery-review" \
    "orrery-init:scripts/init-project.sh" \
    "orrery-doctor:scripts/doctor.sh" \
    "orrery-config:scripts/orrery-config" \
    "orrery-usage:scripts/orrery-usage" \
    "orrery-incidents:scripts/orrery-incidents" \
    "orrery-task:scripts/orrery-task" \
    "orrery-pickup:scripts/orrery-pickup" \
    "orrery-sync:scripts/orrery-sync"
do
    name="${entry%%:*}"
    relative="${entry#*:}"
    check_link "$HOME/.local/bin/$name" "$KIT_DIR/$relative"
done

case ":$PATH:" in
    *":$HOME/.local/bin:"* | *":$HOME/.local/bin/:"*)
        pass "~/.local/bin is on PATH"
        ;;
    *)
        fail "~/.local/bin is not on PATH"
        ;;
esac

printf '\n=== Claude-specific settings and cleanup ===\n'
if command -v claude >/dev/null 2>&1; then
    VALIDATED_CLAUDE="$(
        python3 -c "import sys; sys.path.insert(0, '$KIT_DIR/scripts'); \
from orrery_runtime import VALIDATED_CLAUDE_CLI; print(VALIDATED_CLAUDE_CLI)" \
            2>/dev/null
    )"
    INSTALLED_CLAUDE="$(claude --version 2>/dev/null | awk '{print $1}')"
    if [ -z "$VALIDATED_CLAUDE" ] || [ -z "$INSTALLED_CLAUDE" ]; then
        skip "Claude CLI version could not be compared with the baseline"
    elif [ "$INSTALLED_CLAUDE" = "$VALIDATED_CLAUDE" ]; then
        pass "Claude CLI matches the validated delegated-run baseline ($VALIDATED_CLAUDE)"
    else
        warn "Claude CLI $INSTALLED_CLAUDE differs from the validated baseline $VALIDATED_CLAUDE; re-run the delegated shell probe (docs/setup-guide.md) and update VALIDATED_CLAUDE_CLI"
    fi
fi
# A sandboxed Claude run that dies before cleanup leaves zero-byte
# mount-point files in $HOME. An empty .bash_profile is the damaging
# one: login shells then skip .profile, and PATH loses ~/.local/bin
# machine-wide.
SANDBOX_RESIDUE=""
for name in .bash_profile .bash_aliases .bash_login .bash_logout \
    .zshrc .zprofile .zshenv .zlogin .zlogout \
    .netrc .npmrc .yarnrc .yarnrc.yml .bunfig.toml .ripgreprc; do
    candidate="$HOME/$name"
    if [ -f "$candidate" ] && [ ! -s "$candidate" ] \
        && [ ! -L "$candidate" ]; then
        SANDBOX_RESIDUE="$SANDBOX_RESIDUE $name"
    fi
done
if [ -n "$SANDBOX_RESIDUE" ]; then
    warn "Zero-byte shell/config files in \$HOME:$SANDBOX_RESIDUE - likely sandbox mount-point residue; an empty .bash_profile masks .profile and breaks login PATH. Remove them if you did not create them."
else
    pass "No zero-byte sandbox residue in \$HOME shell configuration"
fi
check_link \
    "$HOME/.claude/hooks/leave-no-trace.py" \
    "$KIT_DIR/global/hooks/leave-no-trace.py"
for name in start register cleanup status; do
    check_link \
        "$HOME/.local/bin/claude-lnt-$name" \
        "$KIT_DIR/scripts/claude-lnt-$name"
done

if python3 - \
    "$KIT_DIR/global/claude-settings.json" \
    "$HOME/.claude/settings.json" <<'PY'
import json
import sys
from pathlib import Path
try:
    canonical = json.loads(Path(sys.argv[1]).read_text())
    live = json.loads(Path(sys.argv[2]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

plugin = "codex@openai-codex"
if canonical.get("enabledPlugins", {}).get(plugin) is not False:
    raise SystemExit(1)
if live.get("enabledPlugins", {}).get(plugin) is not False:
    raise SystemExit(1)

def handlers(settings):
    result = set()
    for event, groups in settings.get("hooks", {}).items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher", "")
            for hook in group.get("hooks", []):
                if isinstance(hook, dict):
                    result.add((
                        event, matcher, hook.get("type"),
                        hook.get("command"), hook.get("timeout"),
                    ))
    return result

if not handlers(canonical) <= handlers(live):
    raise SystemExit(1)
canonical_allow = set(canonical.get("permissions", {}).get("allow", []))
live_allow = set(live.get("permissions", {}).get("allow", []))
if not canonical_allow <= live_allow:
    raise SystemExit(1)
PY
then
    pass "Live Claude settings contain canonical hooks and permissions"
else
    fail "Live Claude settings are missing canonical hooks or permissions"
fi

printf '\n=== Codex principal-surface notification ===\n'
if python3 - \
    "$KIT_DIR/global/codex-hooks.json" \
    "$CODEX_HOME/hooks.json" <<'PY'
import json
import sys
from pathlib import Path

try:
    canonical = json.loads(Path(sys.argv[1]).read_text())
    live = json.loads(Path(sys.argv[2]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

def handlers(settings):
    result = set()
    for event, groups in settings.get("hooks", {}).items():
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher", "")
            for hook in group.get("hooks", []):
                if isinstance(hook, dict):
                    result.add((
                        event,
                        matcher,
                        hook.get("type"),
                        hook.get("command"),
                        hook.get("timeout"),
                    ))
    return result

if not handlers(canonical) <= handlers(live):
    raise SystemExit(1)
PY
then
    pass "Live Codex hooks contain the Orrery SessionStart check"
else
    fail "Live Codex hooks are missing the Orrery SessionStart check"
fi

if python3 - "$CODEX_HOME/config.toml" <<'PY'
import sys
import tomllib
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)
try:
    config = tomllib.loads(path.read_text())
except (OSError, tomllib.TOMLDecodeError):
    raise SystemExit(1)
if config.get("features", {}).get("hooks") is False:
    raise SystemExit(1)
PY
then
    pass "Codex hooks are not disabled in the user configuration"
else
    fail "Codex hooks are disabled or the user configuration is unreadable"
fi
skip "Codex may require the changed SessionStart hook to be trusted with /hooks"

printf '\n=== Containment support ===\n'
if [ "$KERNEL" = "Linux" ]; then
    check_command systemd-run
    check_command systemctl
else
    skip "systemd control-group containment is not applicable on $KERNEL"
fi

printf '\n=== Model endpoints ===\n'
ENDPOINT_REPORT="$(
    python3 - "$KIT_DIR/global/orchestration.json" "$KIT_DIR" <<'PY'
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
from orrery_runtime import load_endpoint  # noqa: E402

manifest = json.loads(Path(sys.argv[1]).read_text())
used = {
    step["endpoint"]: step["id"]
    for step in manifest.get("steps", [])
    if step.get("endpoint")
}
if not used:
    print("PASS|Every role uses its provider's first-party endpoint")
for name, role in sorted(used.items()):
    endpoint = load_endpoint(manifest, name)
    if endpoint.key_env and not os.environ.get(endpoint.key_env, "").strip():
        print(
            f"FAIL|{role} routes to {endpoint.label} but "
            f"{endpoint.key_env} is unset"
        )
    else:
        credential = (
            f"{endpoint.key_env} is set"
            if endpoint.key_env
            else "no credential required"
        )
        print(
            f"PASS|{role} routes to {endpoint.label} "
            f"({endpoint.base_url}; {credential})"
        )
PY
)" || ENDPOINT_REPORT="FAIL|Model endpoint configuration is invalid"
while IFS='|' read -r verdict message; do
    [ -n "$message" ] || continue
    if [ "$verdict" = "PASS" ]; then
        pass "$message"
    else
        fail "$message"
    fi
done <<EOF
$ENDPOINT_REPORT
EOF

printf '\n=== Standing fallback approvals ===\n'
if STANDING_LIST="$(
    python3 - "$KIT_DIR" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from orrery_standing import describe, list_active

for record in list_active():
    print(describe(record))
PY
)"; then
    if [ -n "$STANDING_LIST" ]; then
        while IFS= read -r line; do
            warn "Standing fallback active: $line"
        done <<< "$STANDING_LIST"
        printf 'Revoke with: orrery --revoke-fallbacks\n'
    else
        pass "No standing fallback approvals"
    fi
else
    fail "The standing-approval store could not be read"
fi

printf '\n=== Parked work ===\n'
if PICKUP_REPORT="$(
    python3 - "$KIT_DIR" <<'PY'
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
loader = importlib.machinery.SourceFileLoader(
    "doctor_pickup", str(Path(sys.argv[1]) / "scripts" / "orrery-pickup")
)
spec = importlib.util.spec_from_loader("doctor_pickup", loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
info, warnings = module.describe()
for line in info:
    print(f"INFO:{line}")
for line in warnings:
    print(f"WARN:{line}")
PY
)"; then
    while IFS= read -r line; do
        case "$line" in
            INFO:*) pass "Parked work: ${line#INFO:}" ;;
            WARN:*) warn "Parked work: ${line#WARN:}" ;;
        esac
    done <<< "$PICKUP_REPORT"
else
    warn "The parked-work store could not be described"
fi

printf '\n=== Principal surface ===\n'
# A drifted surface is legitimate: a deliberate in-session model change
# persists here, and Orrery reports it rather than fighting it.
SURFACE_OUTPUT="$(python3 "$KIT_DIR/scripts/orrery-sync" --check 2>&1)"
SURFACE_STATUS=$?
if [ "$SURFACE_STATUS" -eq 0 ]; then
    pass "The principal's own surface matches the manifest"
elif [ "$SURFACE_STATUS" -eq 1 ]; then
    warn "The principal's surface differs from the manifest:"
    printf '%s\n' "$SURFACE_OUTPUT" | sed -n '2,6p'
    printf 'Align it with: orrery-sync\n'
else
    warn "The principal surface could not be checked: $SURFACE_OUTPUT"
fi

printf '\n=== Incident log ===\n'
if INCIDENT_SUMMARY="$(
    python3 - "$KIT_DIR" <<'PY'
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
from orrery_incidents import read_events

since = datetime.now(timezone.utc) - timedelta(days=7)
events = read_events(since=since)
if events:
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
    summary = ", ".join(
        f"{kind} x{count}"
        for kind, count in sorted(kinds.items(), key=lambda i: -i[1])[:4]
    )
    print(f"{len(events)}|{summary}")
PY
)"; then
    if [ -n "$INCIDENT_SUMMARY" ]; then
        COUNT="${INCIDENT_SUMMARY%%|*}"
        SUMMARY="${INCIDENT_SUMMARY#*|}"
        warn "$COUNT incident(s) recorded in the last 7 days: $SUMMARY"
        printf 'Review with: orrery-incidents\n'
    else
        pass "No incidents recorded in the last 7 days"
    fi
else
    warn "The incident log could not be read"
fi

printf '\n=== Adopted repository location ===\n'
ADOPTED_REAL="$(readlink -f "$(pwd)" 2>/dev/null || pwd)"
case "$ADOPTED_REAL" in
    /tmp | /tmp/* | /var/tmp | /var/tmp/*)
        fail "This repository is under a directory every contained run can write"
        printf '  Its .orrery control store is forgeable by a delegate: a\n'
        printf '  planted memory fact reaches every later run as verified.\n'
        printf '  Move the repository, or accept it with\n'
        printf '  ORRERY_ALLOW_TMP_REPOSITORY=1.\n'
        ;;
    *)
        pass "Control store is outside every broad write grant"
        ;;
esac

printf '\n=== Task worktrees ===\n'
TASK_WORKTREE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/orrery/worktrees"
if [ ! -d "$TASK_WORKTREE_ROOT" ]; then
    pass "No task worktrees"
else
    FOUND_TASK_WORKTREE=0
    for task_worktree in "$TASK_WORKTREE_ROOT"/*/*; do
        [ -d "$task_worktree" ] || continue
        FOUND_TASK_WORKTREE=1
        common_dir="$(git -C "$task_worktree" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || {
            warn "orphaned task worktree: $task_worktree"
            continue
        }
        case "$common_dir" in
            */.git) repository="${common_dir%/.git}" ;;
            *)
                repository="$(git -C "$task_worktree" rev-parse --show-toplevel 2>/dev/null)" || {
                    warn "orphaned task worktree: $task_worktree"
                    continue
                }
                ;;
        esac
        task_id="${task_worktree##*/}"
        ledger="$repository/.orrery/ledger/$task_id.jsonl"
        if [ ! -f "$ledger" ]; then
            warn "task worktree without a ledger: $task_worktree"
            continue
        fi
        task_state="$(python3 - "$ledger" <<'PY'
import json
import sys
from pathlib import Path

try:
    lines = Path(sys.argv[1]).read_text().splitlines()
    if not lines:
        raise ValueError
    record = json.loads(lines[-1])
    state = record.get("to")
    if not isinstance(state, str):
        raise ValueError
except (OSError, ValueError, json.JSONDecodeError):
    raise SystemExit(1)
print(state)
PY
)" || {
            warn "torn ledger tail: $ledger"
            continue
        }
        case "$task_state" in
            MERGED|CLOSED|CANCELLED)
                warn "task worktree outlives its task: $task_worktree"
                ;;
        esac
    done
    if [ "$FOUND_TASK_WORKTREE" -eq 0 ]; then
        pass "No task worktrees"
    fi
fi

printf '\n=== Kit repository ===\n'
if [ -z "$(git -C "$KIT_DIR" status --porcelain)" ]; then
    pass "Kit repository is clean"
else
    fail "Kit repository has uncommitted changes"
    git -C "$KIT_DIR" status --short
fi

printf '\n'
if [ "$FAILURES" -eq 0 ]; then
    echo "ORRERY_READY"
    exit 0
fi

printf 'ORRERY_FAILED: %d check(s) failed\n' "$FAILURES" >&2
exit 1
