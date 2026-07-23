---
name: development-orchestrator
description: >
  Automatically orchestrate software implementation, debugging, refactoring,
  testing, and substantial code review using Claude Fable as the principal
  decision-maker and Codex as a specialist worker. Use when the user asks to
  add, implement, fix, debug, refactor, optimize, test, or materially review
  code. Select Luna, Terra, or Sol according to task complexity without
  requiring the user to invoke a workflow command.
user-invocable: false
---

# Automatic Claude-Codex Development Orchestration

Apply this workflow automatically when the user requests software-development
work.

The user should describe the desired outcome normally. Do not require them to
remember or invoke workflow skills.

Claude Fable remains the principal orchestrator. It is responsible for
understanding the request, inspecting the repository, selecting the workflow,
delegating bounded work, evaluating Codex output, inspecting all changes,
running verification, and deciding whether the result is complete.

Do not use the Codex VS Code extension.

Invoke Codex through the installed Codex CLI.

Do not use the `codex:codex-rescue` agent as the normal orchestration mechanism.
It is a one-shot forwarding tool and does not provide the complete adaptive
implementation and review workflow defined here.

## Priority order

Optimize decisions in this order:

1. Correctness and effectiveness.
2. Efficient model and token usage.
3. Execution speed.

Do not invoke Codex merely because it is available.

Do not make Fable and Codex duplicate the same work without a clear benefit.

## Task classification

Classify every development request internally before acting.

### Trivial work

Examples:

- Text, comment, or documentation corrections.
- Obvious one-line fixes.
- Small configuration adjustments.
- Very local changes with negligible regression risk.

Workflow:

1. Fable implements directly.
2. Inspect the diff.
3. Run the smallest relevant verification.
4. Do not invoke Codex unless uncertainty materially justifies it.

### Clear and mechanical work

Examples:

- Repetitive transformations.
- Well-specified boilerplate.
- Narrow test generation.
- Straightforward edits with explicit acceptance criteria.

Use GPT-5.6 Luna with low reasoning when delegation is worthwhile.

### Standard implementation

Examples:

- Contained features.
- Non-trivial bug fixes.
- Focused refactors.
- Multi-file changes with clear architecture.
- New tests around meaningful application logic.

Use GPT-5.6 Terra with medium reasoning as the default implementation worker.

Do not require a separate plan-review cycle for routine, low-risk work.

### Complex or high-risk work

Examples:

- Authentication or authorization.
- Security-sensitive changes.
- Database migrations.
- Concurrency.
- Architectural changes.
- Ambiguous failures spanning multiple systems.
- Large or difficult refactors.
- Changes to public interfaces or persisted data.
- Changes with substantial production risk.

Workflow:

1. Fable investigates the repository.
2. Fable produces an explicit plan with acceptance criteria.
3. A fresh GPT-5.6 Sol run challenges the plan in read-only mode.
4. Fable evaluates the criticism and revises the plan where justified.
5. GPT-5.6 Terra implements the approved plan unless Sol is materially
   justified as the implementer.
6. Fable inspects all changed files and the complete diff.
7. Run the complete relevant verification suite.
8. A fresh GPT-5.6 Sol run reviews the completed uncommitted changes.
9. Fable verifies each finding and corrects legitimate problems.
10. Rerun affected verification.

### Investigation only

When the user asks for diagnosis, research, or explanation without requesting
changes:

- Do not modify files.
- Use a read-only sandbox.
- Use Codex as an independent second opinion only where it materially improves
  the result.
- Separate observed facts, inferences, and unresolved questions.

## Model routing

### GPT-5.6 Luna

Use Luna for:

- Mechanical edits.
- Repetitive transformations.
- Narrow boilerplate.
- Straightforward tests.
- Low-risk work with explicit instructions.

Default reasoning effort: low.

Example invocation:

    codex exec \
      --model gpt-5.6-luna \
      --sandbox workspace-write \
      -c model_reasoning_effort=low \
      --color never \
      "<PROMPT>"

### GPT-5.6 Terra

Use Terra for:

- Standard feature implementation.
- Non-trivial bug fixes.
- Focused refactors.
- Multi-file implementation.
- Test creation involving meaningful logic.
- Most substantial coding work.

Default reasoning effort: medium.

Example invocation:

    codex exec \
      --model gpt-5.6-terra \
      --sandbox workspace-write \
      -c model_reasoning_effort=medium \
      --color never \
      "<PROMPT>"

### GPT-5.6 Sol

Use Sol for:

- Independent plan review.
- Independent final code review.
- Difficult diagnosis.
- Security-sensitive reasoning.
- Architectural analysis.
- Exceptionally difficult implementation where Terra is insufficient.

Default reasoning effort: high.

Example invocation:

    codex exec \
      --model gpt-5.6-sol \
      --sandbox read-only \
      --ephemeral \
      -c model_reasoning_effort=high \
      --color never \
      "<PROMPT>"

Do not use Sol for routine work merely because it is the strongest model.

Do not use maximum reasoning effort by default.

## Permissions

Use `workspace-write` only when Codex is explicitly implementing or correcting
code in the current repository.

Use `read-only` for:

- Planning.
- Plan review.
- Code review.
- Research.
- Diagnosis without requested edits.
- Architecture analysis.

Never use `danger-full-access`.

Do not grant network access automatically.

If implementation requires downloading dependencies, accessing external
services, or modifying files outside the repository, stop and request explicit
authorization.

## Prompt contract

Give Codex one bounded task per run.

For substantial work, structure the prompt using these sections:

    <task>
    State the concrete objective.
    </task>

    <context>
    Identify relevant repository paths, current behavior, architectural
    constraints, and prior decisions.
    </context>

    <acceptance_criteria>
    List observable conditions that define successful completion.
    </acceptance_criteria>

    <constraints>
    Prohibit commits, pushes, releases, unrelated refactors, dependency changes,
    secret exposure, and modifications outside the repository unless explicitly
    authorized.
    </constraints>

    <verification>
    State the tests, checks, builds, or behaviors that must be verified.
    </verification>

    <output_contract>
    Request a concise summary, files changed, verification performed, failures,
    unresolved concerns, and the Codex session identifier when available.
    </output_contract>

Point Codex to repository files and paths instead of pasting large files into
the prompt.

Do not send unrelated conversation history.

Do not ask Codex to make architectural decisions that Fable has not framed.

## Standard workflow

For standard implementation work:

1. Inspect the relevant repository files.
2. Identify concise acceptance criteria.
3. Form a short internal plan.
4. Delegate substantial implementation to Terra.
5. Inspect the actual Git diff.
6. Correct small integration issues directly.
7. Run relevant tests, linting, type checks, and builds.
8. Request a fresh Sol review when the change:
   - contains meaningful logic,
   - spans multiple components,
   - changes an interface,
   - carries regression risk, or
   - is difficult to verify directly.
9. Verify every material review finding against the repository.
10. Correct legitimate findings that remain within the approved scope.
11. Rerun affected verification.

For simple standard work with strong tests and low regression risk, omit the Sol
review when its expected value does not justify its cost.

## Complex workflow

For complex or high-risk work:

1. Inspect architecture, implementation, tests, and relevant history.
2. Define explicit acceptance criteria and failure conditions.
3. Write an implementation plan.
4. Ask a fresh Sol session to challenge the plan.
5. Evaluate the review independently.
6. Revise the plan only where criticism is supported.
7. Delegate implementation to Terra in one coherent run where practical.
8. Split implementation into batches only when necessary for correctness,
   context management, or verification.
9. Inspect every changed file and the complete diff.
10. Run the complete relevant verification suite.
11. Ask a fresh Sol session to review the final uncommitted changes against the
    approved plan and acceptance criteria.
12. Verify every finding.
13. Correct legitimate defects without expanding scope.
14. Rerun verification.

Use no more than two review-and-correction cycles unless further iteration has
a clear and material expected benefit.

## Codex sessions

Prefer one coherent Codex implementation run for a bounded task.

Do not use `--ephemeral` for an implementation thread that may require a
follow-up.

Record the exact Codex session identifier returned by the implementation run.

When a follow-up must continue the same task, resume that exact session:

    codex exec resume <SESSION_ID> "<FOLLOW_UP_PROMPT>"

Never use `resume --last` automatically. Another task may have created the most
recent Codex session.

Use a fresh session for every independent review.

Do not allow the implementation session to review its own work.

Use `--ephemeral` for one-off independent reviews when no follow-up context is
needed.

## Codex Failure and Claude-Only Fallback

Every Codex call must be checked for a successful exit status and usable output.

Codex failure must not trigger repeated retries, uncontrolled model switching,
or abandonment of an ordinary task.

### Account-level unavailability

Treat authentication, subscription, entitlement, billing, quota, and usage
limit errors as account-level failures.

On an account-level failure:

1. Do not retry.
2. Do not try another Codex model.
3. Preserve the current plan and acceptance criteria.
4. Continue implementation with Fable.
5. Perform direct diff inspection and all relevant verification.
6. Record that Codex implementation or independent review was unavailable.

### Model-specific failure

If only the selected model is unavailable, one appropriate alternative Codex
model may be attempted.

Do not try more than one alternative.

Use Fable if the alternative would materially reduce implementation quality,
review independence, or safety.

### Transient failure

Retry a transient process, network, or service failure once.

After the second failure, use Fable and do not call Codex again for that task
unless the user explicitly requests another attempt.

### Partial Codex changes

When Codex fails after modifying the working tree:

1. Inspect `git status`.
2. Inspect the complete diff.
3. Determine which changes are complete, relevant, and correct.
4. Revert or repair incomplete changes.
5. Continue with Fable.
6. Rerun the relevant verification suite.

Never assume that partial Codex output represents a valid implementation.

### Review fallback

When Sol or another independent Codex reviewer is unavailable:

- Fable must review the complete diff against the approved plan and acceptance
  criteria.
- Fable must actively search for regressions, boundary failures, missing tests,
  security issues, and unrelated changes.
- Tests and direct inspection remain mandatory.
- The completion report must state that no independent cross-model review was
  completed.

For high-risk work, pause and ask the user when proceeding without independent
review would leave material unresolved risk.

If the user explicitly asked for Codex participation, report the failure before
substituting Claude-only work.

## Review policy

Codex review findings are advisory, not authoritative.

Fable must:

- Verify each material finding against the repository.
- Reject unsupported, irrelevant, or speculative findings.
- Preserve the distinction between confirmed defects and possible concerns.
- Fix legitimate problems that remain within the approved task scope.
- Avoid adding features merely because a reviewer suggested them.
- Rerun affected tests after corrections.
- Stop after two review cycles unless another cycle has clear expected value.

A review that reports no findings does not replace testing or direct inspection.

## Verification

Before reporting substantial work complete:

- Inspect the complete diff.
- Confirm that every changed line traces to the request or a necessary
  consequence.
- Confirm that the implementation matches the acceptance criteria.
- Run relevant tests.
- Run linting, formatting checks, type checks, and builds where applicable.
- Verify important failure paths and boundary conditions.
- Confirm that no unrelated files were modified.
- Confirm that no credentials, secrets, generated artifacts, or temporary files
  were introduced.
- Review dependency changes explicitly.
- Check `git status` before completion.

Never claim that a command passed unless its successful result was observed.

If verification cannot be performed, state exactly what was not verified and
why.

## Token and execution efficiency

- Do not invoke Codex for trivial tasks.
- Do not use Sol where Luna or Terra is sufficient.
- Do not request plan review for straightforward, low-risk work.
- Keep plans concise unless complexity requires detail.
- Keep Codex prompts bounded and structured.
- Request concise Codex reports.
- Reuse the exact implementation session when follow-up is necessary.
- Use fresh sessions only where independence matters.
- Avoid repeated repository-wide exploration by both models.
- Parallelize only independent, non-conflicting work.
- Prefer targeted checks during implementation.
- Run the full relevant verification suite before completion.
- Do not start automatic, unbounded review gates.

## Safety and scope

Codex and Fable must not independently:

- Commit or push changes.
- Create, delete, or merge branches.
- Create or merge pull requests.
- Tag or publish releases.
- Deploy software.
- Modify production data or infrastructure.
- Expose credentials, tokens, private keys, or `.env` contents.
- Modify files outside the current repository.
- Add, remove, or upgrade dependencies without demonstrated need.
- Refactor unrelated code.
- Change public interfaces or persisted data formats without approval.

These actions require an explicit user request.

## Completion report

For substantial work, report concisely:

- What changed.
- Which work was delegated.
- Which Codex model was used and why.
- Which verification commands ran.
- Whether verification passed.
- Remaining limitations, assumptions, or risks.
- Decisions still requiring user approval.

Do not include unnecessary agent narration or reproduce long Codex reports.
