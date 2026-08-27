# 第二层 Alpha v2 完整输入绑定

该里程碑把冻结的 v2 诊断契约与六项已核验输入绑定为一个内容寻址收据：

- 输入包 ID：`c8363dabde718b0d93f2ba4f33d1c75ab2861834ce11611c653e2f0290157bfb`
- 开发窗口：`2022-01-01..2023-12-31`
- 已见稳健性窗口：`2024-01-01..2024-12-31`，只能报告，不能选因子或改变权重
- 已消费 OOS：`2025-01-01..2026-08-21`，禁止再次使用
- 新冻结 OOS 起点：`2026-08-22`，未获单次评估授权

六项输入分别是市场快照、候选资格、财务负面清单、PIT 财务质量数据、PIT 日估值和统计风险聚类。Alpha 证据分母只使用“候选资格完整且允许新建仓”的股票，再按各因子是否已知做配对；财务负面清单继续作为独立、失败关闭的安全层，未知不得视为干净，也不得被 Alpha 结果覆盖。

输入包只允许执行冻结的离线 Alpha 诊断。它不允许评分、回测、组合构造、下单或交易，也不会自动把研究结论接入策略。

校验命令：

```bash
PYTHONPATH=src .venv/bin/python -m app.cli verify-layer-two-alpha-input-bundle-v2 \
  --bundle-file data/all-a-share-historical-v1/research/layer-two-alpha-input-bundle-v2.json
```
