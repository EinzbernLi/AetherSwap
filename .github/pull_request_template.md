## 对应任务

- TASK：TASK-XXX
- Issue：Closes #XXX
- base：integration/auto-buyer-offer
- 本 PR 只关联/关闭一个任务；默认 Draft。

## 修改范围

<!-- 实际修改的任务白名单文件 -->

## 未修改范围

确认未修改：任务禁止范围、无关业务代码、现有业务测试、依赖和 main。

## 行为变化

<!-- 文档-only 也明确说明 -->

## 安全不变量

- [ ] 未猜测 BUFF 契约；不确定即 BLOCKED。
- [ ] 未记录 Cookie、API Key、令牌、buyer_info 或加密前会话。
- [ ] 不可幂等写请求未自动重试；不明确结果为 result_unknown 且不重发。
- [ ] 订单单记录、付款后首次报价、四方 SteamID、精确库存绑定均保持。
- [ ] pending_receipt=True 未改变；无 accept_all；worker 不负责首次发送。
- [ ] 未删除/弱化测试。

## 测试命令及真实结果

- 命令：
- 真实结果：

## 故障测试

- 场景/命令：
- 真实结果：

## 风险

## 回滚

## 未解决问题

## Luna 自检报告

### 修改范围检查

### 验收标准逐项核对

### 测试命令和真实结果

### 自查过程中发现并修复的问题

<!-- 如未发现，明确填写“未发现”。 -->

### 剩余风险

### 未解决问题

## 交付声明

我没有审查或合并自己的 PR；已按 [.agent/REVIEW_CHECKLIST.md](../.agent/REVIEW_CHECKLIST.md) 自检，并按仓库所有者的风险策略等待所需审查和父代理验收。