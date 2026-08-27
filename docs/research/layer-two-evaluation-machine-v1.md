# 第二层评价机器 v1

本里程碑只使用 2022–2023 开发期，修复“少数持仓组合无法区分 alpha 与噪声”的评价
问题。2024 不参与选择，2025+ 完全不读取。

## 四臂随机对照

- R0：通过市场范围、上市历史、ST/停牌、流动性与规模约束后的候选中随机等权 50 只；
- R1：在 R0 基础上只保留财务排雷 `clean/halved`，再随机等权 50 只；
- T：与 R1 相同的安全宇宙，按冻结 v2 选择的低波因子取前 50；
- B：中证全指全收益 `H00985.CSI`。

固定种子 `20260827`、512 次蒙特卡洛、每 20 个市场日一个锚点、40 日前瞻标签。
这是无成本截面研究，不是 8 万账户可执行回测。

开发期描述结果：R0 平均 40 日收益约 -1.41%，R1 约 -0.67%，排雷增量均值约
+0.75 个百分点，在 512 条随机路径中约 93.95% 为正。T 约 -0.57%，只位于 R1
随机分布约 64.45% 分位，不能证明因子倾斜优于随机安全组合。T 相对同期全收益基准
的描述差约 +1.17 个百分点，但未计成本、样本锚点仅 23 个，禁止作确认性结论。

## 左尾分类

标签为未来 120 个市场日内调整收盘价相对当前下跌至少 40%，或期间出现 ST/退市
失败。五条财务规则按分类器输出 TP/FP/TN/FN、precision、recall、specificity；
未知规则和未知标签保持 unknown。业绩预告的强制披露触发总体尚未进入密封输入，
因此披露选择分层明确为 `not_evaluable`，不能把“未披露”当成零信号。

## IC 衰减

四个冻结因子统一报告 5/10/20/40/60 日 IC。质量因子在 20 日前后反转，40 日门槛
失败；中期动量从 5 日起为负；价值与低波的开发期 IC 未在 40 日前衰减。但统计功效
闸门仍把整个确认性家族判为 `not_evaluable`，这些曲线不能自动改变选择或权重。

```bash
PYTHONPATH=src .venv/bin/python -m app.cli run-layer-two-evaluation-machine --repo-root .
PYTHONPATH=src .venv/bin/python -m app.cli verify-layer-two-evaluation-machine --repo-root .
```

输出固定 `ready_for_scoring/backtest/portfolio_construction/trading=false`。
