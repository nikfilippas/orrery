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

Delegated budgets are progress-aware. A role's `timeout_seconds` is its
base budget; an optional `hard_timeout_seconds` (30 to 14400, at least
the base; the implementer ships with 1800 and both reviewers with 3600)
lets a run whose merged output grew within the last three minutes extend
in two-minute steps up to the cap. `codex exec` streams its work
continuously, so growth is a live progress signal; delegated Claude
runs print only their final JSON and therefore keep plain base-deadline
behaviour. A stalled, silent, or capped run times out exactly as
before, `--hard-timeout` and `ORRERY_AGENT_HARD_TIMEOUT_SECONDS`
override per run, and an explicitly chosen `--timeout` without a hard
cap remains its own bound. The runner echoes the delegate's newest
output line at most every two minutes, sanitised so provider text can
never forge Orrery's consent markers or smuggle terminal escapes into
the transcript; extensions record a `budget-extended` incident, which
deliberately surfaces in the doctor's seven-day summary as
budget-tuning evidence.

The manifest's top-level `verbosity` steers how delegated roles write:
`1` (the default) injects a terse plain-prose report style into every
`ORRERY ROLE HANDOFF`, `2` a milder concise line, and `3` nothing.
`ORRERY_VERBOSITY` overrides it for one run. The dial shapes delegated
prompts only; principal and direct sessions take the static
communication-style rule in `global/AGENTS.md`.

## Watch and capture a delegated run

```bash
orrery-agent --role reviewer --log review.log -- "review the final diff"
orrery-agent --role reviewer --stream -- "..."      # uncapped mirror
orrery-agent --role reviewer --no-stream -- "..."   # counters only
```

The delegate's own output is mirrored to stderr as it arrives, prefixed
with `|` so it is visibly the delegate's rather than Orrery's, and put
through the same sanitiser as the diagnostics tail so it cannot forge a
consent marker or inject terminal escapes. By default each burst is
capped at a few lines and says how many it withheld, because a review
can emit hundreds of kilobytes; `--stream` lifts the cap and
`--no-stream` leaves only the byte counters.

`--log PATH` publishes the complete working transcript, atomically and
on every path including timeout and interruption, which is exactly when
it is most useful. Note the provider asymmetry: `codex exec` streams its
work, so an OpenAI role's log holds the real transcript of tool calls
and messages, while a delegated Claude role currently runs
`--print --output-format json` and produces only its final result
object. The log is untrusted provider output written verbatim, so treat
it as data; it never enters the incident log, which deliberately stores
no provider text.

## Align the principal's own surface

```bash
orrery-sync
orrery-sync --check
```

Most sessions start from a provider's IDE extension or bare CLI, which
Orrery never launches. `scripts/orrery-sync` therefore projects the
configured principal onto that provider's own configuration: `model`,
the thinking level, and the same-provider fallback ladder into
`~/.claude/settings.json` for an Anthropic principal, or `model` and
`model_reasoning_effort` into `$CODEX_HOME/config.toml` for an OpenAI
one. Only the principal's own surface is written; the other must keep
reporting a genuine mismatch.

It runs automatically from `orrery-init`, from `scripts/install.sh`,
and after a successful configuration apply. Those files are
machine-wide, so one adoption aligns every repository. A repository's
`.orrery.json` principal override is deliberately ignored here: it is
correct for that directory only. A principal routed at a custom
endpoint is refused, because first-party model names must not reach a
third-party service.

Adoption trust uses only the private, untracked `.orrery.json` marker at the
repository root. It must be a user-owned regular file with mode `0600`, with a
machine-local trust record in the user state store. The developer umask made
markers created before this change group-writable, so they are refused. For one
release, `orrery-doctor` warns existing users to re-run `orrery-init` and names
the command for that repository. Revoke a repository with `orrery-init --forget
/path/to/repository`.

What the ladder actually covers was measured against the installed CLI
rather than inferred, and the distinction matters: an **overloaded**
service (HTTP 529) is retried a few times and then answered by
substituting the next model in `fallbackModel`, while a **rate or usage
limit** (HTTP 429) is retried against the configured model and never
substituted. Exhausting a plan therefore still requires a deliberate
model change; the ladder rescues a busy service, not an empty
allowance. `tests/run-tests.py` pins both behaviours against a loopback
stub, so a change in the CLI surfaces as a failure rather than as a
false promise.

Thinking is written in the one representation Claude accepts: `max`
travels in `env.CLAUDE_CODE_EFFORT_LEVEL`, `low` through `xhigh` in
`effortLevel`, never both. The Codex rewrite is fail-closed: it parses
the file before and after, changes only the two root assignments, and
writes nothing when they cannot be located unambiguously, so comments
and `[projects.*]` trust entries survive untouched.

`--check` reports drift and exits non-zero without writing; the doctor
uses it and warns rather than failing, because a deliberate in-session
model change is legitimate and Orrery reports it instead of fighting
it.

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
- `ReadOnlyPaths` for read-only roles on the workspace, on the common git
  directory behind it, and on every worktree of the same repository,
  enforced by the kernel rather than by tool rules alone. The mapping is
  explicit rather than implied by absence from the write allowlist,
  because `/tmp`, `/var/tmp` and the provider's own directory are granted
  for the provider CLIs' sake and a repository inside one of them would
  otherwise be writable: a delegate refused its own workspace could still
  rewrite refs, or plant a hook that runs on your next git command.
  Measured, because the deeper mapping wins and the direction is not
  symmetric: a read-only mapping beats a grant containing it and a grant
  naming the same path, but a grant nested *inside* it beats the mapping,
  so `--receipts` pointed inside a reviewed tree is refused rather than
  composed.
- Read-only mappings on the ancestors of those paths, up to the first one
  whose own parent is not writable. A mapping protects a directory's
  contents and makes it a mount point, which cannot be renamed, but says
  nothing about its ancestors: with a repository two levels under `/tmp`,
  a delegate refused every write inside its workspace could otherwise
  rename the workspace's parent and recreate the path with a tree of its
  own. A workspace directly under `/tmp` needs no such mapping, because
  `/tmp` itself cannot be renamed.
- A probe unit composed of exactly those properties, run before the
  delegate, which writes inside the run directory and attempts a write
  into every path being claimed read-only, where a write that succeeds is
  the finding. Every path is resolved first, so the mappings follow the
  real directories rather than the names used to reach them: a symlink
  that merely points at the workspace is not itself protected.
  Where the allowlist cannot be enforced at all,
  every role is refused; where only the workspace mapping fails, read-only
  roles are refused and workers are unaffected. `ORRERY_ALLOW_UNCONFINED=1`
  accepts a degraded run explicitly, and a read-only run that proceeds
  without the guarantee is fingerprinted and inspected as a writer is.

The wrapper’s timeout fires first so diagnostics can be reported. Cleanup then
stops the whole control group and removes the private prompt, settings, log,
and result. Without systemd the runner uses a new process group and announces
that a detached descendant cannot receive the same uncatchable-death
guarantee.

The delegated-run behaviour above was validated against a specific Claude
CLI version, recorded as `VALIDATED_CLAUDE_CLI` in
`scripts/orrery_runtime.py`. After a CLI update the doctor warns until you
re-validate: in a scratch repository under `/home/<user>`, run

```bash
orrery-agent --role implementer --timeout 300 -- \
    "Using the Bash tool, run: echo PROBE-OK. Quote the output, or the
verbatim error if the tool fails."
```

A `PROBE-OK` reply means delegated shell execution still works; update the
recorded version. A bwrap error means the CLI changed behaviour again; keep
the old baseline and investigate before trusting delegated verification.

Delegated Claude runs deliberately do not use the Claude CLI's own
bubblewrap isolation (the bash sandbox and `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`
alike): on CLI 2.1.220 its ancestor-configuration hiding walks past `$HOME`
into root-owned directories where the bind mount point cannot be created
(`bwrap: Can't create file at /home/.mcp.json`), which kills every shell
command in any repository under `/home/<user>`. Containment comes from the
allowlisted service environment, the transient unit, and `ReadOnlyPaths`
instead. If an aborted sandboxed run from another source leaves the CLI's
zero-byte trap files behind (`.env*`, `package.json`, lockfiles, an empty
`node_modules`, plus `.git/info/exclude` entries), the runner sweeps the
residue after the unit stops and reports what it removed.

## Route a role at another model service

A role's `provider` names the CLI that runs it. An optional `endpoint`
names where that CLI sends its requests, so a role can run on Kimi,
DeepSeek, GLM, Qwen, MiniMax, OpenRouter, a local Ollama, or anything else
speaking one of the two wire protocols. Pick one in `orrery-config`; the
page writes both halves of the manifest:

```json
"endpoints": {
  "kimi": {
    "label": "Kimi (Moonshot AI)",
    "adapter": "anthropic",
    "base_url": "https://api.moonshot.ai/anthropic",
    "key_env": "MOONSHOT_API_KEY"
  }
},
"steps": [
  { "id": "implementer", "provider": "anthropic", "model": "kimi-k3[1m]",
    "endpoint": "kimi", "...": "..." }
]
```

- `adapter: anthropic` uses the Claude CLI against an Anthropic Messages
  compatible base URL. Orrery sets `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_AUTH_TOKEN` from `key_env`, and an empty `ANTHROPIC_API_KEY`
  so a first-party key elsewhere cannot silently take over. The routing
  applies to that role's process only, so first-party and third-party
  roles run side by side.
- `adapter: openai` uses the Codex CLI, configured entirely through
  `-c model_providers.<id>.*` overrides on the command line, with
  `wire_api = "responses"`. Codex dropped chat/completions, so a service
  offering only that wire format cannot be driven this way. Mistral is
  the notable example; reach it through a Responses-compatible gateway or
  through OpenRouter instead.
- **Keys are never stored in the manifest**, only the name of the
  environment variable holding one. A missing variable stops the run with
  a named error rather than falling back to a first-party account. A
  local endpoint may set `key_env` to `null`.
- Plain `http://` is refused except on localhost, and a URL may not embed
  credentials.
- Third-party models are not in the first-party catalogue, so Orrery
  cannot validate their thinking levels: the level is left to the
  endpoint's own default, and the doctor reports each routed role and
  whether its credential variable is set.

`global/endpoints.json` holds the presets the configuration page offers,
each with the provider documentation it was taken from. Editing that file
only changes what is offered; a role is routed only when its manifest
entry says so.

## Adopt a repository

```bash
orrery-init [model] /path/to/repository
```

When no usable Git worktree exists and no malformed `.git` marker is present,
the command runs `git init` automatically.

Adoption is what switches the orchestration layer on. The command always
leaves a `.orrery.json` at the repository root (kept out of Git through
`info/exclude`), and both the global policy and the SessionStart check treat
that marker as the gate. In a repository without it, a direct Claude or Codex
session announces "repository not adopted" and behaves as a normal
single-provider session: the model is whatever the interface has selected,
and only Part I of the global policy, the engineering baseline, applies.
Sessions started through `orrery` or `orrery-agent` carry the orchestration
layer with them regardless, so adoption is never required just to run the
launchers.

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

## Task control plane

An adopted repository keeps its task-control state in `.orrery/`:
`contracts/` holds sealed contracts, `ledger/` the append-only transitions,
`dispatch/` receipts and patches, `evidence/` verification packets, `counter`
the next identifier, and `lock` the control lock. Task worktrees are outside
the repository at `$XDG_STATE_HOME/orrery/worktrees` (default
`~/.local/state/orrery/worktrees`).

Create a contract without `task_id`; the command allocates it and seals the
stored copy. A minimal contract is:

```json
{
  "title": "Rename one symbol",
  "goal": "Replace the old symbol in the parser.",
  "acceptance_criteria": [{"id": "tests", "statement": "Tests pass.", "verification": {"command": "pytest -q"}}],
  "scope": {"include": ["parser"], "exclude": []},
  "risk": {"level": "low", "reasons": []},
  "assigned_role": "implementer",
  "target_ref": "refs/heads/main"
}
```

Run `orrery-task create contract.json`, then `orrery-task run T-1`. The
runner works in the dedicated branch worktree, records receipts, commits a
verified change, and writes an evidence packet. `verify` reruns the recorded
criteria; `merge` is explicit; `close` finishes a merged task. A dirty base
needs `--allow-dirty-baseline`; retrying an altered task worktree needs
`--accept-changed-worktree`. These flags are forwarded only with explicit
user consent.

`NO_CHANGE` is a real outcome and needs `close --accept-no-change`. Changes
outside scope remain visible in evidence and require
`merge --accept-out-of-scope`. If a controller dies, `resume` completes a
receipt-backed dispatch, marks a receiptless dead dispatch interrupted, and
leaves a live one alone. Merge refuses the wrong target branch, a dirty or
moved target, a task branch that differs from evidence, a changed contract
digest, and unaccepted out-of-scope changes.

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

Accepted residual: delegate confinement closes what a hostile repository can
write, not what it can read. With the provider CLI sandbox disabled and no
hook-suppression flag, repository hooks running inside a delegate can read the
provider credentials that `HOME` and `CODEX_HOME` expose and exfiltrate them
over an unconfined network. Closing it needs upstream hook suppression or
unit-level egress control, both currently unavailable. Delegate confinement is
not complete while this remains.

## Provider exhaustion and failures

Fallback has two separate phases: Orrery automatically resolves the candidate;
the user explicitly authorizes whether it runs.

- Missing commands and inactive authentication are detected before inference.
- Account, subscription, quota, billing, entitlement, and unknown provider
  failures exclude that provider.
- Model-specific failures, including timeouts and limits announced for one
  model, exclude only that model: providers often limit one model rather
  than the account, so the ladder walks the same provider's nearest models
  first. With the bundled tiers, an unavailable `fable` proposes `opus`,
  and a `gpt-5.6-sol` timeout proposes `gpt-5.6-terra`.
- A same-provider candidate two or more capability tiers away ranks behind
  a near-tier model on the other provider, so a large capability drop
  crosses providers instead.
- Recognized transient failures retry once unless a write-capable attempt
  changed the workspace or its unchanged state cannot be verified. Budget
  timeouts are not retried: a second identical budget would double the
  wall-clock for a likely deterministic overrun.
- Candidate ranking minimizes internal role/model distance, prefers models
  already assigned to comparable roles, uses live picker-visible catalogues
  when available, and maps thinking by relative position in each model's exact
  supported levels.
- A model newly exposed by a provider picker participates automatically. The
  bundled catalogue supplies offline seeds and internal distance anchors.
- An approved rerun starts the exact identity named by
  `--approve-fallback`, accepted from anywhere in the current ranking, and
  excludes every nearer candidate as already ruled out. A non-interactive
  ladder therefore stays approvable rung by rung: after `gpt-5.6-sol` and
  `gpt-5.6-terra` both failed in separate invocations,
  `--approve-fallback openai:gpt-5.5` starts `gpt-5.5` directly.
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

## Incident log

Everything that goes wrong in a launcher - blockers, timeouts, fallback
proposals and their outcomes, consent stops, standing-approval events,
degraded containment, interrupted runs, cleanup failures, reclaimed
runtime state - is appended by `scripts/orrery_incidents.py` as one JSON
line to `$XDG_STATE_HOME/orrery/incidents.jsonl` (default
`~/.local/state/orrery/`), beside the standing-approval store. Events
carry structured identities, statuses, and wrapper-authored reasons
only; prompts, verdicts, provider diagnostics, and credentials are never
stored. The store rotates to `incidents.jsonl.1` past 1 MiB, and writing
is best-effort: a failure warns once and can never change a run's
outcome. The session-start hook records principal mismatches and
surfaces that report no model.

```bash
orrery-incidents --since 7
orrery-incidents --json
```

`scripts/orrery-incidents` aggregates events by kind, role, and model
with a recent tail, and the doctor warns when anything was recorded in
the last 7 days. Use the counts to tune defaults in
`global/orchestration.json` with evidence: a role `timeout_seconds` that
keeps expiring, a model that repeatedly hits per-model limits, or a
surface that never verifies its principal.

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

- `README.md`, `LICENSE`, `logo.svg`, `banner.svg`, `flowchart.svg`, and
  `prompt-layers.svg` —
  project overview, licence, logo, the banner the README opens with, and
  the flowchart it shows. All three drawings are vector: the flowchart is
  generated from `global/orchestration.json` rather than drawn by hand, and
  the mark and banner are drawn rather than exported, so they stay sharp at
  any size and cost 56KB between them. The banner embeds the mark, so the
  two cannot disagree.
- `docs/setup-guide.md` — this operational reference.
- `docs/.nojekyll` — serves this directory verbatim on GitHub Pages.
  Pages runs Jekyll by default, which would try to render Liquid tags in
  the exported page; it has none today, but the page is generated and a
  later edit should not be able to break the published copy silently.
- `docs/config-demo.html` — the configuration page as a standalone file,
  written by `orrery-config --export`. It has no server behind it, so the
  model menus are frozen at the moment it was written and preview and
  apply say so rather than failing quietly. Regenerate it when the chart
  or the page changes; GitHub Pages serves it from this directory.
- `prompt-layers.svg` — what a session and a delegate each actually
  receive, generated from this kit's own files so the line counts cannot
  drift from the instructions they describe.
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
- `scripts/orrery_incidents.py` and `scripts/orrery-incidents` — the
  incident log writer/reader module and its reporting command.
- `scripts/orrery_ledger.py` and `scripts/orrery-task` — the durable task
  ledger, contracts, and state machine, and the task command surface
  (Phase 1 control plane).
- `scripts/orrery-sync` — projects the configured principal onto its
  provider's own configuration for IDE and bare-CLI sessions.
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
- `tests/run-like-ci.sh` — the suite under the ways a CI runner differs from
  a developer machine: `TMPDIR` at `/tmp`, no `init.defaultBranch`, and no
  provider CLI on `PATH`. Every one of those has hidden a real defect behind
  a green local run. It does not simulate the runner's restricted user
  namespaces; the script says why, and confinement behaviour has to be judged
  on a real runner.
- `.github/workflows/ci.yml` — lint and suite on push and pull request. The
  suite needs a systemd user manager, so the job enables lingering before it
  runs. A hosted runner restricts unprivileged user namespaces, so it accepts
  the sandbox and silently drops it; the wrapper measures that and refuses to
  run, and the suite therefore opts in with `ORRERY_ALLOW_UNCONFINED=1` on any
  host that cannot enforce, exactly as an operator there would have to. The
  confinement assertions skip themselves on such a host rather than pretending,
  so CI proves everything except the containment itself.

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

**A delegated run times out** — the runner first extends a run whose
output is still growing, up to the role's `hard_timeout_seconds`, then
returns status 124 after stopping the process tree. Inspect the provider
diagnostics printed before cleanup. The proposal that follows stays on
the same provider's nearest model; recurring expiries or
`budget-extended` events in `orrery-incidents` argue for larger role
budgets.

**The hook says the principal is interface-asserted** — some surfaces
(the VS Code extension among them) omit the model from their
SessionStart payload, so the principal match rests on the interface's
model picker instead of an Orrery check. This is a property of the
surface, expected and not a failure; the session states it in one line,
verifies the thinking level from the environment where possible, and
terminal sessions started with `orrery` stay silent because the
launcher pins the model itself.

**The configuration page refuses Apply** — an external writer changed the
manifest after Preview. Reload and preview the new exact diff.

**The doctor reports stale links** — rerun `./scripts/install.sh`; it is
idempotent and preserves displaced files in a namespaced backup.
