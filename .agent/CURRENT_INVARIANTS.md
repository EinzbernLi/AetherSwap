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
## 分析方法

所有源码结论以 integration/auto-buyer-offer 的精确基线 SHA ce3cfec5d21f5375852a6050582f59debc56048c 为准，并通过远端文件引用与只读源码核对；测试证据只记录真实存在且名称匹配的测试，不把测试覆盖写成代码保证。阶段 A 完成不变量 1—4；阶段 B、C、D 尚未完成。

## 证据索引（阶段 A）

| 文件路径 | 类或函数 | 关键字段/状态 | 相关测试 | 初步涉及不变量 |
| --- | --- | --- | --- | --- |
| app/database.py | Purchase、init_db、db_append_purchase、db_update_purchase | buff_order_id、bill_order_id、buff_sell_order_id、pending_receipt、assetid、部分唯一索引 | tests/test_buff_purchase_persistence.py::test_purchase_round_trip_keeps_buff_reconciliation_ids | 1、2 |
| app/state.py | State.append_purchase | 采购记录追加 | tests/test_buff_purchase_flow_safety.py | 1 |
| app/pipeline_steps.py | _do_wait_payment_and_append、_do_batch_wait_finalize_and_append、_persist_matches、_try_single_buy | 付款后落库、批次 ID、未知结果、pending_receipt | tests/test_buff_purchase_flow_safety.py、tests/test_buff_batch_integrity.py | 1、2、3 |
| app/services/buff_checkout_guard.py | begin_checkout、update_checkout、resolve_checkout、acknowledge_checkout | 持久化 journal、unresolved、intent/stage | tests/test_buff_checkout_guard.py、tests/test_buff_checkout_integration.py | 3 |
| buff/buyer.py | lock_and_get_pay_url、ask_seller_to_send | BUFF order、bill_orders、卖家发货提醒 | tests/test_buff_ask_seller.py、tests/test_buff_purchase_flow_safety.py | 2、3、4 |
| app/services/buff_client.py | lock_and_get_pay_url、ask_seller_to_send | BUFF 客户端转发、supports_batch_buy | tests/test_buff_batch_integrity.py | 2、4 |

## 阶段 A 不变量

### 1. Purchase 记录何时创建

**结论**

正常单笔路径在付款确认成功后才创建 Purchase；记录包括商品、价格、订单标识和 pending_receipt=True。批量路径在批量最终化成功或取得可持久化的部分成功结果后，按已匹配结果追加记录。付款取消、订单未创建、结果未知或批量未完整匹配时，流水线不会继续购买下一件；但数据库追加函数本身不是幂等的 get-or-create 接口。

**保证等级**

PARTIALLY_GUARANTEED：采购流水线的正常时序和异常停止由代码控制，但“任何调用场景下一笔购买绝不产生两条记录”没有被单一事务或幂等键完整保证。TEST_COVERED：现有安全测试覆盖付款取消、未知结果、批量部分成功和已提交金额计数等路径。

**代码证据**

- 文件：app/pipeline_steps.py；app/database.py；app/state.py
- 类/函数：_do_wait_payment_and_append、_do_batch_wait_finalize_and_append、_persist_matches、State.append_purchase、db_append_purchase
- 字段或状态：pending_receipt、buff_order_id、bill_order_id、buff_sell_order_id、batch_id、PurchaseWriteResultUnknown、PurchaseOrderCreatedPending
- 关键控制流：单笔路径在付款等待成功后构造 base_rec 并追加；批量路径在最终化结果核验后按匹配项追加，部分结果先持久化并抛出终止状态；db_append_purchase 直接 session.add 后提交，没有查询已有记录或重试幂等逻辑。

**测试证据**

- 测试文件：tests/test_buff_purchase_flow_safety.py；tests/test_buff_batch_integrity.py
- 测试函数：test_single_order_payment_cancel_does_not_lock_again、test_unknown_write_result_stops_entire_pipeline_with_explicit_status、test_partial_batch_finalize_records_committed_items_and_halts、test_batch_finalize_unknown_persists_prior_successes_before_halting、test_purchase_amount_is_counted_when_post_commit_shipping_prompt_is_blocked
- 实际覆盖内容：付款取消不再次锁单；写结果未知停止流水线；批量部分成功和未知最终化结果保存已知成功项并停止；已提交购买金额被正确计数。
- 未覆盖内容：没有证明跨进程重复调用 db_append_purchase 的幂等性；没有证明所有异常重入场景均只产生一条记录。

**当前缺口或风险**

db_append_purchase 不负责去重；单笔 helper 的 for range(num) 在传入大于 1 时会尝试重复写入同一 buff_order_id。正常入口通常传入一件，但这不是数据库级的完整购买事实模型。批量记录的一条记录对应一个已匹配卖家商品，而不是无条件一条订单一条记录。

**后续自动报价实现约束**

- 必须在已确认付款且订单标识已持久化后，才能产生对应的报价动作。
- 必须禁止因超时、进程重启或后台循环重复执行而再次创建同一购买事实。
- 必须以持久化的 Purchase 记录和唯一业务订单标识做幂等核对，不能仅依赖内存状态。
- 禁止把提醒卖家发货或报价发送成功当作 Purchase 创建前提之外的隐式重试条件。

### 2. buff_order_id、bill_order_id、buff_sell_order_id 的用途

**结论**

当前实现把 buff_order_id 作为 BUFF 买单主标识并建立非空部分唯一索引；单笔路径同时保存 buff_sell_order_id，但不填 bill_order_id。批量路径将批量账单/买单标识保存为 buff_order_id 和 bill_order_id，并将每个卖家行的 ID 保存为 buff_sell_order_id，以便一组 batch_id 下逐项对账。bill_order_id 与 buff_sell_order_id 没有数据库唯一约束。

**保证等级**

CODE_GUARANTEED：字段写入映射和数据库已存在的 buff_order_id 部分唯一索引由当前代码直接给出。TEST_COVERED：持久化往返及批量 ID 完整性有测试覆盖。对字段在所有外部 BUFF 响应形态下的语义，仍只按当前调用方使用方式解释。

**代码证据**

- 文件：app/database.py；app/pipeline_steps.py；buff/buyer.py
- 类/函数：Purchase、init_db、_do_wait_payment_and_append、_persist_matches、_validate_unique_batch_matches、lock_and_get_pay_url、ask_seller_to_send
- 字段或状态：buff_order_id、bill_order_id、buff_sell_order_id、batch_id、bill_orders
- 关键控制流：init_db 创建 ux_purchase_buff_order_id，条件是 buff_order_id 非空；单笔记录写入买单 ID和卖单 ID；批量匹配先拒绝空值/重复账单或卖单 ID，再逐项写入；卖家提醒请求按去重后的 bill_orders 一次发送。

**测试证据**

- 测试文件：tests/test_buff_purchase_persistence.py；tests/test_buff_purchase_flow_safety.py；tests/test_buff_batch_integrity.py；tests/test_buff_ask_seller.py
- 测试函数：test_purchase_round_trip_keeps_buff_reconciliation_ids、test_successful_batch_records_reconciliation_identifiers、test_each_batch_bill_id_is_durable_before_the_next_finalize_post、test_all_batch_ids_are_durable_before_local_db_append_failure、test_finalize_skips_duplicate_sell_rows_without_sending_duplicate_post、test_order_ids_are_normalized_and_deduplicated_in_one_request
- 实际覆盖内容：记录往返保留对账 ID；批量成功时 ID 写入；逐项最终化前 ID 可持久化；重复卖家行不重复发送；提醒请求对订单 ID 规范化和去重。
- 未覆盖内容：没有数据库唯一约束测试证明 bill_order_id 或 buff_sell_order_id 全局唯一；没有证明外部字段在所有 BUFF 版本中的业务语义。

**当前缺口或风险**

bill_order_id 既可能为空（单笔路径），也可能在批量路径按商品项存在；不能把它假设成单笔全局订单号。由于只有 buff_order_id 有部分唯一索引，错误映射或重复写入其他两个字段不会由数据库直接阻止。

**后续自动报价实现约束**

- 必须保留三类 ID 的原始用途和逐项映射，禁止把批量 bill_order_id 当作唯一单品 ID。
- 必须在报价前核对本地 Purchase、BUFF 绑定 SteamID、当前凭据 SteamID 和订单 buyer SteamID；当前代码未完整证明该核对，不能省略。
- 必须对已发送或结果未知的非幂等请求使用持久化状态阻止盲目重发。
- 禁止通过商品名称或未确认的 ID 组合推导报价归属。

### 3. checkout 结果未知时如何阻止继续购买

**结论**

checkout guard 在非幂等 BUFF checkout 首次请求前写入持久化 journal，并以进程内锁和跨进程文件锁保护。请求结果未知时将状态保留为 unresolved，启动门禁和后台 BUFF 活动门禁阻止新的采购或相关后台请求；只有显式、精确匹配的 intent acknowledgement 才能解除重启阻塞。损坏、非法或不可读 journal 走 fail-closed；首次没有 journal 不代表存在未完成 checkout。

**保证等级**

CODE_GUARANTEED：当前 guard 的写前 journal、unresolved 状态和门禁控制流明确。TEST_COVERED：持久化阻塞、损坏/重复键/非有限数字/非法 journal 和精确 acknowledgement 有测试覆盖。此结论不延伸到所有未接入 guard 的外部写路径。

**代码证据**

- 文件：app/services/buff_checkout_guard.py；app/pipeline.py；app/pipeline_steps.py
- 类/函数：begin_checkout、update_checkout、resolve_checkout、acknowledge_checkout、_read_raw_locked、get_pipeline_start_blocker、start_pipeline、_try_single_buy
- 字段或状态：intent_id、unresolved、stage、goods_id、quantity、batch_id、write_result_unknown
- 关键控制流：begin_checkout 在请求前原子写入 intent_prepared；未知异常更新为 unresolved 阶段；get_pipeline_start_blocker/start_pipeline 拒绝未解除 intent；非法 journal 被转换为 unresolved；已 acknowledgement 必须匹配当前 intent ID。

**测试证据**

- 测试文件：tests/test_buff_checkout_guard.py；tests/test_buff_checkout_integration.py；tests/test_buff_purchase_flow_safety.py
- 测试函数：test_intent_is_durable_secret_free_and_blocks_a_second_checkout、test_corrupt_journal_fails_closed、test_duplicate_journal_keys_fail_closed、test_non_finite_journal_numbers_fail_closed、test_parseable_but_invalid_journal_fails_closed、test_unresolved_checkout_blocks_restart_until_explicit_ack、test_restart_ack_requires_the_exact_displayed_intent_id、test_transport_unknown_write_is_converted_to_terminal_purchase_state
- 实际覆盖内容：journal 在第二次 checkout 前阻止；损坏/非法数据 fail-closed；重启需精确 intent acknowledgement；传输未知结果转为终止采购状态。
- 未覆盖内容：未证明所有未来新增 BUFF 写 API 都接入 guard；未证明人工删除或外部替换 journal 后的业务恢复安全性。

**当前缺口或风险**

guard 的无文件状态表示尚无已记录 intent，不是对外部系统没有历史写入的证明。当前安全保证依赖所有非幂等入口正确调用 guard；未接入的新写路径可能绕过它。未知结果没有自动重发证据。

**后续自动报价实现约束**

- 必须在每个非幂等 BUFF/Steam 写请求前先持久化可恢复 intent，并在结果未知时保持 unresolved。
- 必须禁止未知结果下的自动重试、继续购买和自动报价。
- 必须使用精确 intent ID 做人工恢复确认；禁止用重新开始或模糊订单匹配清除阻塞。
- 禁止以清空、删除或覆盖 journal 代替对账。

### 4. 卖家报价如何匹配本地 Purchase

**结论**

当前购买流水线不是接收卖家报价后再查 Purchase，而是在本地付款/购买记录成功落库后，按已有 bill_order_id 列表调用 ask_seller_to_send，向 BUFF 请求卖家发货。批量路径先按批次匹配并校验卖家行 ID，再落库；单笔路径保存买单/卖单 ID但不保存 bill_order_id。因此可以证明“本地记录驱动卖家发货提醒”的控制流，不能证明 Steam 收到的报价会按 SteamID、报价方向和单一 Purchase 完整匹配。

**保证等级**

PARTIALLY_GUARANTEED：BUFF 侧提醒使用已持久化/已校验的账单 ID，且失败或未知不会自动重试；Steam 入站接收路径没有展示以 offer 的 sender、partner、SteamID 做严格卖家报价匹配。TEST_COVERED：账单 ID 去重、逐项状态和未知结果有测试，但没有端到端 Steam 报价归属测试。

**代码证据**

- 文件：app/pipeline_steps.py；buff/buyer.py；app/receive_flow.py
- 类/函数：_do_batch_wait_finalize_and_append、_persist_matches、ask_seller_to_send、fetch_buff_steam_trade、_match_purchase_for_item、try_receive_once
- 字段或状态：bill_order_id、buff_sell_order_id、batch_id、tradeofferid、goods_id、market_hash_name、pending_receipt
- 关键控制流：本地 Purchase 记录后可选调用卖家发货提醒；接收侧只筛选 state == 1 且有 tradeofferid/items_to_trade 的远程交易，再以 goods ID 或唯一名称候选映射本地 pending Purchase；接收函数不核对报价发送方或 SteamID。

**测试证据**

- 测试文件：tests/test_buff_ask_seller.py；tests/test_buff_receive_mapping_safety.py
- 测试函数：test_all_bill_orders_are_sent_once_and_all_ok_is_success、test_partial_per_order_failure_is_not_complete_success、test_top_level_failure_is_not_retried、test_unknown_write_result_is_never_retried、test_exact_goods_id_match_beats_older_same_name_fallback、test_ambiguous_name_only_match_is_rejected、test_unmatched_offer_is_never_accepted_or_scanned
- 实际覆盖内容：BUFF 卖家提醒请求的订单去重、逐项成功判断和未知结果不重试；接收侧 goods ID 优先、名称歧义拒绝、未匹配报价不接受。
- 未覆盖内容：没有测试证明报价 sender/partner/SteamID 与 Purchase 所属账户严格相等；没有测试排除自己发出的 outbound offer；没有端到端证明一个报价只能终结一个 Purchase。

**当前缺口或风险**

fetch_buff_steam_trade 和接收映射没有方向/发送方约束；accept_steam_trade_offer 的 partner 为空。名称唯一只解决局部歧义，不是 Steam 所有权证明。当前不能把卖家发货提醒成功等同于 Steam 报价已经匹配并安全入库。

**后续自动报价实现约束**

- 必须按持久化 Purchase 的订单 ID、商品 ID、SteamID 和报价方向进行匹配；名称只能作为展示或最后的人工核对信息。
- 必须禁止接受 sender/partner/SteamID 未严格验证的报价，包括自己发出的 outbound offer。
- 必须在确认收货前保持 pending_receipt=True，禁止报价动作提前改变库存归属。
- 禁止把卖家发货提醒成功、HTTP 成功或报价存在视为已收货；必须有可验证的 Steam 收货终结证据。

## 阶段 A 额外安全核查

- 一次购买是否可能产生两条 Purchase：正常流水线按一次付款确认追加一次，批量按匹配项追加；但 db_append_purchase 非幂等，绝对唯一性未被完整证明，结论为 PARTIALLY_GUARANTEED。
- buff_order_id 是否有数据库唯一约束：有非空、非空字符串条件的部分唯一索引；不是对空值或其他 ID 的全局唯一约束。
- bill_order_id 是否可能是单笔或批量形态：是；单笔记录当前可为空，批量记录按项保存并由 batch_id 关联。
- checkout guard 是否持久化并 fail-closed：已看到持久化 journal、写前保护及非法 journal fail-closed；仅适用于已接入 guard 的路径，结论为 CODE_GUARANTEED + TEST_COVERED（范围有限）。
- 写结果未知后是否存在自动重试路径：对 BUFF 买单、卖家提醒和 Steam 接收 POST，未发现自动重发；接收侧会做库存轮询/对账，不等于重发。未接入 guard 的未来路径仍 NOT_GUARANTEED。
- buyer-send 与 seller-send 当前是否真正区分：BUFF 买单创建与 ask_seller_to_send 是不同 API；未发现 buyer 侧 Steam outbound send 实现，Steam 接收流程也未做方向核对，完整区分仍 NOT_GUARANTEED。

## 阶段 A 状态

不变量 1—4 已完成初步证据整理；不变量 5—12、最终交叉验证、CI 状态和 GPT 验收尚未完成。本文档仍为 WIP，不能据此宣称自检完成。