# 第一层风险锁恢复反事实验证

本里程碑把已经封印的 `layer-one-risk-lock-recovery-policy-v1` 接到 2005–2021
中证全指历史上，只评价 2013–2021 的既定验证段。它不改变实时风险状态，也不把
模拟确认伪装成用户真实确认。

评价器保留每次锁定前的旧高水位、亏损和红线记录；满足 20 个市场日冷静期、指数
趋势非负、60 日年化波动低于 27%、且为周度首个交易日时，才模拟显式确认并建立
新的风险资本 epoch。首次恢复预算上限为 30%。

当前严格结果：模拟恢复解决了“永久现金”不可达问题，但依旧未通过冻结 hard gates。
2013–2021 合并年化收益约 6.98%，最大回撤约 -24.79%，Calmar 约 0.282；最大回撤
超过 -20% 红线，压力路径同样失败。因此第一层仍不能开放 30% 受控试运行。

```bash
PYTHONPATH=src .venv/bin/python -m app.cli run-layer-one-recovery-counterfactual \
  --repo-root .

PYTHONPATH=src .venv/bin/python -m app.cli verify-layer-one-recovery-counterfactual \
  --repo-root .
```

两条命令均离线，不读取 Token，不使用 2022+ 调参窗口，不消费 OOS，不评分、不下单、
不连接券商。
