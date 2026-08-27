# 滚动分仓日程诊断与评价协议（E7a / E8c）

研究用途 / **非收益证据** / **非交易就绪**。E7a 交付只读日度滚动 tranche 日程诊断与密封评价协议地基；E8c 将路径稳定的评价协议 JSON **内部升级为 schema v2**（`confirmed_for_implementation_but_not_ready`），写入用户已确认的资本 / 持有 / 相位 / 窗口 / 成本 / 评价机决策，并磁盘绑定上游 two-layer 合同、layer-one 协议与 research trial ledger。

本里程碑**不是** tranching 能提高收益的证据，**未**接入 BacktestEngine、评分、股票池或组合构建，**未**对真实行情跑预检、评分、IC、相位、回测或任何新 OOS。

## 日程诊断（纯函数 / 失败关闭）

入口：`src/app/research/rolling_tranche_schedule.py` → `diagnose_rolling_tranche_schedule`

显式参数（**禁止默认值**）：

| 参数 | 含义 |
| --- | --- |
| `market_calendar` | 严格递增、无重复的 `datetime.date` 交易日序列；必须恰好等于闭区间 `[start, end]` |
| `start` / `end` | 决策窗边界；必须出现在日历中且 `start <= end` |
| `tranche_count`（N） | 分仓数（**诊断几何参数**；勿与评价协议 v2 的活动仓位/持有周期混淆） |
| `holding_period_bars`（H） | 每笔决策的持有交易日数 |
| `initial_capital` | 账户初始资金（诊断用）；每仓理论资金 = `initial_capital / N` |

窗内**每个交易日恰好一次**决策分配；按顺序 round-robin 赋给 `tranche_id = 0..N-1`。同一 tranche 在其上一笔 H 根 K 线持有结束前不得再分配；若 `H > N` 或再分配会重叠占用同一笔资金，**失败关闭**（拒绝隐含杠杆）。

### 持有区间约定（精确）

- 决策日位于绝对日历下标 `i` 时，占用连续 H 根市场交易日：`calendar[i] .. calendar[i+H-1]`（**含决策日**，长度恰为 H）。
- 该 tranche 在 `calendar[i+H]` **起**可再次决策（持有结束后的首个交易日；允许同日释放再分配，不算重叠）。
- 在「每日一决策 + round-robin」下，同一 tranche 的下一次分配间隔为 N 个交易日；因此 **`H > N` 必然重叠**，一律拒绝。

### 报告内容

- 输入全量回放：日历、窗、`N`、`H`、`initial_capital`、每仓资金
- `schedule_rows`：每次决策的 tranche、持有起止、是否完整落在日历上、是否越过窗尾
- 每日 `active_tranche_count`、理论已分配/现金比例、warm-up / tail 标记
- 总决策数、每 tranche 决策计数、相位覆盖（`phase_coverage`）
- 自哈希 `report_id`；固定 `diagnostic_only=true`，`ready_for_scoring/backtest/trading=false`，`auto_apply=false`
- **无**选股、价格、订单、收益、PnL、regime、就绪或绩效字段

### N = H 的含义（说明，非推荐）

当 `N = H`（例如同为 20）时：每日开一笔新仓、持有 N 日后同序号 tranche 才释放，稳态下理论满仓；但窗前部存在 **warm-up 现金**（活跃仓数从 1 增至 N），窗尾部存在 **tail**：较晚开仓的持有名义上越过 `end`，窗内只能看到截断占用。这只是日程几何，**不**证明该参数更优。

### 小资金与 100 股整手

`initial_cash=80000` 已确认。评价协议 v2 下活动仓位按股票预算 0/30/60/90% → 0/3/6/9（绝对最多 9；一股一活动 tranche），每股目标名义至少 8000；买不起 100 股整手 / 最低佣金扭曲 / 候选不足 / unknown → **留现金**，不放宽、不回填、不复用别日名单。只读账户/执行结构归因见 [`account-execution-diagnostics.md`](./account-execution-diagnostics.md)（E7b）。

## 评价协议 schema v2（E8c：已确认 / not-ready）

机器可读：[`config/research/tranche-evaluation-protocol-draft-v1.json`](../../config/research/tranche-evaluation-protocol-draft-v1.json)（路径稳定；**内部** `schema_version=2`）

校验器：`src/app/research/tranche_evaluation_protocol.py`

- `status=confirmed_for_implementation_but_not_ready`
- `pending_user_decisions=[]` / `user_decisions_resolved=true`
- 只要事实 / 实现 / 开发证据 / 未来 OOS 观察 blocker 存在，总体 `resolved=false`
- `research_only=true`；`ready_for_scoring/backtest/trading=false`；`auto_apply=false`
- **严格** `extra=forbid`；**拒绝** schema v2 顶层 `tranche_count`（40 是持有/相位周期，不是活动分仓数）
- schema v1 封印草稿仍可校验（见 `tests/fixtures/research/tranche-evaluation-protocol-draft-v1-sealed.json`）

### 磁盘绑定（file verifier **读盘**核对，不是口头声明）

| 上游 | 路径 | 绑定 id |
| --- | --- | --- |
| research trial ledger | `config/research/research-trial-ledger-v1.json` | `ledger_id` |
| two-layer 合同 | `config/research/two-layer-strategy-decision-draft-v1.json` | schema v2 `contract_id` |
| layer-one 协议 | `config/research/layer-one-index-development-protocol-draft-v1.json` | schema v2 `protocol_id` |

漂移（常量与磁盘 id 不一致、伪造绑定、consumed OOS 复用）→ 失败。

### 已确认选择（摘要）

| 项 | 确认值 |
| --- | --- |
| 初始现金 | 80000（**不是** blocker） |
| 股票预算 → 活动仓位/tranche | 0/30/60/90% → 0/3/6/9；绝对最多 9；一股一活动 tranche |
| 持有 / 相位周期 | 40 市场交易日（**不是** `tranche_count`） |
| 相位 | 各档活动 phase 在 40 日周期确定性尽量均匀；报告全 phase + combined tranche path；禁止按收益选 phase；不补跑；风险降仓不受相位限制 |
| 决策/成交 | T 收盘决策；T+1 开盘首次尝试；成交日 = day1；满 40 市场交易日后下一可交易开盘退出；停牌日计时；停牌/涨跌停顺延且无事后成交 |
| 预算调整 | 第一层风险优先；减仓可日度；增仓仅每周首交易日，用前日已知状态 |
| 部署升级 | 仅人工逐档：30% 历史门后；30%≥3 个月无严重异常 → 人工 60%；60%≥3 个月无风险锁 → 人工 90%；永不自动升级；12 月 OOS 继续但非人工升级硬门 |
| ownership proxy | 仅诊断 |
| Layer-2 开发窗 | 2022-01-01..2023-12-31 |
| seen robustness | 2024（已见；仅稳健性报告） |
| 已消费 OOS | 2025-01-01..2026-08-21（复用必须失败） |
| 新冻结 OOS | 自 2026-08-22 起 |
| 基准 | 中证全指全收益（CSI All Share Total Return）；exact symbol = 证据 blocker，禁止猜 |
| 成本 | 佣金 0.00025/边、最低 5；滑点 5bps、压力 15bps；官方历史卖出印花税日程仍为 blocker（禁止 1900 起固定 0.1%） |

### 评价机（协议文本冻结；E8c **不执行**）

- 因子证据以后先：全截面分位数组合 + HAC 重叠修正 IC/ICIR/t
- 10 名/tranche 组合 = 执行检查，不是因子证据
- 全 phase 与 combined tranche path；禁止按收益选 phase/参数
- 现金占用归因至少：候选不足 / 门控 / 买不起整手或最低佣金 / 停牌 / 涨跌停 / 风险预算
- trial family 经 research trial ledger 登记多重试验
- 引用上游 layer-one hard risk gates，不复制漂移；**不造** alpha 收益门槛

### Evidence blockers（至少）

- `pending_factual_source_verification`：exact benchmark symbol；历史印花税表
- `pending_implementation`：tranche evaluation engine/runner；execution cash attribution
- `pending_development_evidence`：alpha 权重 / 分位·ICIR 开发证据
- `future_oos_observation`：新冻结 OOS（与已消费窗分离）
- 另有 ownership / event / PIT 等 `future_enhancement`

结构校验不读磁盘 → binding / consumed-OOS 检查字段为 `false`；文件校验读盘核对后才标 `true`。CLI 区分 structural / user decisions / overall resolved / disk binding / blockers。

## 只读 CLI

必须显式给出本地文件；不静默打开行情、不跑回测：

```bash
.venv/bin/python -m app.cli verify-tranche-evaluation-protocol \
  --protocol-file ./config/research/tranche-evaluation-protocol-draft-v1.json \
  --repo-root .

.venv/bin/python -m app.cli verify-rolling-tranche-schedule-report \
  --report-file /path/to/local-sealed-schedule-report.json
```

## 关闭门（当前仍禁止）

- **禁止**把 40 写成活动 `tranche_count`，或把 3/6/9 映射偷偷改写成别的默认；
- **禁止**猜测基准 symbol，或用 flat 印花税表冒充完成；
- **禁止**复用已消费 OOS，或在新冻结 OOS 满期前声称完整 OOS 通过；
- **禁止**按收益选 phase；禁止同日补跑；禁止放宽整手/最低佣金/候选门；
- **禁止**在证据 blocker 未清时声称 overall resolved / ready；
- **禁止**真实评分 / IC / phase / 回测 / 交易 / 自动升级。
