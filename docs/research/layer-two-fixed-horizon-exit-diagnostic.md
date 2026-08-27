# Layer-two Fixed 40-Market-Bar Exit Diagnostic（E10f-2）

> **醒目：本模块不是真实成交、不是回测、不是收益 / PnL / alpha 评估。**
> 它只对一份已封印的 E10f-1 **假设开放持仓**做固定 **40** 市场日退出**可退出性与卖出成本**研究标签。
> 当前语义对齐冻结的两层策略 / tranche evaluation protocol v2（`holding_period_market_trading_days=40`）。
> **已消费的 `p10_h20` 试验不是现行策略**；本模块不绑定、不提供 holding-period 参数切换、不接受 caller 输入持有期。
> 不修改 lifecycle、不发单、不接评分 / IC / 回测 / API / CLI / DB。

## 模块

| 项 | 路径 |
| --- | --- |
| 引擎 | `src/app/research/layer_two_fixed_horizon_exit_diagnostic.py` |
| 测试 | `tests/test_layer_two_fixed_horizon_exit_diagnostic.py` |

## 冻结语义

1. **上游**：完整 E10f-1 lifecycle record + structural/file inputs、E10f-0 stamp-tax contract、同一 `phase_report.market_calendar`、显式按日 exit observations。复用真实 E10f-1 / E10f-0 structural 与 file verifier；不信任 ready bool。
2. **持有（现行 40 日协议）**：`entry_trade_date` = holding bar 1；完整持有 **40** 个 market bars 后，在 calendar 的 `entry_index + 40`（第 **41** 个市场日）开盘首次尝试退出。`holding_period_market_bars=40`；scheduled 日 `holding_market_bars_elapsed_before_open=40`，之后逐日 +1。停牌 / 跌停 / unknown / 顺延至下一可交易开盘语义不变。
3. **协议审计字段**：报告写入 `tranche_evaluation_protocol_id` / `tranche_evaluation_protocol_path`，必须等于封印常量
   `8ad6b70fa8e37501f6ab9e436b0698a591f25b2b4f3fc14329d97dff47bdea8a` 与
   `config/research/tranche-evaluation-protocol-draft-v1.json`；构造时从 E10f-1 lifecycle 已绑定值校验写入，禁止 caller 伪造。
4. **观察**：仅 `raw_open` / `published_down_limit`；unknown/suspension 必须 null；tradable 必须有限正数非 bool。无 close/high/low。观察须从 scheduled 日起按 calendar **连续**，禁止跳日 / 重复 / 乱序；缺行不得冒充 unknown。
5. **扫描（无后见）**：unknown → 立即终止；停牌 / **严格** `raw_open <= published_down_limit` → `blocked_limit_down` 顺延；仅当 `raw_open > published_down_limit` 才可退出。金额容差（`1e-9`）**只用于金额恒等式**，不得用于法律价格顺序判定。
6. **卖出情景**（仅可退出时）：base 5bps / stress 15bps；佣金常量同 E10e-0；卖方印花税必须经完整验证的 E10f-0 helper。`slipped = raw*(1-bps/10000)`；**严格** `floor_applied = slipped < down`，`fill=max(slipped,down)`，并**严格** `fill >= down` 且 `fill <= raw`（不容差放过逆序）。不计算 return/PnL。
7. **declared_window**：entry / scheduled / 所有 observation / exit date 必须在 `2022-01-01..2024-12-31`；不得借 `verified_through` 跑 2025+。税率边界：`2023-08-27=0.001`，`2023-08-28=0.0005`。

## 与已消费 p10_h20 的区分

| 项 | 现行 E10f-2 / 两层协议 | 已消费 p10_h20 |
| --- | --- | --- |
| 持有市场日 | **40** | 20（历史试验，禁止再绑定） |
| 首次退出尝试 | `entry_index + 40` | 不适用 |
| 参数切换 | **无**；常量封印 | 不得作为本模块输入 |

## Verifier

| 路径 | 行为 |
| --- | --- |
| 结构 | report self-hash + E10f-1/E10f-0 structural + 完整重算；lifecycle / stamp_tax / **`tranche_evaluation_protocol_binding_ok`** / **`exit_observation_binding_ok`** / ready 全 false |
| 文件 | 真实 E10f-1 file（上游 bindings 全 true）+ 固定路径 E10f-0 file（disk + contract_id）+ **真实读取并调用** tranche evaluation protocol file verifier（schema v2；`protocol_id`/path 与常量、report/lifecycle 一致；`tranche_hold.holding_period/cycle == 40`；`decision_timing.fill_day_is_holding_day_1` 与 `exit_after_holding_period_at_next_tradable_open` 为 true；任何缺失/漂移/20 日替换失败关闭）+ 对 `lifecycle_file.file_bindings.store` **逐行**绑定每条 exit observation（`snapshot_id` 与 report/lifecycle 一致；calendar 日属于 store calendar；unknown/停牌/tradable 同 E10e-0 映射，tradable 比对 **raw `open` / `down_limit`**，禁止 `adj_open`）→ **新构造** 四项 binding 全 true + `ready_for_exit_diagnostic=true` |

报告本体 `ready_for_exit_diagnostic=false`；仅 VerificationResult（file 形）可为 true。状态机只允许结构四项 binding 全 false，或文件四项全 true 且 ready true。结构报告可接受显式 observation label 做纯重算；**只有**逐行 MarketStore 绑定且 tranche 协议磁盘绑定后的 file result 才 ready。合成 fixture 须先把 entry + 未来 exit 日线与 calendar 写入同一 InMemoryStore 并形成真实 snapshot，再生成下游报告——不得修改已封印 store 后仍沿用旧 snapshot id。
