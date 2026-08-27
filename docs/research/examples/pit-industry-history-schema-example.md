# PIT 行业历史 schema 示例（合成 / 不完整）

本文件**不是**真实全市场行业账本。下列行与 manifest 字段仅演示契约形状；`complete=false`。

## CSV 示例行

```csv
symbol,industry_scheme,industry_version,industry_code,industry_name,effective_from,effective_to,announced_at,available_at,source_reference
000001.SZ,demo_scheme,demo_v1,D01,Demo Banks,2020-01-02,2021-12-31,2019-12-20T08:00:00Z,2019-12-20T16:00:00Z,synthetic-fixture-row-1
000001.SZ,demo_scheme,demo_v1,D02,Demo Brokers,2022-01-04,,2021-12-15T08:00:00Z,2021-12-15T16:00:00Z,synthetic-fixture-row-2
```

## Manifest 字段清单

| 字段 | 示例值 / 规则 |
| --- | --- |
| `schema_version` | `"1"` |
| `contract_version` | `pit-industry-history-contract-v1` |
| `source_name` | `synthetic-research-fixture` |
| `industry_scheme` / `industry_version` | 与 CSV 一致 |
| `history_file` | 相对文件名，如 `industry_history.csv` |
| `history_file_sha256` | CSV 字节 SHA-256 |
| `coverage` | 与 CSV 生效区间跨度一致 |
| `available_at_definition` / `available_at_evidence` | 非空说明；不得写“下载时间” |
| `generated_at` / `retrieved_at` | UTC 时间戳 |
| `pit_semantics` | 必须为 `point_in_time_history` |
| `complete` | 示例为 `false` |
| `universe_notes` | 可空（仅 `complete=true` 时必填） |
| `manifest_id` | 对除自身外的规范 JSON 自哈希 |

正式数据由用户/可信来源离线提供后，再用 `verify-pit-industry-source` 校验。
