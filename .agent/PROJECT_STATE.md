# AetherSwap Current Project State

Status: **governance v0.3.4 adoption; post-adoption validation pending; product work unchanged**  
State date: **2026-08-28**

This file is a compact current-state entry point, not a transcript. GitHub Issues/PRs remain the durable Task/Result/Review/Acceptance source.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- v0.3.4 adoption source ref: `180324fc67836fefb3a487ee5ea9dcf6b522c394`.
- That source ref is the accepted v0.3.2 governance adoption commit; no product code has advanced during the G8/G9/G10 takeover qualification work.
- `main` remains the release line and is not the active Auto Offer integration line.
- `integration/auto-buyer-offer -> main` requires explicit OWNER approval.

Do not silently treat `main` and `integration/auto-buyer-offer` as synchronized baselines.

## 2. Governance baseline

This adoption upgrades only the ordinary central-governance pin from Aether v0.3.2 to central governance v0.3.4:

- central governance: `EinzbernLi/agent-dev-governance@d000f0768a6e8dad27c8f893165791b67815ff24` (`v0.3.4`);
- canonical Lead Claim sink: `github:EinzbernLi/AetherSwap#149`;
- takeover qualification evidence: `github:EinzbernLi/AetherSwap#147`;
- LPRL remains separately pinned and unchanged at `c769bc0b7b102cd12e54fcd966d638fb88a5a2cc` (`v0.2.2`).

The v0.3.4 repair is deliberately narrow: Lead activation must complete before Task recovery/execution, and control scope must be inherited without widening by inference. No Agent Bus, session DB, distributed lock, second Task authority, local daemon, automatic migration, or automatic cleanup is introduced.

## 3. Current Lead / qualification state

Current durable control chain in #149:

- G8 Codex staging claim `5449198598`: accepted staging PASS;
- G9 Codex claim `5449282726`: control takeover succeeded, qualification `FAIL_CLOSED` because external control-workspace `progress.md` was written before scope recovery;
- G10 Codex claim `5449449417`: latest valid claim, parent G9;
- G10 activation verify `5449453374`: `ACTIVE_CONTROL_ONLY`, unique/monotonic claim, fresh runtime probe recorded, formal continuation not allowed;
- #147 comment `5449455674`: G10 qualification `FAIL_CLOSED` for the same preclaim external planning-file mutation.

The repeated G9/G10 failure is retained as evidence. Do not delete, overwrite, renumber, or synthesize replacement generations.

Effective next takeover scope after v0.3.4 adoption:

```yaml
control_scope:
  mode: qualification_only
  source_ref: "github:EinzbernLi/AetherSwap#149@5449453374"
next_expected_generation: 11
parent_claim_ref: 5449449417
```

The #147 G10 FAIL_CLOSED record `5449455674` is corroborating qualification evidence for that inherited boundary; it is not a second scope authority.

The first post-adoption validation is one fresh Codex session started by ordinary natural language. It must complete the v0.3.4 Lead Activation Gate before any local/planning mutation and then stop for independent review. Do not replay all historical qualification samples.

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
- takeover qualification or governance adoption does not grant such authorization.

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

No LPRL Snapshot/Gate/Retirement/migration/cleanup/reclamation authority is created by this governance adoption.

## 7. LPRL / local-resource track

- Issue #143 effective draft fact records remain accepted.
- Issue #148 remains design-only and substantively paused while takeover repair is being validated.
- No `.local/lprl/` materialization, `.gitignore` change, Snapshot, Gate, Retirement, migration, cleanup, or reclaimability authority exists from this adoption.
- The LPRL module pin is intentionally unchanged.

## 8. Local-development model

- GitHub is the durable Task/Result/Review/Acceptance source.
- Local workspaces/worktrees are execution surfaces, not durable project authority.
- Historical protected-checkout rules remain in `.agent/PROJECT_CONTEXT.md`.
- Local-required tasks use isolated workspaces/worktrees and exact source/tree handoff where required.
- Every new formal Lead session must fresh-probe runtime capability; prior Codex dispatch evidence is not a permanent capability promise.

## 9. Lead Activation Gate

For projects using #149 durable Lead Claims, v0.3.4 precedence is:

```text
Lead activation gate
>
Task recovery / execution

current Lead control scope
>
active_task_ref / primary product task
```

Before `ACTIVE`, only control-plane recovery/verification is allowed. No formal Task execution/dispatch, tests, source/project-local planning mutation, external control-workspace planning/progress mutation for Aether, network/platform/business action, or premature authorization request may occur.

If claim persistence, post-write uniqueness verification, or fresh runtime probe cannot complete, use `LEAD_ACTIVATION_BLOCKED` and fail closed.

## 10. Do Not Change without new accepted authority

- Do not weaken REAL-WRITE gates.
- Do not merge integration to main without OWNER approval.
- Do not implement TASK-050 while its live schema blocker remains open.
- Do not blindly retry ambiguous non-idempotent writes.
- Do not publish secrets/credentials/session material.
- Do not clean/reset/delete/move protected local project resources based only on age, task number or folder name.
- Do not perform LPRL cleanup/migration before a separately authorized future chain exists.
- Do not widen takeover control scope from the natural-language prompt, primary Task ref, or planning files.

## 11. Next actions

1. After the v0.3.4 adoption is merged to `integration/auto-buyer-offer`, start one fresh Codex Sol session with only the ordinary natural-language Aether takeover request.
2. Expect G11 with parent G10 `5449449417`, inherited `qualification_only` scope, claim-before-task ordering, post-write uniqueness verification and fresh runtime probe, with zero pre-ACTIVE local/planning mutation.
3. Web Sol independently reviews #149/#147 evidence. If the v0.3.4 sample passes, close the takeover-repair qualification without replaying all historical samples.
4. Only after that review decide whether #148 substantive design work may resume. TASK-050 and REAL-WRITE remain governed by their own separate blockers/authorization rules.
