# TASK / PR 审查清单

配合 [AGENTS.md](../AGENTS.md)、[WORKFLOW.md](WORKFLOW.md)、[TASK_TEMPLATE.md](TASK_TEMPLATE.md)、[PR 模板](../.github/pull_request_template.md)。

## 角色与范围

- [ ] Luna 只负责实现、测试、自检和 Draft PR；未审查或合并自己的 PR。
- [ ] Sol 只负责派单、GitHub 状态、验收请求、原样传递结论和按结论执行；未自行批准 PR。
- [ ] 网页端 GPT 是最终验收者；仓库所有者保留 integration → main 和真实功能启用权。
- [ ] 只有一个 TASK，Issue、目标和验收可追溯。
- [ ] 分支符合 `luna/TASK-xxx-short-name`，base 为 `integration/auto-buyer-offer`，未改 `main`。
- [ ] 文件严格在任务白名单；无业务、无关测试、依赖或配置变化。
- [ ] Markdown 可渲染，中文 UTF-8 可读，交叉引用和相对链接有效。

## 安全

- [ ] BUFF 不明确即 `BLOCKED`，未猜字段或状态。
- [ ] 未泄露 Cookie、API Key、令牌、`buyer_info` 或加密前会话。
- [ ] 不可幂等写请求无自动重试；不明确结果为 `result_unknown` 且不重发。
- [ ] 每笔订单至多一条购买记录；付款落库后才首次报价。
- [ ] 报价前完成四方 SteamID 核对；库存非仅按名称绑定。
- [ ] `pending_receipt=True` 语义未变；无 `accept_all`；worker 不首次发送。
- [ ] 未删除或弱化安全测试。

## Luna 自检证据

- [ ] 已阅读完整 diff，核对仅任务白名单文件。
- [ ] 已远程回读修改后的文件，确认中文可读且未被替换为问号。
- [ ] 已检查相对链接、Markdown 结构和五文件角色定义一致。
- [ ] PR 描述与实际 diff 一致，包含修改范围、逐项验收、真实命令和结果、发现并修复的问题、剩余风险及未解决问题。
- [ ] 测试、故障测试、风险、回滚和未解决问题如实填写。
- [ ] CI 真实写为 `passed`、`failed` 或 `not_configured`；不得把 `not_configured` 写成 `passed`。

## 网页端 GPT 门禁

- [ ] 未响应时状态为 `awaiting-gpt-review`，没有替代验收。
- [ ] Sol 将网页端 GPT 完整正文原样写回 PR，没有概括、改写或删除阻塞项。
- [ ] `REVIEWED_HEAD_SHA` 与当前完整 PR head SHA 精确相等。
- [ ] 新提交后旧验收已失效并重新发起验收。
- [ ] `CHANGES_REQUESTED` 交回同一 Luna 任务；未创建重复 Issue、分支或 PR。
- [ ] `BLOCKED` 时禁止合并并停止所有依赖任务。
- [ ] 仅在 APPROVE、CI、范围、base、依赖和阻塞门禁全部满足时合入 integration。
- [ ] 无论验收结果如何，都未自动合入 `main`。
