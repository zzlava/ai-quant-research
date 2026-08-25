# A 股五类事件 overlay 里程碑

本里程碑建立可恢复的 Tushare 采集与离线数据契约，不修改评分、不执行交易。五类事件必须作为独立 overlay 绑定到精确的六表行情快照，未来只有通过独立因子诊断后才能进入新策略配置。

## 五张规范表

| 规范表 | 原始接口 | 逻辑版本键 |
| --- | --- | --- |
| `earnings_forecast_events` | `forecast` | `symbol, report_period, ann_date` |
| `earnings_express_events` | `express` | `symbol, report_period, ann_date` |
| `holder_count_events` | `stk_holdernumber` | `symbol, end_date, ann_date` |
| `share_unlock_events` | `share_float` | `symbol, float_date, ann_date, holder_name, share_type, float_share, float_ratio` |
| `audit_opinion_events` | `fina_audit` | `symbol, report_period, ann_date` |

每一行都包含：

- 规范化股票代码；
- 公告日 `ann_date`；
- `available_at=公告日 23:59 Asia/Shanghai`，即日期型公告最早只能供之后的决策使用；
- 基于规范字段重新计算的 `source_row_hash`；
- 接口对应的报告期、事件日、数值及说明字段。

同一报告期不同公告日是不同版本，必须全部保留。相同逻辑键的重复或冲突行会失败，不能用最后一行覆盖。

## 可恢复采集

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

export AIQ_DATA_DIR=./data/all-a-share-historical-v1
export AIQ_DATABASE_URL=sqlite:///data/all-a-share-historical-v1/app.db

read -rs "AIQ_TUSHARE_TOKEN?Tushare Token: "
echo
export AIQ_TUSHARE_TOKEN

.venv/bin/python -m app.cli collect-tushare-all-a-share-events \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --staging-dir ./data/raw/a-share-events-2022-2024-v1 \
  --source-version a-share-events-2022-2024-v1
```

采集器执行五个接口与行情快照中每只股票的笛卡尔积，并将每个“接口×股票”原子写入独立 Parquet。中断后原命令重跑会重新校验并复用已有分区。完整集合写出 `collection_manifest.json` 后不可静默重新盖章；任意分区、汇总文件、来源 manifest 或质量报告发生变化，重跑都会拒绝。

除 `share_float` 外，接口的 `start_date/end_date` 都按公告日查询。Tushare 的 `share_float.start_date/end_date` 实际过滤解禁日，因此该接口通常按单只股票查询完整历史，再只保留请求区间内的 `ann_date`。如果单股请求触及 6000 行上限或返回其他股票，采集器会对请求区间逐个自然日仅使用 `ann_date` 查询全市场，再在本地严格筛选目标股票；回退时不再携带已经被证明不可信的 `ts_code`。每个公告日的全市场响应还会写入 `query-cache/share_float/all-market/<日期>.json`；其他股票遇到同一回退日时只读取这份已验证响应，不会重复联网，也不复制逐股缓存。市场快照中已验证的上市日前日期不发出 API 请求，而是写入带 `listing_date` 的空缓存；这只依据“当时尚未上市”的市场快照事实。单个公告日达到 6000 行时按 `offset=0,6000,12000...` 继续分页，直到末页少于 6000 行；服务端忽略 offset、重复完整页或超过安全页数时失败关闭。逐股缓存只保存实际发起的请求及上市前跳过；回退股票、原因、检查/跳过/实际查询天数、共享缓存命中、本地缓存数和保留行数写入 `query-audit/share_float/*.json`，所有缓存和审计文件均纳入最终 manifest 哈希。

若市场快照中 `listing_date > coverage_end`，该股票在整个研究窗口尚未上市；采集器会为五个端点直接写入空分区，既不联网也不生成逐日 `share_float` 缓存。这是由已验证市场快照给出的窗口事实，并非将接口“无返回”误判为历史事实。

业绩预告 `forecast` 的可得时点只认权威字段 `ann_date`。官方文档中 `first_ann_date` 为首次公告日，语义上不得晚于本次 `ann_date`。若提供方返回 `first_ann_date > ann_date` 的内部矛盾行：采集器**不丢行、不改 `ann_date`、不编造替代首次公告日**，仅把规范/分区中的 `first_ann_date` 置为 null，并把完整非密钥原始字段、原始 `first_ann_date`、规则名与行哈希写入 staging 下内容寻址的 `anomalies/forecast/<sha256>.json`；`collection_request` 记录隔离策略（既有未盖章 staging 可缺省该键以便 resume），`collection_manifest.anomaly_hashes` 与 `quality_report.provider_field_anomalies`（count、rule_distribution、affected_symbols）绑定这些证据。直接物化未经采集器隔离的畸形原始 `forecast` 仍失败关闭。

`quality_report.json` 至少包含：

- 每个接口的分区数、原始/规范行数、覆盖股票数、公告日期范围和年度行数；
- 每个原始字段的空值数；
- 原始行因缺少规范事件必需值而未进入 canonical overlay 的数量；其中股东户数空值保留在原始导出并计数，绝不填成 0；
- 同一逻辑事件的公告版本数；
- 业绩预告类型变更路径；
- 提供方可选字段矛盾隔离计数（如 `first_ann_date_after_ann_date`）及受影响股票；
- 审计意见原始值分布和需要人工分类的非精确“标准无保留意见”文本；
- 解禁量/比例单位、比例非空率和公告至解禁的时间分布；
- `share_float` 仅按公告日全市场回退的股票、查询天数和最终保留行数；
- `ready_for_scoring=false`、`ready_for_trading=false` 的研究边界。

质量报告只说明覆盖率、缺失和修订机制，不能证明因子有效，也不会自动把任何事件变成硬性排除条件。

## 点时事件诊断快照

采集和物化完成后，可以生成只读的逐股票 PIT 诊断：

```bash
export AIQ_DATA_DIR=./data/all-a-share-historical-v1
export AIQ_EVENT_DIR=./data/all-a-share-historical-v1/events-v1

.venv/bin/python -m app.cli diagnose-a-share-event-overlay \
  --strategy all_a_share_historical_value_portfolio_selected_v2 \
  --as-of 2024-12-31 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --event-dir "$AIQ_EVENT_DIR" \
  --output-dir ./data/all-a-share-historical-v1/event-diagnostics/2024-12-31
```

输出目录包含带 SHA-256 的 `event_diagnostics.parquet` 和自校验 `report.json`。逐股票字段包括：

- 决策时刻已知的最新业绩预告及其公告修订次数；
- 最新业绩快报的净利润和营收同比；
- 最新及前一期股东户数和原始变化率；
- 最新审计意见原文，以及是否精确等于“标准无保留意见”；
- 未来 30 个自然日内已经公告的解禁事件数、股份数和已知比例。

公告日当天不会在收盘决策时提前可见：日期型公告统一到上海时间 23:59，最早只能供下一次决策使用。诊断快照固定写入 `ready_for_scoring=false` 和 `ready_for_trading=false`；它不能直接参与排行、剔除或回测。

## 离线事件 overlay 审阅报告

在物化并 `verify-a-share-event-overlay` 之后，对已绑定行情的五表 overlay 生成只读、可哈希校验的覆盖率审阅产物。该命令不联网、不读取 Tushare token，也不修改评分、成员、排除、回测、排行、下单或任何阈值。

`--source-collection-dir` 必填，必须指向已盖章的离线采集目录（真实命令使用 `data/raw/a-share-events-2022-2024-v1`）。审阅会离线校验该目录的 `collection_manifest.json`、`source_manifest.json` 与 `quality_report.json`：先核对 collection manifest 对 source manifest / quality report 的声明哈希，再要求 collection source manifest 哈希与已加载事件 overlay 的 `source_manifest_sha256` 完全一致；任一不匹配或篡改都失败关闭。原始源级缺失计数只来自已校验的 collector `quality_report`，绝不能从 canonical overlay 反推或把不可用源级缺失写成 0。

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

export AIQ_DATA_DIR=./data/all-a-share-historical-v1
export AIQ_EVENT_DIR=./data/all-a-share-historical-v1/events-v1

.venv/bin/python -m app.cli review-a-share-event-overlay \
  --strategy all_a_share_historical_value_portfolio_selected_v2 \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --event-dir "$AIQ_EVENT_DIR" \
  --source-collection-dir ./data/raw/a-share-events-2022-2024-v1 \
  --output-dir ./data/all-a-share-historical-v1/event-overlay-reviews/2022-2024
```

输出目录包含：

- `report.json`：自校验审阅报告，绑定 `market_snapshot_id`、`event_snapshot_id`、`strategy_config_hash`、`source_manifest_sha256`、已校验的 `collection_source_manifest_sha256` / `collection_quality_report_sha256` 与闭区间窗口；
- `annual_source_review.parquet`：分年度、分来源的行级汇总，并带 SHA-256。

报告至少覆盖：

- 各来源分年度公告行数、公告股票数、年末决策时刻 PIT 可见行/股票数；
- 字段缺失、逻辑事件修订版本数、公告相对报告期/解禁日的时间分布；
- 审计意见原始文本分布与非精确“标准无保留意见”文本；
- 股东户数分项：`raw_collection_holder_rows`、`raw_collection_holder_num_blank_rows`（来自已校验 quality report）、`canonical_holder_rows_in_window`、以及 `symbols_with_no_observable_canonical_holder_data`；缺失不是 0 户，canonical 表也不能冒充“源无缺失”；
- 解禁比例分项：`raw_collection_float_ratio_blank_rows`（来自已校验 quality report）与窗口内 canonical 已知/缺失覆盖；缺失比例不是 0 风险；
- PIT 探针：公告当日收盘决策不可见，次一交易日才可见；`symbols_with_no_observable_data` 表示无观测，不是零值风险。

审阅产物固定 `ready_for_scoring=false`、`ready_for_trading=false`。它只帮助人工检查覆盖率、修订、缺失与可得时点，不能授权候选风险规则、硬性剔除或 alpha 结论。

## 离线源目录

目录必须包含五个 CSV/Parquet 文件和 `source_manifest.json`。文件名可以自定，但 manifest 的 key 必须固定：

```text
event-source/
├── source_manifest.json
├── collection_request.json
├── collection_manifest.json
├── quality_report.json
├── exports/
│   ├── forecast.parquet
│   ├── express.parquet
│   ├── stk_holdernumber.parquet
│   ├── share_float.parquet
│   └── fina_audit.parquet
├── anomalies/
│   └── forecast/
└── partitions/
    ├── forecast/
    ├── express/
    ├── stk_holdernumber/
    ├── share_float/
    └── fina_audit/
```

manifest 示例：

```json
{
  "schema_version": "1",
  "source_name": "tushare",
  "source_version": "a-share-events-2022-2024-v1",
  "fetched_at": "2025-01-10T09:00:00+08:00",
  "coverage_start": "2022-01-01",
  "coverage_end": "2024-12-31",
  "files": {
    "forecast": {"path": "exports/forecast.parquet", "sha256": "<64位小写SHA-256>"},
    "express": {"path": "exports/express.parquet", "sha256": "<64位小写SHA-256>"},
    "stk_holdernumber": {"path": "exports/stk_holdernumber.parquet", "sha256": "<64位小写SHA-256>"},
    "share_float": {"path": "exports/share_float.parquet", "sha256": "<64位小写SHA-256>"},
    "fina_audit": {"path": "exports/fina_audit.parquet", "sha256": "<64位小写SHA-256>"}
  },
  "availability_evidence": {
    "forecast": "Tushare doc 45 ann_date",
    "express": "Tushare doc 46 ann_date",
    "stk_holdernumber": "Tushare doc 166 ann_date",
    "share_float": "Tushare doc 160 ann_date",
    "fina_audit": "Tushare doc 80 ann_date"
  },
  "notes": "下载时间仅用于溯源，不作为 historical available_at"
}
```

`fetched_at` 必须带时区；五个来源文件及五条可得时点证据缺一不可。相对路径不能逃出源目录，SHA-256 不匹配直接失败。

## 离线物化

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

.venv/bin/python -m app.cli materialize-a-share-event-overlay \
  --source-dir ./data/raw/a-share-events-2022-2024-v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --output-dir ./data/all-a-share-historical-v1/events-v1
```

物化过程会：

1. 校验五个原始文件的字节哈希；
2. 规范化字段并拒绝空表、无效代码、无效日期、非有限数值、负解禁量等异常；
3. 检查公告日没有越出源 manifest，也没有晚于行情快照；
4. 检查事件股票均存在于绑定行情的 `instruments.parquet`；
5. 原子写入五张 Parquet、原始 `source_manifest.json` 和内容寻址 `manifest.json`。

## 离线复核

```bash
.venv/bin/python -m app.cli verify-a-share-event-overlay \
  --event-dir ./data/all-a-share-historical-v1/events-v1 \
  --market-dir ./data/all-a-share-historical-v1/parquet
```

复核会重新计算每行来源哈希、五表内容哈希、源 manifest 字节哈希和总 `event_snapshot_id`，并验证 `base_market_snapshot_id`。任何 Parquet、来源说明或基础行情版本变化都会失败。

## OOS-3 实际产物（2024-10-08..2026-08-21）

OOS-3 绑定行情快照 `data/all-a-share-oos-20241001-20260821-v1/parquet`（`snapshot_id=b6f664d31d8ffcdabbb655e888467c75dbfa6a7f8bd863d698febb015f5b0427`）。原始采集目录 `data/raw/a-share-events-oos-20241008-20260821-v1` 已盖章，`request_id=0361ae4af34ff990469fffd66fb4aed6df3f024daf44b981f2fd3e0a2e92171c`，五类分区各 5261、合计 26305，`requested_stocks=5261`，`source_version=a-share-events-oos-20241008-20260821-v1`，`source_manifest_sha256=4afa5ef1d958eb50e28c48d91dfb1c3006a69075dabfc82df457b8d8332b244d`，`collection_quality_report_sha256=b043c3fa9746a84dedefc8b05d4944e427c09502fb5089de5f0cc71cc5c647c5`。

离线物化至 `data/all-a-share-oos-20241001-20260821-v1/events-v1`，`event_snapshot_id=73f1dedf83b0c28d0ba5ae933205e2777b02e27d356d4dd5cf62dcb10155b28f`，`covered_symbols=5258`。规范表行数：`earnings_forecast_events=10097`、`earnings_express_events=1952`、`holder_count_events=91708`、`share_unlock_events=917660`、`audit_opinion_events=10738`。`verify-a-share-event-overlay` 已通过，绑定同一 `base_market_snapshot_id`。

覆盖率审阅输出 `data/all-a-share-oos-20241001-20260821-v1/event-overlay-reviews/oos-20241008-20260821-v1`，策略 `all_a_share_historical_value_portfolio_selected_v2`，闭区间 `2024-10-08..2026-08-21`，`report_id=8da8f30077fede9dd30d21d3d7e58cde6929d3384b4de4ea405485a0b3de3346`，`strategy_config_hash=796b793856dcd02a`，`annual_source_rows=15`，`pit_probes=458`。审阅与质量报告均固定 `ready_for_scoring=false`、`ready_for_trading=false`。

已知缺失与限制（仅描述覆盖，不构成因子结论）：

- 股东户数：原始采集 `raw_rows=154873`，`holder_num` 空值 63165 行未进入 canonical；规范表 91708 行；审阅报告 `symbols_with_no_observable_canonical_holder_data=47`。
- 解禁比例：`float_ratio` 空值 13065 行；canonical 已知 904595、缺失 13065。
- 业绩预告：`provider_field_anomalies` 计数 1（`first_ann_date_after_ann_date`，受影响 `688082.SH`），已隔离 `first_ann_date` 并保留 anomaly 证据。
- 审计意见：非精确“标准无保留意见”文本 405 行（保留意见 153、带强调事项段的无保留意见 203、无法表示意见 48、否定意见 1），需人工分类。
- 各来源公告覆盖股票数（quality report）：forecast 3926、express 1063、stk_holdernumber 5214、share_float 2040、fina_audit 5236；大量策略股票在 express / share_float 上无 PIT 可见观测，审阅报告 `symbols_with_no_observable_data` 分项反映的是覆盖缺席而非零值风险。
- OOS-3 窗口含 2025–2026 模拟未来段；事件 overlay 与审阅报告均未挂载评分、成员过滤、排除、回测或交易。

OOS-3 离线命令（不联网、不读取 token）：

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

.venv/bin/python -m app.cli materialize-a-share-event-overlay \
  --source-dir ./data/raw/a-share-events-oos-20241008-20260821-v1 \
  --market-dir ./data/all-a-share-oos-20241001-20260821-v1/parquet \
  --output-dir ./data/all-a-share-oos-20241001-20260821-v1/events-v1

.venv/bin/python -m app.cli verify-a-share-event-overlay \
  --event-dir ./data/all-a-share-oos-20241001-20260821-v1/events-v1 \
  --market-dir ./data/all-a-share-oos-20241001-20260821-v1/parquet

.venv/bin/python -m app.cli review-a-share-event-overlay \
  --strategy all_a_share_historical_value_portfolio_selected_v2 \
  --start 2024-10-08 \
  --end 2026-08-21 \
  --market-dir ./data/all-a-share-oos-20241001-20260821-v1/parquet \
  --event-dir ./data/all-a-share-oos-20241001-20260821-v1/events-v1 \
  --source-collection-dir ./data/raw/a-share-events-oos-20241008-20260821-v1 \
  --output-dir ./data/all-a-share-oos-20241001-20260821-v1/event-overlay-reviews/oos-20241008-20260821-v1
```

## 开发期候选事件诊断（2022-01-01..2023-12-31）

在已验证的行情快照、事件 overlay 与 coverage/PIT 审阅之后，可对**预先声明**的五类事件候选假设做开发窗只读诊断。该命令不是 `score`、`analyze-ic` 或回测，不接入现有策略评分、成员过滤、排除、组合或交易。

严格边界：

- 窗口只能是 `2022-01-01..2023-12-31`；越界失败关闭。
- 2024 已被观察，**不可**用于选择或阈值调参；标签硬截断在 `2023-12-31`，不得用 2024 价格补标签，也不得读取或计算 2025+ 收益。
- PIT：`available_at=ann_date 23:59 Asia/Shanghai`，公告日收盘决策不可见；信号最早对齐下一交易日。未来解禁只使用此前已公告记录。
- 观察单位是事件级或明确去重后的 `(source, symbol, ann_date, first_usable_trade_date, hypothesis/threshold bucket)`，禁止把旧事件按日复制成伪样本。
- 缺失保持 unknown，不得填 0。产物固定 `ready_for_scoring=false`、`ready_for_trading=false`、`development_only=true`。
- 输出只是**候选证据**（覆盖、缺失、方向、2022/2023 同向性），不授权 alpha 结论或硬性剔除。
- 二元假设写出全部 eligible 事件：`signal_value=1/0`（已知）或 `null`（unknown）；汇总不把 0/1 混成无意义全样本均值，而是报告 `labeled_signal_1/0`、两组 raw/rel 均值与 `spread_1_minus_0`；Spearman 使用全部 known 的 0/1。连续假设的 mean/median/win/Spearman 仅针对 labeled known 全样本。
- 解禁比例合计仅在窗口内全部 eligible tranche 的 `float_ratio` 均已知时记为 known；任一缺失则为 unknown。
- `annual_stability_metric` 标明年度稳定性所用指标：二元为 `mean_spread_1_minus_0`，连续为 `spearman`。`same_sign_2022_2023_*` 按每个 forward horizon 比较该指标在 2022 与 2023 的符号；连续候选不得用全样本平均收益符号。`candidate_direction_supported_2022_2023_*` 仅在两年指标均符合预声明 `candidate_direction` 时为 true，缺样本为 null。

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

export AIQ_DATA_DIR=./data/all-a-share-historical-v1
export AIQ_EVENT_DIR=./data/all-a-share-historical-v1/events-v1

.venv/bin/python -m app.cli diagnose-a-share-event-candidates \
  --strategy all_a_share_historical_value_portfolio_selected_v2 \
  --start 2022-01-01 \
  --end 2023-12-31 \
  --market-dir ./data/all-a-share-historical-v1/parquet \
  --event-dir "$AIQ_EVENT_DIR" \
  --output-dir ./data/all-a-share-historical-v1/event-candidate-diagnostics/development-2022-2023-v1
```

输出目录包含自校验 `report.json`、事件级 `observations.parquet`、按假设/年度/horizon 汇总的 `hypothesis_annual_summary.parquet` 及 SHA-256。加载器会重算哈希并拒绝篡改；市场或事件快照错配失败关闭。

预声明假设至少覆盖：业绩预告/快报方向或修订、股东户数变化、未来已公告解禁压力、审计意见。全部候选都会写出，不会按结果偷偷筛选。

## 开发期候选 OOS 冻结（第一次未来 2025+ 评估）

在已核验的开发窗诊断 `report_id=782a042d666600a4383cce72ecff27c2599acbde0acd2b0f2100b164d928bd01` 上，冻结只读研究协议 [`config/research/a-share-event-candidate-oos-freeze-v1.json`](../config/research/a-share-event-candidate-oos-freeze-v1.json)，说明见 [`docs/a-share-event-candidate-oos-freeze.md`](a-share-event-candidate-oos-freeze.md)。

主终点锁定为 20 个交易日相对 `000300.SH` 收益；原始收益与 5/10 日窗口不得晋级候选。确定性门控只使用已封存的 2022/2023 summary，当前锁定 `forecast_upward_revision` 与 `audit_non_standard_opinion`。11 个预声明假设全部保留；通过者不自动进入评分或交易。2024 禁止用于选择。

用户已于 2026-08-25 授权第一次 2025+ 一次性 OOS 评估：公告窗 `2025-01-01..2026-07-23`，完整 20 日标签最晚入场 `2026-07-24`，`label_hard_end=2026-08-21`，绑定合同见 [`config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json`](../config/research/a-share-event-candidate-oos-one-shot-authorization-v1.json)。该授权已于 2026-08-25 消费完毕；封存审查说明见 [`docs/a-share-event-candidate-oos-one-shot-review.md`](a-share-event-candidate-oos-one-shot-review.md)。

一次性 OOS 产物目录 `data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1`：`authorization_id=efee84f049b6e8590a01bce2f185aacd7d01c5fae6845e9dca5432d8de980439`，`freeze_id=5d5298f0115f883c29d96cf2a1892ce4de7295c2068cabea96f23db393bad92e`，`report_id=0db5875c389520443c5249005d420f7bbc949385cd1c94448cf159113a00051d`，消费收据 `receipt_id=e931fafca1dcf596bb9b2b345792c0ded327a13e78e2ea99092fc169fdfce6c6`（[`one-shot-v1.consumption-receipt.json`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1.consumption-receipt.json)）。自校验 [`report.json`](../data/all-a-share-oos-20241001-20260821-v1/event-candidate-oos-evaluations/one-shot-v1/report.json) 绑定 OOS 行情与事件快照；`observation_rows=10931`；`candidate_multiplicity=2`；主终点仅为 20 日相对沪深 300 收益。

候选结果（方向复制诊断，不构成 alpha/盈利结论）：

- `audit_non_standard_opinion`：eligible=10652，known=10652，labeled=10329，signal1=383，signal0=9946；20d 相对均值 signal1=0.0012302876155067062，signal0=0.00884338373488073，spread=-0.007613096119374024；`outcome=direction_replicated`。
- `forecast_upward_revision`：eligible=279，known=185，unknown=94，`known_coverage=0.6630824372759857`，labeled=178，signal1=23，signal0=155；20d 相对均值 signal1=0.0030925688119775078，signal0=-0.016583190146612838，spread=0.019675758958590344；`outcome=not_evaluable`（`known_coverage` 低于冻结门槛 0.90）。

产物固定 `ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false`、`human_review_required=true`。输出目录与消费收据**不可覆盖、不得重跑**。冻结合同校验：`verify-a-share-event-candidate-freeze`。

## 当前边界

- 事件 overlay 尚未挂载到评分引擎。
- 没有声称股东户数、业绩预告、解禁或审计意见具有正收益。
- 2024 已经被观察，不能再作为新增事件因子的未见留出期。
- 当前采集器和质量报告不计算财务造假概率，也不实施“命中两个信号即剔除”、质押比例或解禁比例阈值。
- 提供方 `forecast.first_ann_date` 与 `ann_date` 矛盾时只隔离该可选字段并保留审计证据；隔离结果不得被解释为可得时点或因子信号。
- 点时诊断与离线审阅报告只描述覆盖率、修订、缺失和 PIT 可得行为；下一步仍须人工审查后再讨论候选阈值。任何阈值都要先进行分年度、覆盖率和留出期诊断，不能直接进入排行。
- 审阅报告、诊断快照与开发期候选事件诊断均不得接入评分引擎、成员过滤、回测或交易链路。
- 开发期候选诊断产物仅为候选证据；`development_only=true`，不能当作 OOS 或上线依据。
- 开发期 OOS 冻结合同锁定候选与主终点；一次性授权评估已于 2026-08-25 消费，审查说明见 [`docs/a-share-event-candidate-oos-one-shot-review.md`](a-share-event-candidate-oos-one-shot-review.md)。冻结合同、一次性授权与 OOS 产物均保持 `ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false`，不得自动部署；OOS 输出目录与消费收据不可覆盖、不得重跑。
