#!/usr/bin/env bash

set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$HOME/.claude-codex-kit-backups/$(date +%Y%m%d-%H%M%S)"

backup_existing() {
    local target="$1"

    if [ -e "$target" ] || [ -L "$target" ]; then
        mkdir -p "$BACKUP_DIR"
        cp -aL "$target" "$BACKUP_DIR/"
        rm -rf "$target"
    fi
}

link_file() {
    local source="$1"
    local target="$2"

    mkdir -p "$(dirname "$target")"
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
    "$HOME/.codex/luna.config.toml"

link_file \
    "$KIT_DIR/global/codex/terra.config.toml" \
    "$HOME/.codex/terra.config.toml"

link_file \
    "$KIT_DIR/global/codex/sol.config.toml" \
    "$HOME/.codex/sol.config.toml"

printf "\nInstallation complete.\n"

if [ -d "$BACKUP_DIR" ]; then
    printf "Previous files were backed up to:\n%s\n" "$BACKUP_DIR"
fi
