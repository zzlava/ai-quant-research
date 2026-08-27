# 指数影子观察云端连续性 v1

## 一、结论

2026-08-28 起，影子观察采用“一个规范写入器 + 一个独立见证者”的云端结构：

1. GitHub Actions 是唯一规范写入器；
2. Google Cloud Run Job + Cloud Scheduler + Cloud Storage 是拟部署的独立见证者；
3. 两者不得同时修改同一条 Git 哈希链；
4. 见证者只保存同日上交所公开原始字节、采集时间和密封 receipt；
5. GitHub 缺失时，见证 receipt 也不能自动进入规范账本，必须另行人工审查；
6. 任一云端都不访问券商凭证、不连接券商、不下单、不部署资金、不交易。

这项部署只提高影子证据采集的连续性，不改变
`config/research/index-shadow-observation-plan-v1.json` 的研究语义，也不扩大任何真实资金授权。

## 二、GitHub 规范写入器

证据仓库：`zzlava/ai-quant-shadow-evidence`（私有）。

固定源代码提交：
`b8195bd0bbae3548b65b2720d4c082cdef11b05b`。

调度：

- 08:37 UTC，对应 Asia/Shanghai 16:37；
- 10:07 UTC，对应 Asia/Shanghai 18:07；
- 每日唤醒，但只在封印的周五、年末或年初窗口实际尝试；
- 第二次运行只用于降低 GitHub 调度延迟或丢弃风险；同日追加保持幂等。

持久化：

- 初始化证据、原始行情和观察报告位于私有证据仓库；
- 工作流使用当前仓库短期 `GITHUB_TOKEN`，不保存长期市场或券商密钥；
- 所有第三方 Action 固定到完整提交 SHA；
- Python 环境由固定源提交中的 `uv.lock` 重建；
- 只允许快进追加规定的 raw/observation 路径；其他路径变化立即失败；
- 工作流完成后重新验证观察计划、完整哈希链、readiness 和人工输入门。

验证记录：

- GitHub Actions 手动烟雾测试 `33098444060`：通过；
- 更新 Node 24 兼容 Action 后复测 `33098553879`：通过且无告警；
- 两次测试均发生在封印执行时间前，正确执行 no-op，没有制造观察记录。

当前 GitHub 账户级别不支持对私有仓库启用 branch ruleset，所以暂时只能证明工作流按普通快进
追加，不能把 GitHub 单独称为 WORM 存储。该限制必须由独立对象存储见证解决，不能用文字忽略。

## 三、Google Cloud 独立见证者

选择组件：

- Cloud Scheduler：独立定时触发；
- Cloud Run Job：运行固定容器采集两条上交所公开快照；
- Cloud Storage：保存原始字节、SHA-256、实际采集时间和非规范 receipt；
- Object Retention Lock：在用户明确确认保留期和不可逆影响后才允许启用。

见证者必须满足：

- 使用独立 Google Cloud 服务账号，权限仅限执行任务和创建指定前缀对象；
- 不保存 GitHub、Tushare 或券商凭证；
- 对象创建使用“不存在才创建”条件，重试不能覆盖已有字节；
- receipt 固定标记 `canonical=false`、`automatic_ingestion=false`；
- 捕获失败或行情日期不一致时失败关闭；
- 同日未捕获就是永久缺口，不能次日补抓并伪装成当日证据；
- 即使 GitHub 故障且见证捕获成功，也只能进入人工接受流程。

Google Cloud 当前状态为 `pending_user_cloud_project_and_billing_authorization`。尚未创建项目、服务账号、
Cloud Run Job、Scheduler 或 Bucket，也未产生相关云费用。

## 四、切换与停止规则

- 两次 GitHub无写入烟雾测试通过后，原本机采集 heartbeat 已降级为 18:30 只读云端监控；
- 监控器只报告当天 GitHub运行和规范记录缺失，不得重跑、本地补抓或修改证据；
- Google Cloud见证者完成一次与 GitHub 同日双捕获并核对哈希前，不宣称具备跨平台容灾；
- 任何平台都不能自动把 evidence gate 升级为真实资金门；
- 达到 12 次、84 天和跨年边界后仍只通知人工审查。

## 五、仍需用户提供的云端输入

部署独立见证者前需要：

1. Google Cloud project ID；
2. 允许启用 Cloud Run、Cloud Scheduler、Cloud Build/Artifact Registry 与 Cloud Storage；
3. 明确允许该项目产生少量云资源费用；
4. 选择区域；
5. Object Retention Lock 的保留期限和不可逆锁定是否启用。

上述输入只授权建设影子证据见证，不授权任何券商、订单、资金或交易操作。
