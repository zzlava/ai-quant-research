# 统计功效与 MDE 闸门 v1

## 目的

本里程碑先修复评价机器，不增加因子、不改变权重、不运行评分、回测或 OOS。

任何候选假设在进入确认性检验前，都必须回答：当前设计能否以预先声明的功效，检出具有研究意义的最小效应。若不能，结论只能是 `not_evaluable`，不得写成“因子无效”，也不得因此继续消耗样本外窗口。

## 冻结规划参数

- 单侧正向备择；
- 家族错误率 `0.05`；
- 目标功效 `0.80`；
- 四个候选端点；
- 规划发生在未知 p 值顺序之前，因此使用保守的 Bonferroni 最坏顺序分配，每端点 `alpha=0.0125`；
- 最小关注效应统一暂定为 40 日平均 Spearman IC `0.05`；
- 不把 `0.05` 宣称为市场真值。它是本轮透明、可审计的研究治理门槛；未来若修改，必须建立新版本并在新数据检验前冻结。

## 计算语义

v1 使用已审计 HAC 标准误的正态近似：

```text
MDE = (z_(1-alpha_i) + z_power) * HAC_SE
```

其中 `alpha_i=0.05/4`，`power=0.80`。只有 `MDE <= minimum_effect_of_interest` 时，该端点才记为 `evaluable_for_minimum_effect`。

这是规划近似，不替代有限样本模拟、块自助法或正式经济价值检验。标准误缺失、非有限、无法从冻结报告一致恢复、因子集合变化或源报告哈希变化时必须失败关闭。

## 回顾性校准边界

当前 2022–2023 结果已经被查看，因此只能用于方差/功效校准：

- 不重新解释已有 p 值；
- 不恢复已失败候选；
- 不选择因子或修改权重；
- 不运行 2024/2025+ 新检验；
- 不消费 OOS；
- 始终 `ready_for_scoring=false`、`ready_for_backtest=false`、`ready_for_trading=false`。

当前协议绑定：

- `config/research/statistical-power-gate-v1.json`；
- `data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-v2/report.json`；
- 绑定 SHA-256：`842e99d9fca389584a222e2b80a85cf345ced28329fad4073cd4b5254d0c7351`。

## 只读命令

生成回顾性功效校准报告：

```bash
.venv/bin/python -m app.cli review-statistical-power \
  --output data/all-a-share-historical-v1/research/statistical-power-review-v1.json
```

完整重算并验证：

```bash
.venv/bin/python -m app.cli verify-statistical-power-review \
  --review-file data/all-a-share-historical-v1/research/statistical-power-review-v1.json
```

## 后续门槛

在四臂随机对照里程碑开始前，必须先读取本报告：

- 端点为 `not_evaluable` 时，不得用同一设计做确认性胜负结论；
- 可以扩大独立历史覆盖、降低假设家族数量，或以新协议提高信息量；
- 不允许根据已看到的均值、p 值或 OOS 结果调低最小关注效应；
- 四臂对照仍需独立预注册随机种子、重复次数、执行约束和经济终点。
