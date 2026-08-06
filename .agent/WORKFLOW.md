# TASK 工作流

与 [AGENTS.md](../AGENTS.md)、[TASK_TEMPLATE.md](TASK_TEMPLATE.md)、[REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md)、[PR 模板](../.github/pull_request_template.md) 配套。

## 开始

1. 父代理分配一个 TASK，明确目标、允许/禁止文件、基线、分支和验收标准。
2. 确认分支 luna/TASK-xxx-short-name、PR base integration/auto-buyer-offer，禁止 main。
3. 读取 Issue、实现和测试；BUFF 契约或结果语义不明确即 BLOCKED，不猜测。
4. 按模板建立边界；超出范围先回父代理；记录预期测试，不把未运行命令写成通过。

## 执行

只改允许文件，不删/弱化测试，不改无关业务。不可幂等写请求不自动重试；不明确结果为 result_unknown 且不重发。凭据、Cookie、令牌、buyer_info 和加密前会话不得进入协作产物。失败、范围冲突、依赖缺失或契约不确定时 BLOCKED。保持付款后首次报价时序、四方 SteamID 校验、精确库存绑定、pending_receipt=True 语义及 worker 职责。

## 交付

按 [REVIEW_CHECKLIST.md](REVIEW_CHECKLIST.md) 检查范围、安全、测试和链接；执行匹配测试并如实报告。使用 [PR 模板](../.github/pull_request_template.md)，创建 Draft PR（base 固定、只关联一个任务）；Luna 不审查或合并自己的 PR。返回 SHA、PR URL、命令结果、风险和未解决问题，按仓库所有者规定的风险等级完成所需审查并等待父代理验收。