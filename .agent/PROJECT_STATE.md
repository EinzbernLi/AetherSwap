# AetherSwap Current Project State

Status: **central governance v0.3.6 adopted; G16 Web Lead ACTIVE; TASK-050 offline foundation merged; next live sent-history probe still separately gated**  
State date: **2026-08-28**

This is a compact current-state entry point, not a transcript. Newer exact GitHub Issue/PR/Lead records always supersede stale prose here.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- Current integration baseline after governance v0.3.6 adoption: `d484d6268ef9fc7cef1f1ba538e0b6818fb4ad27`.
- `main` is not the active Auto Offer integration line.
- `integration/auto-buyer-offer -> main` requires explicit OWNER approval.

## 2. Governance baseline

Ordinary governance and LPRL are separately pinned.

Governance adopted through PR #159 / merge `d484d6268ef9fc7cef1f1ba538e0b6818fb4ad27`:

- central governance: `EinzbernLi/agent-dev-governance@c06b82de61de9249d91473bda974228725bdb714` (`v0.3.6`);
- one Active project Lead + bounded same-project Web/Codex/Luna/Terra workers;
- `parallel workers != parallel Leads`;
- Lead owns integration and final acceptance;
- Worker/Validator sessions must not create project Lead Claims merely because they execute a Task in another runtime;
- actual project Lead handoff requires explicit OWNER intent to move the project Lead.

LPRL remains separately pinned:

- `EinzbernLi/agent-dev-governance@d63dae9ac1de65420f410cb36e6c2ccb58cc0478` (`v0.2.3-pilot`).

The ordinary governance repin does not grant local-resource lifecycle authority and does not change REAL-WRITE.

## 3. Current Lead / control state

Canonical sink: Issue #149.

Current exact state:

- G15 claim `5452116330` was a historical task-scoped LPRL-materialization Lead state;
- OWNER subsequently clarified that the separate file-management/LPRL line is not intended to own the Aether project Lead while this Sol/Web session owns the Aether functional line;
- G16 claim `5452445521`, parent G15, restores one Web Aether Lead under `normal_project_continuation`;
- G16 activation verify `5452448339` confirms uniqueness and no G17/newer competing claim;
- runtime: Web / GPT-5.6 Sol;
- TASK-050 offline/program development is within G16 scope;
- file-management/LPRL may run as a separately bounded worker/workstream but not as a sibling Aether Lead;
- live authenticated one-shot remains separately gated;
- REAL-WRITE remains CLOSED;
- integration -> main remains OWNER-gated.

## 4. Lead vs Worker launch semantics

Formal project takeover examples:

```text
接管 Aether 的开发，我们继续。
把 Aether 项目 Lead 移交给 Codex。
```

These enter the Lead Activation Gate.

Task execution examples:

```text
执行 <task_ref>；作为该 Task 的执行者，不接管项目 Lead。
审计 <task_ref>；作为该 Task 的验证者，不接管项目 Lead。
```

These are Worker/Validator sessions and must not modify #149.

An actual project Lead handoff requires explicit OWNER intent to move the **project Lead**. `接管 TASK` / `执行 TASK` / `continue TASK` does not itself transfer Lead.

## 5. Parallel workstreams

Same-project workers may run concurrently only after the Active Lead marks each Task `parallel_safe` and verifies:

- exact frozen baseline;
- declared dependencies and no unmet ordering dependency;
- pairwise disjoint substantive write scopes;
- isolated write surface for implementation work;
- no same-file/shared-mutable-state multiwriter.

Read overlap and unambiguous append-only Result comments are allowed. Merge conflict is not a coordination mechanism. Lead owns rebase/replan/serialisation, integration order and acceptance.

## 6. TASK-050 current state

Issue #130 has been reconciled to the Steam sent-history architecture.

Merged:

- Slice A / PR #155 -> merge `1a62c7b40c784543b398217df4bb17e365ac19c0`, exact-head CI `2180/2180` PASS.
  - BUFF buy-order history retired as Trade Offer identity source.
  - lifecycle/refund history preserved.
- Slice B / PR #158 -> merge `f3ba4dd8746d299e87d01f5e06d01eca5a2dbd93`, exact-head CI `2215/2215` PASS.
  - normalized sent-offer discovery/binding contract exists.
  - 0 candidate -> no binding / caller may WAIT.
  - exactly 1 candidate -> eligible for exact `GetTradeOffer` closure.
  - 2+ -> ambiguity / fail closed.

Current target flow:

```text
persist OFFER_ATTEMPTED + offer_attempted_at
-> exactly one buyer SEND
-> RESULT_UNKNOWN when immutable offer identity is not durably proven
-> bounded Steam sent-history discovery
-> 0 / 1 / 2+
-> unique candidate exact GetTradeOffer closure
-> one Store CAS bind
-> existing confirmation / lifecycle / completed-trade receipt flow
```

No production `GetTradeOffers` transport/parser is implemented yet because real response shape/TLS/time semantics remain unverified.

Latest one-shot packet is #130 comment `5452286470`. It remains **NOT AUTHORIZED** by ordinary continuation.

## 7. Codex ↔ GitHub reconciliation

Cross-runtime audit is recorded in #130 comment `5452399614`.

- TASK-042 and TASK-047 production work are represented in GitHub history and current integration descendants.
- prior TASK-050 production-simulation branch/commit is not repository-resolvable and must not be reconstructed from prose.
- Windows sent-history probe harness (`test_sent_history_probe_outcome.py`, `test_sent_history_direct_probe.py` plus companion control script) was locally verified by Codex but is not present in GitHub source.
- exact local harness recovery/export is defined by #130 comment `5452404406`; that is offline source audit only, not a live probe.

## 8. Safety state

`REAL-WRITE GATE: CLOSED`

Still requires exact separate OWNER authority where applicable:

- live authenticated Steam/BUFF schema/probe requests marked separately gated by Task;
- Steam/BUFF SEND/ACCEPT/CONFIRM/payment/purchase/platform mutation;
- destructive local-resource migration/retirement/cleanup/reclamation;
- integration -> main;
- actual project Lead handoff.

No generic “continue” instruction opens these gates.

## 9. Cold-start rule

A new session first decides whether it is:

```text
project Lead takeover/resume
or
bounded Task Worker/Validator
```

Only the first path creates the next #149 generation. Worker/Validator sessions operate under their Task package and return Results to the Active Lead.

## 10. Next safe actions

1. For the Codex-local probe harness audit, launch Codex as a **Task Worker**, not a Lead takeover.
2. Review any recovered exact local probe source against current integration before deciding whether it belongs in GitHub.
3. Continue TASK-050 offline work under G16.
4. Do not execute the live sent-history one-shot without its separate explicit OWNER authorization.
