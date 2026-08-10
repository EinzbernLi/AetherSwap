# TASK-XXX 任务卡

任务开始前由 Sol 冻结。项目固定事实见 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)，流程见 [`WORKFLOW.md`](WORKFLOW.md)。

## 信息

- TASK：
- Issue：
- 父任务/依赖：
- execution mode：`GitHub-only` / `Local-required`
- base branch：`integration/auto-buyer-offer`
- base SHA：
- task branch：由 Sol 创建/管理；不要求 Luna 创建 GitHub 分支
- 状态：`READY` / `RUNNING` / `LOCAL_VERIFICATION` / `REVIEW` / `CI` / `BLOCKED` / `DONE`
- 风险等级：`LOW` / `MEDIUM` / `HIGH` / `CRITICAL`

## 角色

- OWNER：真实平台写操作、integration → `main` 和重大范围/外部风险的最终授权。
- Sol / Web GPT：同一个技术总控与最终技术验收角色；冻结范围，负责 exact-source/commit、GitHub 发布、CI、merge-to-integration 和 post-merge review。
- Luna：仅在 `Local-required` 时负责本机实现/复现/测试/source-tree handoff；不独立扩大范围，不负责最终技术验收和常规 GitHub 发布。

## 目标与边界

目标：

允许修改（精确路径；其他默认禁止）：

-

明确禁止：

-

安全不变量：

-

范围不足时必须返回 `SCOPE_BLOCKED`；本机环境阻塞时返回 `ENVIRONMENT_BLOCKED`。

## 验收标准

功能/文档验收：

-

当前轮验证：

-

历史回归：

-

CI / baseline：

-

不得把历史执行结果标成当前轮验证。

## Local-required 附加要求

仅在确实需要本机时填写：

- isolated workspace/worktree：
- verifier：见 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)，除非 TASK 明确覆盖
- 受保护 checkout：不得触碰
- target tests：
- historical regression：
- host regression：
- full suite / baseline：
- static/safety scans：

通过后按 [`WORKFLOW.md`](WORKFLOW.md) 生成 unreferenced Git tree handoff，记录 base/tree/path/hash/remote read-back。正常情况下 Luna 不 commit、不 push、不建 PR、不 merge。

## Pre-commit Historical + Simplicity Review

- [ ] 只完成当前 TASK。
- [ ] 每个新增 helper/class/layer 都是当前需求。
- [ ] 没有重复状态机、执行器或写入权威。
- [ ] 没有可删除而不削弱安全的多余层。
- [ ] host touchpoints 最小。
- [ ] 没有纯未来需求抽象。
- [ ] 安全复杂度保留，投机复杂度拒绝。

Historical review 特别结论：

-

## Exact source / commit

- accepted source/tree SHA：
- commit SHA：
- parent SHA：
- exact changed paths：
- minimal diff：PASS / FAIL
- blocking：

## PR / CI / merge

- PR：
- exact head SHA：
- PR CI：
- merge SHA：
- post-merge CI：

在已经授权且冻结的 TASK 内，Sol 可在门禁通过后连续完成 commit → branch/ref → PR → CI → merge-to-integration，无需逐步重复请求 OWNER。

## Post-merge Mandatory Historical + Simplicity Review

- [ ] integration HEAD / merge tree 正确。
- [ ] base → merge diff 没有额外变化。
- [ ] post-merge CI PASS（若配置）。
- [ ] 没有新增竞争权威或隐藏执行框架。
- [ ] TASK 历史依赖和安全不变量仍成立。
- [ ] simplicity gate PASS。

结论：

- `PASS` / `CHANGES_REQUESTED` / `BLOCKED`

## 安全提醒

完整安全规则以 [`AGENTS.md`](../AGENTS.md) 为准。任何真实 Steam/BUFF 写请求仍需要 OWNER 单独明确授权；TASK 技术验收或 integration merge 不等于真实平台写授权，也不等于 integration → `main` 授权。
