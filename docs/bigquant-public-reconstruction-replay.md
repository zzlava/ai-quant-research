# BigQuant 公开重建 CSI300：隔离说明性模拟

此路径只适用于已经由 `fetch-bigquant-public-membership` 收集并通过完整性检查的目录。它不是、也不会生成许可证级或严格点时（PIT）CSI300 成分历史。

## 不可跨越的边界

- `source_date` 是 BigQuant 返回的历史快照日期，不是已证明的 `effective_from` 或历史 `available_at`。
- `retrieved_at` 只记录本机收集时刻；不会复制进标准六表快照的 `available_at` 字段。
- 每一次评分和模拟都会返回 `research_scope=public_reconstruction`、固定中文限制说明，以及 `public_reconstruction_id`。
- 此路径的收益指标只能称为**说明性模拟**；不能与 `baseline_csi300_pit_v1` 或任何正式 PIT 回测比较，也不得作为策略收益证据。

## 数据结构

公开重建包是一个独立覆盖层，至少包括：

- `collection_manifest.json`：原始文件 SHA-256、来源与非 PIT 边界；
- `quality_report.json`：每日成员数完整性；
- `source_documents/bigquant_cn_stock_index_weight.csv`：哈希绑定的原始响应；
- `candidate_membership.csv`：必须逐行与原始响应一致。

运行前会重新验证原始文件 SHA-256、候选行、每日 300 只、质量报告和日期范围。任一文件被篡改、缺失或出现不完整截面即拒绝运行。

## 所需的基础行情快照

公开重建覆盖层不含个股 OHLCV、停牌、复权、指数或全球市场数据。先用现有 `fetch-tushare` 收集所有候选股票的**基础行情快照**，它的静态股票池只用于下载这些行情，不代表 CSI300 历史成员。

建议另用一个数据目录，避免覆盖当前全市场快照：

```bash
export AIQ_DATA_DIR=./data/csi300-bigquant-public-replay-v1
export AIQ_DATABASE_URL=sqlite:///data/csi300-bigquant-public-replay-v1/app.db
export AIQ_PUBLIC_RECONSTRUCTION_DIR=./data/public-reconstruction/csi300-bigquant-2022-2024
```

先导出整个公开重建包中出现过的股票代码；这只是行情下载清单，不是历史 CSI300 成分文件：

```bash
.venv/bin/python -m app.cli export-bigquant-public-symbols \
  --collection-dir "$AIQ_PUBLIC_RECONSTRUCTION_DIR" \
  --output ./data/csi300-bigquant-public-replay-v1/public-reconstruction-symbols.txt
```

再使用既有的受控静态下载配置生成基础行情快照。其静态股票池只用于下载行情；公开重建策略永远不会把它当 CSI300 历史成员：

```bash
.venv/bin/python -m app.cli fetch-tushare \
  --start 2021-10-01 \
  --end 2024-12-31 \
  --strategy baseline_real_cn_v1 \
  --symbols-file ./data/csi300-bigquant-public-replay-v1/public-reconstruction-symbols.txt \
  --source-version bigquant-public-reconstruction-base-v1
```

基础行情应覆盖首个研究日之前至少 60 个交易日，并包含 BigQuant 包中出现的全部股票。基础行情快照自身仍须满足普通六表哈希/完整性校验。

完成基础行情快照后，运行：

```bash
.venv/bin/python -m app.cli preflight-research \
  --strategy csi300_bigquant_public_reconstruction_v1 \
  --start 2022-04-01 \
  --end 2024-12-31
```

预检通过后才可进行评分或说明性模拟：

```bash
.venv/bin/python -m app.cli score \
  --strategy csi300_bigquant_public_reconstruction_v1 \
  --date 2024-12-31

.venv/bin/python -m app.cli backtest \
  --strategy csi300_bigquant_public_reconstruction_v1 \
  --start 2022-04-01 \
  --end 2024-12-31
```

如果 `AIQ_PUBLIC_RECONSTRUCTION_DIR` 未设置、基础行情未覆盖全部成员、某日成员数不为 300、或原始哈希不一致，命令会失败，绝不静默改用其他日期成员。
