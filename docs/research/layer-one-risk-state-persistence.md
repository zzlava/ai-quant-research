# 第一层风险状态持久化（E9b-1 后端）

研究 / 实现地基 only。**不可交易**。本里程碑交付 layer-one 风险锁与人工上限的 SQLite 持久化、只读/变更 API 与严格单测；**不**接评分、股票池、回测、订单或券商；**不**声称 layer-one 可交易或可下单。

## 入口

| 项 | 路径 |
| --- | --- |
| ORM | `src/app/persistence/layer_one_models.py`（`create_all` 新表） |
| 领域存储 | `src/app/research/layer_one_persistence.py` → `LayerOneRiskStateStore` |
| API | `src/app/api/layer_one.py` |
| 测试 | `tests/test_layer_one_persistence.py` |
| 纯状态机（E9a） | `src/app/research/layer_one_regime.py`（封印决策 verifier 仍权威） |

## 流与失败关闭

- 单一命名流：`layer-one-primary`；每次读/写前跑 **全链完整性校验**（失败关闭，不自动修复）
- **缺失状态** → `initialized=false`，effective budget `0`，`risk_lock_active=null`
- 初始化幂等：精确重放始终返回 **sequence-1** 原 audit / 原 authorization / 原 `init_state_id`（即使后续已演进）
- 人工上限：显式非空 `request_id` + 与 prior ceiling **无关** 的 `request_digest`；精确重放返回原回执且不改 stage/current；同 id 发散 → 409
- Unlock：先按 `request_id` 身份匹配（消费/解锁后仍可精确重放）；发散 → 409；新请求且无活跃锁 → 拒绝
- 决策：精确重放返回该决策原 `new_state_id` / 原 revision，而非当前状态

## CAS / 时序

- 流上 `revision` 每次审计事件 +1（含 evidence / 授权 / unlock / decision）
- `GET /risk-state` 暴露 `revision` 与 `last_audit_id`
- `POST /risk-state/decisions` 体为 `{expected_last_audit_id, expected_revision, report}`
- 提交时原子比较 envelope + `report.prior_state_id`；SQL conditional update，0 行 / `IntegrityError` → 409
- `target_trading_day` 必须严格晚于上一已接受决策；突变时间戳必须 `>= current.updated_at`
- 同内容 `state_id` 的乱序决策因此被拒绝

## 完整性校验

读写前全链校验失败关闭、不自动修复：

- 每个 auth / evidence / unlock 行：ORM 不可变列必须等于密封 `payload_json`；`authorization_id` / `evidence_id` / unlock evidence 从密封字段重算
- 审计事件与专用行 **1:1**（无孤儿、无悬空引用）；0.3/0.6 授权引用的 evidence 必须仍存在且类型/覆盖与原密封规则一致
- 当前 ceiling / stage_started / data_snapshot / updated_at / last_decision* / state_json 必须与最新授权或最新决策 / init 一致
- unlock `consumed` 必须与决策审计引用一致

## 部署证据注册

- `POST /layer-one/deployment-evidence`
- 类型：`historical_validation_pass` / `no_severe_anomaly_period`；密封体含 `resulting_state_id` / `resulting_revision`；`request_digest` 用于精确重放
- 授权必须按 ID 加载并校验封印/类型/contract/`data_snapshot_id`
- `0→0.3`：须 `historical_validation_pass`
- `0.3→0.6`：须 `no_severe_anomaly_period` 且覆盖整个当前 0.3 阶段至 `authorized_at`
- `0.6→0.9`：仍从已校验决策审计史证明阶段内无 risk-lock **触发**
- 随机/未知 evidence id → 失败关闭

## API

| 方法 | 路径 |
| --- | --- |
| GET | `/layer-one/risk-state` |
| POST | `/layer-one/risk-state/initialize` |
| POST | `/layer-one/deployment-evidence` |
| POST | `/layer-one/manual-ceiling-authorizations` |
| POST | `/layer-one/unlock-requests` |
| POST | `/layer-one/risk-state/decisions` |
| GET | `/layer-one/audit`（`page_size`≤100） |

变更响应固定 research-only / 不可交易旗标。错误消毒为 400/404/409。

## 明确非目标

- 前端 UI；评分 / IC / phase / backtest / 组合 / 下单 / 券商
- 修改冻结 research JSON；声称 scoring/trading readiness
