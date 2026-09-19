#!/usr/bin/env bash
# Run the suite against an isolated copy of this checkout, pinned to the
# committed manifest and provisioned like a CI runner.
#
# A machine's own role choices now live in ~/.config/orrery/config.json,
# which the suite gives itself an isolated copy of, so `tests/run-tests.py`
# measures the kit and not the machine. What it still reads in place is the
# tracked global/orchestration.json, and a checkout whose manifest carries
# configuration, as every install did before `orrery-config --import`
# existed, still steers the run: that is what made the same unchanged suite
# return 4, 81, 43, 23, 56 and 57 failures in one day. This runner removes
# the remaining dependence by pinning that file to a committed ref. Five
# things have to be right or it measures the harness rather than the kit:
#   - the copy's manifest is the committed one, `git show REF:...`;
#   - .git is kept, or `git ls-files` fails and inventory tests error;
#   - the isolated HOME carries a git identity, or commits fail with
#     "Author identity unknown";
#   - XDG_RUNTIME_DIR is left alone: it carries the systemd user bus and
#     cannot be relocated safely (5 failures became 57 when it was);
#   - the copy is NOT adopted, because a CI checkout is not. Adopting it
#     once masked a real CI failure. The runner must not be kinder than CI.
# The live checkout, the live manifest and the live Claude settings are
# never written; the run reports whether either moved while it ran.
#
# Usage: tests/run-pinned.sh [REF]      REF defaults to HEAD.
# The full log's path is printed; the exit status is the suite's.
set -u
KIT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REF=${1:-HEAD}
WORK=$(mktemp -d)
LOG=$(mktemp --suffix=.log "${TMPDIR:-/tmp}/orrery-pinned-XXXXXX")
trap 'rm -rf "$WORK"' EXIT

fingerprint() { sha256sum "$1" 2>/dev/null | cut -c1-16; }
M0=$(fingerprint "$KIT/global/orchestration.json")
S0=$(fingerprint "$HOME/.claude/settings.json")

cp -a "$KIT" "$WORK/kit"
git -C "$KIT" show "$REF:global/orchestration.json" \
  > "$WORK/kit/global/orchestration.json" || exit 2

mkdir -p "$WORK/home/.claude" "$WORK/home/.codex"
[ -f "$HOME/.claude/settings.json" ] \
  && cp "$HOME/.claude/settings.json" "$WORK/home/.claude/settings.json"
printf '[user]\n\tname = Kit Suite\n\temail = kit@example.invalid\n' \
  > "$WORK/home/.gitconfig"

HOME="$WORK/home" XDG_STATE_HOME="$WORK/home/.local/state" \
  timeout 1500 "$WORK/kit/tests/run-tests.py" > "$LOG" 2>&1
STATUS=$?

echo "manifest pinned to $REF; full log: $LOG"
echo "live manifest moved during the run: $([ "$M0" = "$(fingerprint "$KIT/global/orchestration.json")" ] && echo no || echo YES)"
echo "live settings moved during the run: $([ "$S0" = "$(fingerprint "$HOME/.claude/settings.json")" ] && echo no || echo YES)"
grep -vE "^PASS " "$LOG" | tail -40
exit $STATUS
