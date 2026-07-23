#!/usr/bin/env bash

set -u

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
FAILURES=0

pass() {
    printf "PASS  %s\n" "$1"
}

fail() {
    printf "FAIL  %s\n" "$1" >&2
    FAILURES=$((FAILURES + 1))
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
        "$HOME/.codex/$profile.config.toml" \
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

if grep -Fxq "model = \"gpt-5.6-luna\"" \
    "$HOME/.codex/luna.config.toml" &&
   grep -Fxq "model_reasoning_effort = \"low\"" \
    "$HOME/.codex/luna.config.toml"; then
    pass "Luna profile is valid"
else
    fail "Luna profile is invalid"
fi

if grep -Fxq "model = \"gpt-5.6-terra\"" \
    "$HOME/.codex/terra.config.toml" &&
   grep -Fxq "model_reasoning_effort = \"medium\"" \
    "$HOME/.codex/terra.config.toml"; then
    pass "Terra profile is valid"
else
    fail "Terra profile is invalid"
fi

if grep -Fxq "model = \"gpt-5.6-sol\"" \
    "$HOME/.codex/sol.config.toml" &&
   grep -Fxq "model_reasoning_effort = \"high\"" \
    "$HOME/.codex/sol.config.toml"; then
    pass "Sol profile is valid"
else
    fail "Sol profile is invalid"
fi

printf "\n=== Authentication ===\n"
if codex login status 2>&1 | grep -q "Logged in"; then
    pass "Codex authentication is active"
else
    fail "Codex authentication is unavailable"
fi

printf "\n=== Claude Codex plugin ===\n"
if claude plugin list 2>/dev/null |
    grep -A3 "codex@openai-codex" |
    grep -q "enabled"; then
    pass "Claude Codex plugin is enabled"
else
    fail "Claude Codex plugin is not enabled"
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
