#!/usr/bin/env bash
# Run the suite under the ways a CI runner differs from a developer machine.
#
# Each of these cost a round of red CI against a green local run, because
# none is visible in the code and all are visible only in the environment:
#
#   TMPDIR                the runner uses /tmp, which delegate confinement
#                         deliberately grants for the provider CLIs'
#                         bubblewrap mounts. A fixture built with tempfile
#                         therefore lands inside the write allowlist and
#                         proves nothing about being refused.
#   init.defaultBranch    unset on a runner, so `git init` yields master
#                         while a contract fixture names refs/heads/main.
#   provider CLIs         absent, so anything resolving `claude` or `codex`
#                         through PATH takes a different branch.
#
# Two further differences are deliberately NOT simulated here.
#
# A runner has no ~/.claude/settings.json, so a test lacking HOME
# isolation creates one and stays invisible on a machine where the file
# already exists. Moving the live file aside would race a Claude session
# writing it, so the suite instead checks after every test and names the
# one that wrote it.
#
# A runner restricts unprivileged user namespaces, so systemd accepts a
# unit and then silently fails to enforce part of its sandbox. Faking
# that convincingly was attempted and abandoned: dropping ProtectHome
# also defeats the wrapper's own probe, because ProtectHome covers
# /run/user where the run directory lives, and granting the home
# directory instead breaks unrelated runs. Each attempt produced failures
# CI does not have, and a simulator that invents failures costs more than
# it saves. Confinement behaviour has to be judged on a real runner.
#
# Usage: tests/run-like-ci.sh [test-name-substring ...]

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
kit="$(dirname "$here")"
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

# A PATH with no provider CLIs on it.
clean=""
IFS=: read -ra parts <<<"$PATH"
for entry in "${parts[@]}"; do
    [ -z "$entry" ] && continue
    [ -x "$entry/claude" ] && continue
    [ -x "$entry/codex" ] && continue
    clean="${clean:+$clean:}$entry"
done

# A git configuration shaped like the runner's: an identity so commits
# work, and no init.defaultBranch.
printf '[user]\n\temail = ci@invalid\n\tname = CI\n' >"$scratch/gitconfig"

export PATH="$clean"
export TMPDIR=/tmp
export GIT_CONFIG_GLOBAL="$scratch/gitconfig"

echo "running the suite as a CI runner would see it:"
echo "  TMPDIR=$TMPDIR, no init.defaultBranch, no provider CLIs on PATH"
echo "  (confinement enforcement is not simulated; see the comment above)"
echo

# Not exec: replacing the shell image skips the EXIT trap above, which
# left the temporary git configuration behind on every run.
"$kit/tests/run-tests.py" "$@"
