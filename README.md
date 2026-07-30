<p align="center">
  <img src="logo.png" width="240"
       alt="The Órrery logo: a clockwork orrery with a steel-blue central sphere and brass orbit rings carrying small worker spheres">
</p>

# Órrery

**One orchestrator, four workers, no residue.**

Órrery, pronounced *OR-ər-ee* with the stress the accent marks, takes its
name from the orrery: a clockwork model of the solar system, one mechanism
keeping every body in its proper orbit. This kit does the same for AI-assisted
development. It makes **Claude Code the principal engineer** of every
session and the **Codex CLI its specialist crew**, named for their orbits:
a mechanical worker, an implementer, a plan reviewer and a final
reviewer, each a Codex profile named for its job. Around them sits an atomic configuration system, a
cross-model review harness with hard cleanup guarantees, and a session
hygiene layer that reverts everything a session creates.

You describe outcomes in ordinary language. The orchestration decides who
does what, inspects everything itself, and owns the result.

| 104 | 53 | 4 | 2 |
| :---: | :---: | :---: | :---: |
| deterministic regression tests | doctor checks, no model calls | Codex worker profiles | commands to adopt a repository |

## How a request flows

```mermaid
flowchart TB
    U["ordinary request:<br/>'add a --top flag with tests'"] --> C{classify}
    subgraph ROUTES[" "]
      direction LR
      R[read-only sandboxes only] ~~~ K[Claude implements directly]
      K ~~~ L[mechanical worker edits<br/>workspace-write]
      L ~~~ T[implementer writes it<br/>workspace-write]
      T ~~~ P[Claude writes the plan]
    end
    style ROUTES fill:none,stroke:none

    C -->|investigation| R
    C -->|trivial| K[Claude implements directly]
    C -->|mechanical| L[mechanical worker edits<br/>workspace-write]
    C -->|standard| T[implementer writes it<br/>workspace-write]
    C -->|complex| P[Claude writes the plan]
    P --> Q[plan reviewer labels objections<br/>blocking or advisory]
    Q -->|supported blocking,<br/>rounds remain| P
    Q -->|none remain| J[implementer writes it<br/>workspace-write]
    Q -->|repeated objection<br/>or round cap| E[stop before implementation<br/>and ask the user]
    L --> I[Claude inspects the real diff,<br/>never the worker's summary]
    T --> I
    J --> I
    K --> V
    I --> V[tests, lint, type checks, build]
    V --> S[final reviewer, fresh session,<br/>read-only, ephemeral]
    S --> F{findings}
    F -->|verified real| X[Claude reproduces, fixes,<br/>adds regression tests] --> V
    F -->|none survive| D[done: nothing committed<br/>without explicit approval]

    classDef claude fill:#38617f,stroke:#2a4a62,color:#ffffff
    classDef mechanic fill:#98a2b8,stroke:#737d94,color:#1b2226
    classDef implementer fill:#587b50,stroke:#44603e,color:#ffffff
    classDef planreviewer fill:#8a6a9c,stroke:#6d5279,color:#ffffff
    classDef reviewer fill:#b07e28,stroke:#8c641f,color:#ffffff
    classDef quiet fill:#e8ebee,stroke:#b8c0c7,color:#1b2226
    class U,C,R,K,I,X,P claude
    class L mechanic
    class T,J implementer
    class Q planreviewer
    class S reviewer
    class V,F,D,E quiet
```

What each class sounds like, and who touches it:

| Class | Sounds like | Implements | Reviews |
| --- | --- | --- | --- |
| Investigation | "why does this leak?" | nobody: read-only sandboxes only | the final reviewer as a second opinion when it materially helps |
| Trivial | "fix this typo", "bump the timeout" | Claude directly, smallest relevant check | diff inspection only |
| Mechanical | "rename this across the repo" | the mechanical worker, workspace-write | diff inspection |
| Standard | "add a `--top` flag with tests" | the implementer, workspace-write, one bounded task per run | the final reviewer, when logic or regression risk warrants it |
| Complex | auth, migrations, concurrency | Claude plans, bounded plan review clears blocking objections, then the implementer works in batches | the final reviewer, fresh session, findings verified before any fix |

Delegation never transfers responsibility. Codex failure never strands a
task: account-level errors (quota, billing, authentication) fall back to
Claude-only work under the same acceptance criteria, transient errors get
exactly one retry, a worker that dies mid-change has its partial diff
inspected and repaired rather than trusted, and a missing independent
review is reported as a limitation rather than papered over.

Plan review is a bounded challenge, not a search for model agreement. Round
one classifies objections as blocking or advisory; advisory findings never
prolong the loop. Later rounds narrowly confirm whether the original blocking
objections were addressed. The default cap is two rounds, configurable from
one through four. A repeated blocking objection, or one still open at the cap,
stops before implementation and puts both positions to the user.

## The cast

| Role | Codex profile | Model | Thinking | Used for |
| --- | --- | --- | --- | --- |
| Principal orchestrator | — | the active Claude Code model (default Fable 5 through `fable`) | max | classification, planning, inspection, verification, ownership |
| Mechanical worker | `mechanic` | `gpt-5.6-luna` | low | narrow, mechanical, well-specified edits |
| Implementer | `implementer` | `gpt-5.6-terra` | medium | the default worker for substantial implementation |
| Plan reviewer | `plan-reviewer` | `gpt-5.6-sol` | ultra | challenging a plan before any code is written |
| Final reviewer | `reviewer` | `gpt-5.6-sol` | ultra | reviewing finished work, and difficult diagnosis |

Roles and their profile identifiers are named for what they do, so the
configuration remains legible without requiring knowledge of an internal
metaphor.

The plan reviewer and the final reviewer ship with the same model, so
behaviour is unchanged out of the box, but they are separate profiles:
reviewing foundations and reviewing consequences can be priced
differently.

The orchestrator understands the request, inspects the repository,
classifies the task, delegates bounded work with explicit acceptance
criteria, inspects every diff itself, runs verification, and owns the
outcome. Workers receive one bounded task per run, pointed at repository
paths rather than pasted context, and never review their own work.

No role is defined by a model name in workflow logic. Workers live in
`$CODEX_HOME/<profile>.config.toml`; the orchestrator's model lives in
settings. Swapping any of them is an edit, not a rewrite.

## Independent review, engineered

Reviews run through `orrery-review`, which places
`codex --profile reviewer exec` inside a transient systemd user service. That
single decision buys hard guarantees:

- **Nothing survives.** `KillMode=control-group` plus a `RuntimeMaxSec`
  backstop: timeout, Ctrl-C, SIGKILL of the wrapper, even detached
  SIGTERM-immune descendants all end with the whole control group stopped
  and the runtime state reclaimed. A stale run directory left by an
  uncatchable death is swept by the next invocation.
- **The verdict is never lost.** It reaches stdout before publication is
  attempted; an unwritable `--output` destination, or a closed stdout,
  degrades gracefully instead of destroying a completed, paid-for review.
- **Independence is a fact, not a claim.** A fresh ephemeral session per
  review, a read-only sandbox, and a hard refusal to run if the `reviewer`
  profile is missing, because Codex silently substitutes its default model
  for an unknown profile, and a review by an unannounced model is not an
  independent review.

```mermaid
%%{init: {'theme': 'base', 'themeVariables': {
    'actorBkg': '#38617f', 'actorTextColor': '#ffffff',
    'actorBorder': '#2a4a62', 'signalColor': '#5c6a73',
    'signalTextColor': '#3d4850',
    'noteBkgColor': '#f0e2c8', 'noteTextColor': '#5c4a1e',
    'noteBorderColor': '#b07e28',
    'sequenceNumberColor': '#ffffff'}}}%%
sequenceDiagram
    participant O as Orchestrator
    participant W as orrery-review
    participant M as systemd user manager
    participant X as Codex reviewer
    O->>W: review prompt
    W->>M: transient unit, KillMode=control-group,<br/>RuntimeMaxSec backstop
    M->>X: codex --profile reviewer exec --sandbox read-only --ephemeral
    X-->>W: verdict file
    W-->>O: verdict on stdout, then published atomically
    Note over W,M: timeout, Ctrl-C, SIGKILL of the wrapper,<br/>detached SIGTERM-immune descendants:<br/>the whole control group stops, state is reclaimed
```

In the transcript, a review looks like a discreet handover and return:

```text
↳ Codex Sol · gpt-5.6-sol · independent review
  still running · 30s elapsed
  still running · 60s elapsed
↳ Principal orchestrator · control resumed
```

Review findings are advisory, not authoritative: the orchestrator
reproduces each one against the repository before fixing anything, rejects
what it cannot substantiate, and adds a regression test with every fix.

## A real session, excerpted

Verbatim lines from a recorded first run, before the profiles were
renamed for their roles (Terra is now the implementer, Sol the final
reviewer) ("implement a small CLI tool with
tests, get it independently reviewed"), lightly trimmed. Note the middle
passage: the hygiene layer denies the orchestrator's own unregistered
background delegation, which is then relaunched correctly under a lease.
The system polices itself.

```text
↳ Principal orchestrator · Fable 5

● I'll start by loading the development-orchestrator skill, since the
  user-level policy mandates it for implementation work like this.

● Bash(codex --profile implementer exec --sandbox workspace-write --color never
      "<task> Create a small Python command-line tool 'wordfreq' in this
      repository, plus a pytest…)
  ⎿  Error: Unregistered detached process blocked by Leave No Trace.
     Use claude-lnt-start --ttl <seconds> -- <command>, or keep the
     process in this tool call with trap-based cleanup and wait.

● Bash(claude-lnt-start --ttl 1200 -- codex --profile implementer exec
      --sandbox workspace-write --color never "<task> ...")

● Terra completed successfully. Reading its report and inspecting what
  it actually produced.

↳ Codex Sol · gpt-5.6-sol · independent review
  still running · 30s elapsed
  still running · 60s elapsed
↳ Principal orchestrator · control resumed

● ... it correctly caught that print() crashed with a UnicodeEncodeError
  on non-ASCII words under LC_ALL=C — I reproduced that, fixed it, and
  added its suggested tests plus a regression test for the encoding fix.
```

That run ended with eleven passing tests, a review finding reproduced before
being fixed, and nothing committed, because committing needs an explicit
request.

## Leave No Trace

Five layers guarantee a session reverts every process, socket, temporary
file and browser profile it creates, engaging in this order:

| # | Layer | What it does |
| --- | --- | --- |
| 1 | Guard | A PreToolUse hook parses each command the way bash would (quotes, heredocs, substitutions) and denies unregistered detached processes before they start |
| 2 | Leases | `claude-lnt-start --ttl N -- cmd` grants a process the right to outlive its tool call; leases survive turn boundaries and follow daemonising work |
| 3 | Sweeps | After every tool call and at every stop, unleased session-owned processes are terminated and orphaned automation browser profiles reclaimed; pre-existing user data is never guessed about |
| 4 | Watchdog | A detached observer cleans up if Claude itself dies; if its 24-hour term expires with the session alive, it lapses rather than destroying live state |
| 5 | Session end | Final teardown overrides leases, runs registered rollbacks, and removes the session state directory |

## Changing models and providers

Model identity is data. The installed paths are symlinks, so every change
below is live immediately.

| To change | Edit | Then |
| --- | --- | --- |
| A new Claude model | its alias in `global/claude-models.json` | rerun `orrery-init <alias>` where pinned |
| The global default orchestrator | `model` and thinking in `global/claude-settings.json` | `./scripts/apply-claude-settings.py --model --effort` |
| One repository's orchestrator | nothing: run `orrery-init <alias>` there | writes `.claude/settings.local.json`, kept out of Git |
| One session | nothing: `claude --model <name>` or `/model` | ephemeral |
| A Codex worker | `global/codex/<profile>.config.toml` | `orrery-doctor` |
| Plan-review rounds | the plan-review node in `orrery-config`, or `plan_review_rounds.value` in `global/orchestration.json` | new sessions receive the cap at SessionStart |

**Or configure it visually.** `orrery-config` serves a local page
that draws this same flowchart with adjacent model and thinking dropdowns
inside each model-backed node, built from `global/model-catalogue.json`.
An 🛈 appears only where a model or workflow setting has something to
explain: it shows what runs there, which profile and file it comes from,
and how many other steps share it. Several steps run on one model,
so changing any one of them visibly moves the others rather than hiding
the fact. The plan-review node also carries the bounded round control; the
SessionStart hook injects that value into every new orchestrator session, so
it changes behaviour rather than merely changing the drawing. Saving shows
the exact unified diff of every file it would
touch, applies only through the kit's own atomic write paths, and runs the
doctor inline so the result is validated before your eyes. The page is
generated from `global/orchestration.json` and the live files at load, so
it can never drift from reality; it binds to localhost behind a random URL
token and exits when idle. The thinking dropdown is model-dependent and
offers only the levels listed for the selected model; unknown custom models
remain usable but require manual thinking configuration.

**Running on a single provider.** If Codex is unavailable (quota, billing,
network), the orchestrator continues alone automatically, under the same
acceptance criteria, and says so. To make that deliberate rather than a
fallback, state it in the repository's `CLAUDE.local.md` (for example "Do
not use Codex in this repository"); the migration's conflict scan
recognises exactly this class of instruction. The reverse direction, doing
the orchestration on a Codex-first stack, is not supported: Claude Code is
the harness this kit drives.

**Adding a provider.** The pattern that admits a third provider (a Gemini
CLI, a local model server) is the same one Codex uses: a CLI the
orchestrator can call with a bounded prompt, a per-role profile file that
pins model and thinking, a permission rule for the CLI, and a routing entry
in the orchestration skill. The files to touch are
`global/skills/development-orchestrator/SKILL.md` (routing),
`global/CLAUDE.md` (policy), a profile directory alongside `global/codex/`,
`global/claude-settings.json` (permissions), and `scripts/doctor.sh`
(validation). Roles, not providers, are the fixed points.

## Token usage

```bash
orrery-usage --since 7    # last week, per provider and model
orrery-usage --json       # machine-readable
```

Reads the providers' own local session logs (`~/.claude/projects`,
`$CODEX_HOME/sessions`), deduplicates replayed Claude messages, charges
each Codex session its final cumulative count only, and never touches the
network. It reports fresh input, cache reads, cache writes and output per
provider and model. It is an accounting of usage, not of billing.

## Installation

Requires [Claude Code](https://claude.com/claude-code), the
[Codex CLI](https://github.com/openai/codex), Python 3.11+, `git`, `jq`,
and paid access to both model providers.

### Linux (primary platform)

```bash
git clone <remote> ~/src/orrery
cd ~/src/orrery
./scripts/install.sh     # symlinks, hooks, one atomic settings merge
codex login              # once per machine
orrery-doctor      # 53 checks, no model calls
```

`~/.local/bin` must be on `PATH`; the installer warns and the doctor fails
until it is. The installer is idempotent, backs up anything it moves aside
and says where, and preserves every setting it does not own.

### macOS

The same four commands. Differences, all detected automatically:

| Concern | On macOS |
| --- | --- |
| Review containment | plain process group instead of a transient systemd unit: timeout and interrupt still kill the group, but there is no `RuntimeMaxSec` backstop against an uncatchable wrapper death |
| Atomic settings swap | `renamex_np(RENAME_SWAP)` instead of `renameat2(RENAME_EXCHANGE)` |
| Process attribution | no procfs, so hygiene sweeps degrade conservatively: they never kill what they cannot attribute |
| Doctor | systemd checks are skipped with an explanation, not failed |

The macOS code paths follow Apple's platform documentation but have been
validated on Linux only; treat the first macOS install as a supervised one.

### Adopting an existing repository

```bash
orrery-init [model] /path/to/repository
```

If the target is not already inside a Git worktree, `orrery-init`
initializes one first. It is otherwise additive and idempotent: an existing
`CLAUDE.md` is preserved untouched,
the private workflow block is managed between markers in `CLAUDE.local.md`
so reruns refresh rather than duplicate, both stay out of Git via
`.git/info/exclude`, and a conflict scan flags any existing instruction
that contradicts delegation before it can misfire.

## Proving it works

```bash
./tests/run-tests.py     # 104 deterministic tests, stand-in Codex, no credits
orrery-doctor      # ORRERY_READY when everything holds
```

The suite covers the settings updater's compare-and-swap under contention,
the review wrapper's cleanup on every signal and timing, lease semantics
across turn boundaries, the guard's shell parsing, the green-path install
and migration, and the usage tracker's arithmetic. It never touches live
configuration and spends no credits.

## Repository layout

| Path | Purpose |
| --- | --- |
| `global/CLAUDE.md` | The development policy, installed as `~/.claude/CLAUDE.md` |
| `global/claude-settings.json` | Canonical model, hooks, companion state, permissions |
| `global/claude-models.json` | Friendly model aliases for `orrery-init` |
| `global/model-catalogue.json` | Models offered in the configuration dropdowns |
| `global/codex/*.toml` | Worker profiles: model and thinking level per role |
| `global/skills/development-orchestrator/` | Task classification and routing |
| `global/hooks/leave-no-trace.py` | Session hygiene: guard, leases, sweeps, watchdog |
| `scripts/install.sh` | Installs every link and applies canonical settings |
| `scripts/init-project.sh` | `orrery-init`: migrates a repository |
| `scripts/orrery-review` | Synchronous independent review |
| `scripts/orrery-usage` | Token usage across both providers |
| `scripts/orrery-config` | Visual configuration: schematic, diffs, doctor |
| `global/orchestration.json` | Declarative manifest of steps and their config |
| `scripts/apply-claude-settings.py` | Atomic, locked settings updater |
| `scripts/doctor.sh` | `orrery-doctor`: validates the installation |
| `tests/run-tests.py` | The deterministic regression suite |
| `docs/setup-guide.md` | Installation, routine use, maintenance |

Site-specific facts in `global/CLAUDE.md` (port exclusions, snap-browser
notes) are marked as such; edit them for your machine.

## Licence

MIT. The licence covers this kit only: using it requires your own paid
Claude Code and Codex subscriptions, and the model and product names in the
configuration belong to their respective owners.

## Design principles

- **Claude decides and stays accountable.** Workers do bounded work Claude
  has scoped; Claude inspects the result rather than trusting the report.
- **Independence is a fact, not a claim.** Reviews are fresh sessions,
  read-only, by a different provider, through a wrapper that cannot
  misreport which model ran.
- **Fail closed, degrade honestly.** A missing profile refuses; an absent
  syscall refuses; an absent procfs stops attributing rather than
  guessing; every degradation is announced.
- **No version is ever destroyed.** Settings updates are compare-and-swap
  with preserved backups; a session leaves nothing behind it created and
  deletes nothing it cannot prove it created.
