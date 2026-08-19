# {{INITIATIVE_TITLE}}

{{OVERVIEW}}

## Goal

{{GOAL}}

## Execution

- Mode: **{{EXECUTION_MODE}}**
- Runner: **{{RUNNER_KIND}}**
- Review: {{REVIEW_POLICY}}
- Control plane: {{CONTROL_PLANE_POLICY}}
- Commit policy: {{COMMIT_POLICY}}
- Push policy: {{PUSH_POLICY}}
- Protected targets: {{PROTECTED_TARGETS}}

Any capable agent may lead. A single agent executes roles sequentially; team mode uses
authorized independent workers through the Orchestrate contract.

## Phases

{{PHASE_LIST}}

Phase rule: {{PHASE_GATE_RULE}}.

## Source-of-truth order

{{SOT_ORDER}}. Send unresolved contradictions to {{CONTRADICTION_SINK}}.

## Run or resume

1. Work from `{{REPO_PATH}}`.
2. Read [prompts/00-ORCHESTRATOR.md](prompts/00-ORCHESTRATOR.md).
3. Verify and resume from [prompts/STATE.md](prompts/STATE.md).
4. Continue until acceptance is complete or a real external blocker remains.

No particular vendor, model, fresh session, background runner, or wake tool is
required.

## Verification environment

{{ENV_SETUP}}

## Assumptions

{{ASSUMPTIONS}}
