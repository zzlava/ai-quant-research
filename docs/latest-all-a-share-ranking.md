# 沪深普通 A 股最新日自动排行

`all_a_share_latest_v1` 是一个**当日研究截面**：它按最近可用的 A 股交易日，对当前上市的沪深普通 A 股计算排名。它不提供历史全市场成员关系，不能运行 `backtest`，也不能据此宣称历史策略收益。

## 范围

- 仅上交所、深交所当前上市的普通 A 股：主板、创业板、科创板。
- 不含北交所、ETF、基金、B 股、指数及已退市证券。
- 采集时先排除当日由 `suspend_d` 明确记录为全日停牌的证券；若 60 日窗口首日全日停牌且没有官方 `stk_limit.pre_close` 可作为起点，也会因无法满足 warm-up 而排除。评分时再剔除名称带 `ST` / `*ST` 的证券、上市不足 120 个自然日的证券，以及 20 日平均成交额低于 1 亿元的证券。
- 当前行业字段不是点时行业分类，因此行业因子固定为 0。

## 数据与时间边界

命令先由 `trade_cal` 把 `--as-of` 解析为最近一个可用 A 股交易日，再按交易日批量抓取 60 根日线所需的：`daily`、`daily_basic`、`adj_factor`、`stk_limit` 与 `suspend_d`，并同时拉取沪深 300 和 SPX。

任何一个当日可交易证券的日线、换手、复权或涨跌停记录缺失都会失败；不会把缺失换手率当零，也不会把缺失日线当停牌。只有 `suspend_d` 对当日的明确全日停牌记录可以排除该证券。当前 `stock_basic` 名称仅用于 resolved `as_of` 当日的 ST 剔除；它不被回填成历史 ST 状态。

## 本地运行

请使用新的数据目录，避免覆盖已有受控样本快照：

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

read -rs "AIQ_TUSHARE_TOKEN?Tushare Token: "
echo
export AIQ_TUSHARE_TOKEN
export AIQ_DATA_DIR=./data/all-a-share-latest-v1
export AIQ_DATABASE_URL=sqlite:///data/all-a-share-latest-v1/app.db

.venv/bin/python -m app.cli fetch-tushare-latest-all-a-share \
  --as-of 2026-08-23 \
  --strategy all_a_share_latest_v1
```

成功输出的 `resolved_as_of` 才是实际评分日期。周末或节假日传入的日期会回退到最近开放交易日。默认拒绝覆盖非空快照目录；只有明确需要刷新同一目录时才添加 `--replace-existing`。

接着用同一组环境变量执行只读预检和评分：

```bash
.venv/bin/python -m app.cli preflight-research \
  --strategy all_a_share_latest_v1 \
  --start 2026-08-21 \
  --end 2026-08-21

.venv/bin/python -m app.cli score \
  --strategy all_a_share_latest_v1 \
  --date 2026-08-21
```

将两个命令中的日期替换为采集命令输出的 `resolved_as_of`。排名保存时会带数据快照 ID；它是研究排序和人工复核材料，不是买卖指令。

完成后清除 Token：

```bash
unset AIQ_TUSHARE_TOKEN AIQ_DATA_DIR AIQ_DATABASE_URL
```
