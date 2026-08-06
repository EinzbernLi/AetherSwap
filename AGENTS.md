# AetherSwap 协作与安全规则

入口：流程见 [.agent/WORKFLOW.md](.agent/WORKFLOW.md)，任务模板见 [.agent/TASK_TEMPLATE.md](.agent/TASK_TEMPLATE.md)，审查清单见 [.agent/REVIEW_CHECKLIST.md](.agent/REVIEW_CHECKLIST.md)，PR 模板见 [.github/pull_request_template.md](.github/pull_request_template.md)。

## 角色与权限

- Luna 一次只执行一个 TASK，负责实现、测试、自检和创建 Draft PR；只能修改任务卡允许文件，不得扩大范围，不得审查或合并自己的 PR。
- Sol 总控负责派单、GitHub 状态管理、生成验收请求单、将网页端 GPT 的完整验收正文原样写回 PR，并严格按结论执行。Sol 不负责最终代码验收，不得自行批准 PR。
- 网页端 GPT 负责读取 GitHub 并作出最终验收。网页端 GPT 未响应时，任务保持 `awaiting-gpt-review`，禁止 Sol 或 Luna 代替验收。
- 仓库所有者负责 `integration/auto-buyer-offer` → `main` 的最终批准，以及真实自动报价功能的启用。无论验收结果如何，都不得自动合入 `main`。
- 网页端 GPT 的验收只对验收请求中的精确完整 PR head SHA 有效；Luna 推送新提交后，旧验收立即失效。

## 分支与任务边界

- 只能使用 `luna/TASK-xxx-short-name` 分支；禁止直接修改 `main`。
- PR base 固定为 `integration/auto-buyer-offer`；一个 PR 只关联或关闭一个任务；默认 Draft。
- 发生 `CHANGES_REQUESTED` 时复用同一 Issue、分支和 PR，交回同一 Luna 任务返工，新 SHA 必须重新验收。
- 发生 `BLOCKED` 时标记 `agent:blocked`，禁止合并并停止所有依赖任务，等待网页端 GPT 或仓库所有者明确解除。

## 安全不变量

- BUFF 契约、字段、状态或错误语义不明确时必须 `BLOCKED`，禁止猜测。
- Cookie、API Key、令牌、`buyer_info` 和加密前会话不得进入日志、Issue、PR 或 fixture。
- 不可幂等写请求不得自动重试；超时或不明确结果进入 `result_unknown`，禁止再次发送。
- 一笔订单只能有一条购买记录；首次报价须在付款记录落库后、购买下一件前执行。
- 报价前核对购买记录 SteamID、当前凭据 SteamID、BUFF 绑定 SteamID、订单 `buyer_steamid`。
- 不得只按饰品名称绑定库存或授权自动上架；`pending_receipt=True` 表示未确认入库。
- 不得使用 `accept_all`；worker 只负责恢复和对账，不负责首次发送。
- 不得删除或弱化安全测试；测试失败不得宣称完成。

完成前按 [审查清单](.agent/REVIEW_CHECKLIST.md) 自检，并用 [PR 模板](.github/pull_request_template.md) 报告真实测试、故障测试、风险、回滚和未解决问题。未知信息或安全不变量无法证明时保持 `BLOCKED`。
