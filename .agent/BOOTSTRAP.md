# AetherSwap Project Bootstrap

This is the minimal cold-start / takeover entry point for a new Lead session. It intentionally references durable facts instead of copying project history.

## Project

```yaml
project_name: AetherSwap
project_aliases:
  - Aether
  - AetherSwap
current_phase: auto-offer-integration-plus-governance-continuity
active_development_branch: integration/auto-buyer-offer
stable_code_ref_at_adoption: cc87881cc5ea10c7564494a8be89691249cee1fb
primary_product_task_ref: github:EinzbernLi/AetherSwap#130
governance_work_refs:
  - github:EinzbernLi/AetherSwap#147
  - github:EinzbernLi/AetherSwap#149
lprl_work_refs:
  - github:EinzbernLi/AetherSwap#143
  - github:EinzbernLi/AetherSwap#148
governance_ref: EinzbernLi/agent-dev-governance@4beabe24fdda9a48bcaf7dd6e9565604f7278731
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

Do not ask the OWNER to re-paste development baselines, governance rules, Task IDs, checkpoint IDs or commit SHAs when the current durable facts can resolve them.

## Required read order

1. `.agent/GOVERNANCE_LOCK.yaml`
2. `.agent/LOCAL_POLICY.yaml`
3. `.agent/PROJECT_STATE.md`
4. this `.agent/BOOTSTRAP.md`
5. canonical Lead Claim sink `github:EinzbernLi/AetherSwap#149` and its latest comments
6. latest durable state for the relevant active Task/Result/Lead Acceptance in GitHub
7. current branch/commit/PR/CI facts for the work being resumed
8. only the task-relevant legacy/domain documents listed below
9. task-relevant source/tests/contracts

Do not load the full Issue history or every `.agent/AUTO_OFFER_*` document by default.

## Legacy/domain documents — load only when relevant

- `AGENTS.md` — historical project collaboration/safety rules
- `.agent/WORKFLOW.md` — historical task/release workflow
- `.agent/PROJECT_CONTEXT.md` — durable repository/local execution context, including protected checkout rules
- `.agent/CURRENT_INVARIANTS.md` — historical invariant analysis; verify against newer source/Task facts before treating specific old findings as current
- `.agent/AUTO_OFFER_*.md` — subsystem/task evidence and contracts; load only for the affected domain
- `.agent/TASK_TEMPLATE.md`, `.agent/REVIEW_CHECKLIST.md`, `.github/pull_request_template.md` — compatibility workflow material

If a legacy document conflicts with the current pinned governance, `LOCAL_POLICY`, current `PROJECT_STATE`, or a newer exact Task/Result/Acceptance, use the precedence in `LOCAL_POLICY` rather than silently combining contradictory rules.

## Resume reconciliation

A checkpoint is a state summary rather than current truth. Reconcile it against newer:

- `.agent/PROJECT_STATE.md`;
- GitHub Task/Result/Lead Acceptance;
- current Lead Claim sink;
- branch/commit/PR/CI state.

Do not invent a checkpoint merely to satisfy a template.

## Lead control / takeover

Aether uses Issue #149 as the long-lived canonical Lead Claim sink.

For every new formal Lead session, including Web->Web or Codex->Codex rollover:

1. read #149 and identify the latest valid claim;
2. create the next claim with `parent_claim_ref` bound to that exact claim;
3. write the claim;
4. re-read #149;
5. only proceed when there is no sibling claim from the same parent and no competing duplicate generation.

If sibling/duplicate claims are found, fail closed before formal dispatch/project mutation. Recover only by creating the next generation with `conflicting_claim_refs` listing all competing claim refs, then re-read #149 to verify uniqueness.

Issue #147 is qualification evidence, not the long-lived claim sink.

## Runtime rule

Every new formal Active Lead session performs a fresh Runtime Capability Probe before dispatch, even when the runtime name is unchanged from the prior session. Do not infer capability from `web`, `codex`, `desktop`, or from a previous session.

The accepted #144 pilot proves only that one tested Codex Sol session could explicitly native-dispatch `gpt-5.6-terra / high` with isolated child context. It is calibration evidence, not a permanent capability promise.

## Current blocking / accepted facts

- Primary product Task #130 is BLOCKED pending verified live `/api/market/steam_trade` field semantics.
- REAL-WRITE is CLOSED.
- LPRL cleanup/migration is not authorized.
- Issue #143 effective draft fact records are accepted and the fact-record phase is closed.
- Issue #148 is design-only; no `.local/lprl/` files, `.gitignore` change, Snapshot/Gate/Retirement/migration/cleanup or reclaimability authority exists. Further #148 execution is paused while Lead-control conflict recovery and same-runtime qualification are completed.
- Issue #147 retains takeover qualification evidence. A/B/C remain historical PASS samples; the duplicate Web G5 incident is a same-runtime rollover conflict sample that must be reconciled under v0.3.2 before qualification is considered complete.

## Completion of bootstrap

A resumed Lead should be able to state, without chat-history replay:

- the current development branch and effective code ref;
- the primary product Task and why it is blocked or active;
- current governance/LPRL work relevant to the requested continuation;
- the standing REAL-WRITE and local-resource boundaries;
- canonical Lead Claim sink and current generation/parent;
- whether sibling/duplicate claim conflict exists;
- current runtime dispatch capabilities from a fresh probe;
- the next safe action.
