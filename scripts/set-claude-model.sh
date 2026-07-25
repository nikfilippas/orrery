#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"

SOURCE="$KIT_DIR/global/claude-settings.json"
TARGET="$HOME/.claude/settings.json"

mkdir -p "$(dirname "$TARGET")"

MODEL="$(jq -er '.model' "$SOURCE")"

if [ -e "$TARGET" ]; then
    if ! jq empty "$TARGET" >/dev/null 2>&1; then
        printf "Invalid JSON in existing Claude settings:\n%s\n" "$TARGET" >&2
        exit 1
    fi

    CURRENT_MODEL="$(jq -r '.model // empty' "$TARGET")"

    if [ "$CURRENT_MODEL" = "$MODEL" ]; then
        printf "Claude default model already set to: %s\n" "$MODEL"
        exit 0
    fi

    BACKUP="$TARGET.backup-$(date +%Y%m%d-%H%M%S)"
    cp -a "$TARGET" "$BACKUP"
    printf "Backed up existing Claude settings to:\n%s\n" "$BACKUP"
else
    printf '{}\n' > "$TARGET"
fi

TEMP_FILE="$(mktemp)"

jq \
    --arg model "$MODEL" \
    '.model = $model' \
    "$TARGET" > "$TEMP_FILE"

chmod --reference="$TARGET" "$TEMP_FILE" 2>/dev/null || true
mv "$TEMP_FILE" "$TARGET"

printf "Set Claude default model to: %s\n" "$MODEL"
