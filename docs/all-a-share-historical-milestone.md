# 历史全 A 股底座与点时派生股票池

本里程碑不再依赖 CSI300 历史成员。候选范围是研究窗口内实际上市过的沪深普通 A 股，包含窗口内退市股票，排除北交所和 B 股。每日研究成员由当日可观察字段重新派生：

- 上市至少 120 个自然日；
- 非 ST/PT；
- 非整日停牌；
- 最近 20 个交易日平均成交额不低于 1 亿元；
- 决策时间为收盘后的 17:30（Asia/Shanghai），交易仍只能在下一交易日执行。

ST 历史使用 Tushare 官方 `namechange` 的名称生效区间。当前账号没有 `stock_st` 权限，因此实现不会假装该接口可用，也不会用当前名称回填历史。行情、复权因子、每日指标、涨跌停价和停牌记录按交易日整市场分页采集。若 `suspend_d` 未覆盖暂停上市的完整区间，只对无法解释缺口的股票补查官方旧 `suspend` 区间和停牌前价格种子；无区间证据的缺口仍失败。

## 1. 断点采集

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

read -rs "AIQ_TUSHARE_TOKEN?Tushare Token: "
echo
export AIQ_TUSHARE_TOKEN

.venv/bin/python -m app.cli collect-tushare-all-a-share-history \
  --start 2021-10-08 \
  --end 2024-12-31 \
  --strategy all_a_share_historical_value_quality_v1 \
  --staging-dir ./data/raw/all-a-share-history-20211008-20241231-v1

unset AIQ_TUSHARE_TOKEN
```

每个接口的每个交易日独立写入 Parquet。网络或频率错误后，原命令重跑会复用已验证分区。完整采集后生成 `collection_manifest.json` 和 `quality_report.json`；已有完整集合被修改后不会在重跑时自动重新盖章，而是直接拒绝。

## 2. 离线物化

```bash
export AIQ_DATA_DIR=./data/all-a-share-historical-v1
export AIQ_DATABASE_URL=sqlite:///data/all-a-share-historical-v1/app.db

.venv/bin/python -m app.cli materialize-tushare-all-a-share-history \
  --staging-dir ./data/raw/all-a-share-history-20211008-20241231-v1 \
  --strategy all_a_share_historical_value_quality_v1 \
  --output-dir "$AIQ_DATA_DIR/parquet" \
  --source-version tushare-all-a-share-history-v1
```

物化前会重新核对所有分区字节哈希。输出仍遵守六表快照契约，并把每日派生成员写入 `universe_membership.parquet`。这一步完全离线，不读取 Token。

## 3. 尚未完成的依赖

该策略是价值主导方向，因此正式评分还需要同一全 A 股范围的点时财务/估值 overlay。没有该 overlay 时，行情和股票池快照可以完成，但 `preflight-research` 会按设计拒绝价值策略运行。下一里程碑应批量、断点抓取全 A 股财务与每日估值，并进行覆盖率和报告修订审查。

## 4. 本次实际产物（2026-08-24）

- 原始集合：`data/raw/all-a-share-history-20211008-20241231-v1`；
- 原始 request ID：`0b1e4abf58af7c68e7e00e2ecddc7b205010e8a9f26c6c2bb9f7a81e0699f7d1`；
- 物化快照：`data/all-a-share-historical-v1/parquet`；
- data snapshot ID：`de546fbbf5a6308a76fbfbd077a918cbbedfb3ad0ca361a24212c1bfe3e06857`；
- 覆盖 787 个交易日、5,262 只窗口重叠普通 A 股；
- 3,874,790 条日线、1,650,600 条逐日成员；
- 每日成员 1,340～3,533 只；窗口内符合范围的 141 只退市股票全部保留在 instruments；
- 成员中 ST=0、整日停牌=0、最短上市天数=120；可由快照内重算的成员 20 日平均成交额下限为 100,000,175.55 元；
- 完整快照哈希复算通过；行情/成员预检通过后仅在缺少全 A 股 fundamental overlay 处按预期拒绝。
