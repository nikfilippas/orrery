#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
REQUESTED_DIR="${1:-$PWD}"

if [ ! -d "$REQUESTED_DIR" ]; then
    printf "Directory does not exist: %s\n" "$REQUESTED_DIR" >&2
    exit 1
fi

if ! PROJECT_ROOT="$(
    git -C "$REQUESTED_DIR" rev-parse --show-toplevel 2>/dev/null
)"; then
    printf "Not inside a Git repository: %s\n" "$REQUESTED_DIR" >&2
    exit 1
fi

SHARED_TEMPLATE="$KIT_DIR/project-template/CLAUDE.md"
LOCAL_TEMPLATE="$KIT_DIR/project-template/CLAUDE.local.md"

SHARED_TARGET="$PROJECT_ROOT/CLAUDE.md"
LOCAL_TARGET="$PROJECT_ROOT/CLAUDE.local.md"

printf "\n=== Claude-Codex project migration ===\n"
printf "Repository: %s\n\n" "$PROJECT_ROOT"

if [ -e "$SHARED_TARGET" ] || [ -L "$SHARED_TARGET" ]; then
    printf "Preserved existing shared instructions:\n%s\n" "$SHARED_TARGET"
else
    cp "$SHARED_TEMPLATE" "$SHARED_TARGET"
    printf "Created shared project template:\n%s\n" "$SHARED_TARGET"
fi

python3 - "$LOCAL_TEMPLATE" "$LOCAL_TARGET" <<'PYCODE'
from pathlib import Path
import sys

template_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])

template = template_path.read_text()
start_marker = "<!-- claude-codex-kit:start -->"
end_marker = "<!-- claude-codex-kit:end -->"

if start_marker not in template or end_marker not in template:
    raise SystemExit("Managed-block markers are missing from the local template.")

managed_start = template.index(start_marker)
managed_end = template.index(end_marker) + len(end_marker)
managed_block = template[managed_start:managed_end]

if not target_path.exists():
    target_path.write_text(template)
    print(f"Created private workflow instructions:\n{target_path}")
    raise SystemExit(0)

existing = target_path.read_text()

if start_marker in existing and end_marker in existing:
    existing_start = existing.index(start_marker)
    existing_end = existing.index(end_marker) + len(end_marker)

    prefix = existing[:existing_start].rstrip()
    suffix = existing[existing_end:].lstrip()

    sections = []

    if prefix:
        sections.append(prefix)

    sections.append(managed_block)

    if suffix:
        sections.append(suffix)

    target_path.write_text("\n\n".join(sections).rstrip() + "\n")
    print(f"Updated managed workflow block:\n{target_path}")
else:
    base = existing.rstrip()

    if base:
        updated = base + "\n\n" + managed_block + "\n"
    else:
        updated = template

    target_path.write_text(updated)
    print(f"Appended managed workflow block:\n{target_path}")
PYCODE

EXCLUDE_FILE="$(
    git -C "$PROJECT_ROOT" \
        rev-parse \
        --path-format=absolute \
        --git-path info/exclude
)"

mkdir -p "$(dirname "$EXCLUDE_FILE")"
touch "$EXCLUDE_FILE"

if git -C "$PROJECT_ROOT" \
    ls-files --error-unmatch -- CLAUDE.local.md \
    >/dev/null 2>&1
then
    printf "\nWARNING: CLAUDE.local.md is already tracked by Git.\n"
    printf "It was not removed from the index automatically.\n"
else
    if grep -Fxq "/CLAUDE.local.md" "$EXCLUDE_FILE"; then
        printf "\nPrivate instructions are already locally excluded from Git.\n"
    else
        printf "\n/CLAUDE.local.md\n" >> "$EXCLUDE_FILE"
        printf "\nAdded CLAUDE.local.md to:\n%s\n" "$EXCLUDE_FILE"
    fi
fi

printf "\n=== Conflict scan ===\n"

CONFLICT_PATTERN='((do not|never)[[:space:]]+(use|invoke|call)[[:space:]]+(codex|external agents?|other models?))|((claude|fable)[[:space:]]+only)|((do not|never)[[:space:]]+delegate)|(must[[:space:]]+implement[[:space:]]+directly)|((always|automatically)[[:space:]]+(commit|push|deploy|release))'

if [ -r "$SHARED_TARGET" ]; then
    MATCHES="$(
        grep -Ein "$CONFLICT_PATTERN" "$SHARED_TARGET" || true
    )"

    if [ -n "$MATCHES" ]; then
        printf "Potential orchestration conflicts found in CLAUDE.md:\n"
        printf "%s\n" "$MATCHES"
        printf "\nReview these lines before allowing external-model delegation.\n"
    else
        printf "No obvious orchestration conflicts detected.\n"
    fi
else
    printf "Shared CLAUDE.md could not be read.\n"
fi

printf "\n=== Migration result ===\n"
printf "Shared instructions: %s\n" "$SHARED_TARGET"
printf "Private workflow:    %s\n" "$LOCAL_TARGET"
printf "\nRun /memory in a fresh Claude Code session to confirm both files load.\n"
