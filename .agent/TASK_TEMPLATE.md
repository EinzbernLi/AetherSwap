# TASK-XXX 任务卡

父代理填写边界；一次只执行一个 TASK。流程见 [AGENTS.md](../AGENTS.md) 和 [WORKFLOW.md](WORKFLOW.md)。

## 信息

- TASK：
- Issue：
- 父任务：
- 分支：luna/TASK-xxx-short-name
- PR base：integration/auto-buyer-offer
- 状态：READY / RUNNING / BLOCKED / DONE
- 依赖：

## 目标与边界

目标：

允许修改（精确路径，其他默认禁止）：

- 

禁止：main、未列文件、无关业务/测试/依赖/配置、删除或弱化现有安全测试。

验收标准：

- 
- 

## 安全门禁

- [ ] BUFF 契约不明确即 BLOCKED，不猜测。
- [ ] 不记录 Cookie、API Key、令牌、buyer_info 或加密前会话。
- [ ] 不可幂等写请求不自动重试；不明确结果为 result_unknown 且不重发。
- [ ] 保持订单单记录、付款后首次报价、四方 SteamID、精确库存绑定。
- [ ] 保持 pending_receipt=True；不使用 accept_all；worker 不负责首次发送。
- [ ] 失败如实报告，不删除/弱化测试。

## 交付

- 测试命令及真实结果：
- 故障测试及真实结果：
- 风险：
- 回滚：
- 未解决问题：

完成前使用 [REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md) 和 [PR 模板](../.github/pull_request_template.md)。
