# 第一层 risk lock 恢复语义封印

严格历史重放发现原恢复条件不可达：账户在 -18% 触发 risk lock 后只持现金，账户权益不再随市场修复，因此相对旧高水位的回撤一直停留在锁阈值；“当前观测仍触发锁”又优先于解锁，最终形成永久锁。

本 overlay 不改写过去亏损，也不自动解锁。它冻结以下恢复语义：

1. 至少冷静 20 个市场交易日；
2. 指数趋势不得为负，60 日年化波动必须严格低于 27%；
3. 只允许每周首个市场交易日恢复；
4. 用户必须在醒目提示中显式确认，并确认建立新的风险资本 epoch；
5. 新 epoch 的高水位等于确认时当前权益；旧高水位、旧回撤、risk-lock 触发和红线记录永久保留在审计链；
6. 首次重新入场预算最多 30%；
7. 无活跃锁时禁止重置；服务重启、程序或历史评价均不得自动清锁。

历史验证可额外输出一个反事实敏感性路径：在首次满足全部条件的周度动作日模拟一次人工确认。但它不是已发生的用户动作，不得用于 OOS 声明，也不得自动应用到真实状态。

机器合同：`config/research/layer-one-risk-lock-recovery-policy-v1.json`。

```bash
PYTHONPATH=src .venv/bin/python -m app.cli verify-layer-one-risk-lock-recovery-policy \
  --repo-root .
```

该政策在状态机与持久化审计完成前仍不允许 30% 受控试运行；所有 scoring / backtest / orders / trading readiness 继续为 false。
