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
# Nanoseconds via Python: BSD date on macOS prints a literal N for %N.
BACKUP_DIR="$HOME/.orrery-backups/$(date +%Y%m%d-%H%M%S)-$$-$(
    python3 -c 'import time; print(time.time_ns())'
)"

# Files moved aside are announced even when a later step aborts the
# installer, so nothing a user owned can vanish without a printed pointer.
report_backups() {
    if [ -d "$BACKUP_DIR" ]; then
        printf "Previous files were backed up to:\n%s\n" "$BACKUP_DIR"
    fi
}
trap report_backups EXIT

backup_existing() {
    local target="$1"
    local relative

    if [ -e "$target" ] || [ -L "$target" ]; then
        case "$target" in
            "$HOME"/*) relative="home/${target#"$HOME"/}" ;;
            *) relative="absolute/${target#/}" ;;
        esac
        mkdir -p "$BACKUP_DIR"
        mkdir -p "$BACKUP_DIR/$(dirname "$relative")"
        cp -a "$target" "$BACKUP_DIR/$relative"
        rm -rf "$target"
    fi
}

link_file() {
    local source="$1"
    local target="$2"

    if python3 - "$target" "$KIT_DIR" <<'PY'
import sys
from pathlib import Path

target = Path(sys.argv[1]).expanduser()
if not target.is_absolute():
    target = Path.cwd() / target
# Resolve the containing directory, but not the final component: an
# idempotent target is already a symlink into the kit and must still count as
# a target outside it.
target = target.parent.resolve(strict=False) / target.name
kit = Path(sys.argv[2]).resolve(strict=False)
try:
    target.relative_to(kit)
except ValueError:
    raise SystemExit(1)
PY
    then
        printf "Refusing to install a managed link inside Orrery's source tree: %s\n" \
            "$target" >&2
        exit 2
    fi

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

if [ -e "$CODEX_HOME/AGENTS.override.md" ] ||
   [ -L "$CODEX_HOME/AGENTS.override.md" ]
then
    printf '\nWARNING: %s shadows Orrery'\''s installed Codex policy.\n' \
        "$CODEX_HOME/AGENTS.override.md" >&2
    printf 'It was preserved. Remove or reconcile it before relying on Codex orchestration.\n\n' >&2
fi

link_file \
    "$KIT_DIR/global/AGENTS.md" \
    "$HOME/.claude/AGENTS.md"

link_file \
    "$KIT_DIR/global/AGENTS.md" \
    "$CODEX_HOME/AGENTS.md"

link_file \
    "$KIT_DIR/global/CLAUDE.md" \
    "$HOME/.claude/CLAUDE.md"

link_file \
    "$KIT_DIR/global/skills/development-orchestrator" \
    "$HOME/.claude/skills/development-orchestrator"

link_file \
    "$KIT_DIR/global/skills/development-orchestrator" \
    "$HOME/.agents/skills/development-orchestrator"

link_file \
    "$KIT_DIR/scripts/orrery-session-start" \
    "$HOME/.claude/hooks/orrery-session-start.py"

link_file \
    "$KIT_DIR/scripts/orrery-session-start" \
    "$CODEX_HOME/hooks/orrery-session-start.py"

# Role assignments now come from one provider-neutral manifest. Remove only
# obsolete profile links installed by this checkout; user-owned profiles stay.
for stale in mechanic implementer plan-reviewer reviewer luna terra vesta sol; do
    stale_link="$CODEX_HOME/$stale.config.toml"
    if [ -L "$stale_link" ] &&
       case "$(readlink "$stale_link")" in "$KIT_DIR"/*) true ;; *) false ;; esac
    then
        rm -f -- "$stale_link"
        printf 'Removed the renamed profile link: %s\n' "$stale_link"
    fi
done

mkdir -p "$HOME/.local/bin"

# Remove only command links installed by this checkout under the retired
# namespace. A same-named user file or a link into another checkout is left
# untouched. The legacy spelling is assembled so it cannot leak back into
# current help text or documentation.
legacy_prefix="claude"
legacy_prefix="${legacy_prefix}-codex"
for suffix in init doctor review usage config; do
    retired="$HOME/.local/bin/${legacy_prefix}-${suffix}"
    if python3 - "$retired" "$KIT_DIR" <<'PY'
import os
import sys
from pathlib import Path

link = Path(sys.argv[1])
kit = Path(sys.argv[2]).resolve(strict=False)
if not link.is_symlink():
    raise SystemExit(1)
raw = Path(os.readlink(link))
candidate = (
    raw if raw.is_absolute() else link.parent / raw
).resolve(strict=False)
try:
    candidate.relative_to(kit)
except ValueError:
    raise SystemExit(1)
PY
    then
        rm -f -- "$retired"
        printf 'Removed retired command link: %s\n' "$retired"
    fi
done

link_file \
    "$KIT_DIR/scripts/orrery" \
    "$HOME/.local/bin/orrery"

link_file \
    "$KIT_DIR/scripts/init-project.sh" \
    "$HOME/.local/bin/orrery-init"

link_file \
    "$KIT_DIR/scripts/doctor.sh" \
    "$HOME/.local/bin/orrery-doctor"

link_file \
    "$KIT_DIR/scripts/orrery-review" \
    "$HOME/.local/bin/orrery-agent"

link_file \
    "$KIT_DIR/scripts/orrery-review" \
    "$HOME/.local/bin/orrery-review"

link_file \
    "$KIT_DIR/scripts/orrery-usage" \
    "$HOME/.local/bin/orrery-usage"

link_file \
    "$KIT_DIR/scripts/orrery-incidents" \
    "$HOME/.local/bin/orrery-incidents"

link_file \
    "$KIT_DIR/scripts/orrery-config" \
    "$HOME/.local/bin/orrery-config"

"$KIT_DIR/scripts/install-lnt-hooks.sh" --links-only
"$KIT_DIR/scripts/apply-claude-settings.py" \
    --companion \
    --hooks \
    --permissions
"$KIT_DIR/scripts/apply-claude-settings.py" \
    --hooks \
    --source "$KIT_DIR/global/codex-hooks.json" \
    --target "$CODEX_HOME/hooks.json"

printf "\nInstallation complete.\n"

case ":$PATH:" in
    *":$HOME/.local/bin:"* | *":$HOME/.local/bin/:"*)
        ;;
    *)
        printf '\nWARNING: %s is not on PATH.\n' "$HOME/.local/bin" >&2
        printf 'The installed commands cannot be invoked by name until it is.\n' >&2
        printf 'Add it in your shell profile and start a new shell.\n' >&2
        ;;
esac
