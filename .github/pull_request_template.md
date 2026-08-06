## 对应任务

- TASK：TASK-XXX
- Issue：Closes #XXX
- base：`integration/auto-buyer-offer`
- 风险等级：LOW / MEDIUM / HIGH / CRITICAL
- 本 PR 只关联或关闭一个任务；默认 Draft。

## 角色与验收状态

- Luna：实现、测试、自检和 Draft PR；不负责最终验收。
- Sol：派单、GitHub 状态、生成验收请求、原样传递网页端 GPT 结论并按结论执行；不得自行批准。
- 网页端 GPT：针对精确完整 PR head SHA 作出最终验收。
- 仓库所有者：负责 integration → main 最终批准和真实功能启用。
- 当前 head SHA：
- CI：passed / failed / not_configured
- 网页端 GPT 验收状态：pending / awaiting-gpt-review / APPROVE / CHANGES_REQUESTED / BLOCKED
- REVIEWED_HEAD_SHA：
- 新提交后旧验收立即失效。

## 修改范围

<!-- 列出实际修改的任务白名单文件。 -->

## 未修改范围

确认未修改：任务禁止范围、无关业务代码、现有业务测试、依赖、配置和 `main`。

## 行为变化

<!-- 文档-only 也须明确说明。 -->

## 安全不变量

- [ ] 未猜测 BUFF 契约；不确定即 `BLOCKED`。
- [ ] 未记录 Cookie、API Key、令牌、`buyer_info` 或加密前会话。
- [ ] 不可幂等写请求未自动重试；不明确结果为 `result_unknown` 且不重发。
- [ ] 订单单记录、付款后首次报价、四方 SteamID、精确库存绑定均保持。
- [ ] `pending_receipt=True` 未改变；无 `accept_all`；worker 不负责首次发送。
- [ ] 未删除或弱化测试。

## 检查或测试命令及真实结果

- 命令：
- 真实结果：

## 故障测试

- 场景或命令：
- 真实结果：

## 风险

## 回滚

## 未解决问题

## Luna 自检报告

### 修改范围检查

### 验收标准逐项核对

### 检查/测试命令和真实结果

### 自查过程中发现并修复的问题

<!-- 如未发现，明确填写“未发现”。 -->

### 剩余风险

### 未解决问题

## 网页端 GPT 验收原文

<!-- Sol 必须粘贴完整原文，不得概括、改写或删除阻塞项。 -->

## 交付声明

我没有审查或合并自己的 PR；已按 [.agent/REVIEW_CHECKLIST.md](../.agent/REVIEW_CHECKLIST.md) 自检。网页端 GPT 未响应时保持 `awaiting-gpt-review`；任何新提交均使旧验收失效；无论验收结果如何都不得自动合入 `main`。
