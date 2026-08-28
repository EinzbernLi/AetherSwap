# AetherSwap Project Bootstrap

This is the minimal cold-start entry point. It distinguishes a **project Lead takeover** from a **Task Worker/Validator launch**.

## Project

```yaml
project_name: AetherSwap
project_aliases: [Aether, AetherSwap]
active_development_branch: integration/auto-buyer-offer
primary_product_task_ref: github:EinzbernLi/AetherSwap#130
task_fact_source: github_issue_pr
lead_claim_sink: github:EinzbernLi/AetherSwap#149
governance_ref: EinzbernLi/agent-dev-governance@c06b82de61de9249d91473bda974228725bdb714
governance_version: v0.3.6
lprl_ref: EinzbernLi/agent-dev-governance@d63dae9ac1de65420f410cb36e6c2ccb58cc0478
lprl_profile: v0.2.3-pilot
```

Ordinary governance and LPRL remain separately pinned.

## 1. Decide the session role before doing anything else

### A. Formal project Lead takeover / resume

Only choose this path when the OWNER explicitly asks this session to become or take over the **Aether project Lead**, e.g.:

```text
接管 Aether 的开发，我们继续。
把 Aether 项目 Lead 移交给 Codex。
由这个 Web 会话接任 Aether 总控。
```

Then run the Lead Activation Gate below.

### B. Task Worker / Validator launch

Choose this path when the session is asked to execute/audit/test/export/review a bounded Task/Issue/PR, including when OWNER opens another Web/Codex session.

Canonical wording:

```text
执行 <task_ref>；作为该 Task 的执行者，不接管项目 Lead。
```

Worker rules:

- do not create/advance a claim in #149;
- do not run Lead takeover merely because the prompt contains a Task reference or asks to “continue” that Task;
- do not integrate sibling work or declare final project acceptance;
- obey exact Task scope and return Result to the declared GitHub sink;
- if asked ambiguously to both execute a Task and become Lead, fail closed on Lead claim until OWNER explicitly states the project Lead handoff.

`parallel workers != parallel Leads`.

## 2. Lead Activation Gate

For a real project Lead takeover/resume:

1. read `.agent/GOVERNANCE_LOCK.yaml`;
2. read `.agent/LOCAL_POLICY.yaml`;
3. read `.agent/PROJECT_STATE.md`;
4. read this file;
5. read canonical Lead Claim sink #149;
6. identify latest valid parent claim and effective control scope;
7. create exactly the next parent-bound claim without implicit scope widening;
8. re-read #149, verifying no sibling/duplicate generation or newer competing claim;
9. perform a fresh Runtime Capability Probe;
10. only then become `ACTIVE`;
11. then recover the active Task/PR/CI/source state.

Before `ACTIVE`, only control-plane recovery/verification is allowed. If persistence/uniqueness/probe fails, return `LEAD_ACTIVATION_BLOCKED`.

## 3. Current Lead state

Canonical current control is Issue #149, not this convenience file.

At this revision:

- G16 claim: `5452445521`;
- parent: G15 `5452116330`;
- activation verify: `5452448339`;
- runtime: Web / GPT-5.6 Sol;
- scope: `normal_project_continuation` for Aether functional development plus governance reconciliation;
- file-management/LPRL work may run as a bounded worker/workstream but is not a sibling Aether Lead;
- TASK-050 offline/program development is allowed under its Task boundaries;
- live authenticated one-shot is NOT authorized;
- REAL-WRITE remains CLOSED;
- integration -> main remains OWNER-gated.

Always prefer newer exact #149 evidence if the chain advances.

## 4. v0.3.6 same-project workstreams

One Active Lead may dispatch multiple bounded Web/Codex/Luna/Terra workers. Concurrent execution requires the Lead to mark each Task `parallel_safe` and prove:

- exact frozen baseline;
- explicit dependencies with no unmet ordering dependency;
- pairwise disjoint substantive write scopes;
- isolated branch/PR/worktree or equivalent write surfaces for implementation tasks;
- no shared mutable-state multiwriter.

Read overlap is allowed. Append-only Result comments may share a GitHub sink when Task ownership stays unambiguous. Same-file multiwriter and shared schema/runtime mutation are not parallel-safe by default.

Workers never modify #149, integrate sibling work, or perform final acceptance. Lead owns rebase/replan/serialisation, integration order and final acceptance.

## 5. Current product work

TASK-050 / Issue #130 is the primary Aether functional task.

Current architecture is buyer-send convergence through Steam sent history:

```text
OFFER_ATTEMPTED + offer_attempted_at
-> exactly one SEND
-> RESULT_UNKNOWN when identity is unproven
-> future bounded Steam sent-offer discovery
-> 0 candidate WAIT / 1 candidate exact GetTradeOffer / 2+ fail closed
-> exact closure
-> one Store CAS bind
-> existing confirmation/lifecycle/receipt flow
```

Merged foundation:

- Slice A PR #155: BUFF history retired as offer identity; lifecycle/refund history preserved.
- Slice B PR #158: pure sent-offer discovery/binding contract merged.
- integration baseline after Slice B: `f3ba4dd8746d299e87d01f5e06d01eca5a2dbd93`.

Next production transport/parser remains blocked on a separately OWNER-authorized read-only Steam sent-history TLS/schema capture. Generic “continue development” does not authorize that probe.

## 6. Safety boundaries

- REAL-WRITE CLOSED by default.
- Separately gated live authenticated probes require their own exact OWNER authorization.
- No blind retry of non-idempotent SEND/ACCEPT/CONFIRM.
- No name/price/latest/closest/manual-ID identity recovery.
- No secrets/session material in durable evidence.
- integration -> main requires explicit OWNER approval.
- Destructive local-resource lifecycle actions require separate authority.

## 7. Legacy/domain documents

Load only when relevant:

- `AGENTS.md`
- `.agent/WORKFLOW.md`
- `.agent/PROJECT_CONTEXT.md`
- `.agent/CURRENT_INVARIANTS.md`
- `.agent/AUTO_OFFER_*.md`
- `.agent/TASK_TEMPLATE.md`
- `.agent/REVIEW_CHECKLIST.md`
- `.github/pull_request_template.md`

If legacy wording conflicts with pinned v0.3.6 governance, `LOCAL_POLICY`, current #149 Lead state, or newer exact Task/Result/Acceptance facts, follow the newer authority.
