# AetherSwap Current Project State

Status: **brownfield governance adoption candidate**  
State date: **2026-08-27**  
Adoption tracking: **Issue #145**

This file is the compact current-state entry point. It is not a transcript of historical development. Task contracts, Results, Reviews, CI evidence and Lead Acceptance remain in GitHub Issues/PRs.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`
- Current accepted integration ref at adoption freeze: `cc87881cc5ea10c7564494a8be89691249cee1fb`
- Latest accepted product change at that ref: TASK-053 / Issue #135 / PR #136, disabling the legacy Steam bulk listing confirmer fail-closed.
- `main` currently points to `b26ff06cd1d89452523c5246414dca472c75df7a` and is not the active Auto Offer integration line.
- `integration/auto-buyer-offer -> main` requires explicit OWNER approval.

Do not silently treat `main` and `integration/auto-buyer-offer` as synchronized baselines.

## 2. Current product architecture / accepted direction

- Host/source retains purchase selection, order creation, payment and Host Purchase persistence authority.
- Auto Offer is a delivery-lifecycle module after a Host Purchase exists; it must not create a second purchase scheduler or competing purchase authority.
- First-send authority must be singular and fail closed on ambiguous/non-idempotent outcomes.
- Worker/runtime recovery is for reconciliation/recovery, not a second first-send path.
- Exact identity and lifecycle evidence are required for sensitive delivery/confirmation/reconciliation operations.
- Legacy broad Steam listing confirmation authority was removed in TASK-053; any future automatic listing confirmation requires a new exact-identity, TLS-verified, single-mutation, RESULT_UNKNOWN/no-blind-retry design and separate validation.

Long-form historical/domain evidence remains in `AGENTS.md`, `.agent/WORKFLOW.md`, `.agent/PROJECT_CONTEXT.md`, `.agent/CURRENT_INVARIANTS.md` and `.agent/AUTO_OFFER_*.md`.

## 3. Primary business work

### TASK-050 / Issue #130

Current status: **BLOCKED**.

Blocking reason: the live `/api/market/steam_trade` field contract required for exact BUFF order -> Trade Offer binding has not been verified. The previous implementation whitelist is revoked.

Current terminal marker:

`TASK050_REPLAN_BLOCKED_PENDING_LIVE_STEAM_TRADE_SCHEMA`

Standing rule: do not create a TASK-050 implementation branch, do not infer undocumented fields, and do not run a live BUFF/Steam diagnostic without a new explicit OWNER authorization packet.

## 4. Governance / local-resource tracks

These tracks may progress independently of the blocked business task when they do not mutate the same runtime/state authority.

- Issue #141 — LPRL controlled-migration read-only inspection/evidence. No cleanup or migration authority.
- Issue #143 — draft project-local LPRL fact records. Persistent project-local fact files are not yet accepted/materialized.
- Issue #144 — Codex native subagent dispatch pilot. **Open at the time of this adoption draft** and bound to the pre-v0.3.1 pilot governance baseline.
- Issue #145 — this brownfield governance adoption.

LPRL authority for the ongoing Aether evidence remains pinned to `EinzbernLi/agent-dev-governance@c769bc0b7b102cd12e54fcd966d638fb88a5a2cc` until separately upgraded/replayed.

## 5. REAL-WRITE / safety state

`REAL-WRITE GATE: CLOSED`

No standing permission exists for real Steam/BUFF/platform mutations, payment, trade send/accept/confirmation, or destructive local-resource changes. Exact OWNER authorization is required for the affected action/packet.

No LPRL Snapshot/Gate/Retirement/migration/cleanup/reclamation authority exists from the current governance work.

## 6. Local-development model

- GitHub is the durable Task/Result/Review/Acceptance source.
- Local workspaces/worktrees are execution surfaces, not durable project authority.
- Historical protected checkout and verifier details are retained in `.agent/PROJECT_CONTEXT.md` until a later reviewed compaction replaces them.
- Local-required tasks use isolated workspaces/worktrees and exact source/tree handoff when necessary.
- Local resource discovery has identified mixed Source/Workspace/State/Data/Evidence/Cache and shared Git topology; these remain under LPRL fail-closed handling rather than folder-name cleanup heuristics.

## 7. Governance migration status

This adoption **adds a minimal authority layer; it does not delete legacy governance**.

New authority entry points:

- `.agent/GOVERNANCE_LOCK.yaml`
- `.agent/LOCAL_POLICY.yaml`
- `.agent/PROJECT_STATE.md`
- `.agent/BOOTSTRAP.md`

Legacy governance/domain documents remain referenced compatibility evidence in this phase. Later retirement/compaction requires a separate review proving that no current project-specific invariant or operational boundary would be lost.

## 8. Do Not Change without a new accepted Task / explicit gate

- Do not weaken REAL-WRITE gates.
- Do not merge integration to main without OWNER approval.
- Do not implement TASK-050 while its live schema blocker remains open.
- Do not reintroduce broad/`accept_all` Steam confirmation authority.
- Do not blindly retry ambiguous non-idempotent writes.
- Do not publish secrets/credentials/session material.
- Do not clean/reset/delete/move protected local project resources based only on age, task number or folder name.
- Do not perform LPRL cleanup/migration before the required future Snapshot/Gate/authorization chain exists.

## 9. Next actions

1. Let Issue #144 reach a terminal pilot result under its original frozen dispatch baseline.
2. Validate the effective LPRL draft fact records in Issue #143 and obtain Sol acceptance before any project-local LPRL materialization.
3. Review this governance-adoption PR after #144 closes; only then consider merging it to `integration/auto-buyer-offer`.
4. After adoption merge, run the v0.3.1 natural-language Web <-> Codex takeover qualification (normal takeover, stale checkpoint reconciliation, stale Lead generation).
5. Keep TASK-050 blocked until a separately authorized sanitized live schema capture closes its exact contract gap.
6. After the governance/takeover path is stable, perform a separate reviewed compaction of legacy `.agent` documents; do not combine that cleanup with initial adoption.
