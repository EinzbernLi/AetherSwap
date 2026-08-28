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
governance_upgrade_source_ref: integration/auto-buyer-offer@4aee488af792627be6c50c0880647073a9f67023
primary_product_task_ref: github:EinzbernLi/AetherSwap#130
governance_work_refs:
  - github:EinzbernLi/AetherSwap#147
  - github:EinzbernLi/AetherSwap#149
lprl_work_refs:
  - github:EinzbernLi/AetherSwap#143
  - github:EinzbernLi/AetherSwap#148
governance_ref: EinzbernLi/agent-dev-governance@a42359b56210b02a66cefd809c5851f53a252590
lprl_ref: EinzbernLi/agent-dev-governance@c769bc0b7b102cd12e54fcd966d638fb88a5a2cc
task_fact_source: github_issue_pr
lead_claim_sink: github:EinzbernLi/AetherSwap#149
last_stable_checkpoint: null
```

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

Aether uses central governance v0.3.5. The Lead Activation Gate semantics remain the v0.3.4 parent-bound activation model; v0.3.5 hardens only the runtime cold-start entry into that gate.

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

## v0.3.5 cold-start compatibility

The durable control chain was created before v0.3.5. Do not rewrite, delete, renumber, or synthesize replacement claims merely to adopt the root cold-start guard.

At this adoption point:

- G10 claim `5449449417` remains the latest valid parent in #149;
- G10 activation verify `5449453374` establishes `ACTIVE_CONTROL_ONLY` and `formal_continuation_allowed_under_current_scope: false`;
- #147 comment `5449455674` records the G10 v0.3.2 qualification `FAIL_CLOSED` for preclaim external `progress.md` mutation;
- Aether adopted v0.3.4 at `4aee488af792627be6c50c0880647073a9f67023`;
- the first fresh post-v0.3.4 Codex sample created no durable G11; independent Web audit is #147 comment `5449566016`;
- because no G11 claim was created, generation does not advance: the next monotonic generation is still G11 with parent `5449449417`;
- the effective inherited scope remains `qualification_only`, sourced from G10 activation evidence `5449453374`;
- the first v0.3.5 validation sample must be exactly one fresh ordinary natural-language Codex takeover with the root `AGENTS.md` guard active.

Historical G8 PASS and G9/G10 failures remain evidence. The no-G11 v0.3.4 sample is also retained as a negative cold-start sample. Do not replay all historical qualification cycles.

## Runtime rule

Every new formal Active Lead session performs a fresh Runtime Capability Probe. Do not infer capability from `web`, `codex`, `desktop`, or a previous session.

The accepted #144 pilot proves only that one tested Codex Sol session could native-dispatch a child with isolated context. It is calibration evidence, not a permanent capability promise.

## Current blocking / accepted facts

- Primary product Task #130 remains BLOCKED pending verified live `/api/market/steam_trade` field semantics.
- REAL-WRITE remains CLOSED.
- No new Steam/BUFF/network authorization is granted by governance adoption or takeover qualification.
- LPRL cleanup/migration is not authorized; the LPRL module remains separately pinned to `c769bc0b7b102cd12e54fcd966d638fb88a5a2cc`.
- Issue #143 effective draft fact records remain accepted.
- Issue #148 remains design-only and substantively paused during the takeover repair; no `.local/lprl/` materialization, Snapshot/Gate/Retirement/migration/cleanup/reclaimability authority is created here.
- Issue #147 remains the qualification track. The next required evidence is one clean v0.3.5 fresh Codex takeover from G10 to G11, followed by independent Web review.
- `integration/auto-buyer-offer -> main` remains OWNER-gated.

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
- the next safe action allowed by the current control scope.
