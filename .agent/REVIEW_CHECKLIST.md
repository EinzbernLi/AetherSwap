# TASK / PR 审查清单

配合 [AGENTS.md](../AGENTS.md)、[WORKFLOW.md](WORKFLOW.md)、[TASK_TEMPLATE.md](TASK_TEMPLATE.md)、[PR 模板](../.github/pull_request_template.md)。

## 范围

- [ ] 只有一个 TASK，Issue、目标和验收可追溯。
- [ ] 分支符合 luna/TASK-xxx-short-name，base 为 integration/auto-buyer-offer，未改 main。
- [ ] 文件严格在白名单；无业务、无关测试、依赖或配置变化。
- [ ] 未删除/弱化安全测试；Markdown 可渲染且交叉引用有效。

## 安全

- [ ] BUFF 不明确即 BLOCKED，未猜字段/状态。
- [ ] 未泄露 Cookie、API Key、令牌、buyer_info 或加密前会话。
- [ ] 不可幂等写请求无自动重试；不明确结果为 result_unknown 且不重发。
- [ ] 每笔订单至多一条购买记录；付款落库后才首次报价。
- [ ] 报价前完成四方 SteamID 核对；库存非仅按名称绑定。
- [ ] pending_receipt=True 语义未变；无 accept_all；worker 不首次发送。

## 证据

- [ ] Luna 已检查完整 diff，并在 PR 填写范围检查、逐项验收、测试真实结果、发现并修复的问题、剩余风险和未解决问题。
- [ ] 测试、故障测试、真实结果、风险、回滚、未解决问题已填写。
- [ ] 不确定、超范围或门禁失败时为 BLOCKED 并交父代理。
- [ ] Luna 未审查/合并自己的 PR；已按仓库所有者规定的风险等级安排所需审查。