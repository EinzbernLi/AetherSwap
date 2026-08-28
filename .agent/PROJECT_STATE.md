# AetherSwap Current Project State

Status: **governance v0.3.5 adopted; LPRL v0.2.3-pilot accepted for Aether repin; product work unchanged**  
State date: **2026-08-28**

This file is a compact current-state entry point, not a transcript. GitHub Issues/PRs remain the durable Task/Result/Review/Acceptance source. When this file lags a newer exact Issue/PR/Lead record, reconcile against the newer durable record rather than treating stale prose as authority.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- Current pre-repin integration baseline: `52d00bca0468cdfb5745f32cae16df4bea301a54`.
- `main` remains the release line and is not the active Auto Offer integration line.
- `integration/auto-buyer-offer -> main` requires explicit OWNER approval.

Do not silently treat `main` and `integration/auto-buyer-offer` as synchronized baselines.

## 2. Governance baseline

Ordinary central governance and LPRL are separately pinned:

- ordinary central governance: `EinzbernLi/agent-dev-governance@a42359b56210b02a66cefd809c5851f53a252590` (`v0.3.5`), unchanged by the LPRL repin;
- Aether v0.3.5 adoption commit: `1b96791cc95fbcafcf9941ed2d3bbbce1a5fb978`;
- canonical Lead Claim sink: `github:EinzbernLi/AetherSwap#149`;
- LPRL profile: `EinzbernLi/agent-dev-governance@d63dae9ac1de65420f410cb36e6c2ccb58cc0478` (`v0.2.3-pilot`), separately pinned;
- central LPRL qualification: Issue `agent-dev-governance#25` completed, PR #26 merge commit `d63dae9ac1de65420f410cb36e6c2ccb58cc0478`, focused Terra PASS `5451292545`, Sol final acceptance `5451454492`.

The LPRL repin updates the Facts representation profile only. It does not move ordinary governance to central `main`, does not introduce a second Task authority, and does not create local lifecycle action authority.

## 3. Current Lead / control state

Current durable control is in Issue #149:

- G12 claim `5450113411` resumed #148 design work under explicit task-scoped authority;
- G13 claim `5451471369` is the current Web Sol Lead claim, parent G12;
- G13 activation verify `5451474266` establishes `ACTIVE` after post-write uniqueness verification and a fresh runtime capability probe;
- current control scope is `task_scoped`, sourced from `github:EinzbernLi/AetherSwap#148@5450111293`;
- current safe work is #148 LPRL design / accepted-profile repin review only.

Current G13 does **not** authorize local materialization, local workspace restructuring, mutation of `F:\AetherSwap\`, Snapshot/Gate/Retirement/Migration/Cleanup/Reclamation, TASK-050 product work, live Steam/BUFF diagnostics, or integration-to-main.

## 4. Primary business work

### TASK-050 / Issue #130

Current status: **BLOCKED**.

Blocking reason: the live `/api/market/steam_trade` field contract required for exact BUFF order -> Trade Offer binding remains unverified.

Current terminal marker:

`TASK050_REPLAN_BLOCKED_PENDING_LIVE_STEAM_TRADE_SCHEMA`

Standing rules:

- do not create a TASK-050 implementation branch;
- do not infer undocumented fields;
- do not run a live BUFF/Steam diagnostic without a new explicit OWNER authorization packet;
- governance/LPRL work does not grant such authorization.

## 5. Product architecture / accepted direction

- Host/source retains purchase selection, order creation, payment and Host Purchase persistence authority.
- Auto Offer is delivery lifecycle after a Host Purchase exists; it must not create a second purchase authority.
- First-send authority is singular and must fail closed on ambiguous/non-idempotent outcomes.
- Worker/runtime recovery is reconciliation/recovery, not a second first-send path.
- Exact identity and lifecycle evidence are required for sensitive delivery/confirmation/reconciliation operations.
- Broad/`accept_all` Steam confirmation authority remains forbidden.

## 6. REAL-WRITE / safety state

`REAL-WRITE GATE: CLOSED`

No standing permission exists for real Steam/BUFF/platform mutations, payment, trade send/accept/confirmation, or destructive local-resource changes. Exact OWNER authorization remains required for the affected action/packet.

No LPRL Snapshot/Gate/Retirement/migration/cleanup/reclamation authority is created by the v0.2.3-pilot repin.

## 7. LPRL / local-resource track

- Issue #143 effective draft fact records remain accepted historical evidence.
- Issue #148 remains the active design track.
- Central LPRL v0.2.3-pilot closed the five representation gaps exposed by #148: non-resource container endpoints, deployment-definition/runtime separation, measurement-basis separation, equality-only observation closure, and Source provenance / controlled-Workspace separation.
- `F:\AetherSwap\` remains an upstream-derived deployed/reference Source location and must not be relabeled or mutated into the controlled `EinzbernLi/AetherSwap` development Workspace.
- A future controlled development Workspace remains a separate subject and requires its own later authorized creation/selection step.
- No `.local/lprl/` materialization, `.gitignore` change, Snapshot, Gate, Retirement, migration, cleanup, or reclaimability action is authorized by this repin.
- Any materialization must be a separate frozen Task after the repin itself is reviewed and accepted.

## 8. Local-development model

- GitHub is the durable Task/Result/Review/Acceptance source.
- Local workspaces/worktrees are execution surfaces, not durable project authority.
- Historical protected-checkout rules remain in `.agent/PROJECT_CONTEXT.md`.
- Local-required tasks use isolated workspaces/worktrees and exact source/tree handoff where required.
- Every new formal Lead session must fresh-probe runtime capability; prior Codex dispatch evidence is not a permanent capability promise.
- Root `AGENTS.md` cold-start guard is routing only and is not a local state or authority store.

## 9. Lead Activation Gate / cold-start order

For new formal Lead/takeover/resume sessions:

```text
runtime-auto-loaded root AGENTS guard
-> GOVERNANCE_LOCK / LOCAL_POLICY / PROJECT_STATE / BOOTSTRAP
-> canonical #149 parent + current control scope
-> next parent-bound claim
-> re-read uniqueness
-> fresh Runtime Capability Probe
-> ACTIVE
-> only then Task recovery / execution
```

Precedence remains:

```text
Lead activation gate
>
Task recovery / execution

current Lead control scope
>
active_task_ref / primary product task
```

Before `ACTIVE`, only control-plane recovery/verification is allowed. If claim persistence, post-write uniqueness verification, or fresh runtime probe cannot complete, use `LEAD_ACTIVATION_BLOCKED` and fail closed.

## 10. Do Not Change without new accepted authority

- Do not weaken REAL-WRITE gates.
- Do not merge integration to main without OWNER approval.
- Do not implement TASK-050 while its live schema blocker remains open.
- Do not blindly retry ambiguous non-idempotent writes.
- Do not publish secrets/credentials/session material.
- Do not clean/reset/delete/move protected local project resources based only on age, task number or folder name.
- Do not perform LPRL materialization/cleanup/migration before a separately authorized future chain exists.
- Do not widen takeover control scope from the natural-language prompt, primary Task ref, `AGENTS.md`, or planning files.

## 11. Next safe actions

1. Independently validate the exact Aether LPRL repin change against central accepted v0.2.3-pilot and current #148 boundaries.
2. If that exact candidate passes Lead acceptance, merge only into `integration/auto-buyer-offer` using the repository merge-commit policy; do not merge integration to `main`.
3. After repin acceptance, #148 may finalize the post-v0.2.3 materialization design and freeze a **separate** implementation Task.
4. Local `.local/lprl/` creation and controlled-workspace creation remain blocked until such a separate Task explicitly authorizes them.
