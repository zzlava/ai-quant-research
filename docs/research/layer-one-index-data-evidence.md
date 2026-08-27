# 第一层中证全指数据证据封印

这是身份/source probe 与真实长历史物化完成后的**下游证据收据**，不改写上游不可变封印。

固定文件：`config/research/layer-one-index-data-evidence-v1.json`

它逐项绑定并重新验证：

- 官方/Tushare 身份合同：价格指数 `000985.CSI`，全收益指数 `H00985.CSI`；
- 原始采集 `collection_manifest.json`、全部原始分区及其哈希；
- 离线物化快照 `snapshot_id=9dbc0032539be62518bbc7f64e67cf9deb64e0564dcaca8aecc65bdc1d3890d0`；
- `calendar / price_index / total_return_index` 各 4,858 行，覆盖 `2005-01-04..2024-12-31`；
- 只允许 `2011-08-02`、`2011-08-03` 两日官方 override，禁止插值和前向填充；
- A 股卖方印花税历史合同及其磁盘哈希。

验证器会从原始数据完整重算物化表，不信任 manifest 的声明。任何原始字节、表、合同、哈希、代码身份、日期覆盖或 readiness 漂移都会失败关闭。

本证据只将 `ready_for_layer_one_historical_evaluation` 置为 `true`。它明确保持：

- `ready_for_stock_scoring=false`
- `ready_for_orders=false`
- `ready_for_trading=false`
- `auto_apply=false`

因此它允许下一步做第一层历史研究，但不代表选股、回测产品或交易系统已经可用。
