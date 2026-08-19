# CLI runner options

Read this file only before using the bundled Claude Code or Codex CLI adapters.

## Commands

```bash
# Preferred role router
scripts/spawn-agent.sh <lead|architect|builder|dev|qa|reviewer|scribe> \
  <repo-path> "<task|@task-file|->" [output-file]

# Direct adapters
scripts/spawn-codex.sh  <strong|balanced|fast|t1|t2|t3> \
  <repo-path> "<task|@task-file|->" [output-file]
scripts/spawn-claude.sh <strong|balanced|fast|t1|t2|t3> \
  "<task|@task-file|->" [output-file]
```

Use `@path` to load a prompt from a file or `-` to read it from standard input.
This avoids command-line length limits for large tasks.

Each adapter writes the final response to `output-file` and a sibling
`output-file.status` file. Status is `running`, `ok`, `failed:<code>`, or
`dry-run`, followed by provider metadata.

## Environment

| Variable | Meaning |
|---|---|
| `PROVIDER=auto|claude|codex` | Select runner family. `CLI=` remains a compatibility alias. |
| `PRODUCER_PROVIDER=claude|codex` | Make reviewer auto-selection prefer the other family. |
| `PROFILE=strong|balanced|fast` | Override role profile. |
| `TIER=t1|t2|t3` | Compatibility alias for strong/balanced/fast. |
| `MODEL=<name>` | Pin a model. Omit to use the CLI's configured default. |
| `ORCHESTRATE_CODEX_MODEL_STRONG|BALANCED|FAST` | Configure Codex models by profile without editing scripts. |
| `ORCHESTRATE_CLAUDE_MODEL_STRONG|BALANCED|FAST` | Configure Claude models by profile without editing scripts. |
| `EFFORT=<level>` | Override reasoning effort. Defaults: high/medium/low. |
| `CODEX_SANDBOX=<mode>` | Optionally pass a Codex sandbox mode. |
| `CLAUDE_PERMISSION_MODE=<mode>` | Optionally pass a Claude permission mode. |
| `UNSAFE=1` | Explicitly bypass approvals and sandboxing. Never enable implicitly. |
| `DRY_RUN=1` | Validate routing without launching an agent or printing the task. |

The scripts do not pin versioned models. Model availability changes over time, so
use account configuration, profile variables, or `MODEL` for a deliberate,
tested pin. `MODEL` takes precedence over a profile variable.

## Safety

External agents run with the caller's credentials and can mutate the repository.
Review their task scope and permissions before launch. Keep `UNSAFE` off unless the
execution environment supplies a separate trusted sandbox. Never include secrets in
task text or output paths.
