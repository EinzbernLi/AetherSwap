# AetherSwap Project Bootstrap

This is the minimal cold-start / takeover entry point for a new Lead session. It references durable facts instead of copying project history.

## Project

```yaml
project_name: AetherSwap
project_aliases:
  - Aether
  - AetherSwap
current_phase: auto-offer-integration-plus-governance-continuity
active_development_branch: integration/auto-buyer-offer
primary_product_task_ref: github:EinzbernLi/AetherSwap#130
governance_work_refs:
  - github:EinzbernLi/AetherSwap#147
  - github:EinzbernLi/AetherSwap#149
lprl_work_refs:
  - github:EinzbernLi/AetherSwap#143
  - github:EinzbernLi/AetherSwap#148
governance_ref: EinzbernLi/agent-dev-governance@a42359b56210b02a66cefd809c5851f53a252590
lprl_ref: EinzbernLi/agent-dev-governance@d63dae9ac1de65420f410cb36e6c2ccb58cc0478
lprl_profile: v0.2.3-pilot
task_fact_source: github_issue_pr
lead_claim_sink: github:EinzbernLi/AetherSwap#149
last_stable_checkpoint: null
```

Ordinary governance and LPRL are separately pinned. Do not infer that the LPRL merge moves Aether's ordinary governance pin to central `main`.

## Natural-language takeover

The following kinds of user messages should be sufficient when repository/GitHub access is available:

```text
接管 Aether 的开发，我们继续。
接管 AetherSwap，继续上次开发。
切到 Aether 项目接着做。
```

Interpret them as `TAKEOVER_PROJECT + RESUME_LATEST_DURABLE_STATE`.

Do not ask the OWNER to re-paste development baselines, governance rules, Task IDs, checkpoint IDs or commit SHAs when current durable facts can resolve them. Natural language triggers recovery; it does not widen control scope.

## Root cold-start guard

Aether's root `AGENTS.md` carries the v0.3.5 runtime-auto-loaded cold-start guard for Codex. It exists only to route a new formal Lead/takeover/resume into this Bootstrap and the existing Lead Activation Gate before any planning/progress/test/source/local mutation or Task recovery.

`AGENTS.md` is not a second Task/Result/Lead Claim authority and must not mirror the current generation. Durable control still comes from #149 and the pinned governance facts below.

## Required read / activation order

1. `.agent/GOVERNANCE_LOCK.yaml`
2. `.agent/LOCAL_POLICY.yaml`
3. `.agent/PROJECT_STATE.md`
4. this `.agent/BOOTSTRAP.md`
5. canonical Lead Claim sink `github:EinzbernLi/AetherSwap#149`
6. latest valid parent claim plus its effective current control scope / activation evidence
7. create the next parent-bound claim with inherited or explicitly authorized `control_scope`
8. re-read #149 and verify no sibling/duplicate generation or newer competing claim
9. perform a fresh Runtime Capability Probe
10. only after 5-9 succeed, treat the new Lead as `ACTIVE`
11. then locate/reconcile checkpoint, active Task, latest Result/Acceptance, current branch/commit/PR/CI and task-relevant source/contracts

Do not load the full Issue history or every `.agent/AUTO_OFFER_*` document by default.

Precedence:

```text
Lead activation gate
>
Task recovery / execution

current Lead control scope
>
active_task_ref / primary product task
```

## Pre-ACTIVE boundary

Before the new Lead is `ACTIVE`, only control-plane recovery and verification are allowed.

Do not perform before activation:

- formal Task execution or formal executor dispatch;
- project test runs;
- source edits, project-local planning edits, or other project-local mutation;
- writes to external control-workspace planning/progress files used for this project;
- network/platform/business actions;
- requests for new business/platform execution authorization inferred from a Task that has not yet been recovered within the current control scope.

If #149 cannot be read/written, post-write uniqueness cannot be verified, or the fresh runtime probe cannot complete, use `LEAD_ACTIVATION_BLOCKED` and fail closed. Do not do the Task first and repair the claim later.

## Lead control / scope inheritance

Aether uses Issue #149 as the long-lived canonical Lead Claim sink. Issue #147 contains qualification evidence; it is not the claim sink.

For every new formal Lead session, including Web->Web or Codex->Codex rollover:

1. identify the latest valid claim in #149;
2. recover its effective control scope from the claim or newer explicit durable activation/qualification evidence;
3. create exactly the next generation bound to the exact parent;
4. persist `control_scope` without widening it by inference;
5. re-read #149 and verify uniqueness;
6. perform a fresh runtime probe;
7. only then become `ACTIVE` within that scope.

`control_scope` modes follow central governance v0.3.5: `normal_project_continuation`, `qualification_only`, `task_scoped`, or `blocked`.

A takeover may preserve or narrow scope. It must not widen scope merely because the prompt says “continue development”, because a primary Task exists, or because a planning file mentions work.

If sibling/duplicate claims are found, fail closed before formal dispatch/project mutation and recover only through the next generation with explicit conflict evidence.

## Current durable control reconciliation

The adoption-era G8-G11 samples remain historical qualification evidence, but they are no longer the current parent chain. Do not synthesize, renumber, or replay them as current state.

At this state revision:

- G12 claim `5450113411` resumed Issue #148 under `task_scoped` authority;
- G13 claim `5451471369`, parent G12, is the current Web Lead claim;
- G13 activation verify `5451474266` records uniqueness and the fresh runtime capability probe;
- the effective scope remains sourced from `github:EinzbernLi/AetherSwap#148@5450111293`;
- exact newer #149 evidence always supersedes this compact snapshot if the chain advances again.

## Runtime rule

Every new formal Active Lead session performs a fresh Runtime Capability Probe. Do not infer capability from `web`, `codex`, `desktop`, or a previous session.

The accepted historical subagent pilot proves only capabilities of that tested session. It is calibration evidence, not a permanent capability promise.

## Current blocking / accepted facts

- Primary product Task #130 remains BLOCKED pending verified live `/api/market/steam_trade` field semantics.
- REAL-WRITE remains CLOSED.
- No new Steam/BUFF/network authorization is granted by governance adoption, takeover, or LPRL repin.
- Aether's LPRL module is separately pinned to central accepted v0.2.3-pilot merge `d63dae9ac1de65420f410cb36e6c2ccb58cc0478`.
- Central LPRL Issue #25 is completed; PR #26 passed independent Terra review after one correction and Sol final acceptance.
- Issue #143 effective draft fact records remain accepted historical evidence.
- Issue #148 is the active LPRL design track. The v0.2.3-pilot representation replay closes the prior schema gaps but does **not** authorize local materialization.
- `F:\AetherSwap\` is an upstream-derived deployed/reference Source location, not the controlled `EinzbernLi/AetherSwap` development Workspace. Do not reset/clean/repurpose it into the fork workspace.
- No `.local/lprl/` creation, controlled-workspace creation, Snapshot/Gate/Retirement/migration/cleanup/reclaimability action is authorized by this repin.
- `integration/auto-buyer-offer -> main` remains OWNER-gated.

## LPRL next-step boundary

The accepted v0.2.3-pilot pin makes the #148 design representable. It does not make the design executable by itself.

A future local materialization step requires a **separate frozen Task** that explicitly defines at minimum:

- exact Aether code/ref baseline and isolated/controlled execution surface;
- exact accepted LPRL pin;
- allowed local paths and anchored source-control exclusion;
- accepted historical evidence mapping and no implicit fact refresh;
- no secret/session content;
- draft/null status and digest semantics;
- no invented runtime Deployment;
- no Snapshot/Gate/Retirement/Migration/Cleanup/Reclamation;
- exact Result handoff and independent review.

Until such a Task is accepted, local materialization remains blocked.

## Legacy/domain documents — load only when relevant

- `AGENTS.md`
- `.agent/WORKFLOW.md`
- `.agent/PROJECT_CONTEXT.md`
- `.agent/CURRENT_INVARIANTS.md`
- `.agent/AUTO_OFFER_*.md`
- `.agent/TASK_TEMPLATE.md`, `.agent/REVIEW_CHECKLIST.md`, `.github/pull_request_template.md`

If legacy material conflicts with pinned governance, `LOCAL_POLICY`, effective `PROJECT_STATE`, current Lead scope, or newer exact Task/Result/Acceptance facts, follow current authority precedence rather than combining contradictory rules.

## Completion of bootstrap

A resumed Lead should be able to state, without chat-history replay:

- current development branch and effective code ref;
- current Lead generation/parent and effective control scope;
- whether the Lead Activation Gate passed;
- current runtime capabilities from a fresh probe;
- primary product Task status and standing REAL-WRITE/local-resource boundaries;
- ordinary governance pin and separately pinned LPRL profile/ref;
- the next safe action allowed by the current control scope.
