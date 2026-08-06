# TASK-002 当前交易安全不变量

> 文档状态：READY_FOR_GPT_REVIEW
>
> 本文档是 TASK-002 / Issue #4 的独立顶层执行记录，已完成自检并请求 GPT 验收；不表示已批准、合并或形成业务代码保证。

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

## 分析方法

所有源码结论以 integration/auto-buyer-offer 的精确基线 SHA ce3cfec5d21f5375852a6050582f59debc56048c 为准，并通过远端文件引用与只读源码核对；测试证据只记录真实存在且名称匹配的测试，不把测试覆盖写成代码保证。阶段 A—D 已完成，当前状态以阶段 D 最终交叉验证与交付记录为准。

## 证据索引

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

PARTIALLY_GUARANTEED：字段写入映射可由当前代码确认，但 init_db 只是尝试创建 buff_order_id 的非空部分唯一索引，创建异常会被直接吞掉；现有测试未验证索引实际存在或重复写入被拒绝。TEST_COVERED：持久化往返及批量 ID 完整性有测试覆盖。对字段在所有外部 BUFF 响应形态下的语义，仍只按当前调用方使用方式解释。

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

bill_order_id 既可能为空（单笔路径），也可能在批量路径按商品项存在；不能把它假设成单笔全局订单号。由于代码只尝试为 buff_order_id 创建部分唯一索引，且创建异常被吞掉，不能仅凭源码确认该约束实际存在；错误映射或重复写入其他两个字段不会由数据库直接阻止。

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
- buff_order_id 是否有数据库唯一约束：init_db 尝试创建只针对非空、非空字符串的部分唯一索引，但创建异常被直接吞掉；现有测试未验证索引实际存在或重复写入被拒绝，因此不能把它视为完整唯一性保证。
- bill_order_id 是否可能是单笔或批量形态：是；单笔记录当前可为空，批量记录按项保存并由 batch_id 关联。
- checkout guard 是否持久化并 fail-closed：已看到持久化 journal、写前保护及非法 journal fail-closed；仅适用于已接入 guard 的路径，结论为 CODE_GUARANTEED + TEST_COVERED（范围有限）。
- 写结果未知后是否存在自动重试路径：对 BUFF 买单、卖家提醒和 Steam 接收 POST，未发现自动重发；接收侧会做库存轮询/对账，不等于重发。未接入 guard 的未来路径仍 NOT_GUARANTEED。
- buyer-send 与 seller-send 当前是否真正区分：BUFF 买单创建与 ask_seller_to_send 是不同 API；未发现 buyer 侧 Steam outbound send 实现，Steam 接收流程也未做方向核对，完整区分仍 NOT_GUARANTEED。

## 阶段 B 不变量

### 5. 收货前库存基线如何建立

**结论**

try_receive_once 在接受 Steam 报价前要求读取当前收件方库存；对每个物品记录收货前 assetid 基线，并在后续轮询中排除基线及已使用 assetid。预收货快照失败时不会接受报价。该基线是一次收货尝试的本地时间点快照，不是跨重启、跨报价或跨账户的持久化所有权证明。

**保证等级**

CODE_GUARANTEED：当前接收控制流在 accept 前取得快照，并按基线过滤新增资产。TEST_COVERED：快照失败不接受、旧同名资产排除和多次轮询等待均有测试。跨进程或快照之后又发生的其他库存变化未被此路径完全证明。

**代码证据**

- 文件：app/receive_flow.py
- 类/函数：try_receive_once、fetch_buff_steam_trade、accept_steam_trade_offer
- 字段或状态：pending_receipt、assetid、market_hash_name、baseline_assetids、already_used
- 关键控制流：先加载 pending Purchase 和远程报价，再读取收件方库存形成 baseline；baseline 缺失或读取失败立即返回；接受成功或结果未知后轮询库存，并只选择不在 baseline/已使用集合中的资产。

**测试证据**

- 测试文件：tests/test_buff_receive_mapping_safety.py
- 测试函数：test_failed_pre_accept_inventory_snapshot_leaves_offer_unaccepted、test_inventory_poll_waits_for_all_new_assets_and_excludes_old_same_name、test_partial_inventory_visibility_never_persists_seller_assetid、test_unknown_accept_result_is_reconciled_from_inventory
- 实际覆盖内容：预接受快照失败保持报价未接受；旧同名资产被排除；部分可见库存不提前落库卖家资产 ID；接受结果未知时通过库存对账。
- 未覆盖内容：没有证明库存快照和 Steam 报价属于同一严格核验的账户；没有跨重启持久化 baseline；没有方向或 sender/partner 校验。

**当前缺口或风险**

库存基线只能说明“本次扫描之前未见的 assetid”，不能单独说明新增资产一定来自目标卖家报价。若账户收到其他并行资产，代码依赖商品名和数量筛选，仍存在归属不确定性。

**后续自动报价实现约束**

- 必须在任何自动收货或后续报价动作前建立并成功读取收货方库存基线。
- 必须排除基线资产、已分配资产和与目标商品不一致的资产。
- 必须把快照失败、部分可见和结果未知视为未终结，不得接受后继续购买。
- 禁止把一次性内存 baseline 当作跨账户或跨报价的所有权证明。

### 6. 买家侧新增 assetid 如何识别

**结论**

当前接收逻辑把收货后库存中不在收货前 baseline、也不在本次已使用集合中的资产视为候选，并按精确 market_hash_name 分组，等待达到目标数量后将新 assetid 写回本地 Purchase，设置 pending_receipt=False。卖家报价中的资产 ID不会直接写入买家记录。

**保证等级**

CODE_GUARANTEED：新增候选的差集、去重、名称分组和按数量等待由 try_receive_once 明确实现。TEST_COVERED：新旧同名资产排除、部分可见和完整数量等待有测试。将新增资产归因于特定卖家报价仍为 PARTIALLY_GUARANTEED，因为代码没有报价发送方/SteamID 方向校验。

**代码证据**

- 文件：app/receive_flow.py
- 类/函数：_match_purchase_for_item、try_receive_once
- 字段或状态：assetid、pending_receipt、goods_id、market_hash_name、baseline_assetids、already_used
- 关键控制流：报价物品先映射到本地 pending Purchase；库存差集按 market_hash_name 聚合；只有数量足够且每个目标记录均可分配时，才逐项写入收件方新 assetid，并将 pending_receipt 置为 false。

**测试证据**

- 测试文件：tests/test_buff_receive_mapping_safety.py
- 测试函数：test_exact_goods_id_match_beats_older_same_name_fallback、test_ambiguous_name_only_match_is_rejected、test_partial_inventory_visibility_never_persists_seller_assetid、test_inventory_poll_waits_for_all_new_assets_and_excludes_old_same_name
- 实际覆盖内容：goods ID 优先于较旧的名称候选；名称歧义不接受；部分可见不会保存错误 assetid；轮询等待所有新资产并排除旧资产。
- 未覆盖内容：没有 sender/partner/SteamID 严格匹配测试；没有证明并行交易或同名外部入账不能被归因到本 Purchase。

**当前缺口或风险**

“新增”是相对本次本地快照的差集，不是 Steam 交易流水中带有唯一交易归属的证明。goods_id 不可用时仍允许唯一名称候选；唯一名称可降低歧义但不能替代账户和报价方向校验。

**后续自动报价实现约束**

- 必须只使用收件方库存中相对可靠 baseline 的新 assetid，并保留逐项分配记录。
- 必须优先使用经过核验的 goods_id，名称匹配只能在明确唯一且有额外归属证据时使用。
- 必须禁止直接采用卖方物品清单中的 assetid 作为买家所有权。
- 在 sender、partner、SteamID 和报价方向未严格核验时，禁止把新增资产用于自动上架或下一笔报价。

### 7. 自动上架为何必须使用精确 assetid

**结论**

sell pipeline 以精确 assetid 查找本地 Purchase 和当前 Steam 库存；缺失、已售、pending_receipt、已上架或 assetid 不再是当前持有资产时会拒绝授权。listing plan 也按精确 assetid 组装并提交 Steam 上架请求。商品名称不是所有权标识。

**保证等级**

CODE_GUARANTEED：当前授权和请求计划均以 assetid 为主键式匹配，并明确拒绝 pending_receipt 和缺失资产。TEST_COVERED：同名错误资产、历史售出资产、当前资产和缺失 assetid 均有安全测试。其他调用方若绕过 sell pipeline 未被这些测试覆盖。

**代码证据**

- 文件：app/sell_pipeline.py
- 类/函数：_find_buy_record、_build_listing_plan、_submit_listings、_run_sell_phase_impl
- 字段或状态：assetid、pending_receipt、listing、listing_status、sold_at、sale_price、market_hash_name
- 关键控制流：_find_buy_record 只按精确 assetid 查 Purchase，并拒绝待收货/已售/已上架；_build_listing_plan 在定价前检查当前库存中同一 assetid；_submit_listings 使用该精确 assetid 发起上架。

**测试证据**

- 测试文件：tests/test_sell_pipeline_ownership_guard.py
- 测试函数：test_historical_sold_same_name_does_not_authorize_different_asset、test_unsold_same_name_does_not_authorize_different_asset、test_exact_asset_must_still_be_a_current_holding、test_exact_current_asset_is_authorized、test_missing_inventory_assetid_fails_closed、test_listing_plan_never_prices_or_lists_same_name_personal_item
- 实际覆盖内容：历史同名、当前未购买同名、已不持有的精确资产和缺失 assetid 均不能授权；当前精确资产可授权；计划不会给同名个人物品定价或上架。
- 未覆盖内容：没有证明所有未来上架入口都调用同一 ownership guard；Steam API 已提交后的外部状态不由本地测试完全证明。

**当前缺口或风险**

卖出请求存在针对 Steam 特定“上一操作完成中”消息的一次特殊重试；这不是 BUFF checkout 的通用重试保证。若其他代码路径只按名称处理库存，仍可能绕过 sell pipeline 的精确所有权保护。

**后续自动报价实现约束**

- 必须使用精确 assetid 进行所有授权、定价、上架和售出对账。
- 必须在请求前确认该 assetid 仍是当前收件账户持有，且本地 Purchase 未售出、未上架、未 pending_receipt。
- 禁止按饰品名称替代 assetid，也禁止因同名历史记录授权当前不同资产。
- 禁止把 Steam 上架请求的特殊恢复重试扩展成未知 BUFF/报价写入的自动重试。

### 8. pending_receipt 的当前真实语义

**结论**

当前代码在购买记录落库时将 pending_receipt 设为 true，表示付款/购买事实已记录但收件方尚未被本地库存差集证明已收到对应资产。receive_flow 成功完成新增 assetid 分配后才设为 false。sell pipeline 明确拒绝 pending_receipt 记录；但 sync_sold 的名称补 assetid 路径可以把缺失 assetid 的记录直接补回并清除 pending_receipt，因而该状态不是全系统不可绕过的收货终结门。

**保证等级**

PARTIALLY_GUARANTEED：主接收路径的状态转换和 sell pipeline 拒绝明确。NOT_GUARANTEED：sync_sold 的名称补全可能绕过基于收货 baseline 的证明。TEST_COVERED：接收映射和 sell ownership guard 有直接测试；没有证明所有后台维护路径均保持 pending_receipt 语义。

**代码证据**

- 文件：app/pipeline_steps.py；app/receive_flow.py；app/sell_pipeline.py；app/sync_sold.py
- 类/函数：_do_wait_payment_and_append、_do_batch_wait_finalize_and_append、try_receive_once、_find_buy_record、_fill_assetid_from_inventory、run_sync_sold_from_history
- 字段或状态：pending_receipt、assetid、listing、listing_status、sold_at、market_hash_name
- 关键控制流：购买追加时 pending_receipt=True；收货库存差集成功分配后改为 false；_find_buy_record 对 pending_receipt 直接拒绝；_fill_assetid_from_inventory 对缺失 assetid 的 Purchase 按名称找库存第一项并写入 assetid、listing=False、listing_status=None、pending_receipt=False。

**测试证据**

- 测试文件：tests/test_buff_receive_mapping_safety.py；tests/test_sell_pipeline_ownership_guard.py；tests/test_repair_error_records.py
- 测试函数：test_partial_inventory_visibility_never_persists_seller_assetid、test_inventory_poll_waits_for_all_new_assets_and_excludes_old_same_name、test_missing_inventory_assetid_fails_closed、test_rebuild_keeps_pending_receipt_when_not_seen_anywhere、test_rebuild_uses_exact_name_matching_for_missing_assetid
- 实际覆盖内容：主接收路径不保存卖家 assetid；不足数量时继续等待；sell pipeline 对缺失 assetid fail-closed；repair 在未发现资产时保留 pending_receipt；repair 的名称匹配行为被单独测试。
- 未覆盖内容：没有证明 sync_sold 名称补全不会把错误的同名资产标成已收货；没有统一的“收货终结”事务或跨模块状态机测试。

**当前缺口或风险**

sync_sold._fill_assetid_from_inventory 仅比较 Purchase 名称与库存 market_hash_name/name，并取第一个未使用 assetid；同名资产可能被错误关联。pending_receipt 因而可能在未验证目标报价归属时被清除，之后 sell pipeline 可能授权错误资产。

**后续自动报价实现约束**

- 必须把 pending_receipt=True 解释为尚未完成可验证收货；它必须阻止自动上架、售出确认和所有权归属，但在 Purchase 已持久化、方向/SteamID 核验和 durable intent 门禁满足时，允许发送用于完成交付的首次报价。
- 首次报价发送不得清除 pending_receipt；只有精确 assetid 收货终结才能清除。
- 必须只有统一收货终结流程在验证 baseline、新资产、商品 ID、账户和报价方向后，才能清除 pending_receipt。
- 禁止 sync_sold 或任何修复流程仅按饰品名称为自动交易清除 pending_receipt。
- 无法证明 assetid 归属时必须保持 pending_receipt 并进入人工对账。

## 阶段 B 额外安全核查

- pending_receipt 是否会被 sell pipeline 处理：会被读取，但 _find_buy_record 明确拒绝，不能进入上架授权。
- pending_receipt 是否会被 sync_sold 处理：会；sync_sold 处理缺失 assetid 的 Purchase，并可能按名称补回 assetid、清除 pending_receipt，这是当前高风险路径。
- assetid 是否存在仅按饰品名称关联的路径：存在于 app/sync_sold.py 的 _fill_assetid_from_inventory；repair 路径也存在缺失 assetid 的精确名称匹配逻辑。主 receive_flow 另有 goods_id 优先、唯一名称兜底。
- 是否存在统一的收货终结入口：try_receive_once 是主收货入口，但没有覆盖 sync_sold 的状态变更，也没有端到端统一终结测试，结论为 UNKNOWN。
- 自动上架是否严格使用 assetid：sell pipeline 的已检查路径是；对绕过该 pipeline 的未来调用方不能推断，结论为 CODE_GUARANTEED（当前路径）而非全局保证。

## 阶段 C 不变量

### 9. steam_confirm.accept_all 的适用范围和风险

**结论**

SteamConfirmer.accept_all 接受传入的全部 confirmations，不按 listing、交易、订单、assetid 或来源类型筛选；auto_confirm_once 会将 get_confirmations 的全部结果交给它。sell pipeline 在上架后可调用自动确认。因此当前方法只适合“调用方已经完成严格筛选”的前提，代码本身没有提供该筛选；没有发现针对 accept_all 的直接安全测试。

**保证等级**

NOT_GUARANTEED：方法名和控制流明确是全量接受，当前代码不能证明传入集合只包含目标上架确认。UNKNOWN：未发现 accept_all 的专门测试，无法证明生产调用方永远传入安全子集。

**代码证据**

- 文件：app/steam_confirm.py；app/sell_pipeline.py
- 类/函数：SteamConfirmer.accept_all、auto_confirm_once、_auto_confirm_listings
- 字段或状态：confirmations、listing、assetid、confirmation type/order 元数据（当前 accept_all 未使用）
- 关键控制流：auto_confirm_once 获取全部 confirmations 后直接调用 accept_all；accept_all 对每个传入 confirmation 执行接受，没有目标类型、assetid 或订单白名单判断；sell pipeline 在上架流程后可触发自动确认。

**测试证据**

- 测试文件：未发现针对 SteamConfirmer.accept_all 或 auto_confirm_once 的直接测试；tests/test_sell_pipeline_ownership_guard.py 只覆盖上架授权，不覆盖 Steam confirmation 接受范围
- 测试函数：没有可引用的 accept_all 专门测试
- 实际覆盖内容：现有 sell ownership 测试证明当前 assetid 上架授权，不证明 confirmation 筛选。
- 未覆盖内容：全量 confirmation、非上架 confirmation、错误账户和错误 assetid 的拒绝行为均未覆盖。

**当前缺口或风险**

若确认列表包含非目标或非上架确认，accept_all 没有本地防线。自动确认还可能与其他 Steam 操作并行；当前不能将“上架计划按 assetid 正确”推导为“确认操作按 assetid 正确”。

**后续自动报价实现约束**

- 必须按确认类型、目标 assetid、订单/上架上下文和账户 SteamID 建立白名单后，才能接受单个确认。
- 必须禁止使用未筛选的 accept_all 承担自动报价、收货或其他 confirmation。
- 必须将确认结果与对应本地 Purchase/Listing 绑定并持久化；不确定结果必须停止自动写入。
- 在完成专门的 confirmation 安全测试前，禁止扩大自动确认范围。

### 10. sync_sold 按名称补 assetid 的风险

**结论**

sync_sold 的 _fill_assetid_from_inventory 对缺失 assetid 的 Purchase，仅用本地名称与库存 market_hash_name/name 相等来找候选，选择第一个未使用 assetid，并清除 pending_receipt。它没有使用收货 baseline、goods_id、报价 sender/partner、SteamID 或交易 offer 唯一标识；因此同名资产可能被错误关联，并被后续售出同步或上架流程当作本地购买资产。

**保证等级**

NOT_GUARANTEED：当前代码直接存在名称-only 关联路径，不能保证资产归属。TEST_COVERED：该路径的名称匹配行为有 repair 测试，但测试覆盖的是现状而不是安全保证。

**代码证据**

- 文件：app/sync_sold.py；app/sell_pipeline.py
- 类/函数：_fill_assetid_from_inventory、run_sync_sold_from_history、_find_buy_record
- 字段或状态：Purchase.name、assetid、pending_receipt、market_hash_name、name、sold_at、listing
- 关键控制流：对缺失 assetid 的记录遍历库存，名称相等即选第一个未使用资产；写入 assetid 并设 pending_receipt=False；随后按 assetid 处理 sold history。此路径不要求目标报价或商品 ID 证据。

**测试证据**

- 测试文件：tests/test_repair_error_records.py；tests/test_sell_pipeline_ownership_guard.py
- 测试函数：test_rebuild_uses_exact_name_matching_for_missing_assetid、test_rebuild_keeps_pending_receipt_when_not_seen_anywhere、test_historical_sold_same_name_does_not_authorize_different_asset
- 实际覆盖内容：repair 名称匹配的现有行为；未发现资产时保留 pending_receipt；sell pipeline 的当前精确 assetid guard 拒绝历史同名错误资产。
- 未覆盖内容：没有证明 sync_sold 的名称补全不会选错同名资产；没有测试将 sync_sold 与实际收货报价、SteamID 和 goods_id 联合核验。

**当前缺口或风险**

该路径会削弱 pending_receipt 的收货语义，形成“名称补 assetid 后可售”的风险链。不能把 repair 测试中的“使用名称匹配”解释成 ownership 保证。

**后续自动报价实现约束**

- 必须禁止 sync_sold 或等价维护路径仅按名称补 assetid、清除 pending_receipt 或授权后续自动交易。
- 必须使用精确 assetid 与可验证收货证据；无法证明时保持 pending_receipt。
- 必须把名称-only 结果标为 UNKNOWN/人工对账，不得作为自动报价前置条件。

### 11. 后台 worker 与采购流水线之间的互斥关系

**结论**

采购流水线使用 auth、BUFF activity 和 pipeline 生命周期锁，并在未解决 checkout、运行中 pipeline 或凭据不安全时阻止相关活动。receive_worker 在安全检查后持有 auth/activity 锁，处理 pending_receipt 收货；listing_check_worker 处理已有 listing/assetid 的售出和维护。worker 不负责首次购买，也不调用 ask_seller_to_send；首次卖家发货提醒由采购路径在记录落库后可选执行。该互斥能阻止当前已接入的并发活动，但不能证明所有未来 Steam/BUFF 写入口都加入同一锁。

**保证等级**

PARTIALLY_GUARANTEED：当前 worker 与 pipeline 的锁和门禁明确，首次报价职责分离明确；全系统互斥因外部/未来入口范围未证明而不是全局 CODE_GUARANTEED。TEST_COVERED：重复 pipeline、checkout 期间后台读取和锁相关路径有测试。

**代码证据**

- 文件：app/services/workers.py；app/pipeline.py；app/services/buff_checkout_guard.py；buff/buyer.py
- 类/函数：receive_worker、listing_check_worker、_buff_background_request_is_safe、_session_keepalive_is_safe、exclusive_pipeline_maintenance、start_pipeline、ask_seller_to_send
- 字段或状态：pipeline running、unresolved checkout、pending_receipt、listing、auth/activity locks、buff_activity_guard
- 关键控制流：worker 在请求前检查 shutdown/unresolved/running/auth 状态并持有共享锁；pipeline 启动和维护使用互斥锁；worker 只做收货、库存/售出、会话维护，不做首次购买或首次卖家提醒。

**测试证据**

- 测试文件：tests/test_pipeline_lock.py；tests/test_buff_checkout_integration.py；tests/test_buff_checkout_guard.py
- 测试函数：test_start_pipeline_rejects_duplicate_running_pipeline、test_start_pipeline_allows_restart_after_thread_exits、test_durable_guard_freezes_credentials_and_background_reads、test_running_pipeline_freezes_manual_credential_replacement、test_unresolved_checkout_blocks_restart_until_explicit_ack
- 实际覆盖内容：重复 pipeline 被拒绝；线程退出后可按规则重启；durable guard 冻结后台读取和凭据替换；未解决 checkout 阻止重启。
- 未覆盖内容：没有覆盖所有 worker 与所有未来写入口的全局锁审计；没有测试 worker 发送首次报价，因为当前实现不承担该职责。

**当前缺口或风险**

后台循环的异常 sleep/recovery 不能被解释为非幂等写请求重试。未来若把首次报价放入 worker，容易绕过当前“付款落库后、购买下一件前”的时序约束；现有代码没有为此提供安全保证。

**后续自动报价实现约束**

- 必须继续由受 guard 和 pipeline 生命周期控制的采购流程承担首次报价时序；worker 禁止承担首次发送。
- 必须在 auth、BUFF activity、pipeline 和 checkout 状态之间保持同一互斥规则。
- 必须禁止后台 worker 在 unresolved 或未知结果时自动重发非幂等请求。
- 新增写入口必须先纳入统一锁和 durable guard，并补充并发测试后才能启用。

### 12. 后续自动报价绝对不能破坏的行为

**结论**

后续自动报价不能破坏以下现有安全行为：非幂等写请求写前建立 durable guard；结果未知进入 unresolved 并停止继续采购；Purchase 及三类 BUFF 对账 ID 按现有映射持久化；收货前保持 pending_receipt；收货使用库存 baseline 和新增 assetid；自动上架使用精确 assetid；sell pipeline 排除待收货和非当前持有资产；worker 不发送首次报价；不使用未筛选的 accept_all；不以名称作为所有权证明。当前代码对 Steam 报价方向、sender/partner 和关键 SteamID 严格相等仍有缺口，因此这些是必须新增的约束而不是当前已实现保证。

**保证等级**

PARTIALLY_GUARANTEED：上述行为在各自已检查路径中分别存在，但没有一个统一状态机覆盖全部 Purchase、BUFF、Steam offer、inventory 和 listing 生命周期。TEST_COVERED：采购未知结果、收货映射、上架 ownership、checkout guard 和锁有分散测试。NOT_GUARANTEED：SteamID 严格四方核对、outbound 自发报价排除、统一收货终结入口和 accept_all 范围均未被当前测试证明。

**代码证据**

- 文件：app/pipeline_steps.py；app/services/buff_checkout_guard.py；app/receive_flow.py；app/sell_pipeline.py；app/sync_sold.py；app/steam_confirm.py；app/services/workers.py；app/database.py
- 类/函数：_try_single_buy、_do_wait_payment_and_append、try_receive_once、_find_buy_record、_fill_assetid_from_inventory、SteamConfirmer.accept_all、receive_worker、db_append_purchase
- 字段或状态：unresolved、pending_receipt、assetid、buff_order_id、bill_order_id、buff_sell_order_id、listing、sold_at、SteamID 相关会话字段
- 关键控制流：购买写前 journal 和未知终止；收货 baseline 差集；精确 assetid 上架 guard；worker/pipeline 互斥；名称-only sync_sold 和全量 accept_all 是当前需限制的反例路径。

**测试证据**

- 测试文件：tests/test_buff_checkout_guard.py；tests/test_buff_checkout_integration.py；tests/test_buff_purchase_flow_safety.py；tests/test_buff_receive_mapping_safety.py；tests/test_sell_pipeline_ownership_guard.py；tests/test_pipeline_lock.py
- 测试函数：分别覆盖 durable guard、未知结果终止、批量 ID、库存差集、pending_receipt、精确 assetid ownership 和 pipeline 互斥的已列测试集合。
- 实际覆盖内容：分模块验证当前安全边界和失败关闭行为。
- 未覆盖内容：没有全流程自动报价测试；没有覆盖接受自己 outbound offer、seller sender/partner、四方 SteamID 严格相等或全量 confirmation 筛选。

**当前缺口或风险**

当前系统并不存在一条被测试证明的“购买—付款—卖家报价—Steam 收货—精确资产—上架—售出”统一终结链。尤其 sync_sold 名称补全、receive_flow 方向缺失、accept_all 全量接受和 SteamID 关键流程未严格相等核对，均不能被未来实现忽略。

**后续自动报价实现约束**

- 必须把报价动作绑定到唯一、已持久化且未终结的 Purchase 和 durable intent。
- 必须在发送前严格相等核对 Purchase SteamID、当前凭据 SteamID、BUFF 绑定 SteamID、订单 buyer SteamID，并核对 seller/buyer 方向。
- 必须禁止未知结果自动重试、禁止接受自己发出的 outbound offer、禁止按名称授权 assetid、禁止 accept_all 未筛选 confirmations。
- 必须在可验证收货终结并清除 pending_receipt 后，才允许精确 assetid 上架；任何不确定性都进入人工对账/阻塞。
- 必须保持 worker 只做恢复和对账，不承担首次报价发送。

## 阶段 C 额外安全核查

- buyer-send 与 seller-send 当前是否真正区分：BUFF 的 lock_and_get_pay_url（买单创建）与 ask_seller_to_send（卖家发货提醒）是不同调用；未发现买家侧 Steam outbound send；但 receive_flow 未检查方向，因此“真正安全区分”仍 NOT_GUARANTEED。
- 是否存在接受自己发出的 outbound offer 的风险：存在未被当前代码排除的风险。fetch_buff_steam_trade 主要按 state、tradeofferid 和物品存在筛选，没有 sender/partner/SteamID 方向核对；没有专门测试，结论为 NOT_GUARANTEED。
- worker 是否承担首次报价发送：当前没有。worker 处理收货、库存/售出、listing 和会话维护；首次买单/卖家提醒在采购流水线。未来不得改变此职责。
- 是否存在统一的收货终结入口：try_receive_once 是主入口，但 sync_sold 可独立补 assetid 并清 pending_receipt；没有覆盖全链路的统一终结入口，结论为 UNKNOWN。
- SteamID 是否在关键流程进行严格相等核对：部分账号模块有相等检查，但 BuffBuyer.verify_session 采用 BUFF 返回的绑定 steamid，未与当前凭据 SteamID、Purchase SteamID 和订单 buyer_steamid 做四方严格相等核对；receive_flow 也未以 SteamID 匹配，结论为 NOT_GUARANTEED。
- 是否存在 assetid 仅按名称关联路径：存在，app/sync_sold.py 的 _fill_assetid_from_inventory 是明确路径；主 receive_flow 也在 goods_id 不可用时允许唯一名称候选。
- checkout guard 是否持久化并 fail-closed：已实现且有 guard/integration 测试，但仅保护接入路径；不能扩展成所有未来写 API 的全局保证。
- 写结果未知后是否存在自动重试路径：未发现 BUFF 买单、卖家提醒或 Steam 接受请求的盲目自动重发；接收侧库存轮询是对账，不是重发。Steam listing 的特定“previous action completes”一次处理不改变此结论。
- bill_order_id 是否可能是单笔或批量形态：单笔当前可为空，批量按 item 保存并由 batch_id 关联；不能假设全局唯一。
- buff_order_id 是否有数据库唯一约束：init_db 尝试创建非空部分唯一索引，但创建异常被直接吞掉；bill_order_id 和 buff_sell_order_id 没有对应数据库唯一约束。
- 一次购买是否可能产生两条 Purchase：常规控制流限制为确认后追加，但底层追加非幂等，绝对唯一性仍 PARTIALLY_GUARANTEED。
- pending_receipt 是否会被 sell pipeline 或 sync_sold 处理：sell pipeline 读取并拒绝；sync_sold 会处理缺失 assetid 并可能清除它，构成已识别风险。
- 是否存在统一收货终结入口：没有被代码和测试证明，标为 UNKNOWN。

## 阶段 D 最终交叉验证与交付记录

- 文档状态：READY_FOR_GPT_REVIEW。
- 远端目标：EinzbernLi/AetherSwap。
- TASK / Issue：TASK-002 / #4。
- base：integration/auto-buyer-offer；base SHA：ce3cfec5d21f5375852a6050582f59debc56048c。
- head 分支：luna/TASK-002-current-invariants；最终完整 head SHA 以 Draft PR #9 的远端回读为准。
- 唯一修改文件：.agent/CURRENT_INVARIANTS.md。
- 未修改：app/**、buff/**、steam/**、web/**、tests/**、.github/**、requirements 文件、.agent/BASELINE.md、integration 分支和 main。
- 未执行：真实 BUFF 或 Steam 购买、报价、接受报价、上架或其他写操作。

### 12 项完成对照

| 编号 | 状态 | 结论摘要 |
| --- | --- | --- |
| 1 | 已完成 | 付款后创建 Purchase；底层追加非幂等，绝对一条记录保证为部分保证。 |
| 2 | 已完成 | 三类 BUFF 对账 ID 的单笔/批量映射、唯一性边界已记录。 |
| 3 | 已完成 | checkout guard 写前持久化、未知结果 unresolved、启动阻塞和 fail-closed 已记录。 |
| 4 | 已完成 | BUFF 卖家发货提醒与本地 Purchase 驱动关系已记录；Steam offer 方向/发送方仍有缺口。 |
| 5 | 已完成 | 接受前库存 baseline 及失败关闭行为已记录。 |
| 6 | 已完成 | 买家侧新增 assetid 的差集、去重、商品名分组和未覆盖归属风险已记录。 |
| 7 | 已完成 | 当前 sell pipeline 精确 assetid ownership guard 已记录。 |
| 8 | 已完成 | pending_receipt 的真实状态语义及 sync_sold 绕过风险已记录。 |
| 9 | 已完成 | accept_all 全量接受、适用前提、缺少筛选和测试缺口已记录。 |
| 10 | 已完成 | sync_sold 名称补 assetid 的错误归属风险已记录。 |
| 11 | 已完成 | worker/pipeline/checkout 互斥边界及 worker 不承担首次发送已记录。 |
| 12 | 已完成 | 后续自动报价必须保持的绝对约束、未知路径和未证明项已汇总。 |

### 证据验证方法

1. 以精确 integration SHA ce3cfec5d21f5375852a6050582f59debc56048c 读取源码和测试对象。
2. 用 git cat-file 验证引用的源码文件真实存在。
3. 用 git grep 验证引用的函数、类、字段和测试名称；发现并更正了一个测试名称拼写差异。
4. 用 GitHub compare 验证 base 到 head 的文件列表只有 .agent/CURRENT_INVARIANTS.md。
5. 回读 Draft PR #9，验证 base、head、Draft 状态和唯一文件。
6. GitHub Actions 运行 31081997834 的 tests job 已通过；统计为 446 total、445 passed、1 registered failure、0 errors、0 collection errors；baseline gate passed；未修改 pytest baseline。

### 保证等级数量概况

以下为 12 个不变量中按章节出现的标签计数，标签可重叠：

- CODE_GUARANTEED：4 项（3、5、6、7 的当前代码路径）。
- TEST_COVERED：11 项（1—8、10—12 均有分散直接测试证据；9 无专门测试）。
- NOT_GUARANTEED：4 项（8、9、10、12 明确包含未保证性质）。
- PARTIALLY_GUARANTEED：4 项（1、4、8、11）。
- UNKNOWN：明确存在于 accept_all 专门覆盖、统一收货终结入口、全局互斥以及 Steam offer 归属等未证明问题中。

### 所有 UNKNOWN 或未确认事项

- SteamConfirmer.accept_all 没有专门测试，不能证明生产 confirmation 集合已筛选。
- 没有统一覆盖 Purchase、offer、inventory、listing 和售出生命周期的收货终结状态机。
- receive_flow 没有被证明严格核对 sender、partner、报价方向和 SteamID。
- 没有被证明排除接受自己发出的 outbound offer。
- Purchase SteamID、当前凭据 SteamID、BUFF 绑定 SteamID、订单 buyer_steamid 的四方严格相等核对未在关键采购/收货路径完成。
- sync_sold 可能按名称补 assetid 并清除 pending_receipt。
- db_append_purchase 没有完整幂等 get-or-create 语义；bill_order_id 和 buff_sell_order_id 没有数据库唯一约束。
- 未接入 checkout guard 的未来非幂等写入口不受本次代码证据覆盖。
- worker 的异常循环不等于安全的非幂等写重试；未来首次报价发送职责仍禁止交给 worker。

### 风险与回滚

- 风险等级：HIGH。主要风险是 Steam offer 方向/发送方缺少严格校验、名称-only assetid 补全、全量 accept_all、pending_receipt 可能被维护路径清除，以及关键 SteamID 未完成四方核对。
- 回滚方式：关闭或删除 Draft PR #9，或将 luna/TASK-002-current-invariants 回退到 base integration/auto-buyer-offer；本任务没有触碰业务代码、测试、依赖、CI、main 或 integration 分支。
- 回滚不会撤销 Issue #4 的执行恢复记录；该记录是本次执行审计历史。

### 顶层执行器自检

- 当前执行方式：独立顶层执行任务；没有创建或调用下级代理。
- 当前执行器身份：OWNER_ATTESTED。
- 声明模型/思考等级：gpt-5.6-luna / high。
- 平台实际元数据：未提供；不声称 PLATFORM_VERIFIED 或 CONFIG_VERIFIED。
- 旧子代理仅作为失败历史记录，未被引用为本次执行证明。
- 同一 Issue #4、同一分支和同一 Draft PR 全程复用。
- 只修改了唯一允许的远端文档文件。
- 未执行真实 BUFF/Steam 写操作。
- 未启动 TASK-003，未批准或合并 PR。
- Luna 自检：completed；交叉验证、CI、唯一文件和范围检查已完成。
- 请求网页端 GPT 进行最终验收；READY_FOR_GPT_REVIEW 不表示已验收或已合并。