# AetherSwap Current Project State

Status: **governance v0.3.5 cold-start guard adopted; post-adoption validation pending; product work unchanged**  
State date: **2026-08-28**

This file is a compact current-state entry point, not a transcript. GitHub Issues/PRs remain the durable Task/Result/Review/Acceptance source.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- v0.3.5 adoption source ref: `4aee488af792627be6c50c0880647073a9f67023`.
- That source ref is the accepted Aether v0.3.4 governance adoption commit; no product code has advanced during the post-adoption takeover validation.
- `main` remains the release line and is not the active Auto Offer integration line.
- `integration/auto-buyer-offer -> main` requires explicit OWNER approval.

Do not silently treat `main` and `integration/auto-buyer-offer` as synchronized baselines.

## 2. Governance baseline

This adoption upgrades only the ordinary central-governance pin from v0.3.4 to v0.3.5:

- central governance: `EinzbernLi/agent-dev-governance@a42359b56210b02a66cefd809c5851f53a252590` (`v0.3.5`);
- Aether v0.3.5 adoption commit: `1b96791cc95fbcafcf9941ed2d3bbbce1a5fb978`;
- canonical Lead Claim sink: `github:EinzbernLi/AetherSwap#149`;
- takeover qualification evidence: `github:EinzbernLi/AetherSwap#147`;
- LPRL remains separately pinned and unchanged at `c769bc0b7b102cd12e54fcd966d638fb88a5a2cc` (`v0.2.2`).

v0.3.5 does not change Lead Claim or `control_scope` semantics. It adds one minimal runtime cold-start rule: because Codex already auto-loads the repository-root `AGENTS.md`, Aether places a short non-authoritative guard at the top of that existing file so a new Lead/takeover/resume reaches the existing Lead Activation Gate before planning/progress/test/source/local mutation or Task recovery.

No Agent Bus, session DB, distributed lock, second Task authority, local daemon, automatic launcher, migration, or cleanup mechanism is introduced.

## 3. Current Lead / qualification state

Current durable control chain in #149:

- G8 Codex staging claim `5449198598`: accepted staging PASS;
- G9 Codex claim `5449282726`: control takeover succeeded, qualification `FAIL_CLOSED` because external control-workspace `progress.md` was written before scope recovery;
- G10 Codex claim `5449449417`: latest valid claim, parent G9;
- G10 activation verify `5449453374`: `ACTIVE_CONTROL_ONLY`, unique/monotonic claim, fresh runtime probe recorded, formal continuation not allowed;
- #147 comment `5449455674`: G10 qualification `FAIL_CLOSED` for the same preclaim external planning-file mutation;
- Aether v0.3.4 adoption merged at `4aee488af792627be6c50c0880647073a9f67023`;
- the first fresh post-v0.3.4 Codex takeover created no durable G11; independent Web audit `5449566016` records that post-adoption sample as not qualified.

Because the v0.3.4 sample created no G11, generation did not advance. Do not delete, overwrite, renumber, or synthesize a replacement generation.

Effective next takeover scope after v0.3.5 adoption:

```yaml
control_scope:
  mode: qualification_only
  source_ref: "github:EinzbernLi/AetherSwap#149@5449453374"
next_expected_generation: 11
parent_claim_ref: 5449449417
```

The next sample is exactly one fresh Codex Sol session started by the ordinary natural-language Aether takeover request. Root `AGENTS.md` must route it into governance recovery before any planning/progress mutation. PASS requires G11 from exact parent G10, inherited `qualification_only` scope, post-write uniqueness verification and fresh runtime probe, with zero pre-ACTIVE Task/test/source/local/network action.

Do not replay all historical qualification samples.

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
- Do not widen takeover control scope from the natural-language prompt, primary Task ref, `AGENTS.md`, or planning files.

## 11. Next actions

1. Start one fresh Codex Sol session with only the ordinary natural-language Aether takeover request.
2. Expect G11 with parent G10 `5449449417`, inherited `qualification_only` scope, root-guard-before-planning ordering, post-write uniqueness verification and fresh runtime probe.
3. Web Sol independently reviews #149/#147 evidence. If the v0.3.5 sample passes, close the takeover-repair qualification without replaying historical samples.
4. Only after that review decide whether #148 substantive design work may resume. TASK-050 and REAL-WRITE remain governed by their own separate blockers/authorization rules.
