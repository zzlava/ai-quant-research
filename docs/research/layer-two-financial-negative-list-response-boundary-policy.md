# Layer-Two Financial Negative-List Response Boundary Policy (E11b-2d)

联网采集请求仍绑定 `config/research/financial-negative-list-response-boundary-policy-v2.json`；
`config/research/financial-negative-list-response-boundary-policy-v3.json` 仅用于完成该既有请求的
离线封印验收。v1/v2 均继续保留并可独立验证。

## 目标

- 仅处理两个已观察到且可审计的边界形状：
  - A（`balancesheet`/`income`）：`ann_date` 在窗口内，`end_date <= 2024-12-31`，但
    `f_ann_date > 2024-12-31`。
  - B（`fina_indicator`/`fina_audit`）：按报告期查询返回 `end_date <= 2024-12-31`、
    `ann_date > 2024-12-31` 且不存在 `f_ann_date` 的后续版本。
- 不改写原始语义，不放宽其它 2025+ 形状；其余全部 fail-closed。

## 处理规则

- 对上述两种形状：
  - 计算完整规范化源行的 `source_row_hash`
  - 不将该行写入 PIT parquet
  - 生成逐行 sealed quarantine receipt（仅元数据，不含财务数值/审计文本 payload）
- receipt 仅允许以下核心字段：
  - endpoint/symbol/ann_date/f_ann_date/end_date
  - report_type/comp_type/end_type/update_flag（若存在）
  - effective_disclosure_date
  - reason_code=`FNLD-013`
  - source_row_hash
- endpoint 特定约束：
  - A 必须保留整数 `report_type/comp_type` 与 `update_flag`，有效时点为 `f_ann_date`。
  - v2 要求 `end_type` 为整数；v3 只在源响应本身未提供该元数据时允许其保持 null，存在时仍必须为整数。
  - `fina_indicator` 的 B 只允许 `update_flag`，其它报告类型字段必须为 null，有效时点为 `ann_date`。
  - `fina_audit` 的 B 上述四个可选字段必须全部为 null，有效时点为 `ann_date`。
- receipt 只保存元数据和完整源行哈希；`interestdebt`、审计意见、审计费用等未来 payload
  均不得写入 receipt 或 PIT parquet。

## 绑定与验证

- 策略文件自封存（policy_id = canonical sha256）。
- 强绑定 E11b-2a base protocol 的 path/id/file sha。
- collection 的 `source_manifest`、`quality_report`、`collection_manifest` 同时绑定：
  - receipt 路径->文件哈希映射
  - receipt 计数（总数、按 endpoint、按 reason_code）
- verifier 对缺失、篡改、伪造、schema 漂移、哈希/计数不一致、额外文件全部 fail-closed。

## v3 离线封印记录

- 适用原始 request：`465f7d74a30a463b67746134430b629ec7d6b7d4c181c76c4fad95c8675aa75f`
- 离线授权：`config/research/financial-negative-list-finalization-authorization-20260827-v1.json`
- 不改写既有 Parquet 或 receipt；不访问网络或 Keychain。
- 22,176 个分区全部复用验证；6,730 个 receipt 全量校验。
- 源缺失 `end_type` 共 135 条：`balancesheet=93`、`income=42`。
- 封印后的 collection ID：`f83789dfdb26367fa16e935b0a0348dceb90883e2e9d5db56ec0a120c123b6bc`。
- 三份 manifest 同时绑定 v3 policy、离线授权文件哈希和上述缺失计数。

## 边界声明

- 仅用于离线历史研究治理，不构成评分、回测、交易就绪授权。
