# AetherSwap 协作与安全规则

入口：流程见 [.agent/WORKFLOW.md](.agent/WORKFLOW.md)，模板见 [.agent/TASK_TEMPLATE.md](.agent/TASK_TEMPLATE.md)，审查见 [.agent/REVIEW_CHECKLIST.md](.agent/REVIEW_CHECKLIST.md)，PR 见 [.github/pull_request_template.md](.github/pull_request_template.md)。

- Luna 一次只能执行一个 TASK，只能修改任务卡允许文件；不得扩大范围。
- 只能使用 luna/TASK-xxx-short-name 分支，禁止直接修改 main；PR base 为 integration/auto-buyer-offer；一个 PR 只关闭一个任务；默认 Draft。
- BUFF 契约、字段、状态或错误语义不明确时必须 BLOCKED，禁止猜测。
- Cookie、API Key、令牌、buyer_info 和加密前会话不得进入日志、Issue、PR 或 fixture。
- 不可幂等写请求不得自动重试；超时/不明确结果进入 result_unknown，禁止再次发送。
- 一笔订单只能有一条购买记录；首次报价须在付款记录落库后、购买下一件前执行。
- 报价前核对购买记录 SteamID、当前凭据 SteamID、BUFF 绑定 SteamID、订单 buyer_steamid。
- 不得只按饰品名称绑定库存或授权自动上架；pending_receipt=True 表示未确认入库。
- 不得使用 accept_all；worker 只负责恢复和对账，不负责首次发送。
- 不得删除或弱化安全测试；测试失败不得宣称完成。

完成前按 [审查清单](.agent/REVIEW_CHECKLIST.md) 自检，并用 [PR 模板](.github/pull_request_template.md) 报告真实测试、故障测试、风险、回滚和未解决问题。未知信息或安全不变量无法证明时保持 BLOCKED。
