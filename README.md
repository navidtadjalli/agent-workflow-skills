# Agent Workflow Skills

Portable `taskforge`, `orchestrate`, and `dispatch` skills for planning,
executing, and queueing complex work with either one capable agent or an
optional team.

## What changed

- One agent can run either workflow end to end.
- Team mode is optional and uses native subagents first, then Claude Code or Codex
  CLI adapters when explicitly selected.
- Skill instructions are provider-neutral and follow the open Agent Skills layout.
- Models are not pinned to versioned names; runner scripts use each CLI's configured
  default unless `MODEL` is set.
- External runners inherit normal permissions. Full permission bypass requires the
  explicit `UNSAFE=1` opt-in.
- Taskforge state is resumable without pretending every host supports heartbeats or
  self-wake.
- Dispatch queues headless work behind a usage governor, so a run that outlives the
  plan window winds down at a clean seam and resumes after the reset instead of
  dying mid-edit. Its daemon and systemd unit are never installed or started for
  you; see `skills/dispatch/references/operations.md`.

## Layout

```text
skills/
├── dispatch/
├── orchestrate/
└── taskforge/
install.sh
tests/run.sh
```

## Install

Preview changes:

```bash
./install.sh --all --dry-run
```

Install symlinks for Claude Code and Agent Skills-compatible hosts such as Codex:

```bash
./install.sh --all
```

Existing destinations are never overwritten. Use `--force` to move conflicting
copies into a timestamped backup under
`${XDG_STATE_HOME:-~/.local/state}/agent-workflow-skills/backups/` before linking.

Discovery paths:

- Claude Code: `~/.claude/skills/{taskforge,orchestrate,dispatch}`
- Codex and other open Agent Skills hosts: `~/.agents/skills/{taskforge,orchestrate,dispatch}`

## Use

- Claude Code: `/taskforge ...`, `/orchestrate ...`, or `/dispatch ...`
- Codex: `$taskforge ...`, `$orchestrate ...`, or `$dispatch ...`
- Other compatible agents: load the corresponding `SKILL.md`.

Run validation:

```bash
./tests/run.sh
```

No installer command creates commits, configures a remote, or pushes changes.
