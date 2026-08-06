# TASK 工作流

与 [AGENTS.md](../AGENTS.md)、[TASK_TEMPLATE.md](TASK_TEMPLATE.md)、[REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md)、[PR 模板](../.github/pull_request_template.md) 配套。

## 角色分工

- Luna 负责单个 TASK 的实现、测试、自检和 Draft PR，不负责最终验收，不得审查或合并自己的 PR。
- Sol 总控负责派单、GitHub 状态管理、生成固定格式验收请求、原样传递网页端 GPT 完整验收正文并按结论执行；不得自行批准 PR。
- 网页端 GPT 读取 GitHub，并针对精确完整 PR head SHA 作出最终验收。
- 仓库所有者负责 `integration/auto-buyer-offer` → `main` 的最终批准和真实自动报价功能启用。

## 开始

1. Sol 分配一个 TASK，明确目标、允许和禁止文件、基线、分支、风险、依赖和验收标准。
2. Luna 确认分支符合 `luna/TASK-xxx-short-name`，PR base 为 `integration/auto-buyer-offer`，禁止修改 `main`。
3. Luna 读取完整 Issue、现有实现和测试；BUFF 契约或结果语义不明确即 `BLOCKED`，禁止猜测。
4. Luna 只修改任务卡白名单；范围冲突、依赖缺失、权限不足或结果不明确时报告 Sol。

## 执行与交付

Luna 实现后运行匹配检查，阅读完整 diff，核对文件范围、相对链接、Markdown 和安全不变量，并按 [REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md) 自检。使用 [PR 模板](../.github/pull_request_template.md) 创建或更新同一 Draft PR，真实填写命令、结果、风险、回滚和未解决问题。

不可幂等写请求不自动重试；不明确结果为 `result_unknown` 且不重发。凭据、Cookie、令牌、`buyer_info` 和加密前会话不得进入协作产物。保持付款后首次报价时序、四方 SteamID 校验、精确库存绑定、`pending_receipt=True` 语义及 worker 职责。

## 网页端 GPT 验收

1. Sol 从 GitHub 获取当前 PR head 的完整 SHA，并按 [TASK_TEMPLATE.md](TASK_TEMPLATE.md) 生成验收请求。
2. 网页端 GPT 未响应时，任务状态保持 `awaiting-gpt-review`；禁止 Sol 或 Luna 代替验收。
3. Sol 必须将网页端 GPT 的完整验收正文原样写回 PR，不得概括、改写或删除阻塞项。
4. 验收只对 `REVIEWED_HEAD_SHA` 指向的精确完整 SHA 有效；任何新提交都使旧验收立即失效。

## 返工、阻塞与合并

- `CHANGES_REQUESTED`：Sol 原样写回验收正文，交回同一 Luna 任务；不创建重复 Issue、分支或 PR。Luna 返工并重新自检，新提交产生新 head SHA，Sol 重新发起验收，旧验收不得复用。
- `BLOCKED`：Issue 标记 `agent:blocked`，禁止合并，停止当前任务及所有依赖任务，等待网页端 GPT 或仓库所有者明确解除。
- `APPROVE`：只有精确 SHA、CI、范围、base、依赖和阻塞门禁全部通过时，Sol 才可合入 `integration/auto-buyer-offer`。
- 无论验收结果如何，都不得自动合入 `main`。
