# AetherSwap Bootstrap

This file is the compact cold-start entrypoint. GitHub Issue/PR facts and the canonical Lead sink remain the durable authority; do not reconstruct current work from stale prose or chat history.

## Project

```yaml
project: AetherSwap
repository: EinzbernLi/AetherSwap
active_branch: integration/auto-buyer-offer
release_target: main
task_result_fact_source: github_issue_pr
canonical_lead_sink: github:EinzbernLi/AetherSwap#149
ordinary_governance:
  version: "0.3.18"
  ref: aff6ff205eca64c594cc10b67a3454a765076bb0
  update_mode: github_native_notify
lprl:
  version: "v0.2.4-pilot"
  ref: 5f22e63414374b64ebbf4bd91601ede2f54e6f65
latest_completed_product_task: github:EinzbernLi/AetherSwap#233@5518389907
latest_product_merge: 40817ebf42b16d5702924c3a300211ea43b50ade
current_product_task: null
product_task_state: no_next_task_activated
real_write: CLOSED
```

## Role selection

### Formal Project Lead takeover

Only explicit OWNER intent to take over the project activates Lead takeover semantics. Recover the latest durable Lead parent from #149, perform the required claim/activation uniqueness checks and fresh capability probe, reconcile current work, recommend exactly one resume target, then enter `AWAIT_OWNER_CONTINUE` for a takeover-only message. The embedded word “继续” does not itself authorize execution.

### Bounded Worker / Validator execution

An exact Task activation such as `#issue@comment` plus “作为该 Task 的执行者，不接管项目 Lead” is a bounded execution contract, not a Lead handoff. The executor must not modify #149, integrate sibling work, widen scope, or claim project control.

## Exact running-Task revision

Running execution is bound to the exact activated Task revision. Later comments on the same mutable Issue may be read as durable facts but do not silently change scope, permissions, tests, acceptance, or Result contract. A material later authority revision requires route-appropriate explicit re-anchor evidence; otherwise stop with `NEEDS_ATTENTION_OR_REANCHOR_REQUIRED`.

## Current control and work

- Current authoritative Lead: G20 claim `#149@5502768640`, activation `#149@5502770266`; always re-read #149 before a terminal Result or other authority-bearing transition.
- TASK-083/#234 and TASK-084/#236 are completed slimming work already integrated.
- TASK-085/#240 governance 0.3.18 adoption and PR #242 are completed and accepted.
- TASK-082/#233 exact implementation revision `#233@5510226684` is completed and accepted through Lead Acceptance `#233@5518389907`.
- TASK-082 product PR #244 is merged at `40817ebf42b16d5702924c3a300211ea43b50ade`; post-merge CI #385 passed `2282` tests with zero failures/errors/skips and baseline gate PASSED.
- Independent Validator TASK-087/#245 is completed PASS.
- No next product Task is activated by this bootstrap state. New work must be frozen in a new/existing exact Task before bounded execution.

## Accepted buyer-send baseline

- BUFF exact `wait_send_offers` evidence owns buyer-send eligibility.
- First buyer SEND durably enters `OFFER_ATTEMPTED` before one write.
- SEND response does not fabricate/bind Steam Trade Offer identity and does not create a new unbound buyer `RESULT_UNKNOWN`.
- Later `OFFER_ATTEMPTED` ticks read realtime `/steam_trade` before any fresh wait-send resend authority.
- Ordinary absence/wait is per-order SAFE_WAIT and does not block unrelated orders.
- Legacy unbound buyer `RESULT_UNKNOWN` is not made resendable.
- Historical buyer-offer identity archaeology, Steam GetTradeOffers/sent-history enumeration, and pre-SEND fingerprint designs are retired from the active production path.

## Governance layering

- `.agent/GOVERNANCE_LOCK.yaml` pins one exact accepted ordinary-governance source/revision.
- `.agent/LOCAL_POLICY.yaml` preserves Aether-specific stricter Owner gates, safety constraints, concurrency, verification, calibration and local-resource rules; central adoption must not weaken them.
- Update discovery is `github_native_notify`; ordinary takeover/new Task still does not proactively follow upstream main, adopt a release, or advance the pin.
- Project-local calibration remains downstream-local derived evidence only and does not become Task/Result/Acceptance authority.
- LPRL is independently pinned to `v0.2.4-pilot@5f22e634...`; ordinary governance must not silently repin it. No Control Snapshot/Gate or lifecycle mutation authority is accepted for the current phase.

## Safety boundaries

- `REAL-WRITE` is CLOSED by default.
- TASK-082 acceptance does not authorize a real-order canary.
- Real Steam/BUFF writes, payments, SEND/ACCEPT/CONFIRM, separately gated authenticated probes, destructive/protected local-resource actions, scope expansion, Lead handoff, and integration->main require the exact applicable OWNER authorization.
- Protected historical checkout/runtime state is not a Task workspace.
- Never publish credentials, cookies, tokens, decrypted session material, raw secret values, or other sensitive authentication material.

## Cold-start order

1. Read `.agent/GOVERNANCE_LOCK.yaml`, `.agent/LOCAL_POLICY.yaml`, and this compact state/bootstrap entrypoint.
2. Resolve the requested role: Lead takeover vs exact bounded Task execution.
3. Read only the minimum exact GitHub facts required for that role, including #149 when Lead continuity matters.
4. Re-read current branch/Task/CI facts before authority-bearing action.
5. Respect exact Task revision, Owner gates, independent validation, and REAL-WRITE boundaries.
