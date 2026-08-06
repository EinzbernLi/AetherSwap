# TASK-XXX 任务卡

父代理填写边界；一次只执行一个 TASK。流程见 [AGENTS.md](../AGENTS.md) 和 [WORKFLOW.md](WORKFLOW.md)。

## 信息

- TASK：
- Issue：
- 父任务：
- 分支：`luna/TASK-xxx-short-name`
- PR base：`integration/auto-buyer-offer`
- 状态：`READY` / `RUNNING` / `awaiting-gpt-review` / `BLOCKED` / `DONE`
- 风险等级：`LOW` / `MEDIUM` / `HIGH` / `CRITICAL`
- 依赖：

## 角色

- Luna：实现、测试、自检和 Draft PR；不负责最终验收，不审查或合并自己的 PR。
- Sol：派单、GitHub 状态管理、生成验收请求、原样传递网页端 GPT 结论并按结论执行；不得自行批准。
- 网页端 GPT：读取 GitHub，并针对精确完整 PR head SHA 作出最终验收。
- 仓库所有者：批准 `integration/auto-buyer-offer` → `main`，并决定是否启用真实自动报价。

## 目标与边界

目标：

允许修改（精确路径，其他默认禁止）：

-

禁止：`main`、未列文件、无关业务/测试/依赖/配置、删除或弱化现有安全测试。

验收标准：

-
-

## 必须使用的验收请求格式

【请求网页端 GPT 验收】

仓库：EinzbernLi/AetherSwap
任务：TASK-XXX
Issue：#X
PR：#X
目标分支：integration/auto-buyer-offer
当前 head SHA：完整 SHA
风险等级：LOW / MEDIUM / HIGH / CRITICAL
CI：passed / failed / not_configured
Luna 自检：completed
请求动作：最终验收

网页端 GPT 未响应时，任务保持 `awaiting-gpt-review`，禁止由 Sol 或 Luna 代替验收。

## 必须使用的验收结论格式

VERDICT: APPROVE | CHANGES_REQUESTED | BLOCKED
REVIEWED_HEAD_SHA: 完整 SHA

BLOCKING:
- 阻塞问题；没有则写“无”

NON_BLOCKING:
- 非阻塞建议；没有则写“无”

ALLOWED_ACTION:
- APPROVE：允许合入 integration/auto-buyer-offer
- CHANGES_REQUESTED：只允许交回 Luna 返工
- BLOCKED：停止当前任务及所有依赖任务

禁止合入 main。

Sol 必须将完整验收正文原样写回 PR，不得概括、改写或删除阻塞项。

## 合并门禁

只有同时满足以下条件时，Sol 才可将任务 PR 合入 `integration/auto-buyer-offer`：

1. 网页端 GPT 返回 `VERDICT: APPROVE`。
2. `REVIEWED_HEAD_SHA` 等于当前 PR head 的完整 SHA。
3. CI 为 `passed`，或任务明确允许 `not_configured`。
4. 没有新的未审查提交。
5. 没有超范围文件。
6. 没有未解决的阻塞项。
7. PR base 正确。
8. 任务依赖仍满足。

## 返工与阻塞

`CHANGES_REQUESTED` 时复用同一 Issue、分支和 PR；Luna 修改并重新自检，新提交产生新 head SHA；旧验收立即失效，Sol 重新发起验收。

`BLOCKED` 时标记 `agent:blocked`，禁止合并并停止所有依赖任务，等待网页端 GPT 或仓库所有者明确解除。

## 安全门禁

- [ ] BUFF 契约不明确即 `BLOCKED`，不猜测。
- [ ] 不记录 Cookie、API Key、令牌、`buyer_info` 或加密前会话。
- [ ] 不可幂等写请求不自动重试；不明确结果为 `result_unknown` 且不重发。
- [ ] 保持订单单记录、付款后首次报价、四方 SteamID、精确库存绑定。
- [ ] 保持 `pending_receipt=True`；不使用 `accept_all`；worker 不负责首次发送。
- [ ] 失败如实报告，不删除或弱化测试。

## 交付

- 检查或测试命令及真实结果：
- 故障测试及真实结果：
- 风险：
- 回滚：
- 未解决问题：

完成前使用 [REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md) 和 [PR 模板](../.github/pull_request_template.md)。
