# 第二层 Alpha 开发证据协议（E11a）

Layer-two alpha development evidence protocol (E11a). **Protocol / evidence gates only** — this milestone does **not** run data, score, wire scoring, generate strategy YAML, backtest, or trade. E11a runs no data whatsoever: every number in this document is a frozen rule, not a computed result. It freezes how four high-is-good factor families may earn development evidence and equal weights; it does **not** invent return hurdles beyond the stated IC / quintile / Holm gates — the evidence gates below are exactly the bar, never more, never less.

| 项 | 路径 |
| --- | --- |
| 机器可读协议 | [`config/research/layer-two-alpha-development-protocol-v1.json`](../../config/research/layer-two-alpha-development-protocol-v1.json) |
| 校验器 | `src/app/research/layer_two_alpha_development_protocol.py` |
| 测试 | `tests/test_layer_two_alpha_development_protocol.py` |

| 字段 | 值 |
| --- | --- |
| `status` | `confirmed_for_development_but_not_ready` |
| `ready_for_*` / `auto_apply` | 全部 `false` |
| `does_not_run_data` / `does_not_score` / `does_not_wire_scoring` / `does_not_generate_strategy_config` | 全部 `true` |

## 上游磁盘绑定（漂移即失败）

| 上游 | 路径 | 绑定 id |
| --- | --- | --- |
| research trial ledger | `config/research/research-trial-ledger-v1.json` | `1fc944251212da4972a087b4c54263912d621e43ad400b5936d6a492f1f9b9f4` |
| two-layer 决策合同 | `config/research/two-layer-strategy-decision-draft-v1.json` | `27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6` |
| tranche 评价协议 | `config/research/tranche-evaluation-protocol-draft-v1.json` | `8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a` |
| allocation 实现解释协议 | `config/research/layer-two-allocation-implementation-protocol-v1.json` | `0cbde5a96ccbe89fe87613101fad5210d96c87142b1f1dc9e6bfd975ef2b60e2` |

file verifier **逐个读盘 verify** 上述四份上游文件；四个 binding 必须**同时**为 true，禁止 partial bindings。structural verifier 四个 binding 均为 false。

## 研究窗口（非重叠、按时间顺序）

| 窗口 | 区间 | 角色 |
| --- | --- | --- |
| development | 2022-01-01 .. 2023-12-31 | 开发证据唯一可选窗 |
| seen_robustness | 2024-01-01 .. 2024-12-31 | **仅报告**；不得选因子或改权重 |
| consumed_oos | 2025-01-01 .. 2026-08-21 | **禁止**复用 |
| new_frozen_oos | 自 2026-08-22 起 | E11a **不可评价** |

consumed OOS 不得与 development / seen_robustness 重叠。

## 四个 high-is-good 因子族（固定顺序）

1. **quality** — ROE/ROIC/毛利率高、资产负债率低、经营现金流/营收高；等权 CS average-rank 百分位；至少 3 个已知成分；基本面 timing：`strict_initial_as_announced`，`available_at<=decision_at`，`report_period<=as_of`，`max_report_age_days=550`。
2. **value** — 正 PE/PB/PS 的 inverted average-rank 等权均值；至少 2 个已知；日频估值 `available_at<=decision_at`，`date<=as_of`，`max_age_days=10`。
3. **medium_momentum_12_1** — `adjusted_close[t-21]/adjusted_close[t-242]-1`；243 根有序正有限 market bar；禁止 future rows。
4. **defensive_low_vol** — 60 个简单收益（`return_count=60`）的负样本标准差（`sample_stdev_ddof=1`），来自 61 根有序正有限 adjusted close（`close_count=61`），按 `sqrt(242)` 年化（`annualization_sqrt_242=true`），符号固定为 `negative`。

## 前瞻标签窗口与 pooling（`ForwardLabelAndPoolingPolicy`）

- 每一个因子证据观测都要求决策日 `t` **与**标签终点 `t+h` 落在**同一个**证据窗口内（`same_window_endpoint_required`）。
- 开发期 40d/20d/5d 标签不得跨过 2023-12-31（`development_labels_must_not_cross_2023_12_31`）；2024 稳健性标签不得跨过 2024-12-31，且永不读取 consumed OOS（`robustness_2024_labels_must_not_cross_2024_12_31` / `robustness_2024_must_never_read_consumed_oos`）。
- 评价日历 = 每一个 eligible 的 market trading day，**不是** tranche phases（`calendar_every_eligible_market_trading_day_not_tranche_phases`），仍需满足同窗终点与覆盖门。
- 没有精确 `t+h` market-calendar 终点的日期视为无效/unknown，**永不**缩短 horizon（`never_shorten_horizon` / `missing_exact_endpoint_is_unknown`）。
- Pooled 指标 = 逐决策日观测的算术平均（`pool_arithmetic_mean_of_per_decision_day_observations`），**永不**在 name-row 层面 pool（`never_pool_at_name_row_level`）。

## 排名：精确百分位 / 五分位 / Spearman

**`CrossSectionRankingPolicy`（百分位公式冻结）**

- 百分位公式：`(average_rank_1_based - 1)/(n - 1)*100`；`n=1` 视为 unknown（不是 50）。
- Ties 取平均秩；缺失/无效保持 unknown，**永不**填补（`missing_or_invalid_never_imputed`），**永不**填 0/中性。
- **任何阶段**都不 winsorize（`no_winsorization_at_any_stage`，不仅仅是排名后）。
- 低方向/inverted 百分位 = `100 - p`。
- 多成分因子（quality、value）：各成分先排名到百分位 → 对已知成分百分位取均值 → 对结果 family composite **重新按同一公式排名**得到最终 0..100 值（`component_to_family_composite_rule`）。

**`QuintileSemanticsPolicy`**

- 基于平均秩（ties 平均）；bucket 公式 `min(floor((rank-1)/n*5), 4)`，与 `quantile_portfolios.py` 保持一致。
- Ties 永不跨桶拆分；全相等或空的 top/bottom 桶视为无效。
- 桶内等权平均；`quantile_count` 固定为 5。

**`SpearmanIcSemanticsPolicy`**

- 仅在配对的、已知且有限的因子/标签行上取平均秩（pairwise deletion）。
- 因子全相等或标签全相等视为无效，**不**强制为零相关。

## 精确时间序列推断（`InferencePolicy` + 精确 NW/Holm）

### Newey-West / Bartlett（`NeweyWestBartlettExactAlgorithm`）

- 输入：按时间排序、有限、**无 gap filling** 的逐 decision-day metric 序列 `x_1..x_n`；均值 `xbar` 为算术平均。
- `gamma_k = (1/n) * sum_{t=k+1..n} (x_t-xbar)(x_{t-k}-xbar)`，含 `gamma_0`，分母一律为 `n`。
- Bartlett 权重：`w_k = 1 - k/(L+1)`。
- 长期方差：`LRV = gamma_0 + 2*sum_{k=1..min(L,n-1)} w_k*gamma_k`。
- `var(mean) = LRV/n`。若 `n <= L`，或 LRV/方差非有限或 `<=0`，则推断统计量与 **raw** HAC p 为 **null/undefined**，**禁止**强制成数字。
- 正向检验：`stat = xbar/sqrt(var_mean)`，`p = 1-Phi(stat)`（标准正态 CDF）。市值带负向检验：`p = Phi(stat)`。

### raw 报告 vs Holm 代入

证据/方差缺失时：`hac_statistic` 与 `hac_p_value` 保持 null；`holm_input_p_value=1` 且 `holm_rejection=false`。

### Holm step-down（`HolmStepDownExactAlgorithm`）

- 假设族**仅**四个 pooled h40 daily IC；spread 正向性与分年方向是 gate，**不是** Holm 成员。
- 排序键：effective p 升序，并列时按冻结族序 `quality/value/medium_momentum_12_1/defensive_low_vol` 决胜。
- 排序位 `i=1..4` 阈值：`alpha/(4-i+1)`；顺序拒绝直到第一次失败，其后全部非拒绝。
- 每个因子报告：sorted position、threshold、effective p、raw p、rejected。

## Bar-window 端点（禁止 skip-compress）

- **动量**：恰好最新 **243** 根连续 market-calendar 观测（止于 decision t）；公式固定用窗内 `t-242` 与 `t-21`；任一缺失/未验证交易日 → 因子 unknown。
- **低波**：恰好最新 **61** 根连续 market-calendar 观测（止于 decision t）形成恰好 **60** 个简单收益；任一缺失/未验证 → unknown。
- **标签**：精确 `h` 终点 = 同标的 market-calendar 观测 `t+h`；缺失/未验证 → unknown，horizon **永不**平移/缩短。

## 覆盖门（coverage gates）

- 每个 decision：factor-known CS ≥ 500 且 ≥ 60% eligible names。
- 合并主评分日 ≥ 120，且 2022、2023 各 ≥ 40。

## 冻结前选择（无连续优化）

每个因子须同时满足：

- pooled 40d mean IC > 0
- pooled top-minus-bottom quintile spread > 0
- 2022 与 2023 **分别**对两项指标方向为正
- cluster-companion pooled 方向为正（同时要求 pooled h40 IC 与 spread 均为正）
- pooled 40d IC 通过 Holm 校正下单侧 HAC（四假设；**仅** pooled h40 daily IC 进 Holm；spread/分年方向仅为 gate）

## 市值带（`SizeBandDiagnosticSafeguards`，结构化边界，diagnostic safeguards，非新 trial）

- 精确边界（CNY 自由流通市值）：`[3e9,5e9)`、`[5e9,1e10)`、`[1e10,+inf)`。低于 3e9 在 safeguard 之外；unknown 保持 unknown，**不会**静默通过。
- 一个带为 positive 当且仅当该带 pooled h40 mean IC **与** pooled h40 top-minus-bottom spread **同时**为正；至少两个带须为 positive。
- 一个带为显著 negative：对 pooled h40 daily IC 做下尾单侧 Newey-West/Bartlett 检验（`H0: mean>=0` vs `H1: mean<0`），5% 显著即判定；任一带显著为负则该 safeguard 失败。
- 有效主评分日 < 40 的带视为 unknown，**不能**计入 positive。

## 权重

- 合格因子：等权，和为 1；不合格：0。
- 若无一合格 → 权重不可用，协议保持 not ready。

## 2024 稳健性（report-only）

仅可测试**已冻结**的合格集合与等权。若 pooled 40d IC 或 quintile spread **反转** → 稳健性失败，但**永不** retune 权重。

## 精确簇伙伴因子（`ClusterCompanionPolicy`，替代旧版模糊 `IndustryClusterPolicy`）

绑定到 two-layer 已封存的 `StatisticalRiskClusterPolicyConfirmed`：

- lookback = 120 交易日 → 120 个收益 / 121 根 adjusted close；Pearson 阈值 0.65；connected-component chain linkage 语义。
- 禁止 static/current industry labels（`static_current_industry_labels_forbidden` / `no_current_industry_labels`）。
- 簇成员仅在每个自然月的**首个 market trading day**、仅用截至该决策收盘的数据重新计算（`recompute_anchor`）；此前的分配一直保持到**下一个月度锚点之前**（`carry_assignment_until_before_next_anchor`）；新股/未分配/信息不完整 → unknown，**不回填**（`new_unassigned_or_incomplete_is_unknown_no_backfill`）。
- 伙伴因子 = 同一个最终原始 family composite，在**每个完整簇内部**用同一百分位公式**重新排名**（`companion_factor_definition`）；单例簇视为 unknown（`singleton_clusters_unknown`）。
- 伙伴证据要求：配对的已知名字、同样的日历/终点/覆盖/五分位/Spearman 门（`companion_evidence_basis`），且要求 pooled h40 IC 与 spread **同时**为正。
- 仅作 safeguard：**永不**作为独立权重，**永不**作为第五个 Holm 假设（`safeguard_only_never_independent_weight` / `never_fifth_holm_hypothesis`）。
- 不替代 industry PIT，不作自动权重选择器（`not_replacement_for_industry_pit` / `not_automatic_weight_selector`）。

## Report-only diagnostics

次 horizon、raw vs cluster companion、long-only top-quintile returns — 均不可选择或改权重。

## 资格分母与 PIT 快照绑定（新增，仍为协议描述，不接线）

**`EligibilityDenominatorPolicy`**

- 分母 = 在该决策时点，具有完整、已验证的第二层候选资格 **且** 财务负面清单裁决齐备的名字集合。
- 只有 `eligible_for_new_entry=true` **且**财务裁决非 hard-exclude/非 unknown 的名字才进入因子证据。
- alpha 因子本身**绝不**决定资格（`alpha_factor_must_not_determine_eligibility`）。

**`PitSnapshotBindingPolicy`**

- 所有相关的 `as_of`/`decision_at`/`available_at` 必须是 point-in-time，且共享**完全相同**的已封存市场快照。
- 本协议可以描述所需的未来输入绑定，但仍然只是协议——所有 ready 标志保持 `false`。

## Ledger 登记说明

未来 ledger 更新须登记 **一个四假设 family**；**E11a 不修改** `research-trial-ledger-v1.json`。

## 与 allocation / scoring 的边界

本协议冻结证据规则与窗口；**不**实现因子计算引擎、**不**接入 production scoring/backtest；E11a **不跑任何数据**。证据 blocker 明确：`factor_evidence_pipeline`、`alpha_weight_wiring` 仍为 pending。上述所有 IC / quintile / Holm / cluster / size-band 门槛就是全部证据要求本身——它们不是额外收益门槛的下限，也不会被静默放宽或收紧。
