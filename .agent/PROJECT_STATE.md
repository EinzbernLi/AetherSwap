# AetherSwap Current Project State

Status: **ordinary governance 0.3.18 adopted; G20 remains canonical Project Lead; TASK-082 Steamauto-parity buyer lifecycle merged and accepted; REAL-WRITE CLOSED**
State date: **2026-09-03**

This file is a compact recovery entrypoint. Newer exact GitHub Task/Result/Review/Acceptance, branch, CI, and Lead-sink facts supersede this convenience summary.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- Latest accepted product merge: TASK-082 / PR #244, commit `40817ebf42b16d5702924c3a300211ea43b50ade`, tree `983c3c4d65509339be9e179c2025f08fc72c7892`.
- TASK-082 post-merge Python CI #385 / run `33699049892`: `2282 passed`, 0 failed/errors/skipped, baseline gate PASSED.
- The current integration head must always be re-read before authority-bearing work; later convenience-state commits do not silently re-anchor an exact running Task.
- `integration/auto-buyer-offer -> main` remains explicitly OWNER-gated.

## 2. Governance and local policy

- Ordinary governance accepted pin: `EinzbernLi/agent-dev-governance` `0.3.18@aff6ff205eca64c594cc10b67a3454a765076bb0`.
- TASK-085/#240 and governance PR #242 are completed and accepted.
- Update mode remains `manual_pinned`: no proactive update workflow, no follow-main, no automatic adoption or pin advance.
- `.agent/LOCAL_POLICY.yaml` remains the project-specific stricter layer for Owner gates, REAL-WRITE, product safety, concurrency, verification, calibration, and local-resource boundaries.
- Project-local calibration remains a downstream-local derived routing cache only; it is not Task/Result/Acceptance authority and is not exported upstream.
- LPRL remains independently pinned exactly to `v0.2.4-pilot@5f22e63414374b64ebbf4bd91601ede2f54e6f65`; ordinary governance does not repin or reinterpret it.
- No accepted LPRL Control Snapshot/Gate/Retirement/Migration authority exists for this phase, and no local-resource mutation is authorized by governance adoption.

## 3. Project Lead continuity

- Canonical Lead sink: `github:EinzbernLi/AetherSwap#149`.
- Current durable control is G20: claim `#149@5502768640`, activation verification `#149@5502770266`.
- #149 is authoritative over this convenience prose. A bounded Worker/Validator Task does not transfer Project Lead authority.

## 4. Completed current product work

- TASK-083/#234 + PR #235: completed first slimming pass.
- TASK-084/#236 + PR #238: completed second slimming pass.
- TASK-082/#233 exact implementation revision `#233@5510226684`: completed and accepted.
- TASK-082 implementation result: `#233@5511772049`.
- Independent product validation TASK-087/#245: PASS and closed completed.
- TASK-082 PR #244 merged to integration at `40817ebf42b16d5702924c3a300211ea43b50ade`.
- Accepted buyer-send production semantics are now Steamauto-parity: exact `wait_send_offers` owns send eligibility; `OFFER_ATTEMPTED` is normal durable attempted state; realtime `/steam_trade` is checked before later resend eligibility; ordinary SAFE_WAIT is per-order and does not block unrelated orders; legacy unbound `RESULT_UNKNOWN` is not made resendable.
- Retired historical buyer-offer identity archaeology, sent-history/GetTradeOffers enumeration, and pre-SEND fingerprint designs remain outside the active production path.

## 5. Safety and authorization

- `REAL-WRITE: CLOSED`.
- No real-order canary is currently authorized by TASK-082 acceptance.
- Real Steam/BUFF writes, payments, SEND/ACCEPT/CONFIRM, separately gated authenticated probes, protected local-resource lifecycle actions, and integration->main retain their exact OWNER authorization gates.
- Generic wording such as “继续/开始/可以” does not widen those gates.
- Protected historical checkouts/runtimes must not be reset, cleaned, repurposed, migrated, retired, or reclaimed without exact authority.

## 6. Immediate continuation

1. Re-read the current integration head and #149 before the next authority-bearing Task.
2. No next product Task is activated by this convenience update; select/freeze the next repository task separately.
3. Any fresh real-order/full-flow canary remains a separate explicit OWNER-gated action while REAL-WRITE is CLOSED.
