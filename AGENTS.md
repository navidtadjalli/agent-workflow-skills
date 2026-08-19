# Repository instructions

- Keep both skills valid under the open Agent Skills format.
- Keep provider-specific details out of core workflow rules.
- Preserve single-agent mode as a complete path, not a degraded placeholder.
- Keep unsafe permission bypass opt-in only.
- Preserve root compatibility shims unless a migration path replaces them.
- Run `./tests/run.sh` after every change.
- Do not pin ephemeral model versions in skill instructions or runner defaults.
