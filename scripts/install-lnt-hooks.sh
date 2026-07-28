#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
HOOK_SOURCE="$KIT_DIR/global/hooks/leave-no-trace.py"
HOOK_TARGET="$HOME/.claude/hooks/leave-no-trace.py"
LINKS_ONLY=0

if [ "${1:-}" = "--links-only" ]; then
    LINKS_ONLY=1
    shift
fi

if [ "$#" -ne 0 ]; then
    printf 'Usage: %s [--links-only]\n' "$0" >&2
    exit 2
fi

unique_suffix() {
    python3 - <<'PY'
import os
import time

print(f"{time.time_ns()}-{os.getpid()}")
PY
}

install_link() {
    local source="$1"
    local target="$2"

    mkdir -p "$(dirname "$target")"

    if [ -L "$target" ] &&
       [ "$(readlink -f "$target")" = "$(readlink -f "$source")" ]; then
        printf 'Link already correct: %s\n' "$target"
        return
    fi

    # A target that is not a link but resolves to the source is the source
    # itself. Moving it aside would remove the canonical file.
    if [ ! -L "$target" ] && [ -e "$target" ] &&
       [ "$(readlink -f "$target")" = "$(readlink -f "$source")" ]; then
        printf 'Refusing to install %s over its own source.\n' "$target" >&2
        exit 2
    fi

    if [ -e "$target" ] || [ -L "$target" ]; then
        local backup="${target}.backup-lnt-$(unique_suffix)"
        mv -- "$target" "$backup"
        printf 'Backed up %s to %s\n' "$target" "$backup"
    fi

    ln -s -- "$source" "$target"
    printf 'Installed link: %s -> %s\n' "$target" "$source"
}

install_link "$HOOK_SOURCE" "$HOOK_TARGET"

for command in start register cleanup status; do
    install_link \
        "$KIT_DIR/scripts/claude-lnt-$command" \
        "$HOME/.local/bin/claude-lnt-$command"
done

if [ "$LINKS_ONLY" -eq 0 ]; then
    "$KIT_DIR/scripts/apply-claude-settings.py" --hooks
fi
