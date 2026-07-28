#!/usr/bin/env bash

set -euo pipefail

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
BACKUP_DIR="$HOME/.claude-codex-kit-backups/$(date +%Y%m%d-%H%M%S)-$$-$(date +%N)"

backup_existing() {
    local target="$1"

    if [ -e "$target" ] || [ -L "$target" ]; then
        mkdir -p "$BACKUP_DIR"
        cp -a "$target" "$BACKUP_DIR/"
        rm -rf "$target"
    fi
}

link_file() {
    local source="$1"
    local target="$2"

    mkdir -p "$(dirname "$target")"

    if [ -L "$target" ] &&
       [ "$(readlink -f "$target")" = "$(readlink -f "$source")" ]; then
        printf "Link already correct: %s\n" "$target"
        return
    fi

    # A target that is not a link but resolves to the source is the source
    # itself, for example when CODEX_HOME points inside the kit. Linking it
    # would delete the canonical file and leave a self-referential link.
    if [ ! -L "$target" ] && [ -e "$target" ] &&
       [ "$(readlink -f "$target")" = "$(readlink -f "$source")" ]; then
        printf "Refusing to install %s over its own source.\n" "$target" >&2
        exit 2
    fi

    backup_existing "$target"
    ln -s "$source" "$target"
    printf "Linked %s -> %s\n" "$target" "$source"
}

link_file \
    "$KIT_DIR/global/CLAUDE.md" \
    "$HOME/.claude/CLAUDE.md"

link_file \
    "$KIT_DIR/global/skills/development-orchestrator" \
    "$HOME/.claude/skills/development-orchestrator"

link_file \
    "$KIT_DIR/global/codex/luna.config.toml" \
    "$CODEX_HOME/luna.config.toml"

link_file \
    "$KIT_DIR/global/codex/terra.config.toml" \
    "$CODEX_HOME/terra.config.toml"

link_file \
    "$KIT_DIR/global/codex/sol.config.toml" \
    "$CODEX_HOME/sol.config.toml"

mkdir -p "$HOME/.local/bin"
link_file \
    "$KIT_DIR/scripts/init-project.sh" \
    "$HOME/.local/bin/claude-codex-init"

link_file \
    "$KIT_DIR/scripts/doctor.sh" \
    "$HOME/.local/bin/claude-codex-doctor"

link_file \
    "$KIT_DIR/scripts/claude-codex-review" \
    "$HOME/.local/bin/claude-codex-review"

"$KIT_DIR/scripts/install-lnt-hooks.sh" --links-only
"$KIT_DIR/scripts/apply-claude-settings.py" --all

printf "\nInstallation complete.\n"

if [ -d "$BACKUP_DIR" ]; then
    printf "Previous files were backed up to:\n%s\n" "$BACKUP_DIR"
fi
