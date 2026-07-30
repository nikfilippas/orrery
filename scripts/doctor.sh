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
if [node_by_id[target]["x"] for target in targets] != sorted(
    node_by_id[target]["x"] for target in targets
):
    raise SystemExit("classifier nodes are not ordered left to right")
forward = next(
    edge for edge in edges
    if edge.get("from") == "plan" and edge.get("to") == "plan-review-step"
)
reverse = next(
    edge for edge in edges
    if edge.get("from") == "plan-review-step" and edge.get("to") == "plan"
)
if (
    not forward.get("offset")
    or forward["offset"] != -reverse.get("offset", 0)
):
    raise SystemExit("plan-review cycle curves are not symmetric")
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
            if claude auth status --json >/dev/null 2>&1; then
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
            if codex login status 2>&1 | grep -q "Logged in"; then
                pass "OpenAI authentication is active"
            else
                fail "OpenAI authentication is unavailable"
            fi
        else
            fail "Configured provider command missing: codex"
        fi
    fi
done <<< "$CONFIGURED_PROVIDERS"

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
    "orrery-usage:scripts/orrery-usage"
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

printf '\n=== Containment support ===\n'
if [ "$KERNEL" = "Linux" ]; then
    check_command systemd-run
    check_command systemctl
else
    skip "systemd control-group containment is not applicable on $KERNEL"
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
