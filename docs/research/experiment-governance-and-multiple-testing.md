# 实验治理、多重检验与冻结研究协议

本文档说明为什么原始 Sharpe / 单次最好结果不能直接横向比较，以及机器可读 trial ledger 的审计口径。它不是评分、回测或交易授权。

机器可读台账：[`config/research/research-trial-ledger-v1.json`](../../config/research/research-trial-ledger-v1.json)

只读校验：

```bash
.venv/bin/python -m app.cli verify-research-trial-ledger \
  --ledger-file ./config/research/research-trial-ledger-v1.json \
  --repo-root .
```

该命令只校验自哈希、trial 图、证据/回执路径与 OOS 消费规则；**不评分、不回测、不交易**。

## 为什么原始 Sharpe 不能直接比较

1. **试了多少次会改变“最好结果”的解释**：同一研究过程中若比较了多个配置、持仓规模、持有期或事件假设，再挑出最高 Sharpe，会系统性高估可重复性。
2. **数据合同不同**：`public_reconstruction`、严格 PIT、开发窗、留出窗、一次性 OOS 不是同一抽样机制，数值不可直接并排当作同一检验。
3. **已消费 OOS 是终态**：同一 `freeze_id` 的一次性 OOS 一旦有消费回执，就不能再标成 reusable / clean OOS，也不能用同一窗口调参后重跑。
4. **描述性切片不是新试验**：同一配置的分年切片、成本压力情景、稳健性表或相位诊断，不自动等于新的独立策略 trial。

## 台账只提供试验次数下界

当前回填台账设置：

- `complete=false`
- `historical_backfill=true`
- `trial_count_is_lower_bound=true`

含义：登记的是“文档/冻结合同/消费回执中能明确识别”的 trial **下界**，不是全历史穷尽目录。未写入台账的探索、口头讨论、未落盘试跑，只能使真实多重检验负担 **不低于** 台账计数。

计数口径（回填）：

- **计入**：有明确假设与证据路径的策略/配置候选；预先声明的 12 个组合构造候选；事件冻结中注册的 11 个提名假设；带 `freeze_id` / `authorization_id` / receipt 的一次性 OOS 消费。
- **不计入独立 trial**：同一配置的年度切片、成本情景、冻结报告中的描述性表格、相位/IC 诊断网格。
- **非正式 holdout**：如 v4 叙事性 holdout，若缺少机器可读 freeze/authorization/receipt，则 `oos_consumed` 保持 `false`（不能伪造绑定），但 evidence 仍记录该窗口已被使用。

## Deflated Sharpe：缺绑定则 `not_evaluable`

Deflated Sharpe（或同类多重检验调整）若要给出数值，至少需要事先绑定并可审计的输入，例如：

- 观察到的 Sharpe；
- trial Sharpe 分布的标准差；
- 收益样本量；
- 偏度与峰度；
- 有效独立试验数。

本仓库当前 **尚未** 把上述输入与任何已审计公式完整绑定到评分或交易路径。实现仅提供输入模型与评估出口：

- 任一关键输入为 `null` / unknown → 状态必须是 `not_evaluable`；
- **禁止**在缺输入时用 0、空字符串或猜测值伪装；
- **禁止**输出伪算的 Deflated Sharpe 数值或 p 值。

本文件不引用或创造未经验证的公式。

## 冻结协议与下一次能否运行

机器审计问题应能从台账直接回答：

| 问题 | 台账字段 |
| --- | --- |
| 试过多少假设（下界） | `trial_count` + `complete=false` |
| 哪个 OOS 已消费 | `oos_consumed=true` 且绑定 `freeze_id` / `authorization_id` / `receipt_path` |
| 同一 freeze 能否再跑 | 同一 `freeze_id` 若已消费则为终态，不得标 available/clean |
| 能否评分/交易 | 顶层与每条 trial 固定 `ready_for_scoring=false`、`ready_for_trading=false`、`auto_deploy=false` |

下一步新假设必须：新 `trial_id`、新配置身份（如适用）、未消费的前瞻窗口与新的冻结/授权合同；不得回写已消费 OOS。
