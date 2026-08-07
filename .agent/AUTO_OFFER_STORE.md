# Auto Offer 独立 SQLite 状态仓库

`app.auto_offer.store.AutoOfferStore` 是 Auto Offer 的独立持久化边界。它只使用 Python 标准库 `sqlite3`，默认宿主路径为 `config/auto_offer.db`，不访问 `app.database`、BUFF、Steam、库存或 Trade Offer。

## 生命周期与 schema

构造 `AutoOfferStore(path)` 不创建目录或文件；显式调用 `initialize()` 才会创建目标目录、启用 `WAL`、`synchronous=FULL`、`busy_timeout=5000` 并创建或严格校验 schema v1。schema 版本通过 SQLite `PRAGMA user_version` 固定为 `1`，不执行未知版本迁移、删除或重建。

状态表为 `auto_offer_delivery`。`purchase_id` 与 `buff_order_id` 均为唯一身份，`revision >= 1` 用于乐观并发控制，`pending_receipt` 只允许 `0/1`。

## 写入契约

`ensure_initial()` 只接受 `pending_direction`、未知方向、`pending_receipt=True` 的初始快照。相同完整快照幂等返回原 revision；身份或内容冲突 fail closed。

`advance()` 在写入前重新验证 `DeliverySnapshot` 和 `validate_delivery_transition()`，并要求 purchase、BUFF order、account、recipient SteamID 不变。只有 revision 精确匹配才会提交，成功后 revision 加一；过期写入抛出 `AutoOfferStoreStaleWriteError`，事务失败回滚旧行。

所有读取都会重建并再次验证 `DeliverySnapshot`。未知枚举、非法时间、错误码、收货证明、schema 或数据库错误都不会被跳过或修复，而是抛出存储错误。`list_recoverable()` 仅返回未完成状态，按插入 id 确定性排序；`result_unknown` 保持原状态，不会自动重试或发送报价。

测试数据库必须使用 pytest 的 `tmp_path / "auto_offer.db"`，不得使用生产数据库或外部服务。
