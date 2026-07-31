# Orrery setup and operation

Orrery runs a configurable principal orchestrator plus four bounded roles:
mechanic, implementer, plan reviewer, and final reviewer. Anthropic and OpenAI
are adapters, not workflow roles. Every role can use either provider.

## Requirements

- Python 3.11 or newer
- `git` and `jq`
- Claude Code when any configured role uses Anthropic
- Codex CLI when any configured role uses OpenAI
- systemd user services on Linux for the strongest delegated-process
  containment

Authenticate only the providers you plan to use:

```bash
claude
codex login
```

## Install

```bash
git clone <remote> ~/src/orrery
cd ~/src/orrery
./scripts/install.sh
orrery-doctor
```

The installer:

- links `global/AGENTS.md` to `~/.claude/AGENTS.md` and
  `$CODEX_HOME/AGENTS.md`;
- links the one-line Claude importer to `~/.claude/CLAUDE.md`;
- installs the orchestration skill at
  `~/.claude/skills/development-orchestrator` and
  `~/.agents/skills/development-orchestrator`;
- installs the Claude lifecycle hook and its helper commands;
- merges the Orrery SessionStart check into both Claude and Codex hook
  configuration without removing unrelated hooks;
- installs `orrery`, `orrery-agent`, `orrery-review`, `orrery-config`,
  `orrery-init`, `orrery-doctor`, and `orrery-usage` in `~/.local/bin`;
- applies Claude-specific permissions plus the provider-neutral startup check;
- removes obsolete profile links only when this checkout owns them.

Existing targets are backed up under a timestamped `~/.orrery-backups`
directory with their home-relative paths preserved, so two files named
`AGENTS.md` cannot collide.

`CODEX_HOME` defaults to `~/.codex`. An explicit empty value is rejected. A
pre-existing `$CODEX_HOME/AGENTS.override.md` is preserved and produces a
warning because Codex loads it instead of `AGENTS.md`.

Codex treats changed non-managed hooks as untrusted until they are reviewed.
After installation, open `/hooks`, inspect the Orrery SessionStart command, and
trust it. `orrery-doctor` reports a disabled or missing hook but cannot grant
trust on the user's behalf.

Make sure `~/.local/bin` is on `PATH`.

## Configure roles

Run:

```bash
orrery-config
```

The page binds only to `127.0.0.1`, uses a random URL token, and exits after an
idle timeout. It is generated from:

- `global/orchestration.json` for role assignment, workflow settings, and
  chart geometry;
- the installed Claude and Codex CLIs for picker-visible models and each
  model's exact thinking levels;
- `global/model-catalogue.json` for provider fallbacks and Orrery-specific
  defaults when a live provider catalogue is unavailable.

The two local catalogues are read concurrently without running a model.
Failure is isolated per provider, equivalent Claude aliases are deduplicated,
and future picker-visible models appear automatically. Every role sees the
same Anthropic and OpenAI groups. A known model determines its provider and
rebuilds the adjacent thinking selector from that model's reported levels. A
custom model requires an explicit provider and has no inferred thinking
selector.

Preview computes one unified manifest diff. Apply is accepted only for the
exact content previously previewed, uses an atomic replacement, and runs the
doctor.

Default assignments:

| Role | Provider | Model | Thinking |
| --- | --- | --- | --- |
| principal | Anthropic | `fable` | `max` |
| mechanic | OpenAI | `gpt-5.6-luna` | `low` |
| implementer | OpenAI | `gpt-5.6-terra` | `medium` |
| plan reviewer | OpenAI | `gpt-5.6-sol` | `ultra` |
| final reviewer | OpenAI | `gpt-5.6-sol` | `ultra` |

These are defaults only. All five may use Anthropic, all five may use OpenAI,
or they may be mixed in either direction.

The plan-review cap is also in the manifest. It accepts one through four
rounds and defaults to two.

## Start and delegate

Start the configured principal in the current repository:

```bash
orrery
```

The launcher also reads an ignored `.orrery.json` in the repository root when
present. It passes provider/model/thinking explicitly:

- Claude: interactive `claude --model … --effort …`
- Codex: interactive `codex --model … -c model_reasoning_effort=…`

The launcher supervises that interactive process. Command presence and login
are checked without inference. A non-zero provider exit can therefore produce
a fallback proposal instead of silently returning to the shell.

Run supporting roles:

```bash
orrery-agent --role mechanic -- "PROMPT"
orrery-agent --role implementer -- "PROMPT"
orrery-agent --role plan-reviewer -- "PROMPT"
orrery-agent --role reviewer -- "PROMPT"
```

Useful options:

```bash
orrery-agent --role reviewer --timeout 600 --output verdict.txt -- "PROMPT"
printf '%s\n' "PROMPT" | orrery-agent --role reviewer
orrery-agent --role reviewer --no-fallback -- "PROMPT"
```

When a provider fails, Orrery displays the nearest potential candidate. In a
terminal, choose from a numbered menu: fall back for this run only, for
every project in this login session, for every project until the
provider-stated reset time (offered only when that time appears in this
failure's diagnostics), or stop. Enter and any unrecognised input stop; `y`
remains an alias for the run-only choice. A non-interactive caller does not
start the candidate; it prints `ORRERY FALLBACK APPROVAL REQUIRED` together
with the candidate, the offerable scopes, and the rerun flags. After the
user agrees, repeat the same command with the exact approval before `--`:

```bash
orrery-agent --role reviewer \
  --approve-fallback anthropic:fable -- "PROMPT"
orrery-agent --role reviewer --approve-fallback anthropic:fable \
  --approval-scope until:2026-08-05T16:49 -- "PROMPT"
```

The flag approves only that provider/model, starts it directly rather than
retrying the process that produced the proposal, and never changes
`global/orchestration.json`. `--approval-scope` defaults to `run`; `session`
and `until:<ISO8601>` record a standing approval, which is prior recorded
consent. A later invocation whose configured role still matches starts the
recorded candidate directly, never re-ranked, and prints a disclosure line
on every use. Session-scope records live under
`$XDG_RUNTIME_DIR/orrery/standing.json` and die with the login session
(stamped with the boot id, or capped at 24 hours where none is readable);
until-scope records live under `${XDG_STATE_HOME:-~/.local/state}/orrery/
standing.json` and expire at their recorded time. Standing approvals are
listed by `orrery-doctor` and on the configuration page, are removed by
`orrery --revoke-fallbacks` (or the page's revoke control), are skipped
when the role's configured identity changes, and are always overridden by
`--no-fallback`, an explicit `--approve-fallback`, and the
changed-workspace inspection rule.

Direct Claude, Codex CLI, and Codex IDE sessions do not pass through the
launcher. Their SessionStart hook compares the active provider/model with the
configured principal. On mismatch it visibly warns the user and tells the
active agent to request explicit approval before tools, edits, or delegation.
Opening the provider surface is not approval. The check refreshes on startup,
resume, clear, and compaction while honoring an unrevoked approval already in
the conversation. SessionStart identifies the model but does not expose the
active thinking level; start through `orrery` when that setting must be
enforced rather than user-verified.

`orrery-review` is a compatibility link to the same runner and defaults to the
`reviewer` role.

The runner prepends a stable role header that makes the process non-principal
and prevents recursive orchestration. Prompts enter over stdin and are held in
a private `0600` runtime file, never in process arguments.

OpenAI receives an explicit model and reasoning override rather than a profile.
Anthropic receives an explicit model and effort plus
`--exclude-dynamic-system-prompt-sections`; noninteractive sessions are not
persisted. Reviews use read-only/plan permissions. Worker roles use
workspace-write.

On systemd systems the delegated process runs in a transient service with:

- `KillMode=control-group`
- a `RuntimeMaxSec` backstop
- a private umask
- a fixed working directory

The wrapper’s timeout fires first so diagnostics can be reported. Cleanup then
stops the whole control group and removes the private prompt, settings, log,
and result. Without systemd the runner uses a new process group and announces
that a detached descendant cannot receive the same uncatchable-death
guarantee.

## Adopt a repository

```bash
orrery-init [model] /path/to/repository
```

When no usable Git worktree exists and no malformed `.git` marker is present,
the command runs `git init` automatically.

Instruction migration:

- neither file exists: create `AGENTS.md` and one-line `CLAUDE.md`;
- `AGENTS.md` exists alone: preserve it and create the Claude import;
- arbitrary `CLAUDE.md` exists alone: preserve it, copy it to `AGENTS.md` for
  Codex, and warn that manual deduplication is needed;
- both exist: preserve both and warn when Claude does not import AGENTS;
- a wrapper exists without its target: restore the project AGENTS template.

The command never replaces arbitrary existing instructions. It removes only
recognised retired Orrery-managed blocks from `CLAUDE.local.md`, preserving
surrounding personal text.

A known model argument writes:

```json
{
  "orchestrator": {
    "provider": "anthropic",
    "model": "fable",
    "thinking": "max"
  }
}
```

to `.orrery.json`. The file and `CLAUDE.local.md` are added to the repository’s
local exclude file when untracked. Existing unrelated JSON keys survive.

## Instruction and caching design

`AGENTS.md` is canonical because Codex discovers it natively. Claude Code does
not read it directly, so `CLAUDE.md` imports `@AGENTS.md`. Project templates use
the same structure.

Keep the global policy stable and project instructions concise. Provider CLIs
manage server-side prompt caching automatically. Orrery puts static
instructions before the changing assignment, avoids model/effort switches
inside a session, and keeps reviewer contexts fresh. There is no cache shared
between providers or models.

Do not add empty `.mcp.json`, Gemini, Cursor, or Copilot instruction files.
Tool-specific MCP, hook, permission, and execution settings belong in their
native configuration only when actually used.

## Leave No Trace

Claude sessions receive lifecycle hooks from
`global/hooks/leave-no-trace.py`. The hook:

- rejects unregistered detached work before it starts;
- attributes session-owned processes conservatively;
- supports bounded leases through `claude-lnt-start`;
- runs registered rollback commands;
- sweeps abandoned automation browser profiles; and
- cleans state at stop, compaction, session end, or watchdog detection.

Use only the Playwright-bundled Chromium, pinned to the version in
`global/AGENTS.md`. Never fall back to snap or system Chromium. Close the
browser and inspect for residue after visual work.

Delegated Anthropic and OpenAI processes have a separate containment and
runtime-state cleanup path in `orrery-agent`. Claude lifecycle cleanup remains
Claude-specific. Principal-mismatch notification is installed for both Claude
and Codex, while both receive the same cleanup requirements through
`AGENTS.md`.

## Provider exhaustion and failures

Fallback has two separate phases: Orrery automatically resolves the candidate;
the user explicitly authorizes whether it runs.

- Missing commands and inactive authentication are detected before inference.
- Account, subscription, quota, billing, entitlement, and unknown provider
  failures exclude that provider.
- Model-specific failures prefer another model on the same authenticated
  provider.
- Recognized transient failures retry once unless a write-capable attempt
  changed the workspace or its unchanged state cannot be verified.
- Candidate ranking minimizes internal role/model distance, prefers models
  already assigned to comparable roles, uses live picker-visible catalogues
  when available, and maps thinking by relative position in each model's exact
  supported levels.
- A model newly exposed by a provider picker participates automatically. The
  bundled catalogue supplies offline seeds and internal distance anchors.
- An approved rerun skips the failed provider/model instead of spending
  another attempt on it.
- If a failed writer changes the Git workspace, Orrery mechanically refuses
  an inline handoff. Inspect the complete working tree before approving the
  separately rerun candidate.
- A missing independent review must be reported.

Authentication and catalogue checks do not spend model tokens, but they cannot
prove remaining credits. This is why the interface says “potential” candidate.
If an approved candidate also fails, Orrery reports it and considers the next
remaining candidate. With neither provider authenticated, no automatic
fallback is possible and that limitation is explicit.

Cross-provider fallback starts fresh context and drops provider-specific
principal CLI arguments. Use `--no-fallback` when the exact configured provider
or model is required.

## Token usage

```bash
orrery-usage --since 7
orrery-usage --json
```

The command reads local provider session logs and never contacts a provider.

## Verification and maintenance

```bash
./tests/run-tests.py
orrery-doctor
```

The suite uses fake provider commands. The doctor makes no model calls.

Do not add every provider release by hand. `orrery-config` discovers new
picker-visible models and thinking levels automatically. Update
`global/model-catalogue.json` only when the offline fallback or an
Orrery-specific default should change. Pinned identifiers remain available
through the custom option and must not be duplicated as aliases in the
fallback menu.

When changing workflow geometry, update `global/orchestration.json`, render the
configuration page at desktop and narrow widths, inspect screenshots, and
verify that paths and labels do not intersect unrelated nodes.

## Repository map

The maintained artefacts are:

- `README.md`, `LICENSE`, and `logo.png` — project overview, licence, and logo.
- `docs/setup-guide.md` — this operational reference.
- `global/AGENTS.md` — canonical shared policy.
- `global/CLAUDE.md` — the exact `@AGENTS.md` Claude import.
- `global/claude-settings.json` — Claude-only hooks, permissions, and companion
  state; it intentionally contains no role model.
- `global/codex-hooks.json` — the Codex SessionStart principal-mismatch hook.
- `global/model-catalogue.json` — provider fallback choices and
  Orrery-specific thinking defaults.
- `global/orchestration.json` — the role manifest, workflow setting, and
  configuration-chart geometry.
- `global/hooks/.gitignore` and `global/hooks/leave-no-trace.py` — lifecycle
  state exclusion and implementation.
- `global/skills/development-orchestrator/SKILL.md` — shared detailed workflow.
- `project-template/AGENTS.md` and `project-template/CLAUDE.md` — repository
  instruction templates.
- `scripts/orrery`, `scripts/orrery_runtime.py`, and
  `scripts/orrery_fallback.py` — supervised principal launch, validated
  provider adapters, availability checks, ranking, and consent.
- `scripts/orrery_standing.py` — standing fallback approvals: scoped,
  revocable prior consent recorded outside the repository.
- `scripts/orrery-session-start` — direct Claude/Codex surface comparison and
  approval notification.
- `scripts/orrery_model_catalogue.py` — no-inference live model and
  thinking-capability discovery.
- `scripts/orrery-review` — the `orrery-agent` runner and compatibility review
  entry point.
- `scripts/orrery-config`, `scripts/orrery-usage`, `scripts/init-project.sh`,
  `scripts/doctor.sh`, and `scripts/install.sh` — configuration, accounting,
  adoption, diagnostics, and installation.
- `scripts/apply-claude-settings.py` and `scripts/install-lnt-hooks.sh` —
  atomic Claude-settings application and lifecycle-link installation.
- `scripts/claude-lnt-start`, `scripts/claude-lnt-register`,
  `scripts/claude-lnt-cleanup`, and `scripts/claude-lnt-status` — explicit
  lifecycle helpers.
- `tests/run-tests.py`, `tests/fake-codex`, and `tests/fake-claude` — offline
  regression suite and provider stand-ins.

## Troubleshooting

**`orrery` says a provider command is unavailable** — install or authenticate
the CLI selected for the principal, approve the proposed candidate, or choose
an available provider in `orrery-config`.

**A direct Codex session does not show the principal mismatch** — restart the
session, inspect `/hooks`, trust the Orrery SessionStart hook, and confirm
`features.hooks` is not disabled in `$CODEX_HOME/config.toml`.

**Codex ignores the installed policy** — inspect
`$CODEX_HOME/AGENTS.override.md`; it shadows `$CODEX_HOME/AGENTS.md`.

**Claude and Codex receive different project instructions** — make
`AGENTS.md` canonical and reduce `CLAUDE.md` to `@AGENTS.md`.

**A delegated run times out** — the runner returns status 124 after stopping
the process tree. Inspect the provider diagnostics printed before cleanup.

**The configuration page refuses Apply** — an external writer changed the
manifest after Preview. Reload and preview the new exact diff.

**The doctor reports stale links** — rerun `./scripts/install.sh`; it is
idempotent and preserves displaced files in a namespaced backup.
