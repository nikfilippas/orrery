#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
MODELS_FILE="$KIT_DIR/global/claude-models.json"

usage() {
    printf 'Usage: claude-codex-init [model] [directory]\n' >&2
    printf 'A bare argument that names a known model alias selects the\n' >&2
    printf 'principal orchestrator for the repository; anything else must\n' >&2
    printf 'be an existing directory. Known model aliases:\n' >&2
    python3 - "$MODELS_FILE" <<'PY' >&2 || true
import json
import sys
from pathlib import Path

try:
    table = json.loads(Path(sys.argv[1]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

for alias in sorted(table):
    print(f"  {alias} -> {table[alias]}")
PY
}

is_model_alias() {
    python3 - "$MODELS_FILE" "$1" <<'PY'
import json
import sys
from pathlib import Path

try:
    table = json.loads(Path(sys.argv[1]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)

raise SystemExit(0 if isinstance(table, dict) and sys.argv[2] in table else 1)
PY
}

MODEL_ALIAS=""
REQUESTED_DIR="$PWD"
DIRECTORY_CHOSEN=0

for argument in "$@"; do
    if [ -z "$MODEL_ALIAS" ] && is_model_alias "$argument"; then
        MODEL_ALIAS="$argument"
    elif [ -d "$argument" ] && [ "$DIRECTORY_CHOSEN" -eq 0 ]; then
        REQUESTED_DIR="$argument"
        DIRECTORY_CHOSEN=1
    else
        # A second directory is refused rather than silently winning, so an
        # accidental extra argument cannot migrate the wrong repository.
        printf 'Unexpected argument: %s\n' "$argument" >&2
        usage
        exit 2
    fi
done

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

# `--git-path` output is relative to the repository root unless Git already
# absolutised it, as it does for worktrees. `--path-format=absolute` would
# be simpler but only exists from Git 2.31, and older rev-parse echoes an
# unknown option instead of failing.
EXCLUDE_FILE="$(
    git -C "$PROJECT_ROOT" rev-parse --git-path info/exclude
)"
case "$EXCLUDE_FILE" in
    /*) ;;
    *) EXCLUDE_FILE="$PROJECT_ROOT/$EXCLUDE_FILE" ;;
esac

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

if [ -n "$MODEL_ALIAS" ]; then
    if ! MODEL_VALUE="$(
        python3 - "$MODELS_FILE" "$MODEL_ALIAS" <<'PY'
import json
import sys
from pathlib import Path

table = json.loads(Path(sys.argv[1]).read_text())
value = table.get(sys.argv[2])

if not isinstance(value, str) or not value.strip():
    raise SystemExit(f"invalid model alias entry: {sys.argv[2]}")

print(value.strip())
PY
    )"; then
        printf 'Could not resolve the model alias: %s\n' "$MODEL_ALIAS" >&2
        exit 2
    fi

    # The model is a personal choice, so it goes into the repository's
    # settings.local.json rather than the shared settings, through the
    # atomic updater so that unrelated personal settings survive.
    MODEL_SOURCE="$(mktemp "${TMPDIR:-/tmp}/claude-codex-model.XXXXXX")"
    trap 'rm -f "$MODEL_SOURCE"' EXIT
    python3 - "$MODEL_VALUE" > "$MODEL_SOURCE" <<'PY'
import json
import sys

print(json.dumps({"model": sys.argv[1]}, indent=2))
PY

    printf "\n=== Principal orchestrator ===\n"
    LOCAL_SETTINGS="$PROJECT_ROOT/.claude/settings.local.json"

    # The updater follows a settings symlink, so the file that actually
    # changes is the resolved referent. The tracked-file warning and the
    # exclusion patterns are both derived from that resolution; a referent
    # outside the repository needs neither, because nothing in this
    # repository's status can change.
    RELATIVE_TARGET="$(
        python3 - "$PROJECT_ROOT" "$LOCAL_SETTINGS" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
link = Path(sys.argv[2])
target = link.resolve(strict=False) if link.is_symlink() else link

try:
    print(target.resolve(strict=False).relative_to(root))
except ValueError:
    print("")
PY
    )"

    # `:(literal)` because brackets and asterisks are glob syntax in a Git
    # pathspec, and the referent's name is a literal file name.
    if [ -n "$RELATIVE_TARGET" ] &&
       git -C "$PROJECT_ROOT" \
           ls-files --error-unmatch -- ":(literal)$RELATIVE_TARGET" \
           >/dev/null 2>&1
    then
        printf 'WARNING: %s is tracked by Git.\n' "$RELATIVE_TARGET"
        printf 'The model selection will appear as a tracked change; untrack\n'
        printf 'the file to keep the choice personal.\n'
    fi

    "$KIT_DIR/scripts/apply-claude-settings.py" \
        --model \
        --source "$MODEL_SOURCE" \
        --target "$LOCAL_SETTINGS"

    while IFS= read -r pattern; do
        [ -n "$pattern" ] || continue
        if ! grep -Fxq "$pattern" "$EXCLUDE_FILE"; then
            printf '%s\n' "$pattern" >> "$EXCLUDE_FILE"
        fi
    done <<PATTERNS
$(
        python3 - "$RELATIVE_TARGET" <<'PY'
import re
import sys

patterns = ["/.claude/settings.local.json"]
relative = sys.argv[1]


def escape(text: str) -> str:
    # Git reads exclude patterns as globs, so a literal name containing
    # `*`, `?`, `[`, `]` or a backslash has to escape them to keep
    # matching literally.
    return re.sub(r"([\\*?\[\]])", r"\\\1", text)


if relative:
    parts = relative.rsplit("/", 1)
    if len(parts) == 2:
        prefix, name = escape(parts[0] + "/"), parts[1]
    else:
        prefix, name = "", parts[0]
    stem = escape(name)
    patterns.append(f"/{prefix}{stem}.backup-claude-codex-*")
    patterns.append(f"/{prefix}.{stem}.claude-codex.lock")

print("\n".join(patterns))
PY
)
PATTERNS

    printf 'Model for this repository: %s -> %s\n' \
        "$MODEL_ALIAS" "$MODEL_VALUE"
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
printf "\nRun /context in a fresh Claude Code session to confirm both files load.\n"
