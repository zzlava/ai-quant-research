# 两层策略实施门（E6a）

研究用途 / **非交易就绪**。本文记录 E6a 只读接口与未决参数合同；不构成策略配置、回测授权或实盘许可。

## 已完成（E1–E5 评价机器与执行修复）

| 里程碑 | 产物 | 作用 |
| --- | --- | --- |
| E1 | IC / 分位组合诊断（`analyze-ic`） | 截面 Spearman IC、HAC、分位 spread；固定 diagnostic-only |
| E2 / 治理 | research trial ledger | 试验下界、已消费 OOS 终态、禁止把 raw Sharpe 当可比较证据 |
| E3a | 仓位漏斗与资金利用诊断 | 信号漏斗、整手/现金拒单、预算恒等；不改策略参数 |
| E3b | 固定换仓全相位敏感性 | 全 phase 诊断；禁止按收益选相位 |
| E4 | 持仓期执行语义 | 停牌强制持有、跌停顺延、缺 bar 失败关闭；退市不猜测结算 |
| E5 | 实验台账与多重检验边界 | Deflated Sharpe 缺绑定则 `not_evaluable` |

上述机器可复现评价与执行失败关闭，但**不能**替用户选择两层策略的经济参数。

## E6a 本晚交付（不依赖经济参数选择）

1. **未决参数合同** `config/research/two-layer-strategy-decision-draft-v1.json`
   - `status=blocked_pending_user_decisions`
   - 需用户判断的字段全部为 `null`（unknown 列表不用 `[]` 伪装）
   - `initial_cash=80000` 已确认，**不是** blocker
   - 已消费 OOS 禁止重用；绑定当前 research trial `ledger_id`
   - 自哈希 `contract_id`；`ready_for_scoring/backtest/trading=false`、`auto_deploy=false`
2. **校验器** `src/app/research/two_layer_contract.py` + CLI `verify-two-layer-decision-contract`
   - hash / extra / 越界 / min>max / 预算档位 / status·ready 矛盾 → 失败
   - v1：任一必填决定为 null → `resolved=false` + 稳定排序 blockers
   - v2：`user_decisions_resolved` 可与总体 `resolved` 分离；证据门非空或 not-ready → 总体 `resolved=false`
3. **纯组合接口** `src/app/research/two_layer_allocation.py`
   - 显式传入 Layer1 预算与 Layer2 个股权重后做数学合成
   - 缺证据、日期不一致、预算不守恒、重复 symbol、与防守资产冲突 → fail-closed
   - 输出固定 `diagnostic_only=true`、`ready_for_orders=false`、`ready_for_trading=false`
   - **不**接入 BacktestEngine / ScoringEngine / StrategyConfig，**不**下单

## E6c 本晚交付（指数风险特征 + 开发协议地基）

1. **只读指数风险特征** `src/app/research/index_risk_features.py`
   - 显式 lookback；`<= as_of`；缺/重/非正收盘失败关闭
   - 仅连续描述量（SMA、波动、回撤等）；无 regime / 风险预算
   - 固定 diagnostic gate；不接入评分/回测/交易
2. **第一层长历史开发协议** `config/research/layer-one-index-development-protocol-draft-v1.json`
   - E6c：schema v1 未决草稿地基（封印兼容仍可校验）
   - E8b：升级为 schema v2 `confirmed_for_implementation_but_not_ready`（用户决策已确认；事实/实现/开发证据/未来 OOS blocker → 总体 `resolved=false`）
   - 磁盘绑定当前 research trial `ledger_id` **与** 本文件对应的 two-layer schema v2 `contract_id`
   - 拒开发/验证窗重叠；历史验证段禁止称 OOS；拒绑定/复用已消费 OOS；拒 flat 印花税冒充完成
   - CLI：`verify-layer-one-index-protocol`（必须显式 `--protocol-file` / `--repo-root`）

详见 [`index-risk-features-and-layer-one-protocol.md`](./index-risk-features-and-layer-one-protocol.md)。

## 关闭门（确认前禁止）

在用户完成明日决策清单并更新合同之前：

- **禁止**生成新的可运行策略 YAML / 配置；
- **禁止**把均线、波动率目标、股票预算档位、tranche 数、行业上限等偷偷写成默认值；
- **禁止**真实开发回测、评分、IC/phase 选型，以及任何**新** OOS；
- **禁止**复用已消费 OOS 窗口调参；
- **禁止**连接券商或自动部署。

只读校验示例：

```bash
.venv/bin/python -m app.cli verify-two-layer-decision-contract \
  --draft-file ./config/research/two-layer-strategy-decision-draft-v1.json

.venv/bin/python -m app.cli verify-layer-one-index-protocol \
  --protocol-file ./config/research/layer-one-index-development-protocol-draft-v1.json \
  --repo-root .
```

详见 [`tomorrow-user-decisions.md`](./tomorrow-user-decisions.md)。
