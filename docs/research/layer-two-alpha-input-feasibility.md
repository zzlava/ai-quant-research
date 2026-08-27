# 第二层 Alpha 输入可行性上限审查

本里程碑在计算任何 IC、收益标签或组合之前，先回答一个更基础的问题：当前已封印的候选资格包与财务负面清单，是否**有可能**达到冻结协议的横截面覆盖门槛。

审查采用对策略最有利的上限假设：每个符合候选资格且财务裁决为 `clean` / `halved` 的名称，四个因子全部已知。如果连这个乐观上限也无法通过，真实因子诊断只会更差，因此必须失败关闭。

## 绑定输入

- 冻结 E11a alpha 开发协议与 E11b-0b 运行合同；
- 旧的只读输入 inventory（继续保留其历史状态，不覆盖）；
- `candidate-eligibility-pack-v1`，必须经全量重算校验；
- `financial-negative-list-verdict-overlay-v1`，必须经 726 个分区、来源与哈希全量重算校验；
- 市场与基本面 snapshot ID 继承并交叉绑定。

所有输入路径必须在仓库内，禁止软链接和 OOS 命名空间。报告 verifier 会重新执行上游严格校验并逐日重算，不只检查外层 self-hash。

## 冻结门槛

- 主标签：40 个市场交易日；标签端点不得越过同一证据窗口；
- 每个决策日 factor-known 横截面至少 500 只；
- factor-known 比例至少为 eligible 名称的 60%；
- 2022–2023 开发期至少 120 个有效主诊断日；
- 2022 和 2023 各至少 40 个有效主诊断日。

财务 `insufficient_evidence` 保持 unknown，不得当作 clean；`hard_excluded` 不进入 alpha eligible 上限。

## 当前结论

当前 726 个交易日输入的乐观上限结果：

| 区间 | 交易日 | eligible 上限中位数 | eligible 上限最大值 | 满足 500 且 h40 端点有效的日期上限 |
| --- | ---: | ---: | ---: | ---: |
| 2022 | 242 | 63.5 | 848 | 38 |
| 2023 | 242 | 67.0 | 840 | 37 |
| 2024（只报告） | 242 | 75.0 | 1087 | 37 |
| 2022–2023 合计 | 484 | — | — | 75 |

因此 2022、2023 和 pooled 三道冻结门槛均不可达。`ready_for_alpha_diagnostic_execution=false`，且本阶段不物化统计风险簇 companion、不计算因子 IC、不使用 2024 选择规则。

这不是 alpha 失效结论，而是**当前“财务排雷完整裁决必须进入因子证据分母”的评价协议无法提供足够统计功效**。下一步必须先冻结新的研究设计；推荐把：

1. alpha 因子有效性检验建立在完整候选资格横截面；
2. 财务负面清单作为独立的安全 overlay 报告覆盖与风险，不用低覆盖 known-only 子样本选择 alpha；
3. 实际候选应用继续保持 unknown 失败关闭；

三者分离。该语义变化需要新版本协议与试验账本记录，不能悄悄修改现有 E11a。

## 命令

```bash
PYTHONPATH=src .venv/bin/python -m app.cli \
  review-layer-two-alpha-input-feasibility

PYTHONPATH=src .venv/bin/python -m app.cli \
  verify-layer-two-alpha-input-feasibility \
  --report-file data/all-a-share-historical-v1/research/layer-two-alpha-input-feasibility-v1.json
```

两条命令均不联网、不读取 Token、不计算 IC/收益标签、不评分、不回测、不构造组合、不下单、不交易。
