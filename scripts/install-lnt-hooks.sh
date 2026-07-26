#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
KIT_DIR="$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)"
SOURCE_SETTINGS="$KIT_DIR/global/claude-settings.json"
TARGET_SETTINGS="$HOME/.claude/settings.json"
HOOK_SOURCE="$KIT_DIR/global/hooks/leave-no-trace.py"
HOOK_TARGET="$HOME/.claude/hooks/leave-no-trace.py"

mkdir -p "$HOME/.claude/hooks" "$HOME/.local/bin"
ln -sfn "$HOOK_SOURCE" "$HOOK_TARGET"

for command in start register cleanup status; do
    ln -sfn "$KIT_DIR/scripts/claude-lnt-$command" "$HOME/.local/bin/claude-lnt-$command"
done

python3 - "$SOURCE_SETTINGS" "$TARGET_SETTINGS" <<'PY'
from pathlib import Path
import json
import os
import shutil
import sys
import time

source_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
source = json.loads(source_path.read_text())
target = json.loads(target_path.read_text()) if target_path.exists() else {}
marker = "leave-no-trace.py"

hooks = target.setdefault("hooks", {})
for event, groups in list(hooks.items()):
    cleaned_groups = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks", [])
        kept = [
            handler
            for handler in handlers
            if not (
                isinstance(handler, dict)
                and marker in str(handler.get("command", ""))
            )
        ]
        if kept:
            copy = dict(group)
            copy["hooks"] = kept
            cleaned_groups.append(copy)
    if cleaned_groups:
        hooks[event] = cleaned_groups
    else:
        hooks.pop(event, None)

for event, groups in source.get("hooks", {}).items():
    hooks.setdefault(event, []).extend(groups)

rendered = json.dumps(target, indent=2) + "\n"
existing = target_path.read_text() if target_path.exists() else ""
if rendered == existing:
    print("Leave No Trace hooks are already installed.")
    raise SystemExit(0)

if target_path.exists():
    backup = target_path.with_name(
        target_path.name + ".backup-lnt-" + time.strftime("%Y%m%d-%H%M%S")
    )
    shutil.copy2(target_path, backup)
    print(f"Backed up Claude settings to:\n{backup}")
else:
    target_path.parent.mkdir(parents=True, exist_ok=True)

temp = target_path.with_suffix(".tmp")
temp.write_text(rendered)
os.chmod(temp, 0o600)
temp.replace(target_path)
print("Installed Leave No Trace hooks.")
PY
