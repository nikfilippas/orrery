#!/usr/bin/env bash

set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQUESTED_DIR="${1:-$PWD}"

if [ ! -d "$REQUESTED_DIR" ]; then
    printf "Directory does not exist: %s\n" "$REQUESTED_DIR" >&2
    exit 1
fi

if ! PROJECT_ROOT="$(git -C "$REQUESTED_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
    printf "Not inside a Git repository: %s\n" "$REQUESTED_DIR" >&2
    exit 1
fi

SOURCE="$KIT_DIR/project-template/CLAUDE.md"
TARGET="$PROJECT_ROOT/CLAUDE.md"

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    printf "Existing project instructions preserved:\n%s\n" "$TARGET"
    exit 0
fi

cp "$SOURCE" "$TARGET"

printf "Created project instructions:\n%s\n" "$TARGET"
printf "\nReview and complete the project-specific placeholders before substantial work.\n"
