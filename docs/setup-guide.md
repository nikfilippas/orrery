# Claude-Codex Orchestration Kit

Reusable configuration that makes the active Claude model the principal
orchestrator of a development session and Codex a specialist worker reached
directly through its CLI.

This guide describes how the kit is installed, used and maintained. It does
not reproduce the contents of the files: an earlier version of this document
pasted every script inline, and every one of those copies had drifted from
the code by the time anyone read them. Where a file matters, it is named and
linked, and the file itself is the specification.

## Design

Claude decides and stays accountable. Codex does bounded work Claude has
scoped, and Claude inspects the result rather than trusting the report.

Three Codex profiles, each pinning its own model and reasoning effort:

| Profile | Model | Reasoning | Role |
| --- | --- | --- | --- |
| `luna` | `gpt-5.6-luna` | low | narrow, mechanical, well-specified edits |
| `terra` | `gpt-5.6-terra` | medium | the default worker for substantial implementation |
| `sol` | `gpt-5.6-sol` | high | independent review, and difficult diagnosis |

Two rules shape everything else:

- **Reviews run through the Codex CLI, never the companion plugin.** The
  `codex@openai-codex` plugin is disabled with the JSON Boolean `false`, and
  its broker, `review` and `adversarial-review` paths are not used.
- **A session leaves no trace.** Processes, sockets, temporary files and
  browser profiles created by a session are reverted by the session.

## Files

| Path | Purpose |
| --- | --- |
| `global/CLAUDE.md` | The development policy, installed as `~/.claude/CLAUDE.md` |
| `global/claude-settings.json` | Canonical model, hooks, companion state and permission rules |
| `global/skills/development-orchestrator/SKILL.md` | Task classification and model routing |
| `global/hooks/leave-no-trace.py` | Session lifecycle hook and `claude-lnt-*` implementation |
| `global/codex/luna.config.toml` | Low-effort Codex profile |
| `global/codex/terra.config.toml` | Medium-effort Codex profile |
| `global/codex/sol.config.toml` | High-effort Codex profile |
| `project-template/CLAUDE.md` | Shared per-repository template |
| `project-template/CLAUDE.local.md` | Private per-repository workflow block |
| `scripts/install.sh` | Installs every link and applies the canonical settings |
| `scripts/apply-claude-settings.py` | Atomic, locked settings updater |
| `scripts/set-claude-model.sh` | Compatibility wrapper for `--model` |
| `scripts/set-codex-companion-state.sh` | Compatibility wrapper for `--companion` |
| `scripts/install-lnt-hooks.sh` | Installs the Leave No Trace links and hooks |
| `scripts/claude-codex-review` | Synchronous independent review through Sol |
| `scripts/claude-lnt-start` | Runs a process under a lease that outlives the tool call |
| `scripts/claude-lnt-register` | Registers a rollback command |
| `scripts/claude-lnt-cleanup` | Runs cleanup for a session on demand |
| `scripts/claude-lnt-status` | Reports owned processes and registered cleanups |
| `scripts/init-project.sh` | Migrates a repository without rewriting its instructions |
| `scripts/doctor.sh` | Validates the installation without model calls |
| `tests/run-tests.py` | Deterministic regression suite |
| `tests/fake-codex` | Stand-in Codex CLI used by the suite |

## Installation

Requires Ubuntu or another systemd Linux, Claude Code, Codex CLI, Python 3.11
or newer, `git` and `jq`.

```bash
git clone <remote> ~/src/claude-codex-kit
cd ~/src/claude-codex-kit
./scripts/install.sh
claude-codex-doctor
```

`install.sh` is idempotent: a link that is already correct is left alone, and
nothing is backed up on a rerun. It refuses to run if a target resolves to
its own source, which is what happens when `CODEX_HOME` points inside the
kit.

`CODEX_HOME` defaults to `~/.codex`. An explicitly empty value is rejected
rather than silently treated as the home directory.

The installer creates:

- `~/.claude/CLAUDE.md`, `~/.claude/skills/development-orchestrator`,
  `~/.claude/hooks/leave-no-trace.py`
- `$CODEX_HOME/{luna,terra,sol}.config.toml`
- `~/.local/bin/claude-codex-{init,doctor,review}` and
  `~/.local/bin/claude-lnt-{start,register,cleanup,status}`

and then applies the canonical settings into `~/.claude/settings.json` in one
locked transaction, preserving everything the kit does not own.

## Routine use

```bash
claude-codex-init /path/to/repository   # migrate a repository
claude-codex-doctor                     # validate, no model calls
./tests/run-tests.py                    # full regression suite
./tests/run-tests.py CODEX_HOME         # only matching tests
```

Independent review:

```bash
claude-codex-review --timeout 600 --output verdict.txt -- "REVIEW PROMPT"
```

The wrapper runs `codex --profile sol exec` read-only inside a transient
systemd user service. A timeout, an interruption, or an uncatchable death of
the wrapper stops the whole control group. It refuses to run if the `sol`
profile is missing, because Codex exits zero on an unknown `--profile` and
silently substitutes its default model.

Non-interactive sessions should run at moderate effort:

```bash
claude -p --effort medium "..."
```

Maximum effort is paid on every turn of an agentic loop. Measured on this
kit, the same delegated task at `xhigh` never reached the delegation step in
eight minutes, and completed in under three at `medium`.

## Changing models

- **One Codex profile:** edit `global/codex/<profile>.config.toml`. The
  installed paths are symlinks, so the change is live immediately.
- **The Claude orchestrator, globally:** edit `model` in
  `global/claude-settings.json`, then run
  `./scripts/apply-claude-settings.py --model`.
- **For one session:** `claude --model <name>`.
- **For one repository:** set it in that repository's `CLAUDE.local.md`.

Run `claude-codex-doctor` afterwards. It validates that each profile sets a
model and the expected reasoning effort, and that the default Claude model
matches the canonical settings.

## Validation

```bash
cd ~/src/claude-codex-kit
./tests/run-tests.py          # deterministic, no credits spent
claude-codex-doctor           # installation state, no model calls
git diff --check
```

The suite uses a stand-in Codex CLI, never calls a model, and never touches
the live Claude or Codex configuration. `doctor.sh` reports
`CLAUDE_CODEX_KIT_READY` when everything passes, including a clean
repository.

## Browsers

Browser work uses the Playwright-bundled Chrome for Testing only, pinned, and
never a system browser. The snap Chromium is the failure mode this policy
exists to prevent: it runs in a private mount namespace whose profiles are
invisible to ordinary `du`, excluded from tmpfiles ageing, and backed by RAM.

For a screenshot, prefer the driver's own command, which needs no script and
closes its browser itself:

```bash
npx --yes playwright@1.62.0 screenshot \
    --viewport-size 1280,720 "file://$PWD/page.html" shot.png
```

Abandoned automation profiles are swept by the Leave No Trace hook, matching
the drivers' exact profile prefixes so a directory such as
`playwright_test-results` is never touched.

## Troubleshooting

**`claude-codex-init: command not found`** — `~/.local/bin` is not on `PATH`.
Add it to the shell profile and start a new shell.

**A Codex profile appears ignored** — confirm
`$CODEX_HOME/<profile>.config.toml` exists and is a link into the kit. An
unknown `--profile` does not fail; Codex falls back to the default model, so
a typo produces work from a model you did not choose.

**The active model does not match the kit** — `~/.claude/settings.json` is
authoritative for the default. Run `./scripts/apply-claude-settings.py
--model`, and check for a per-repository override in `CLAUDE.local.md`.

**`CLAUDE.local.md` appears in Git status** — `init-project.sh` adds it to
`.git/info/exclude`. If it was committed before migration, remove it from the
index deliberately; the script will not do that for you.

**The doctor fails only on a dirty repository** — that is the intended
result while work is uncommitted.

**A headless browser survives `timeout`** — a snap browser reparents to
systemd and outlives its launcher, and `kill` from a VS Code shell is denied
by AppArmor. Stop its `snap.chromium.chromium-*.scope` instead, and never
stop a scope whose root process lacks `--headless=new`, which would be the
interactive browser. The bundled-browser policy avoids this entirely.

**VS Code freezes during a large agentic run** — reduce effort for
non-interactive work as above, and prefer `claude -p` outside the editor for
long runs.

## Maintenance

- Update Claude Code and Codex CLI through their own updaters; the kit pins
  neither.
- After changing kit policy or scripts, run `./tests/run-tests.py` and
  `claude-codex-doctor`, then commit. Installed paths are symlinks, so a
  change is live without reinstalling.
- Refresh migrated repositories by rerunning `claude-codex-init` in them,
  which replaces only the managed block in `CLAUDE.local.md`.
- Roll back a settings change from the timestamped
  `~/.claude/settings.json.backup-claude-codex-*` file written before each
  update.
