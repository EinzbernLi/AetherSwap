# TASK-002 当前交易安全不变量

> 文档状态：WIP
>
> 本文档是 TASK-002 / Issue #4 的独立顶层执行初始索引，当前内容尚未验收，不得视为 READY、GPT 已审查或业务保证。

## 范围与基线

- TASK：TASK-002
- Issue：#4
- 仓库：EinzbernLi/AetherSwap
- base：`integration/auto-buyer-offer`
- base SHA：`ce3cfec5d21f5375852a6050582f59debc56048c`
- head 分支：`luna/TASK-002-current-invariants`
- 执行器身份：`OWNER_ATTESTED`
- 声明模型/思考等级：`gpt-5.6-luna / high`
- 平台实际元数据：未提供；不声称 `PLATFORM_VERIFIED` 或 `CONFIG_VERIFIED`
- 真实 BUFF/Steam 写操作：未执行

## 证据状态枚举

- `CODE_GUARANTEED`：当前代码控制流直接支持该行为；不等同于测试覆盖。
- `TEST_COVERED`：指定测试实际覆盖该行为；不等同于所有代码路径均安全。
- `PARTIALLY_GUARANTEED`：代码或测试只支持部分条件，必须说明边界。
- `NOT_GUARANTEED`：当前代码或测试不能保证该安全性质，不能写成实现承诺。
- `UNKNOWN`：现有证据不足以判断，不能用推测补足。

## 12 项目录

1. Purchase 记录何时创建
2. `buff_order_id`、`bill_order_id`、`buff_sell_order_id` 的用途
3. checkout 结果未知时如何阻止继续购买
4. 卖家报价如何匹配本地 Purchase
5. 收货前库存基线如何建立
6. 买家侧新增 `assetid` 如何识别
7. 自动上架为何必须使用精确 `assetid`
8. `pending_receipt` 的当前真实语义
9. `steam_confirm.accept_all` 的适用范围和风险
10. `sync_sold` 按名称补 `assetid` 的风险
11. 后台 worker 与采购流水线之间的互斥关系
12. 后续自动报价绝对不能破坏的行为

## 证据索引（初始）

| 文件路径 | 类或函数 | 关键字段 | 相关测试 | 初步涉及不变量 |
|---|---|---|---|---|
| 待从 `integration/auto-buyer-offer` 逐项读取 | 待核验 | 待核验 | 待核验 | 1–12 |

## 验收状态

- 当前状态：WIP。
- 12 项结论尚未完成。
- 代码证据与测试证据尚未完成交叉验证。
- 本文档不是旧子代理的交付物，也不引用旧子代理作为本次执行证明。