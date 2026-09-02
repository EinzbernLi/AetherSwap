# AetherSwap Current Project State

Status: **ordinary governance 0.3.18 adoption candidate; G20 remains canonical Project Lead; TASK-083/TASK-084 complete; TASK-082 implementation complete on its isolated branch and not merged; REAL-WRITE CLOSED**
State date: **2026-09-03**

This file is a compact recovery entrypoint. Newer exact GitHub Task/Result/Review/Acceptance, branch, CI, and Lead-sink facts supersede this convenience summary.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- TASK-085 frozen base: commit `1e5005e8688a5ef67cd992e1123cd0e12cd40947`, tree `37a966a53e2b9256c33fcc07bdc11f8a948c45de`.
- The current integration head must always be re-read before authority-bearing work; a later material head does not silently re-anchor a running Task.
- `integration/auto-buyer-offer -> main` remains explicitly OWNER-gated.

## 2. Governance and local policy

- Ordinary governance candidate pin: `EinzbernLi/agent-dev-governance` `0.3.18@aff6ff205eca64c594cc10b67a3454a765076bb0`.
- Update mode remains `manual_pinned`: no proactive update workflow, no follow-main, no automatic adoption or pin advance.
- `.agent/LOCAL_POLICY.yaml` remains the project-specific stricter layer for Owner gates, REAL-WRITE, product safety, concurrency, verification, calibration, and local-resource boundaries.
- Project-local calibration remains enabled as a downstream-local derived routing cache only; it is not Task/Result/Acceptance authority and is not exported upstream.
- LPRL remains independently pinned exactly to `v0.2.4-pilot@5f22e63414374b64ebbf4bd91601ede2f54e6f65`; ordinary-governance adoption does not repin or reinterpret it.
- No accepted LPRL Control Snapshot/Gate/Retirement/Migration authority exists for this phase, and no local-resource mutation is authorized by governance adoption.

## 3. Project Lead continuity

- Canonical Lead sink: `github:EinzbernLi/AetherSwap#149`.
- Current durable control is G20: claim `#149@5502768640`, activation verification `#149@5502770266`.
- #149 is authoritative over this convenience prose. A bounded Worker/Validator Task does not transfer Project Lead authority.

## 4. Current work

- TASK-083/#234 + PR #235: completed slimming pass.
- TASK-084/#236 + PR #238: completed second slimming pass.
- TASK-082/#233 exact implementation revision `#233@5510226684` has been executed by a separate bounded implementation worker on `task/082-steamauto-parity`; its implementation result is `#233@5511772049`. It is not merged into `integration/auto-buyer-offer` at this state.
- TASK-085/#240 remains governance/control-plane adoption only. This Worker does not modify, rebase, dispatch, merge, or otherwise absorb TASK-082 product work.

## 5. Safety and authorization

- `REAL-WRITE: CLOSED`.
- Real Steam/BUFF writes, payments, SEND/ACCEPT/CONFIRM, authenticated probes when separately gated, protected local-resource lifecycle actions, and integration->main all retain their exact OWNER authorization gates.
- Generic wording such as “继续/开始/可以” does not widen those gates.
- Protected historical checkouts/runtimes must not be reset, cleaned, repurposed, migrated, retired, or reclaimed by this governance task.

## 6. Immediate continuation

1. Finish TASK-085 Worker validation, PR, exact-head CI, and terminal Result against its exact frozen base.
2. Independent governance-level validation and G20 Lead Acceptance remain separate.
3. TASK-082 review/integration remains a separate product-work decision; it must not be folded into TASK-085 or used to alter this governance candidate's frozen baseline.
