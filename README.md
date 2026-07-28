# Claude-Codex Development Kit

Reusable configuration for Claude Code with:

- The active Claude model as the principal orchestrator.
- GPT-5.6 Terra as the default substantial implementation worker.
- GPT-5.6 Sol for difficult reasoning and independent review.
- GPT-5.6 Luna for narrow and mechanical work.
- Automatic task routing from ordinary natural-language requests.
- Conservative Git, dependency, and release safeguards.

## Automatic fallback

If Codex is unavailable because of authentication, subscription, entitlement,
quota, or billing, the principal orchestrator continues the task as the sole implementation agent.
Transient Codex failures receive one retry. High-risk tasks pause for user
input when the absence of independent cross-model review leaves material risk.

## Independent review

Automated reviews run directly through the Codex CLI. The
`codex@openai-codex` companion plugin, its broker, and its `review` and
`adversarial-review` paths are disabled and must not be reintroduced.

```bash
claude-codex-review --timeout 600 --output verdict.txt -- "REVIEW PROMPT"
```

The wrapper runs `codex --profile sol exec` read-only inside a transient
systemd user service, so a timeout, an interruption, or an uncatchable death
of the wrapper still stops the whole Codex control group.

## Installed locations

- Global policy: `~/.claude/CLAUDE.md`
- Claude user settings: `~/.claude/settings.json`
- Orchestration skill:
  `~/.claude/skills/development-orchestrator/SKILL.md`
- Leave No Trace hook: `~/.claude/hooks/leave-no-trace.py`
- Codex profiles:
  - `$CODEX_HOME/luna.config.toml`
  - `$CODEX_HOME/terra.config.toml`
  - `$CODEX_HOME/sol.config.toml`

`CODEX_HOME` defaults to `~/.codex`. An explicitly empty value is rejected.

## Commands

- `claude-codex-init /path/to/repository` migrates a repository safely.
- `claude-codex-doctor` validates the installation without model calls.
- `claude-codex-review` runs a synchronous independent Sol review.
- `claude-lnt-start`, `claude-lnt-register`, `claude-lnt-cleanup`, and
  `claude-lnt-status` manage Leave No Trace session state.
- `scripts/apply-claude-settings.py --all` applies the canonical model,
  companion state, and hooks in one locked, atomic transaction while
  preserving unrelated live settings.

## Documentation

`docs/setup-guide.md` covers installation, routine use, changing models,
validation, browsers, troubleshooting and maintenance.

## Tests

```bash
./tests/run-tests.py            # everything
./tests/run-tests.py CODEX_HOME # only tests whose name contains the token
```

The suite is deterministic, uses a stand-in for the Codex CLI, spends no
credits, and never touches the live Claude or Codex configuration.

The Codex VS Code extension is not required. Claude invokes Codex through the
Codex CLI.
