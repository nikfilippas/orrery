#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
MODELS_FILE="$KIT_DIR/global/model-catalogue.json"

usage() {
    printf 'Usage: orrery-init [model] [directory]\n' >&2
    printf 'A known model selects a private repository override; any other\n' >&2
    printf 'argument must be an existing directory. Known models:\n' >&2
    python3 - "$MODELS_FILE" <<'PY' >&2 || true
import json
import sys
from pathlib import Path

try:
    providers = json.loads(Path(sys.argv[1]).read_text())["providers"]
except (OSError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
for provider, entries in providers.items():
    for entry in entries:
        print(f"  {entry['id']} ({provider})")
PY
}

model_spec() {
    python3 - "$MODELS_FILE" "$1" <<'PY'
import json
import sys
from pathlib import Path

try:
    providers = json.loads(Path(sys.argv[1]).read_text())["providers"]
except (OSError, KeyError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
matches = [
    (provider, entry)
    for provider, entries in providers.items()
    for entry in entries
    if entry.get("id") == sys.argv[2]
]
if len(matches) != 1:
    raise SystemExit(1)
provider, entry = matches[0]
thinking = entry.get("default_thinking") or ""
print(f"{provider}\t{entry['id']}\t{thinking}")
PY
}

MODEL=""
MODEL_SPEC=""
REQUESTED_DIR="$PWD"
DIRECTORY_CHOSEN=0

for argument in "$@"; do
    if [ -z "$MODEL" ] && resolved="$(model_spec "$argument" 2>/dev/null)"; then
        MODEL="$argument"
        MODEL_SPEC="$resolved"
    elif [ -d "$argument" ] && [ "$DIRECTORY_CHOSEN" -eq 0 ]; then
        REQUESTED_DIR="$argument"
        DIRECTORY_CHOSEN=1
    else
        printf 'Unexpected argument: %s\n' "$argument" >&2
        usage
        exit 2
    fi
done

git_at() (
    unset \
        GIT_DIR \
        GIT_WORK_TREE \
        GIT_COMMON_DIR \
        GIT_INDEX_FILE \
        GIT_OBJECT_DIRECTORY \
        GIT_ALTERNATE_OBJECT_DIRECTORIES \
        GIT_CEILING_DIRECTORIES
    directory="$1"
    shift
    git -C "$directory" "$@"
)

PROJECT_ROOT=""
if INSIDE_WORKTREE="$(
        git_at "$REQUESTED_DIR" rev-parse --is-inside-work-tree 2>/dev/null
    )" &&
   [ "$INSIDE_WORKTREE" = "true" ] &&
   PROJECT_ROOT="$(
        git_at "$REQUESTED_DIR" rev-parse --show-toplevel 2>/dev/null
    )"
then
    :
else
    if [ "$(
        git_at "$REQUESTED_DIR" rev-parse --is-bare-repository 2>/dev/null ||
            true
    )" = "true" ]; then
        printf 'Cannot initialize files in a bare Git repository: %s\n' \
            "$REQUESTED_DIR" >&2
        exit 1
    fi
    if EXISTING_MARKER="$(
        python3 - "$REQUESTED_DIR" <<'PY'
import os
import sys
from pathlib import Path

current = Path(sys.argv[1]).resolve(strict=False)
for directory in (current, *current.parents):
    marker = directory / ".git"
    if os.path.lexists(marker):
        print(marker)
        raise SystemExit(0)
raise SystemExit(1)
PY
    )"; then
        printf 'Git metadata exists but is not a usable worktree: %s\n' \
            "$EXISTING_MARKER" >&2
        exit 1
    fi
    git_at "$REQUESTED_DIR" init --quiet
    PROJECT_ROOT="$(
        git_at "$REQUESTED_DIR" rev-parse --show-toplevel
    )"
    printf 'Initialized Git repository: %s\n' "$PROJECT_ROOT"
fi

printf '\n=== Orrery project instructions ===\n'
printf 'Repository: %s\n\n' "$PROJECT_ROOT"

python3 - \
    "$KIT_DIR/project-template/AGENTS.md" \
    "$KIT_DIR/project-template/CLAUDE.md" \
    "$PROJECT_ROOT" <<'PY'
import os
import re
import shutil
import sys
from pathlib import Path

agents_template = Path(sys.argv[1])
claude_template = Path(sys.argv[2])
root = Path(sys.argv[3])
agents = root / "AGENTS.md"
claude = root / "CLAUDE.md"
local = root / "CLAUDE.local.md"


def present(path: Path) -> bool:
    return os.path.lexists(path)


if not present(agents) and not present(claude):
    shutil.copyfile(agents_template, agents)
    shutil.copyfile(claude_template, claude)
    print(f"Created canonical project instructions:\n{agents}")
    print(f"Created Claude import wrapper:\n{claude}")
elif present(agents):
    print(f"Preserved existing canonical instructions:\n{agents}")
    if not present(claude):
        shutil.copyfile(claude_template, claude)
        print(f"Created Claude import wrapper:\n{claude}")
    else:
        print(f"Preserved existing Claude instructions:\n{claude}")
        try:
            claude_text = claude.read_text()
        except OSError:
            claude_text = ""
        if "@AGENTS.md" not in claude_text:
            print(
                "WARNING: CLAUDE.md does not import @AGENTS.md; Claude and "
                "Codex may receive different project instructions."
            )
else:
    try:
        claude_text = claude.read_text()
    except OSError as exc:
        raise SystemExit(f"Cannot read existing CLAUDE.md: {exc}")
    if claude_text.strip() == "@AGENTS.md":
        shutil.copyfile(agents_template, agents)
        print(f"Repaired missing canonical instructions:\n{agents}")
    else:
        shutil.copyfile(claude, agents)
        print(f"Copied existing Claude instructions for Codex:\n{agents}")
        print(
            "WARNING: CLAUDE.md was preserved as written; manual deduplication "
            "is required: reconcile it into AGENTS.md, then replace CLAUDE.md "
            "with @AGENTS.md."
        )

# Retire only Orrery-owned workflow blocks. Personal text around them survives.
if present(local) and local.is_file():
    text = local.read_text()
    names = ("orrery", "claude" + "-codex-kit")
    for name in names:
        pattern = re.compile(
            rf"(?ms)^[ \t]*<!-- {re.escape(name)}:start -->.*?"
            rf"^[ \t]*<!-- {re.escape(name)}:end -->[ \t]*(?:\n|$)"
        )
        text = pattern.sub("", text)
    cleaned = text.strip()
    local.write_text(cleaned + "\n" if cleaned else "")
    print(f"Removed retired Orrery workflow blocks from:\n{local}")
PY

EXCLUDE_FILE="$(git_at "$PROJECT_ROOT" rev-parse --git-path info/exclude)"
case "$EXCLUDE_FILE" in
    /*) ;;
    *) EXCLUDE_FILE="$PROJECT_ROOT/$EXCLUDE_FILE" ;;
esac
mkdir -p "$(dirname "$EXCLUDE_FILE")"
touch "$EXCLUDE_FILE"

for pattern in "/.orrery.json" "/CLAUDE.local.md"; do
    name="${pattern#/}"
    if git_at "$PROJECT_ROOT" ls-files --error-unmatch -- "$name" \
        >/dev/null 2>&1
    then
        printf 'WARNING: %s is tracked; it was not removed from Git.\n' "$name"
    elif ! grep -Fxq "$pattern" "$EXCLUDE_FILE"; then
        printf '%s\n' "$pattern" >> "$EXCLUDE_FILE"
        printf 'Added private exclusion %s to %s\n' "$pattern" "$EXCLUDE_FILE"
    fi
done

# The marker that adopts the repository: the SessionStart check and the
# global policy apply Orrery's orchestration layer only where it exists.
MARKER="$PROJECT_ROOT/.orrery.json"
if [ ! -f "$MARKER" ]; then
    printf '{}\n' > "$MARKER"
    printf 'Created adoption marker %s\n' "$MARKER"
fi

if [ -n "$MODEL" ]; then
    IFS=$'\t' read -r MODEL_PROVIDER MODEL_VALUE MODEL_THINKING <<< "$MODEL_SPEC"
    python3 - \
        "$PROJECT_ROOT/.orrery.json" \
        "$MODEL_PROVIDER" \
        "$MODEL_VALUE" \
        "$MODEL_THINKING" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
if os.path.lexists(path) and path.is_symlink():
    raise SystemExit(f"Refusing to replace a symlink: {path}")
try:
    data = json.loads(path.read_text()) if path.exists() else {}
except json.JSONDecodeError as exc:
    raise SystemExit(f"{path} is not valid JSON: {exc}")
if not isinstance(data, dict):
    raise SystemExit(f"{path} must contain a JSON object")
role = {
    "provider": sys.argv[2],
    "model": sys.argv[3],
    "thinking": sys.argv[4] or None,
}
data["orchestrator"] = role
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    "w", dir=path.parent, prefix=f".{path.name}.", delete=False
) as handle:
    json.dump(data, handle, indent=2)
    handle.write("\n")
    handle.flush()
    os.fsync(handle.fileno())
    temporary = Path(handle.name)
temporary.replace(path)
print(
    "Repository principal override: "
    f"{role['provider']} / {role['model']} / "
    f"{role['thinking'] or 'no thinking selector'}"
)
PY
fi

printf '\n=== Conflict scan ===\n'
CONFLICT_PATTERN='((do not|never)[[:space:]]+(use|invoke|call)[[:space:]]+(codex|claude|external agents?|other models?))|((claude|codex|fable)[[:space:]]+only)|((do not|never)[[:space:]]+delegate)|(must[[:space:]]+implement[[:space:]]+directly)|((always|automatically)[[:space:]]+(commit|push|deploy|release))'
FOUND=0
for candidate in AGENTS.md CLAUDE.md CLAUDE.local.md; do
    path="$PROJECT_ROOT/$candidate"
    [ -r "$path" ] || continue
    if [ "$candidate" = "CLAUDE.md" ] &&
       [ "$(tr -d '[:space:]' < "$path")" = "@AGENTS.md" ]
    then
        continue
    fi
    matches="$(grep -Ein "$CONFLICT_PATTERN" "$path" || true)"
    if [ -n "$matches" ]; then
        printf 'Potential orchestration conflicts in %s:\n%s\n' \
            "$candidate" "$matches"
        FOUND=1
    fi
done
[ "$FOUND" -eq 1 ] ||
    printf 'No obvious orchestration conflicts detected.\n'

printf '\n=== Principal surface ===\n'
# Adoption is the one command a user is expected to run, so the
# configured principal is projected onto its provider's own
# configuration here too: an extension or CLI session started
# afterwards begins on the right model at the right thinking level,
# with the same-provider fallback ladder armed. The projection is
# global, so doing it once aligns every repository. A failure is
# reported and never blocks adoption.
if ! python3 "$KIT_DIR/scripts/orrery-sync"; then
    printf 'The principal surface could not be aligned; run orrery-sync.\n' >&2
fi

printf '\n=== Migration result ===\n'
printf 'Canonical instructions: %s/AGENTS.md\n' "$PROJECT_ROOT"
printf 'Claude wrapper:         %s/CLAUDE.md\n' "$PROJECT_ROOT"
printf 'Start a new Orrery session to load the resulting instruction chain.\n'
