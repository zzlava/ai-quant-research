# PIT 行业历史来源契约（E6b）

研究用途 / **非交易就绪**。本文定义可审计点时（PIT）行业历史的离线 CSV + JSON manifest 契约与 fail-closed 校验。仓库**不提供**声称完整的真实行业文件；仅允许 schema 示例与合成 fixture。

## 关闭门

在可审计 PIT 行业历史来源**尚未提供并通过校验**之前：

- 两层策略第二层的历史行业中性 / 行业上限路径保持 **blocked**；
- 不得用 `stock_basic` 等**当前静态行业**冒充历史分类；
- 不得把下载时间或文件生成时间当成 `available_at`；
- 统计风险簇只是过渡风险代理，不能填补行业 alpha / 行业中性缺口。

决策合同字段 `layer_two.pit_industry_source_requirement` 仍由用户确认；本契约只提供机器可验证的数据形状。

## CSV schema

必填列（顺序不限，但不得缺列、不得额外列）：

| 列 | 说明 |
| --- | --- |
| `symbol` | 证券代码 |
| `industry_scheme` | 分类体系名 |
| `industry_version` | 体系版本 |
| `industry_code` | 行业代码 |
| `industry_name` | 行业名称 |
| `effective_from` | 生效起始日（ISO date） |
| `effective_to` | 生效结束日（可空=开放区间） |
| `announced_at` | 公告时刻（UTC，须含 `T`） |
| `available_at` | 决策可观测时刻（UTC，须含 `T`） |
| `source_reference` | 来源引用 |

约束：

- `announced_at <= available_at`
- `effective_from <= effective_to`（当结束日非空）
- 同一 `(symbol, scheme, version)` 的生效区间不得重叠；开放区间不得被后续行穿越
- unknown 字段不得用 `""` / `0` 伪装

## Manifest

JSON manifest **自哈希**（`manifest_id`），并绑定：

- `history_file` / `history_file_sha256`
- `source_name`、`industry_scheme`、`industry_version`
- `coverage.start` / `coverage.end`（须与 CSV 生效区间跨度一致）
- `available_at_definition` / `available_at_evidence`
- `generated_at` / `retrieved_at`（UTC）
- `pit_semantics=point_in_time_history`（拒绝 `current_static` 等冒充）
- `complete` 与 `universe_notes`（`complete=true` 时 notes 必填）
- 固定 `does_not_score/backtest/trade=true`，`ready_for_*=false`

## 点时选择

`select_industry_as_of(records, symbol, effective_date, decision_at)`：

1. 仅保留 `available_at <= decision_at` 且生效区间覆盖 `effective_date` 的行；
2. 0 条 → 明确 `status=unknown`（不回退当前行业或区间末日行业）；
3. \>1 条 → **失败**（歧义）；
4. 恰好 1 条 → `status=known` 并返回该记录。

## CLI

```bash
.venv/bin/python -m app.cli verify-pit-industry-source \
  --history-file /path/to/history.csv \
  --manifest-file /path/to/manifest.json
```

只读校验；输出固定 `does_not_score=true`、`does_not_backtest=true`、`does_not_trade=true`。

## 合成示例（非完整真实数据）

见 `docs/research/examples/pit-industry-history-schema-example.md`。任何 `complete=false` 的示例仅用于形状演示，不得当作全市场行业账本。
