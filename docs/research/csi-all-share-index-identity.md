# 中证全指指数身份与长历史覆盖核对（只读事实契约）

身份契约首先解决“第一层究竟应读取哪两条指数序列”；后续数据里程碑已经在不改写该上游契约的前提下完成哈希绑定原始采集与离线物化。全程未运行评分、IC、回测、OOS 或交易。

机器可读契约：[`config/research/csi-all-share-index-identity-v1.json`](../../config/research/csi-all-share-index-identity-v1.json)

校验器：`src/app/research/csi_all_share_index_identity.py`

## 已确认身份

| 用途 | 官方代码 | Tushare `ts_code` | 收益定义 | 状态 |
| --- | --- | --- | --- | --- |
| 第一层市场状态 | `000985` | `000985.CSI` | 价格指数 | 身份确认；价格历史覆盖探针通过 |
| 业绩比较 | `H00985` | `H00985.CSI` | 全收益 | 身份确认；官方恢复规则已封印，具备严格物化前置条件 |
| 诊断备用 | `N00985` | `N00985.CSI` | 净收益 | 身份确认；不属于冻结基准 |

三条身份同时由中证官方事实表和 Tushare `index_basic` 精确代码查询核对；固定 `2024-07-01..2024-07-05` 日线探针每条均返回 5 行。

主要证据：

- 中证官方《中证全指指数事实表》（2026-07-31）：确认价格指数 `000985`、全收益 `H00985`、净收益 `N00985`；
- 中证官方《中证全指指数编制方案》：确认基日 `2004-12-31`、基点 `1000`；
- Tushare `index_basic`：确认 `000985.CSI` / `H00985.CSI` / `N00985.CSI` 的精确身份；
- Tushare `index_daily`：确认日线接口可读取三条精确代码。

所有来源 URL、访问时内容 SHA-256、访问时间和非敏感探针元数据均被契约自哈希封印。契约不保存 Token，也不保存指数点位。

## 2005–2024 覆盖结论

| 序列 | 行数 | 首日 | 末日 | 重复键 | 必需字段空值 |
| --- | ---: | --- | --- | ---: | ---: |
| `000985.CSI` | 4,858 | 2005-01-04 | 2024-12-31 | 0 | 0 |
| `H00985.CSI` | 4,857 | 2005-01-04 | 2024-12-31 | 0 | 0（`trade_date/close/pre_close`） |

交叉日期集合发现：Tushare 的 `000985.CSI` 比 `H00985.CSI` 多 `2011-08-02` 一天。全收益源的 `open/high/low` 全为空，但冻结基准只需要收盘到收盘收益，因此 OHLC 空值本身不是阻断项。

中证官方历史接口对 `H00985` 的同窗响应返回 4,860 行。以 Tushare 的价格指数和上交所开市日历定义严格的 4,858 日目标日历后：

- 官方额外的 `2005-01-01` 与 `2018-06-18` 不属于目标开市日，必须排除；
- 官方 `2011-08-02` 是目标开市日，可恢复 Tushare 缺行；
- 两源共有日期中，`2011-08-03` 的收盘差异达到约 2.1521 bps，超过单纯两位小数舍入的范围；
- 修复规则固定为：Tushare 为主源，仅 `2011-08-02`、`2011-08-03` 使用哈希绑定的中证官方原始行；禁止扩大覆盖日期，禁止插值和前向填充。

当前结论：

- 指数身份事实核对：**完成**；
- 价格指数长历史物化前置条件：**完成**；
- 全收益严格长历史的**来源前置条件**：**完成**；
- 离线长历史 materializer 与原始响应哈希绑定采集：**已实现并完成一次封印运行**；
- 禁止用价格收益、前值、插值或人工点位补值，也禁止把官方响应中非开市日写入目标序列；
- 下一步是把该快照作为事实 overlay 迁移进第一层开发协议；迁移前仍不能作为策略输入。

## 已完成的原始采集与严格快照

实现：`src/app/providers/csi_all_share_long_history.py`

安全启动脚本：`scripts/run_csi_all_share_index_collection.sh`

固定产物：

| 层 | 路径 | 封印 ID | 结果 |
| --- | --- | --- | --- |
| 原始采集 | `data/raw/csi-all-share-index-2005-2024-v1` | `36ce1fe87e8cd42ba5640ae8ab21d180cf9b2a4e9e91fc5b39e937b0c9a18dfc` | 13 个原始文件，全部进入 manifest 哈希 |
| 严格快照 | `data/research/csi-all-share-index-2005-2024-v1` | `9dbc0032539be62518bbc7f64e67cf9deb64e0564dcaca8aecc65bdc1d3890d0` | 日历、价格、全收益各 4,858 行 |

物化器从原始层重新计算全部输出；全收益表中只有 `2011-08-02`、`2011-08-03` 两行标记为中证官方覆盖，其余日期继续使用 Tushare 主源。每个输出行记录原始行哈希、原始文件相对路径和文件 SHA-256。

上游身份契约是不可改写的阶段性收据，所以其只读 CLI 仍会原样输出当时的 `pending_implementation` blocker；当前完成状态由下游 collection manifest 和 snapshot manifest 证明，不能为了改变提示而重盖上游契约。

采集后的离线复核：

```bash
PYTHONPATH=src .venv/bin/python -m app.cli verify-csi-all-share-long-history-collection \
  --staging-dir ./data/raw/csi-all-share-index-2005-2024-v1 \
  --identity-contract ./config/research/csi-all-share-index-identity-v1.json

PYTHONPATH=src .venv/bin/python -m app.cli verify-csi-all-share-long-history-snapshot \
  --staging-dir ./data/raw/csi-all-share-index-2005-2024-v1 \
  --snapshot-dir ./data/research/csi-all-share-index-2005-2024-v1 \
  --identity-contract ./config/research/csi-all-share-index-identity-v1.json
```

## 离线校验

```bash
.venv/bin/python -m app.cli verify-csi-all-share-index-identity \
  --contract-file ./config/research/csi-all-share-index-identity-v1.json \
  --repo-root .
```

输出必须保持：

- `factual_identity_verified=true`
- `price_series_ready_for_long_history_materialization=true`
- `total_return_series_ready_for_strict_long_history_materialization=true`
- `blocker=offline_long_history_materializer_and_hash_bound_raw_collection_not_yet_implemented`
- `ready_for_scoring/backtest/trading=false`

最后两项分别是上游阶段性状态和永久安全门闩；实际下游完成状态应读取严格快照 manifest，而不是改写上游契约。
