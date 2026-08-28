# AetherSwap Cold-Start Guard — MUST RUN FIRST

This repository uses one Active project Lead plus bounded Task-scoped workers.

## Formal Lead / takeover entry

Only when the OWNER explicitly asks the current session to become or take over the **Aether project Lead** (for example: `接管 Aether 的开发，我们继续。`, `把 Aether Lead 移交给 Codex`) does this session enter the Lead Activation Gate.

Before any planning/progress write, project test, source mutation, formal dispatch, network/platform/business action, or Task execution as Lead:

1. read `.agent/GOVERNANCE_LOCK.yaml`, `.agent/LOCAL_POLICY.yaml`, `.agent/PROJECT_STATE.md`, `.agent/BOOTSTRAP.md`;
2. read canonical Lead Claim sink `github:EinzbernLi/AetherSwap#149` and identify the latest valid parent plus effective control scope;
3. create exactly the next parent-bound generation without widening scope by inference;
4. re-read #149 and verify no sibling/duplicate generation or newer competing claim;
5. perform a fresh Runtime Capability Probe;
6. only after all gates pass may the session become `ACTIVE` and act as project Lead.

If the claim sink cannot be read/written, uniqueness cannot be verified, or the fresh probe cannot complete, return `LEAD_ACTIVATION_BLOCKED` and fail closed.

## Task Worker / Validator entry — MUST NOT TAKE OVER LEAD

A session launched to execute, audit, validate, export, reproduce, test, or review a bounded GitHub Task/Issue/PR is a **Worker/Validator by default**, even when it runs in another Web/Codex session.

Canonical launch wording:

```text
执行 <task_ref>；作为该 Task 的执行者，不接管项目 Lead。
```

For such a session:

- read the Task package and the minimum governance needed to obey it;
- do **not** create or advance a Lead Claim in #149;
- do **not** reinterpret `接管 TASK` / `执行 TASK` / `audit TASK` as project takeover;
- do **not** integrate sibling work or announce final project acceptance;
- return Results to the declared GitHub sink for the Active Lead to review/integrate.

An actual Lead handoff requires explicit OWNER intent to move the **project Lead**, not merely a request to run work in Codex/Web/Luna/Terra.

`parallel workers != parallel Leads`.

# AetherSwap 协作与安全规则

项目固定事实见 [`.agent/PROJECT_CONTEXT.md`](.agent/PROJECT_CONTEXT.md)，流程见 [`.agent/WORKFLOW.md`](.agent/WORKFLOW.md)，任务模板见 [`.agent/TASK_TEMPLATE.md`](.agent/TASK_TEMPLATE.md)，审查清单见 [`.agent/REVIEW_CHECKLIST.md`](.agent/REVIEW_CHECKLIST.md)，PR 模板见 [`.github/pull_request_template.md`](.github/pull_request_template.md)。

## 角色与权限

- **OWNER**：产品/业务最终授权者。真实 Steam/BUFF/平台写操作、真实交易启用、单独 gated 的 live authenticated probe，以及 `integration/auto-buyer-offer` → `main` 必须按对应 gate 明确批准。
- **Sol / Web Lead**：唯一 Aether Active Lead Controller，负责架构、Task decomposition、范围冻结、GitHub 状态、dispatch、冲突裁决、Review、integration 和最终技术验收。
- **Task Worker / Validator**：Web、Codex、Luna、Terra 等运行时均可在冻结 Task 下执行。它们不是 sibling Lead，不得修改 #149 Lead Claim、扩大 scope、集成 sibling work 或替代 Active Lead 做最终 acceptance。

在已经授权且范围冻结的 TASK 内，技术门禁通过后，Active Lead 可连续完成 task branch → PR → `integration/auto-buyer-offer` 的发布链路，不需要 OWNER 为 commit、push、PR、merge-to-integration 逐步重复授权。

仍需单独 OWNER gate：

- 真实 Steam/BUFF/平台写请求或交易；
- 明确标成 independently gated 的 live authenticated probe；
- actual project Lead handoff；
- integration → `main`；
- 超出冻结 TASK 的范围扩张；
- destructive local-resource migration / retirement / cleanup / reclamation。

## Parallel workstreams

同一项目允许多个 Task Worker 并行，但只有 Lead 明确标记 `parallel_safe` 时才可并发。必须同时满足：exact frozen baseline、依赖已声明且无未满足顺序依赖、实质写范围两两不重叠、实现任务使用隔离 branch/PR/worktree 或等价写表面。

读取同一证据可以重叠；可明确归属 Task 的 append-only Result comment 可以共享 sink。写同一项目文件、schema migration 或 shared mutable state 默认必须串行或重新拆分。不得把 merge conflict 当协调机制。

Active Lead 保留 integration order、冲突裁决和 final acceptance。

## GitHub 与本机边界

- GitHub 是持久 Task/Result/Review/Acceptance 与代码事实源；聊天附件、本机 workspace、临时 patch 不是长期权威。
- 本机执行必须使用隔离 workspace/worktree，并遵守 [`PROJECT_CONTEXT.md`](.agent/PROJECT_CONTEXT.md) 的 protected checkout 规则。
- 本机最终源码需要交回时，使用 exact source/tree/file-hash handoff；不要用聊天描述重建缺失源码。
- Active Lead 负责验收、GitHub commit/branch/PR/CI 和 integration merge。

## TASK 与范围

- 一次只推进一个主要业务 TASK；明确 `parallel_safe` 的独立工作流可并行。
- TASK 开始前冻结 objective、base、dependencies、allowed/forbidden scope、安全不变量和验收标准。
- 只能修改白名单范围。必须越界才能正确完成时返回 `SCOPE_BLOCKED`。
- 环境阻塞返回 `ENVIRONMENT_BLOCKED`，不得伪装成代码失败。
- `CHANGES_REQUESTED` 复用同一 Issue/branch/PR；新 source/head 必须重新验收。

## 安全不变量

- BUFF/Steam 契约、字段、状态、identity 或错误语义不明确时必须 fail closed；禁止猜测。
- Cookie、API Key、token、`buyer_info`、decrypted session material 不得进入日志、Issue、PR 或 fixture。
- 不可幂等写请求不得自动重试；超时/不明确结果进入 `RESULT_UNKNOWN`，禁止 blind retry。
- 一笔订单只能有一条购买/交付 authority；首次报价必须在付款事实持久化之后。
- 报价/接收/确认必须使用 exact supported identity evidence；name/price/latest/time proximity/manual ID 不是 authority。
- `pending_receipt=True` 不是已入库证明。
- 禁止 `accept_all`；worker/recovery 不得形成第二条 first-send authority。
- 不得删除、skip、弱化安全测试来换取 PASS。
- 没有对应 OWNER gate，不得执行真实 Steam/BUFF write 或 separately gated live probe。

## 审查门禁

每个 material implementation TASK 至少经过：冻结设计与范围 → 当前真实验证 → Historical + Simplicity Review → exact-source/exact-commit review → exact-head CI → merge-to-integration → post-merge review/CI（适用时）。

在第一个真实 write-side Auto Offer TASK 前，仍需复核 TASK-007 `DeliveryExecutor` 与 Planner/Coordinator/Runtime 的 authority 关系，避免竞争执行框架。
