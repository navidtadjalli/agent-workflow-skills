# Task {{NN}} prompt — {{TASK_TITLE}}

**Role:** {{ROLE}} · **Profile:** {{PROFILE}} · **Spec:**
[../{{NN}}-{{TASK_SLUG}}.md](../{{NN}}-{{TASK_SLUG}}.md)

Implement Task {{NN}} for `{{INITIATIVE_SLUG}}`.

## Read in order

1. [_shared.md](./_shared.md)
2. [../{{NN}}-{{TASK_SLUG}}.md](../{{NN}}-{{TASK_SLUG}}.md)
3. [../readme.md](../readme.md)
4. [../acceptance.md](../acceptance.md)

## Execute

- Stay inside the numbered spec and its non-goals.
- Follow mutation authority: {{TASK_MUTATION_POLICY}}.
- Run `{{ACCEPTANCE_SUMMARY}}` plus the task-specific checks.
- Do not edit any file under `prompts/` and do not self-certify completion.

## Report

Return status, changed files or artifacts, exact verification results, and residual
risks. If blocked, identify the concrete external dependency and the smallest next
decision. Leave STATE changes to the lead.
