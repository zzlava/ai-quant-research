# 第二层组合分配实现解释协议（E10d-0）

研究 / 实现解释 only。本里程碑**冻结**两层策略中「8000 元最小基础仓」与 size / financial 风险乘数如何共存的解释；**不**实现约束分配器，**不**构造组合，**不**计算订单，**不**接入评分 / 回测 / 交易 / 券商。

| 项 | 路径 |
| --- | --- |
| 机器可读协议 | [`config/research/layer-two-allocation-implementation-protocol-v1.json`](../../config/research/layer-two-allocation-implementation-protocol-v1.json) |
| 校验器 / 解释助手 | `src/app/research/layer_two_allocation_protocol.py` |
| 测试 | `tests/test_layer_two_allocation_protocol.py` |

| 字段 | 值 |
| --- | --- |
| `status` | `confirmed_for_implementation_but_not_ready` |
| `ready_for_scoring` / `backtest` / `portfolio_construction` / `orders` / `trading` | 全部 `false` |
| `does_not_*` | 全部 `true` |

## 上游磁盘绑定（漂移即失败）

| 上游 | 路径 | 绑定 id |
| --- | --- | --- |
| two-layer 决策合同 | `config/research/two-layer-strategy-decision-draft-v1.json` | `27a6fd11a8324aea2eca90353a5ca5ceeba69ee4d3d2ebee6445d72ef92a18d6` |
| layer-one 指数协议 | `config/research/layer-one-index-development-protocol-draft-v1.json` | `b7aa9de1539cdd791aee5b74ca8ec3f269b6ed809a070caa917686742c4b1b2f` |
| tranche 评价协议 | `config/research/tranche-evaluation-protocol-draft-v1.json` | `8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a` |

file verifier **逐个读盘 verify**；常量 / path / 磁盘 content hash 任一漂移 → 失败。

## 冻结解释（用户已确认）

### 1. 资本与袖套预算

- `initial_cash=80000` 仅是研究起点。
- 实际分配使用已验证的 `current_account_equity` 与 layer-one 当前 `risk_budget ∈ {0, 0.3, 0.6, 0.9}`。
- `sleeve_budget = current_account_equity * risk_budget`，**绝不超支**。

### 2. 8000 是乘数前的 minimum_base_slot_notional

- 预算档位最大活动 slot / tranche：`0 / 0.3 / 0.6 / 0.9 → 0 / 3 / 6 / 9`（绝对上限 9）。
- 非零预算：`base_slot_count = min(contract_cap, floor(sleeve_budget / 8000))`；若为 0 → **不开仓**。
- `base_slot_notional = sleeve_budget / base_slot_count`，因此**乘数前**每槽 `≥ 8000`。
- **不是**乘数后的 floor。

### 3. 最终目标名义与风险乘数

```
final_target_notional = base_slot_notional * size_multiplier * financial_multiplier
```

| 乘数 | 允许值 |
| --- | --- |
| size | `0.5` / `0.75` / `1.0` |
| financial | `0` / `0.5` / `1.0` / `unknown` |

- `financial=0` → **硬排除**。
- `financial=unknown` → **留现金**。
- 乘数后可以低于 8000。
- **禁止**抬回 8000、**禁止**舍入补偿、**禁止**把降权偷偷变成硬排除。

### 4. v1 释放资本一律留现金

风险乘数释放的资本：

- 不得同日转给其他候选；
- 不得回填小盘股；
- 不得放宽阈值。

未来若要在其他合格股间再分配 → **新协议 + 新审查**。

### 5. 簇上限分母 = sleeve_budget

- 每簇目标名义总和 `≤ 0.35 * sleeve_budget`，最多 2 只。
- 分母**不是**实际已投资金额（避免现金机械抬高已投股票的簇权重）。
- unknown / incomplete cluster report → **整体留现金**。

### 6. 执行层边界（本协议不算订单）

100 股整手、费用、涨跌停 / 停牌、T+1 open 属后续执行层。若最终目标买不起一手或成本门失败 → 该槽留现金，**不得提高目标**。

### 7. 留现金情形

候选不足、unknown、无可用 slot、买不起 → 留现金；不复用其他日期名单；不追赶补仓。

### 8. 活动仓位 vs 40 日周期

- `active_target_count` 可因资本 / 候选不足小于档位 cap。
- `active_tranche_count = active_target_count`。
- **40** 是持有 / phase cycle，**不是**活动 tranche 数。

### 解释输入失败关闭

解释助手与 worked-example 数值字段：

- **拒绝 bool**（`True`/`False` 不得被 float 强制成 `1.0`/`0.0`）
- **拒绝 NaN / Inf**
- **拒绝负** equity / budget / notional / sleeve
- **允许** `current_account_equity=0`（在公式下不开基础槽）

## 用例：80k × 30% → 降权到 2k

1. `current_account_equity=80000`，`risk_budget=0.3` → `sleeve_budget=24000`。
2. `floor(24000/8000)=3`，档位 cap=3 → **3 个基础槽 × 8000**（乘数前）。
3. 候选落在流通市值 30–50 亿档（size `×0.5`），且财务预警恰好 1 项（financial `×0.5`）：
   - `final = 8000 * 0.5 * 0.5 = 2000`。
4. 这是刻意保留的**降权**语义：目标名义可以低于 8000，**不得抬回 8000**。
5. 若执行层发现 2000 买不起 100 股整手 / 成本门失败 → **留现金失败关闭**，仍不得为了成交抬高目标。

确定性对照（解释助手 `plan_base_slots`）：

| equity | budget | sleeve | slots | base_slot_notional |
| --- | ---: | ---: | ---: | ---: |
| 70k | 0.3 | 21000 | 2 | 10500 |
| 80k | 0.3 | 24000 | 3 | 8000 |
| 100k | 0.3 | 30000 | 3 | 10000 |

## 增强层

真实 PIT 行业、机构 / 事件可在后续增强层加入，但**不得静默改写本协议**。

## 明确非目标

1. 不修改既有冻结合同 / layer-one / tranche 协议或策略 / 交易代码。
2. 不实现可运行分配器 / 不生成订单。
3. 不跑 score / IC / backtest，不接券商。
4. 不把 8000 解释成乘数后 floor，不把减资再分配，不把簇分母改成 invested。
