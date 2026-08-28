# AetherSwap Cold-Start Guard — MUST RUN FIRST

当当前会话是新的正式 Lead、项目接管或恢复会话（包括“接管 Aether 的开发，我们继续。”这类自然语言入口）时，**在任何 planning/progress 写入、测试、源码修改、Task 恢复/执行、executor dispatch、network/platform/business action 之前**，必须先完成以下控制面恢复：

1. 读取 `.agent/GOVERNANCE_LOCK.yaml`、`.agent/LOCAL_POLICY.yaml`、`.agent/PROJECT_STATE.md`、`.agent/BOOTSTRAP.md`；
2. 读取 canonical Lead Claim sink `github:EinzbernLi/AetherSwap#149`，找到最新有效 parent claim 和当前 control scope；
3. 创建下一单调 generation，并精确绑定 `parent_claim_ref`，不得凭“继续开发”扩大 scope；
4. 写后重新读取 #149，确认无 sibling / duplicate generation / newer competing claim；
5. 执行 fresh Runtime Capability Probe；
6. 只有上述全部成功后才可视为 `ACTIVE`，然后才能恢复 Task/Result/代码工作。

如果 claim sink 不可读/不可写、唯一性无法验证或 fresh probe 无法完成，返回 `LEAD_ACTIVATION_BLOCKED` 并 fail closed。**不得先写 `progress.md` / planning 文件、先跑测试或先做 Task，再补 claim。**

本 guard 仅用于把 runtime cold-start 路由到现有治理入口；它不是新的 Task/Result/Lead Claim authority，不镜像当前 generation，不授予 Steam/BUFF/业务/本地资源权限。具体状态和权限仍以 pinned governance、#149、Task/Result/Acceptance 及 OWNER gate 为准。

# AetherSwap 协作与安全规则

项目固定事实见 [`.agent/PROJECT_CONTEXT.md`](.agent/PROJECT_CONTEXT.md)，流程见 [`.agent/WORKFLOW.md`](.agent/WORKFLOW.md)，任务模板见 [`.agent/TASK_TEMPLATE.md`](.agent/TASK_TEMPLATE.md)，审查清单见 [`.agent/REVIEW_CHECKLIST.md`](.agent/REVIEW_CHECKLIST.md)，PR 模板见 [`.github/pull_request_template.md`](.github/pull_request_template.md)。

## 角色与权限

- **OWNER**：产品/业务最终授权者。真实 Steam/BUFF/平台写操作、真实交易启用，以及 `integration/auto-buyer-offer` → `main` 的推进必须由 OWNER 明确批准。
- **Sol / Web GPT**：同一个技术总控角色，不拆分成两个验收者。Sol 负责规划、冻结 TASK 范围、GitHub 状态、架构与安全审查、最终技术验收，以及通过门禁后的 commit、branch/ref、push、PR、CI 核对、merge 到 integration 和 post-merge review。
- **Luna**：只负责确实需要 OWNER 本机环境的执行，例如本机实现、复现、测试、文件处理和 source-tree handoff。Luna 不是子代理，也不是独立架构/范围决策者；不得自行扩大 TASK 或替代 Sol 做最终技术验收。

在已经授权且范围冻结的 TASK 内，技术门禁通过后，Sol 可连续完成 task branch → PR → `integration/auto-buyer-offer` 的 GitHub 发布链路，不需要 OWNER 为 commit、push、PR、merge-to-integration 逐步重复授权。

仍需单独 OWNER gate 的事项：

- 真实 Steam/BUFF/平台写请求或交易；
- integration → `main`；
- 超出冻结 TASK 的范围扩张；
- 仓库发布链路之外的重大外部风险动作。

## GitHub 与本机边界

- GitHub 是唯一持久事实来源；聊天附件、本机 workspace、临时 patch 不能成为长期权威。
- 能在 GitHub 完成的事情不交给 Luna。
- 本机执行必须使用隔离 workspace/worktree，遵守 [`PROJECT_CONTEXT.md`](.agent/PROJECT_CONTEXT.md) 的 verifier 与受保护 checkout 规则。
- 本机最终源码需要交回时，默认使用基于冻结 base tree 的 **unreferenced Git tree handoff**：精确路径、hash、remote read-back、minimal diff；不要把大型 ZIP/base64 Issue payload 作为常规交接协议。
- Sol 从已验收 tree 创建 commit，并负责后续 branch/ref、PR、CI 和 integration merge。

## TASK 与范围

- 一次只推进一个业务 TASK；治理/硬化任务可使用独立编号，不占已预留的功能 TASK 编号。
- TASK 开始前必须冻结目标、base、允许文件、禁止范围、安全不变量和验收标准。
- 只能修改白名单文件。必须修改白名单外文件才能正确完成时返回 `SCOPE_BLOCKED`，不得自行扩展。
- 本机环境本身阻塞时返回 `ENVIRONMENT_BLOCKED`，不要把环境故障伪装成代码失败。
- `CHANGES_REQUESTED` 复用同一 Issue/分支/PR；新 source/head 必须重新做对应验收。

## 安全不变量

- BUFF 契约、字段、状态或错误语义不明确时必须 `BLOCKED`，禁止猜测。
- Cookie、API Key、令牌、`buyer_info` 和加密前会话不得进入日志、Issue、PR 或 fixture。
- 不可幂等写请求不得自动重试；超时或不明确结果进入 `RESULT_UNKNOWN`，禁止自动再次发送。
- 一笔订单只能有一条购买记录；首次报价必须在付款记录持久化后、购买下一件前发生。
- 报价前必须精确绑定购买记录 SteamID、当前凭据 SteamID、BUFF 绑定 SteamID、订单 `buyer_steamid`；禁止模糊/first-account fallback。
- 不得只按饰品名称绑定库存或授权自动上架；`pending_receipt=True` 表示尚未确认进入买方库存。
- 不得使用 `accept_all`；worker 只负责恢复/对账，不负责首次发送。
- 不得删除、跳过或弱化安全测试来换取 PASS；只能把本轮真实执行结果称为当前验证。
- 没有 OWNER 明确授权时不得执行真实 Steam/BUFF 写操作。

## 审查门禁

每个实现 TASK 至少经过：

1. 冻结设计与范围；
2. 当前轮真实验证；
3. pre-commit Historical + Simplicity Review；
4. exact-source / exact-commit review；
5. exact-head CI；
6. merge-to-integration；
7. post-merge CI（仓库已配置时）；
8. Mandatory Historical + Simplicity Review。

在第一个真实 write-side Auto Offer TASK 之前，必须重新审查 TASK-007 `DeliveryExecutor` 与 Planner/Coordinator/Runtime 的权威关系，避免形成竞争执行框架。
