# 第二层 Alpha 覆盖分离政策

本政策是**观察任何新因子 IC 或前瞻收益之前**作出的协议修正。它不覆盖或篡改 E11a v1，而是为后续 v2 协议冻结一个单一修正：把“因子是否有效”的统计评价样本与“财务排雷是否足以放行实际候选”的安全门分开。

## 为什么必须分开

封印的输入可行性报告已经证明：在 E11a v1 的分母（候选资格完整且财务五规则裁决完整）下，即使假设四个因子对所有 eligible 名称都已知，2022–2023 也最多只有 75 个有效 h40 决策日，且 2022/2023 分别只有 38/37，无法达到冻结的 120 与每年 40 门槛。

降低 500 只门槛、把财务 unknown 当 clean，或只在“财务恰好有完整披露”的非随机子样本上选择 alpha，都会制造更严重的统计或选择偏差，均被明确禁止。

作为只读可行性对照，候选资格本身在 2022/2023/2024 每个交易日的 eligible 上限最低分别为 2,025 / 2,053 / 1,636 只，全部高于 500；因此分离后可以保留原有统计门槛，而不是为了迁就数据降低标准。

## 冻结修正

- Alpha 因子证据分母：候选资格完整且 `eligible_for_new_entry=true` 的横截面；500 只、60%、120 日、每年 40 日门槛全部不变。
- 财务负面清单：独立安全 overlay；不得决定因子 IC 的样本。unknown 仍不等于 clean，已知硬排除不能被 alpha 抵消，任何后续受控试运行的新建仓仍须遵守失败关闭。
- 统计风险簇 companion：使用与原始因子证据相同的候选 eligible / factor-known 横截面，禁止再按财务 known-only 条件化；仍只是统计风险代理，不是行业分类，也不是第五个假设。
- 窗口不变：2022–2023 开发；2024 只报告、不得选型或改权重；2025-01-01..2026-08-21 已消费且禁止；新的冻结 OOS 自 2026-08-22 开始，本政策不授权评估。

## 当前门闩

该政策本身仍固定 `ready_for_alpha_diagnostic_execution=false`。执行前还必须完成：

1. 新版本 alpha 开发协议和运行合同；
2. 在试验账本登记新的四假设族；
3. 新的严格输入 assembler 与月度风险簇 companion；
4. 独立测试与审查。

政策绝不授权评分、回测、组合、订单或交易。

```bash
PYTHONPATH=src .venv/bin/python -m app.cli \
  freeze-layer-two-alpha-coverage-separation-policy

PYTHONPATH=src .venv/bin/python -m app.cli \
  verify-layer-two-alpha-coverage-separation-policy \
  --policy-file config/research/layer-two-alpha-coverage-separation-policy-v1.json
```
