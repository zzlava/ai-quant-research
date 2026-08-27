# 影子执行观察与受控真实资金输入门 v1

## 一、当前结论

影子账本已经具备可重复验证的初始化、官方收盘后行情采集、追加式哈希链和执行诊断。
它仍然处于 **观察阶段**：不访问券商凭证、不连接券商、不提交订单、不部署资金，也不把
收盘后盘口解释成可成交价格或策略收益。

观察计划：
`config/research/index-shadow-observation-plan-v1.json`

观察计划 ID：
`606ec8e16aaadf14f7d9fa1647023ba4a4bea5cb09d17bf833ce7f2523563d33`

受控真实资金输入门：
`config/research/index-controlled-live-input-gate-v1.json`

输入门 ID：
`7a3a35cb47dea3401c7cfc0af05fe0b34dc23b3b279d8ab3bb67fd1ec23df9e2`

> ⚠️ 当前所有真实资金、券商连接、组合构造、下单和交易 readiness 均为 false。观察满足
> 最低数量后也只能进入人工审查，绝不自动升级。

## 二、固定观察方法

- 每周五 16:30（Asia/Shanghai）采集上交所公开的收盘后快照；
- 若周五不是市场日，因行情日期不匹配而失败关闭，跳过且不补填；
- 观察文件按市场日期严格递增，每条记录引用前一条记录哈希；
- 原始行情字节与 SHA-256 一并保存，验证时逐条重算；
- 同一日期重复运行只返回已验证记录，不覆盖、不重封印；
- 至少 12 条观察且跨度至少 84 个自然日，才允许进行一次执行层人工复核；
- 另需记录自然年最后市场日和下一自然年第一市场日，用于检查年度恢复 30/70 的操作语义。

默认本地路径：

- 原始快照：`data/raw/index-shadow-observations-v1/YYYY-MM-DD/`；
- 追加报告：`data/shadow/index-risk-budget-shadow-v1/observations/YYYY-MM-DD.json`。

两者均在 Git 忽略的 `data/` 下；可审计规则、代码和测试进入版本控制。

云端连续性、GitHub规范写入器与独立见证者的边界见
`docs/research/index-shadow-cloud-continuity-v1.md`。云端部署不改变本计划的证据门，也不授权
任何券商连接、订单、资金或交易。

## 三、每条观察诊断什么

对 `510300.SH` 与 `511010.SH` 分别记录：

1. 最优买价、最优卖价和价差（bp）；
2. 最优一档可见数量及其对影子数量的覆盖比例；
3. 固定 5bp 单边滑点假设能否覆盖半个报价价差；
4. 按最优买价假设清算时的佣金，以及 5 元最低佣金是否生效；
5. 100 份整手造成的 30/70 配置偏差；
6. 仅按收盘价标记的虚拟权重漂移。

一档数量只说明某一收盘后时点的可见深度，不能证明真实下单时会成交。所有腿固定记录：

- `hypothetical_order_status=not_submitted`；
- 订单生命周期的请求、提交、成交和撤单数量全部为 0，券商订单号与时间戳为 `null`；
- `after_close_quote_is_executable=false`；
- `actual_fill_claim=false`。

观察不能用来计算 alpha、判断策略有效性或追涨换产品。产品对仍然冻结，不允许 ETF 轮动。

## 四、影子产品复核

### 510300.SH

- 上交所材料确认其为华泰柏瑞沪深 300 ETF，跟踪沪深 300 指数；
- 产品资料概要披露管理费 0.15%、托管费 0.05%；
- 2025 年四季报披露期末基金资产净值约 4,222.58 亿元；
- 上交所有主做市服务公告；
- 风险仍包括二级市场折溢价、跟踪偏离、指数波动和极端流动性变化。

### 511010.SH

- 上交所材料确认其为国泰上证 5 年期国债 ETF，跟踪上证 5 年期国债指数；
- 产品资料概要披露管理费 0.15%、托管费 0.05%；
- 2026 年一季报披露期末基金资产净值约 38.13 亿元；
- 上交所有主做市服务公告；
- 它采用优化抽样复制，存在跟踪误差、利率风险、折溢价和流动性风险，不等于现金。

上述材料支持两只产品作为 **影子执行替代** 继续观察，但不支持以下推断：

- 它们与历史研究代理 `H00985.CSI`、`H11010.CSI` 完全相同；
- 历史指数收益可以直接继承给 ETF；
- 当前券商账户一定可以买卖；
- 真实盘口、成本或成交会等于影子假设；
- 两只产品已经构成投资推荐或真实资金组合。

所以最终真实产品映射仍为
`pending_manual_broker_eligibility_and_user_decision`。

## 五、受控升级前缺失的人工输入

输入门故意把下列字段保持为 `null`，系统不得推断或代填：

- 券商法定名称；
- 券商费率页面、合同或用户截图的本地证据及 SHA-256；
- ETF 实际双边佣金率、每笔最低佣金；
- 交易所及监管费用是否已包含在佣金中；
- 当前账户对 `510300.SH`、`511010.SH` 的买卖资格；
- 精确受控资金金额；
- 精确计划执行日期；
- 精确授权产品；
- 用户醒目的真实资金升级确认原文。

提供费率与资格材料，只授权只读核对，不自动授权访问券商凭证或连接券商。即便输入齐全，
仍必须另建新版本的受控执行合同，重新计算整手、现金、成本与产品映射，并再次人工确认。

## 六、命令

```bash
cd /Users/janlei/Desktop/quant/ai-quant-research

PYTHONPATH=src .venv/bin/python -m app.cli verify-index-shadow-observation-plan

# 只允许在相应市场日收盘后运行；日期必须与上交所返回日期一致。
PYTHONPATH=src .venv/bin/python -m app.cli collect-index-shadow-observation \
  --expected-date YYYY-MM-DD \
  --record-reason weekly

PYTHONPATH=src .venv/bin/python -m app.cli verify-index-shadow-observation-chain
PYTHONPATH=src .venv/bin/python -m app.cli review-index-shadow-observation-readiness
PYTHONPATH=src .venv/bin/python -m app.cli verify-index-controlled-live-input-gate

# 年末在 12 月 31 日收盘后运行；若当天休市，命令读取官方快照中的最后市场日。
PYTHONPATH=src .venv/bin/python -m app.cli collect-index-shadow-year-boundary \
  --calendar-year YYYY \
  --record-reason year_end

# 年初 1 月 1 日至 10 日每日尝试，第一市场日成功后后续调用幂等返回同一记录。
PYTHONPATH=src .venv/bin/python -m app.cli collect-index-shadow-year-boundary \
  --calendar-year YYYY \
  --record-reason year_start
```

## 七、醒目的人工门

在观察证据达到最低要求、券商输入完成且新版本受控执行合同独立审查通过前，禁止请求真实
下单。将来仍必须由用户针对 **指定日期、指定产品、指定金额** 提供如下醒目确认：

> ⚠️ 我确认将影子执行升级为受控真实资金试运行，并理解这不是收益保证；本次仅授权指定
> 日期、产品和金额，不授权自动交易。

这句话目前只是一份模板，`confirmation_present=false`；本里程碑没有取得该授权。
