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

## Installed locations

- Global policy: `~/.claude/CLAUDE.md`
- Orchestration skill:
  `~/.claude/skills/development-orchestrator/SKILL.md`
- Codex profiles:
  - `~/.codex/luna.config.toml`
  - `~/.codex/terra.config.toml`
  - `~/.codex/sol.config.toml`

The Codex VS Code extension is not required. Claude invokes Codex through the
Codex CLI.
