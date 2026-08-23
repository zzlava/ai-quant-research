# BigQuant 免费公开重建数据包

此流程只用于**公开第三方重建研究**，不产出、替代或验证授权级历史点时（PIT）成员数据。它与
`historical_membership`、`membership_source_manifest.json` 和六表快照导入链路隔离。

BigQuant 当前数据页将 `cn_stock_index_weight` 标为每日更新、免费，并给出指数、成员代码、名称和权重字段；
它是低成本研究候选来源，但历史查询在今天可成功，不证明该信息在历史决策时已经可得。

## 本地准备

在 BigQuant 用户中心创建自己的 Access Key / Secret Key。不要把它们贴进终端历史、YAML、CSV、Git 或聊天。

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research
.venv/bin/python -m ensurepip --upgrade
.venv/bin/python -m pip install -e '.[bigquant]'
export AIQ_BIGQUANT_ACCESS_KEY='本机凭证'
export AIQ_BIGQUANT_SECRET_KEY='本机凭证'
```

## 收集 2022--2024 候选数据

```bash
.venv/bin/python -m app.cli fetch-bigquant-public-membership \
  --start 2022-01-01 \
  --end 2024-12-31 \
  --output-dir ./data/public-reconstruction/csi300-bigquant-2022-2024
```

命令输出一个独立目录：

- `source_documents/bigquant_cn_stock_index_weight.csv`：SDK 返回的原始表格副本；
- `candidate_membership.csv`：仅在行级字段合法时生成的研究候选；字段是 `source_date`，不是 `effective_from`；
- `quality_report.json`：每个来源日期的成员数、权重和、重复/非法代码错误；
- `collection_manifest.json`：查询窗口、来源链接、原始响应 SHA-256 和明确的可用性边界。

只有 `eligible_for_public_reconstruction=true` 时，候选数据才具有“每日均为 300 只”的最低完整性。即便通过，仍只可称为“公开重建 CSI300 历史研究数据”，不得称为“授权/许可级精确 PIT 数据”。

`retrieved_at` 仅记录这次收集发生的时间，绝不能复制为 `available_at`；本流程也不会自动调用 `build-universe-membership`、`verify-universe-source`、`score` 或 `backtest`。
