# TASK 工作流

与 [`AGENTS.md`](../AGENTS.md)、[`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md)、[`TASK_TEMPLATE.md`](TASK_TEMPLATE.md)、[`REVIEW_CHECKLIST.md`](REVIEW_CHECKLIST.md) 和 [PR 模板](../.github/pull_request_template.md) 配套。

## 1. 开始与冻结

1. Sol 读取 GitHub 当前状态和项目固定上下文，确定当前唯一 TASK。
2. 在 Issue 中冻结：目标、base SHA、允许/禁止文件、风险、依赖、安全不变量、验收标准。
3. 判断执行模式：
   - **GitHub-only**：GitHub 已具备完成任务所需源码/工具，Sol 直接实现和验证；
   - **Local-required**：必须依赖 OWNER Windows 环境、本机文件/软件或本机复现，才交给 Luna。
4. 范围不足返回 `SCOPE_BLOCKED`；本机环境阻塞返回 `ENVIRONMENT_BLOCKED`。两者都不得被自行扩范围掩盖。

## 2. GitHub-only 执行

GitHub-only 任务默认由 Sol 完成：编辑、exact diff、commit、task branch/ref、PR、CI、merge-to-integration 和 post-merge review 都保持在 GitHub。

不要为了“分工”把纯 GitHub 操作交给 Luna，也不要创建没有必要的本地 checkout。

## 3. Local-required 执行

Luna 只执行 Issue 已冻结的本机任务：

- 使用隔离 workspace/worktree；
- 使用 [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md) 指定的 verifier；
- 不扰动受保护历史 checkout；
- 只修改白名单；
- 运行本轮真实 target / regression / full / baseline 等冻结验收；
- 报告真实命令、环境、collection/pass/fail/skip、changed files、diff/status、安全扫描和未解决问题。

历史测试结果只能作为历史证据，不能冒充当前轮验证。

### Source-tree handoff

本机实现通过后，Luna 不负责常规 commit/push/PR/merge。最终源码默认按以下方式交回 GitHub：

1. 确认 frozen base SHA / base tree；
2. 生成基于该 base tree 的 unreferenced Git tree；
3. tree 只包含 TASK 白名单产生的实际变化；
4. 记录 exact paths、每文件 hash、tree SHA；
5. 从 GitHub remote read-back blob/tree 并与本机 rehash 一致；
6. 确认非 TASK blob 全部继承 base，且无整文件行尾/编码噪音；
7. 把验证和 tree handoff 写回 Issue。

大型 ZIP/base64 Issue payload 不是常规 source handoff。若某工具限制迫使使用替代协议，必须在 Issue 中说明，并仍保证 exact-source 可验证。

## 4. Pre-commit review

Sol 在 commit 前执行 Historical + Simplicity Review：

- 当前 diff 是否只完成当前 TASK；
- 每个新 helper/class/layer 是否现在就需要；
- 是否形成重复状态机、执行器或写入权威；
- 能否删掉某层而不削弱安全；
- host touchpoints 是否最小；
- 是否加入纯未来需求抽象；
- 安全复杂度保留，投机复杂度拒绝。

如果 local-required，Sol 只从 GitHub handoff 接受 exact source；不能只凭测试摘要批准未知字节。

## 5. Commit 与发布

技术门禁通过后，Sol 负责：

1. 从已验收 source/tree 创建 exact commit；
2. GitHub-native compare 再核对 parent、tree、changed paths 和 minimal diff；
3. 创建/移动 task branch ref；
4. 创建 PR 到 `integration/auto-buyer-offer`；
5. 核对 exact PR head 与 CI；
6. CI 和技术审查通过后 merge 到 integration。

已经授权且冻结的 TASK 不要求 OWNER 为上述每一步重复授权。

若 exact-source、exact-commit 或 CI 出现新问题，停止发布并记录 `CHANGES_REQUESTED`；修正后必须重新验收新的 exact source/head。

## 6. Merge 后

merge 后必须：

1. 确认 integration HEAD 和 merge tree；
2. 核对 base → merge diff 没有额外文件/内容；
3. 等待并核对 post-merge CI（workflow 已配置时）；
4. 执行 Mandatory Historical + Simplicity Review；
5. 把 merge SHA、CI 和历史审查结论写回 Issue；
6. 只有全部 PASS 才关闭 TASK。

Mandatory Historical Review 要特别检查新 TASK 是否与历史模块形成重复权威。第一个真实 write-side Auto Offer TASK 之前，必须明确处理 TASK-007 `DeliveryExecutor` 与 Planner/Coordinator/Runtime 的长期角色。

## 7. 需要 OWNER 的独立门禁

以下事项不因 GitHub 发布授权而自动放开：

- 真实 Steam/BUFF/平台写请求、交易、确认或自动化启用；
- `integration/auto-buyer-offer` → `main`；
- 冻结 TASK 外的范围扩张；
- 仓库发布链路之外的重大外部风险动作。

## 8. Luna 执行路由

Sol 按任务复杂度选择 Luna 的模型/思考等级，并在对话中单独告诉 OWNER。该选择不写进可复用 TASK prompt 或仓库规范；prompt 只保存任务事实、范围和执行要求。
