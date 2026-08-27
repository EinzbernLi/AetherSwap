# AetherSwap Project Bootstrap

This is the minimal cold-start / takeover entry point for a new Lead session. It intentionally references durable facts instead of copying project history.

## Project

```yaml
project_name: AetherSwap
project_aliases:
  - Aether
  - AetherSwap
current_phase: auto-offer-integration-plus-governance-adoption
active_development_branch: integration/auto-buyer-offer
stable_code_ref_at_adoption: cc87881cc5ea10c7564494a8be89691249cee1fb
primary_product_task_ref: github:EinzbernLi/AetherSwap#130
governance_work_refs:
  - github:EinzbernLi/AetherSwap#143
  - github:EinzbernLi/AetherSwap#144
  - github:EinzbernLi/AetherSwap#145
governance_ref: EinzbernLi/agent-dev-governance@eda77469c90ac0153bffdb7a5fc7ad8917bb552f
lprl_ref: EinzbernLi/agent-dev-governance@c769bc0b7b102cd12e54fcd966d638fb88a5a2cc
task_fact_source: github_issue_pr
last_stable_checkpoint: null
lead_claim_ref: null
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
4. latest durable state for the relevant active Task/Result/Lead Acceptance in GitHub
5. current branch/commit/PR/CI facts for the work being resumed
6. only the task-relevant legacy/domain documents listed below
7. task-relevant source/tests/contracts

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

A checkpoint, if later created, is a state summary rather than current truth. Reconcile it against newer:

- `.agent/PROJECT_STATE.md`;
- GitHub Task/Result/Lead Acceptance;
- branch/commit/PR/CI state.

Current adoption starts with `last_stable_checkpoint: null`; do not invent one merely to satisfy a template.

## Lead control / takeover

A cross-runtime or cross-formal-session takeover may create the next monotonic Lead Claim generation using the project's existing GitHub Issue/PR fact source. Do not create a second file-native claim authority unless the project explicitly changes fact-source mode.

Before formal dispatch or project mutation, a returning/stale Lead must verify that a newer durable Lead generation has not superseded it.

## Runtime rule

Every new Active Lead session performs a fresh Runtime Capability Probe before dispatch. Do not infer capability from `web`, `codex`, `desktop`, or from a previous session.

## Current blocking facts at adoption

- Primary product Task #130 is BLOCKED pending verified live `/api/market/steam_trade` field semantics.
- REAL-WRITE is CLOSED.
- LPRL cleanup/migration is not authorized.
- Issue #144 was launched under the previous dispatch baseline and must finish before this adoption PR is merged.

## Completion of bootstrap

A resumed Lead should be able to state, without chat-history replay:

- the current development branch and effective code ref;
- the primary product Task and why it is blocked or active;
- current governance/LPRL work relevant to the requested continuation;
- the standing REAL-WRITE and local-resource boundaries;
- whether a newer checkpoint/Lead Claim exists;
- current runtime dispatch capabilities;
- the next safe action.
