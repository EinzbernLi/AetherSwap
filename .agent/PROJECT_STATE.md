# AetherSwap Current Project State

Status: **central governance v0.3.7 adopted; PF-014 preserved; G16 Web Lead ACTIVE; TASK-050 authority unchanged; live sent-history actions remain separately gated**  
State date: **2026-08-29**

This is a compact current-state entry point, not a transcript. Newer exact GitHub Issue/PR/Lead records always supersede stale prose here.

## 1. Current code line

- Active development branch: `integration/auto-buyer-offer`.
- PF-014 adoption exact base: `3966722d23412ee1360df8123cad66e60aab1438` (tree `4fc4219a8b19681bad68d99c31edee78e91d367c`).
- Read GitHub for the current integration head; do not treat a recorded adoption base as permanent current-head truth.
- `main` is not the active Auto Offer integration line.
- `integration/auto-buyer-offer -> main` requires explicit OWNER approval.

## 2. Governance baseline

Ordinary governance and LPRL are separately pinned.

Current ordinary governance:

- central governance: `EinzbernLi/agent-dev-governance@f49065796e277cb6859ebb3c92324d9b072b316d` (`v0.3.7`);
- PF-014 durable re-anchor semantics remain adopted and unchanged by the v0.3.7 Worker/session simplification;
- runtime-local planning/memory/session/IDE/checkpoint/scratch is non-authoritative execution aid and provisional until durable re-anchor;
- durable Task contract and durable project/control facts outrank executor-local planning;
- complete formal Task/evidence contracts remain in GitHub; activation uses short pointers;
- after durable re-anchor, role-stable Worker/Validator sessions may be reused by default; a fresh conversation is mandatory only when the Task/evidence contract requires freshness or identity isolation;
- bounded Worker/Validator sessions may use platform-native internal subagents under parent accountability, but children remain below the governance role layer and do not inherit Lead authority or widen scope/permissions/result sink;
- executor self-check and same-parent child review are execution evidence, not governance-level independent validation; formal independent validation requires substantive independence but not a brand-new conversation unless the contract requires freshness;
- durable completion requires the required Result to exist in the canonical sink and be read back before acceptance;
- Source identity, physical Location, controlled Workspace, and runtime Deployment remain distinct;
- stale candidate currency is separate from reusable evidence: stale source cannot be accepted as current merely because evidence may be reusable;
- safe pre-anchor bounded diagnostics may run only when known no-write or isolated, offline/no network-platform-business effect, and they remain provisional;
- pre-anchor evidence may be reused only after exact input/baseline plus unchanged evidence/test contract are reconciled/revalidated; otherwise rerun;
- authority-bearing/formal Task execution, dispatch, mutation, network/platform/business action, protected-runtime mutation, and durable PASS/Acceptance require applicable durable re-anchor and Lead activation;
- if expected `G(N+1)` is missing/unverifiable, takeover did not complete: prior valid `G(N)` remains authoritative until a successful retry completes claim write, reread/uniqueness, and fresh capability probe;
- one Active project Lead + bounded same-project Web/Codex/Luna/Terra workers remains the project model;
- `parallel workers != parallel Leads`; Lead owns integration and final acceptance;
- actual project Lead handoff still requires explicit OWNER intent.

LPRL remains separately pinned and unchanged:

- `EinzbernLi/agent-dev-governance@d63dae9ac1de65420f410cb36e6c2ccb58cc0478` (`v0.2.3-pilot`).

The ordinary governance repin does not grant local-resource lifecycle authority and does not change REAL-WRITE.

## 3. Current Lead / control state

Canonical sink: Issue #149.

Current exact state at this adoption:

- G15 claim `5452116330` was a historical task-scoped LPRL-materialization Lead state;
- OWNER subsequently clarified that the separate file-management/LPRL line is not intended to own the Aether project Lead while this Sol/Web session owns the Aether functional line;
- G16 claim `5452445521`, parent G15, restores one Web Aether Lead under `normal_project_continuation`;
- G16 activation verify `5452448339` confirms uniqueness; #149 must be reread before authority-sensitive execution/final acceptance to detect any G17/newer claim;
- runtime: Web / GPT-5.6 Sol;
- TASK-050 offline/program development is within G16 scope;
- file-management/LPRL may run as a separately bounded worker/workstream but not as a sibling Aether Lead;
- live authenticated one-shot remains separately gated;
- REAL-WRITE remains CLOSED;
- integration -> main remains OWNER-gated.

This section is a convenience snapshot only. Always reread #149 before takeover, authority-sensitive execution, and final governance acceptance; newer canonical evidence wins.

## 4. Lead vs Worker launch semantics

Formal project takeover examples:

```text
接管 Aether 的开发，我们继续。
把 Aether 项目 Lead 移交给 Codex。
```

These enter the durable re-anchor / Lead Activation transaction.

Canonical bounded Task activation:

```text
执行 <task_ref>；先读取 Issue/指定 comment；作为该 Task 的执行者，不接管项目 Lead。
```

A formal Task does not require a fresh conversation by default after durable re-anchor. Lead guidance stays short: `reuse_existing`, `recommend_new`, or `must_be_fresh`; only a Task/evidence contract requiring freshness or identity isolation makes a new conversation mandatory.

A bounded Worker/Validator may use platform-native internal subagents under the parent session's accountability. Internal children do not inherit Lead authority, widen Task scope/permissions/result sink, or earn governance-level independent-validation credit merely by being different children. A child becomes a formal Worker/Validator only with a distinct durable role/Result contract.

An actual project Lead handoff requires explicit OWNER intent to move the **project Lead**. `接管 TASK` / `执行 TASK` / `continue TASK` does not itself transfer Lead. Formal independent validation requires substantive independence, but a new conversation is not required unless the Task/evidence contract says so. Active Aether Lead retains final acceptance.

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

TASK-050 live/authenticated actions remain separately gated by their exact durable packets and OWNER authorization. Governance v0.3.7 adoption does not authorize, repeat, or widen any such live action.

## 7. Codex ↔ GitHub reconciliation

Cross-runtime audit is recorded in #130 comment `5452399614`.

- TASK-042 and TASK-047 production work are represented in GitHub history and current integration descendants.
- prior TASK-050 production-simulation branch/commit is not repository-resolvable and must not be reconstructed from prose.
- exact local evidence must be reconciled to current source/baseline and its evidence/test contract before reuse; local planning or checkpoint state alone is never authority.

## 8. Safety state

`REAL-WRITE GATE: CLOSED`

Still requires exact separate OWNER authority where applicable:

- live authenticated Steam/BUFF schema/probe requests marked separately gated by Task;
- Steam/BUFF SEND/ACCEPT/CONFIRM/payment/purchase/platform mutation;
- destructive local-resource migration/retirement/cleanup/reclamation;
- integration -> main;
- actual project Lead handoff.

No generic “continue” instruction opens these gates.

## 9. Cold-start / durable re-anchor rule

Runtime-local continuity context may restore first, but it is non-authoritative. Before authority-bearing work, a project Lead takeover/resume follows:

```text
runtime-local context (provisional)
-> durable project/control facts
-> latest valid G(N)
-> exactly one parent-bound G(N+1)
-> canonical reread + uniqueness
-> fresh Runtime Capability Probe
-> ACTIVE
```

If `G(N+1)` is absent or unverifiable, the prior valid `G(N)` remains authoritative. Worker/Validator sessions do not create the next Lead generation merely because they execute a bounded Task.

## 10. Next safe actions

1. Continue bounded governance or TASK-050 offline/program work under the current G16 scope after reconciling current durable facts.
2. Reuse role-stable Worker/Validator sessions where appropriate; require freshness only when the Task/evidence contract does.
3. Treat any pre-anchor diagnostic evidence as provisional and apply the exact-input/baseline/test-contract reuse rule.
4. Do not execute separately gated live Steam/BUFF actions without their exact OWNER authorization.
5. Do not merge `integration/auto-buyer-offer` to `main` without separate OWNER approval.
