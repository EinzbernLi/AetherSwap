# TASK / PR 审查清单

配合 [`AGENTS.md`](../AGENTS.md)、[`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)、[`WORKFLOW.md`](WORKFLOW.md)、[`TASK_TEMPLATE.md`](TASK_TEMPLATE.md) 和 [PR 模板](../.github/pull_request_template.md)。

## 角色、事实来源与范围

- [ ] Sol / Web GPT 被视为同一个技术总控与最终技术验收角色，没有旧式“双验收者”描述。
- [ ] Luna 只承担确实需要 OWNER 本机环境的执行，不拥有独立架构/范围决策权或常规 GitHub 发布职责。
- [ ] GitHub 是唯一持久事实来源；本机 workspace/聊天附件不是长期权威。
- [ ] 当前只有一个明确 TASK；Issue、base、目标、白名单、禁止范围和验收标准可追溯。
- [ ] changed paths 严格等于 TASK 白名单；无第七文件或无关业务/测试/依赖/配置变化。
- [ ] `main` 未被任务分支直接修改。

## 本机执行（仅 Local-required）

- [ ] 使用隔离 workspace/worktree，没有扰动 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) 标记的受保护 checkout。
- [ ] 使用冻结 verifier/环境，或 Issue 明确记录了覆盖理由。
- [ ] 当前轮真实命令、collection/pass/fail/error/skip、diff/status 和安全扫描已写入 GitHub。
- [ ] 没把历史测试结果冒充当前轮验证。
- [ ] source handoff 使用 exact unreferenced Git tree 或 Issue 明确记录的等价可验证协议。
- [ ] local/remote hash 与 remote read-back 一致。
- [ ] 非 TASK blob 继承 base；无整文件行尾/编码噪音。

## Historical + Simplicity Review

- [ ] diff 只解决当前 TASK。
- [ ] 每个新增 class/helper/layer 都是当前需求，而非未来预留。
- [ ] 没有第二套状态机、执行器、Store 写入权威或调度框架。
- [ ] 没有可删除而不削弱安全的中间层。
- [ ] host touchpoints 保持最小。
- [ ] 安全复杂度有明确不变量依据；投机复杂度已拒绝。
- [ ] 历史模块/任务仍兼容；需要硬化时创建独立任务，不静默改写历史契约。
- [ ] 第一个真实 write-side Auto Offer TASK 之前已重新判断 TASK-007 `DeliveryExecutor` 与 Planner/Coordinator/Runtime 的权威关系。

## 安全

- [ ] BUFF 契约或结果语义不明确时 `BLOCKED`，没有猜字段/状态。
- [ ] 未泄露 Cookie、API Key、token、`buyer_info` 或加密前会话。
- [ ] 不可幂等写请求无自动重试；未知结果保持 `RESULT_UNKNOWN` 且不自动重发。
- [ ] 一单一购买记录；首次报价在付款记录持久化后、下一购买前。
- [ ] 身份绑定精确，无 first-account/fuzzy/name-only fallback。
- [ ] `pending_receipt=True` 仍表示未确认进入买方库存；无 `accept_all`；worker 不负责首次发送。
- [ ] 未删除、跳过或弱化安全测试来换取 PASS。
- [ ] 未获得 OWNER 明确授权时没有真实 Steam/BUFF/平台写请求或交易。

## Exact source / commit / PR

- [ ] Sol 已审 exact source/tree，而不是只看测试摘要。
- [ ] commit parent/tree/changed paths 与 accepted source 一致。
- [ ] GitHub-native diff minimal；没有未解释的 line-ending/encoding churn。
- [ ] PR base 为 `integration/auto-buyer-offer`，head SHA 与最终技术验收完全一致。
- [ ] exact-head CI PASS；新 head 出现后旧验收已失效并重做。
- [ ] `CHANGES_REQUESTED` 复用原 Issue/分支/PR；`SCOPE_BLOCKED`/`ENVIRONMENT_BLOCKED` 没被绕过。

## Merge 与关闭

- [ ] merge-to-integration 只在 exact source、CI、范围、依赖和安全门禁通过后执行。
- [ ] 已确认 merge SHA、integration HEAD 和 merge tree。
- [ ] base → merge diff 没有 merge-time 额外变化。
- [ ] post-merge CI PASS（若 workflow 配置）。
- [ ] Mandatory Historical + Simplicity Review PASS 后才关闭 TASK。
- [ ] integration → `main` 没有被自动执行，除非 OWNER 明确批准。

## 文档质量

- [ ] Markdown 可渲染，中文 UTF-8 可读。
- [ ] 相对链接有效。
- [ ] 稳定项目事实集中在 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)，没有在多份文件复制长篇互相漂移的版本。
