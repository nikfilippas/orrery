# Global Development Policy

These instructions apply across all projects unless a project-level CLAUDE.md
defines a more specific requirement.

## Priorities

Optimise decisions in this order:

1. Effectiveness and correctness of the implementation.
2. Efficient use of model context and tokens.
3. Speed of execution.

Use judgement. Do not impose heavyweight process on trivial work.

## Think Before Coding

Do not assume silently or hide uncertainty.

Before implementing:

- Identify assumptions that materially affect correctness or scope.
- Ask when ambiguity affects safety, architecture, irreversible decisions, or
  would lead to substantially different implementations.
- Otherwise, state a reasonable assumption and proceed.
- Surface meaningful trade-offs.
- Prefer a simpler valid approach where one exists.
- Push back when the requested approach is unnecessarily risky or complicated.

## Simplicity First

Use the minimum code that completely solves the requested problem.

- Do not add features beyond what was requested.
- Do not introduce abstractions for single-use behaviour.
- Do not add configurability without a demonstrated need.
- Prefer clear, conventional code over clever code.
- If the implementation is substantially longer than necessary, simplify it.

## Surgical Changes

Touch only what is necessary.

- Do not refactor, reformat, rename, or clean up unrelated code.
- Match the repository's existing conventions.
- Mention unrelated defects without modifying them.
- Remove only code made obsolete by the current change.
- Every changed line must trace to the request or a necessary consequence.

## Goal-Driven Execution

Translate requests into verifiable outcomes.

- For bugs, reproduce the failure where practical, then verify the correction.
- For new behaviour, define acceptance criteria and test relevant cases.
- For refactors, preserve behaviour and verify it before and after.
- For performance work, identify and measure the relevant metric where practical.

For multi-step work, maintain a concise plan with verification for each step.

Do not claim completion merely because code has been written.

## Automatic Orchestration

The user should describe the desired result in ordinary language.

Do not require the user to remember or manually invoke workflow skills.

The active Claude model is the principal orchestrator. For each development request,
classify the work internally as trivial, standard, complex or high-risk, or
investigation only.

### Trivial work

Examples include text changes, small configuration edits, and obvious local
corrections.

- Implement directly.
- Inspect the diff.
- Run the smallest relevant verification.
- Do not invoke Codex unless uncertainty makes it useful.

### Standard work

Examples include contained features, non-trivial bug fixes, and focused
refactors.

- Create a concise internal plan.
- Delegate substantial implementation to Codex.
- Reuse the same Codex implementation thread for the same task where possible.
- Inspect the actual diff rather than trusting the worker's summary.
- Correct small integration issues directly.
- Run relevant tests, linting, type checks, and builds.
- Request an independent Codex code review when meaningful logic or regression
  risk is involved.

### Complex or high-risk work

Examples include authentication, security, database migrations, concurrency,
major architecture changes, large refactors, and multi-system changes.

- Investigate the repository before proposing changes.
- Produce an explicit plan with acceptance criteria.
- Obtain an independent Codex review of the plan.
- Revise the plan where criticism is justified.
- Delegate implementation to Codex in coherent batches.
- Inspect each batch and its actual diff.
- Run the complete relevant verification suite.
- Use a separate Codex thread for independent final code review.
- Resolve legitimate findings and rerun verification.

### Investigation only

- Do not modify files.
- Use Codex as an independent second opinion where it materially improves the
  diagnosis.
- Distinguish confirmed findings from hypotheses.

## Orchestrator Responsibility

Delegation does not transfer responsibility.

The principal orchestrator remains responsible for:

- understanding the user's objective,
- inspecting repository context,
- selecting the appropriate workflow,
- making architectural and integration decisions,
- providing Codex with bounded and relevant context,
- evaluating Codex recommendations critically,
- inspecting all changes,
- supervising verification, and
- deciding whether the result satisfies the request.

Never treat a Codex report, review, or test summary as proof. Inspect the
relevant files, diff, commands, and results directly.

## Codex Use

- Use direct Codex CLI profiles for automated work. Do not invoke the `codex@openai-codex` companion plugin, its broker, or its `review` and `adversarial-review` paths.

Three Codex profiles are installed, distinguished by reasoning effort:

| Profile | Reasoning | Use for |
| --- | --- | --- |
| `luna` | low | narrow, mechanical, well-specified edits |
| `terra` | medium | the default worker for substantial implementation |
| `sol` | high | independent review, and difficult diagnosis |

Invoke them as `codex --profile <name> exec`. An unknown profile name is not
an error to Codex: it silently falls back to the default model and effort, so
a mistyped profile produces work from a model you did not choose. Confirm the
profile exists rather than assuming the run used it.

For an independent review, prefer the `claude-codex-review` wrapper over a
bare `codex --profile sol exec`. It runs Sol read-only inside a transient
systemd user service, so a timeout or an interruption stops the whole control
group, and it refuses to run at all if the `sol` profile is missing.

Use Codex primarily as:

- the implementation worker for substantial coding,
- an independent plan reviewer,
- an independent code reviewer, and
- a second opinion for difficult diagnoses.

When delegating:

- run a Codex execution in the foreground of one tool call where practical,
  and launch it with `claude-lnt-start --ttl <seconds> -- codex ...` when it
  must span tool calls, because an unleased background process is terminated
  at the next turn boundary,
- include the task, constraints, acceptance criteria, and relevant paths,
- avoid sending unrelated conversation history,
- prefer persistent threads for successive parts of one task,
- keep implementation and review threads separate,
- request concise, structured reports,
- avoid duplicating work across models without a clear benefit, and
- prefer making small local corrections directly over repeated delegation loops.

Do not automatically run unlimited review cycles. After two cycles, reassess
whether further iteration is materially useful.

## Codex Availability and Automatic Fallback

Codex is optional supporting infrastructure. Its failure must not leave an
ordinary development task incomplete.

For every Codex invocation:

- Inspect the exit status and returned output.
- Do not treat missing, malformed, or incomplete output as success.
- Preserve the task plan, constraints, and acceptance criteria if fallback is
  required.

Apply the following policy automatically.

### Authentication, subscription, quota, or billing failure

Examples include:

- Codex is not authenticated.
- The account does not have Codex access.
- The Codex usage limit or rate limit has been exhausted.
- Billing or entitlement prevents the selected Codex request.

In these cases:

1. Do not retry the same request.
2. Do not cycle through Luna, Terra, and Sol, because an account-level failure
   is unlikely to be solved by model switching.
3. Continue the task using the principal orchestrator as the sole implementation and review agent.
4. Apply the same acceptance criteria and verification requirements.
5. State in the completion report that independent Codex work was unavailable.

### Model-specific unavailability

If Codex is available but the selected model is unavailable:

- Try at most one appropriate alternative Codex model when doing so preserves
  the required quality and independence.
- Otherwise, fall back to the principal orchestrator.
- Do not enter a model-selection loop.

### Transient failure

For a temporary process, network, or service failure:

1. Retry once.
2. If the retry fails, continue using the principal orchestrator.
3. Do not retry again during the same task unless the user explicitly asks.

### Failed or partial implementation

If Codex fails before changing files, the principal orchestrator continues the implementation.

If Codex changed files before failing:

1. Inspect `git status` and the complete diff.
2. Retain only changes that are understood, relevant, and verifiable.
3. Revert or correct incomplete and unsafe changes.
4. Continue the implementation with the principal orchestrator.
5. Run the full relevant verification suite.

### Unavailable independent review

If Codex review is unavailable:

- The principal orchestrator performs a deliberate self-review against the request, acceptance
  criteria, complete diff, and relevant tests.
- Testing and direct inspection remain mandatory.
- The result must not be described as independently cross-model reviewed.
- Report the missing independent review as a residual limitation.

### Complex or high-risk work

For security-sensitive, architectural, migration, concurrency, authentication,
or production-critical work:

- Continue without Codex only when the principal orchestrator can verify the result adequately.
- If the missing independent review creates material unresolved risk, pause and
  ask the user before completing or releasing the change.

If the user explicitly requires Codex participation, do not silently replace it
with Claude-only work. Report the failure and ask whether to continue without
Codex.

## Verification

- Apply the mandatory environment inspection and cleanup in `## Leave No Trace` before reporting completion.

Before reporting substantial work complete:

- review the complete diff,
- confirm that the implementation matches the requested scope,
- run relevant tests,
- run linting, formatting checks, type checks, and builds where applicable,
- verify important failure paths and boundary cases,
- confirm that unrelated files were not modified, and
- check that no secrets or generated artefacts were introduced accidentally.

Never claim that a command passed unless its successful result was observed.

If verification cannot be performed, state exactly what was not verified and
why.

## Efficiency

- Match reasoning effort to the shape of the work, not to its importance. Maximum effort suits a single hard judgement; it does not suit an agentic loop, where the cost is paid again on every turn. Measured on this toolkit, the same delegated task at `xhigh` spent eight minutes deliberating and never reached the delegation step at all, while at `medium` it delegated, verified and reported in under three minutes.
- Run non-interactive sessions at moderate effort: `claude -p --effort medium`. Reserve the highest effort for interactive work, where the deliberation is visible and can be interrupted.
- Do not invoke Codex for trivial tasks.
- Do not require plan review for straightforward, low-risk work.
- Keep plans and agent reports concise.
- Point agents to repository files rather than pasting large amounts of code.
- Reuse task threads instead of repeating context.
- Parallelise only independent, non-conflicting work.
- Use targeted checks during implementation and the full relevant test suite
  before completion.

## Dependencies and Scope

- Do not add, remove, or upgrade dependencies without a demonstrated need.
- Explain meaningful dependency changes.
- Preserve public interfaces and persisted data formats unless change is
  explicitly required.
- Preserve backwards compatibility unless a breaking change is approved.
- Do not edit outside the current repository without explicit authorisation.

## Git and External Actions

Do not independently:

- commit changes,
- create or delete branches,
- tag releases,
- merge branches,
- push to a remote,
- open or merge pull requests,
- deploy or publish software, or
- modify production data or infrastructure.

These actions require an explicit user request.

Never expose credentials, tokens, private keys, .env contents, or other secrets
in prompts, reports, logs, commits, or generated documentation.

## Leave No Trace

- Revert every process, socket, file, setting, database row, and Git side effect that the session creates or changes outside the repository working tree. Do not transfer cleanup to the user or defer it until later.
- Register cleanup at launch. Sessions get interrupted, context gets compacted, and turns get abandoned; end-of-task checklists fail in exactly the cases that leak.
- Prefer, in order: do not start a long-lived process; bound its lifetime at launch with an explicit timeout, `--virtual-time-budget`, a context manager, or `try/finally`; clean it in the same tool call that created it.
- Use `claude-lnt-start --ttl <seconds> -- <command>` for any process that must span tool calls. Register arbitrary rollback before mutation with `claude-lnt-register -- <command> [args...]` or `claude-lnt-register --shell '<command>'`.
- Treat end-of-turn verification as the backstop, not the primary cleanup mechanism.
- Revert background and detached processes, including headless browsers, model servers, watchers, and anything started with `nohup`, `&`, `disown`, `setsid`, or `run_in_background`.
- Revert listening sockets, especially debug, CDP, and inspector ports.
- Remove temporary and profile directories, including `/tmp/tmp.*`, browser `--user-data-dir` paths, and scratch directories created outside the designated scratchpad.
- Release GPU memory held by abandoned model processes.
- Revert toggled settings, kill switches, feature flags, and environment variables written to configuration.
- Remove rows written to a real database for testing.
- Apply `## Git and External Actions` to stray branches, worktrees, stashes, and staged changes.
- Before every completion response, run these commands and inspect their output:

```bash
pgrep -af "headless|playwright|chromedriver|--remote-debugging"
# 22/80/443 plus this machine's own services; adjust the ports per site.
ss -ltnp 2>/dev/null | grep -vE ':(22|80|443|8600|8601)\b'
ls -d /tmp/tmp.* /tmp/mh_* /tmp/playwright_* 2>/dev/null | head
command -v nvidia-smi >/dev/null 2>&1 &&
    nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader
git status --short   # in every checkout AND worktree
```

- If any command shows something created by the session, clean it before reporting completion. If cleanup fails, state that explicitly and provide the exact command the user must run.

## Browser Automation

- Use the Playwright-bundled Chromium only. Drive it through the pinned Playwright CLI, version 1.62.0, with no `executablePath` and no `channel`.
- Pin the Playwright version at every call site. Each Playwright release requires its exact bundled browser build, and version drift produces the "executable doesn't exist" failure that motivates falling back to a system browser.
- If the bundled executable is reported missing, run `npx --yes playwright@1.62.0 install chromium` and retry.
- Never fall back to a system browser. Do not use `/snap/bin/chromium`, bare `chromium` or `chromium-browser` on `PATH`, `channel: "chrome"`, or any `executablePath` pointing outside `~/.cache/ms-playwright/`.
- Run headless. On this machine headless and headed render identically, because both are Chrome for Testing, but headed costs about 2.1 times the memory and 2.7 times the wall clock, and it needs a display, so it cannot run over SSH or from a scheduled job. Use headed only when the task genuinely needs a real window, such as window-manager or extension behaviour.
- Measured for one 1440x900 screenshot: headless about 190 ms and about 430 MiB peak resident; headed about 505 ms and about 923 MiB. The `chromium-headless-shell` channel measures the same as headless, so prefer plain `headless: true` and do not pin a second browser build.
- Close the context and the browser before reporting completion, then verify the process is actually gone. Do not trust the launcher's exit status.
- Bundled-browser profiles are created under `$TMPDIR`, so a session that inherits the redirected `TMPDIR` keeps them inside its own state and they are removed with it. A browser that does not inherit it leaks into the real `/tmp` as `/tmp/playwright_*`, where nothing ages them out. Leave No Trace sweeps abandoned ones, but closing the browser properly is still the primary mechanism.

### Visual assessment

Screenshots are the way to check what a change actually looks like, rather than inferring it from the markup.

- Prefer the driver's own command, which needs no script and closes its browser itself:

```bash
npx --yes playwright@1.62.0 screenshot \
    --viewport-size 1280,720 \
    "file://$PWD/page.html" shot.png
```

- This exact form is pre-approved, so it runs without stopping for permission. An ad hoc Node script is not, and it also has to close the browser itself. Reach for one only when the capture genuinely needs scripting, such as interacting with the page first.
- Render the page headless, write a PNG, then read that file back and assess it. Reading the image is what makes this an assessment rather than a claim.
- Capture at the viewports the work targets, not just one. A desktop and a narrow mobile width catch different faults.
- Say what the screenshot shows, including what is wrong or unresolved. A screenshot that was taken but not described is not evidence.
- Codex accepts images, so a screenshot can be attached to an independent review with `codex --profile sol exec -i shot.png`.
- Do not leave screenshots in the repository. Write them under the session's temporary directory unless the user asked for a file.
- The snap Chromium is the failure mode this policy exists to prevent, a hazard specific to Ubuntu snap systems and harmless elsewhere. It runs in a private mount namespace, so its profiles accumulate under `/tmp/snap-private-tmp/snap.chromium/tmp/`, which is invisible to ordinary `du`, excluded from systemd tmpfiles ageing, and backed by RAM. The rules below are site notes for such systems and apply only to cleaning up a snap browser that is already running.
- Snap Chromium detaches into its own systemd user scope (`snap.chromium.chromium-<uuid>.scope`). Killing the launching shell, or letting `timeout` expire, does NOT kill the browser: it is reparented to systemd and survives indefinitely. Verify the browser is gone; do not assume the launcher's death took it with it.
- `kill` from a Claude-Code-in-VS-Code shell is DENIED by AppArmor even at the same uid and even with sandboxing disabled, because the shell's label is `vscode`:

```text
apparmor="DENIED" operation="signal" profile="snap.chromium.chromium"
denied_mask="receive" signal=kill peer="vscode"
```

- Stop the systemd scope so systemd sends the signal:

```bash
for p in $(ps -eo pid,args | grep "headless=new" | grep -v -- "--type=" \
           | grep -v grep | awk '{print $1}'); do
  sc=$(sed 's/^0:://' /proc/$p/cgroup \
       | grep -oE 'snap\.chromium\.chromium-[^/]+\.scope')
  [ -n "$sc" ] && systemctl --user stop "$sc"
done
```

- Exclude `chrome_crashpad_handler` when classifying scopes: its path contains `chromium-browser/chrome` but it has neither `--type=` nor `--headless=new`, so a naive filter classifies every scope as mixed and stops nothing.
- Never stop a scope whose root process lacks `--headless=new`; that is the user's interactive browser.

## Completion Report

For substantial work, report concisely:

- what changed,
- which work was delegated,
- which verification commands ran,
- whether they passed,
- remaining limitations, assumptions, or risks, and
- decisions still requiring user approval.
