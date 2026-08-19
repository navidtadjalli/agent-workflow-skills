# Shared acceptance — {{INITIATIVE_SLUG}}

Apply these checks to every task in addition to its numbered spec.

## Correctness

- The result satisfies the stated outcome and scenarios.
- Edge and error paths are covered proportionally to risk.
- No unrelated behavior changes or fake-green verification are present.

## Verification

Run:

```bash
{{SHARED_ACCEPTANCE_COMMANDS}}
```

Environment: {{ENV_SETUP}}.

Determinism requirement: {{DETERMINISM_RULE}}.

## Security and safety

- No secrets, unsafe interpolation, injection path, permission expansion, or
  unrequested external call is introduced.
- Additional constraints: {{SECURITY_NOTES}}.

## Scope and policy

- Changed files or artifacts belong to the task.
- {{LINT_RULE}} is clean on changed files.
- Control-plane policy: {{CONTROL_PLANE_POLICY}}.
- Commit policy: {{COMMIT_POLICY}}.
- Push policy: {{PUSH_POLICY}}.
- Protected targets remain untouched: {{PROTECTED_TARGETS}}.

## Review

- Apply {{REVIEW_POLICY}}.
- Inspect actual output; worker or implementer summaries are not proof.
- Label same-context review `self-review`; claim independent review only when a
  separate reviewer actually inspected the result.
