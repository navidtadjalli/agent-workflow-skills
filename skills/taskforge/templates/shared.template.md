# Shared task rules — {{INITIATIVE_SLUG}}

Read these rules before every numbered task.

## Scope and truth

- Perform only the numbered spec. Report out-of-scope discoveries to
  {{CONTRADICTION_SINK}}.
- Resolve disagreement in this order: {{SOT_ORDER}}.
- Preserve production behavior unless the spec explicitly authorizes a behavior
  change.
- Follow repository instructions and surrounding conventions.

## Quality

- Run {{LINT_RULE}} on changed files only.
- Use this verification environment: {{ENV_SETUP}}.
- Add or update verification appropriate to the change. Do not create fake-green
  assertions, hidden skips, or claims without command output.
- Keep the solution as small as the requirement permits.

## Mutation authority

- Control plane: {{CONTROL_PLANE_POLICY}}.
- Commit policy: {{COMMIT_POLICY}}.
- Push policy: {{PUSH_POLICY}}.
- Never touch: {{PROTECTED_TARGETS}}.
- Do not add `.tasks/` to a deliverable unless the control-plane policy explicitly
  says it is tracked.

## State ownership

Never edit `prompts/STATE.md`, `prompts/00-ORCHESTRATOR.md`, or another prompt
while performing a task role. Never mark your own task done or claim that a review
passed. In single mode, return to the lead role after implementation and verification;
in team mode, report to the lead.

Finish with:

```text
STATUS: complete|partial|blocked
SUMMARY: outcome and one line per changed file or artifact
VERIFICATION: exact commands and real results
RISKS: residual risks, decisions needed, or none
```
