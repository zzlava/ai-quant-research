import { FormEvent, useMemo, useState } from "react";

import {
  authorizeLayerOneManualCeiling,
  getLayerOneAudit,
  getLayerOneRiskState,
  initializeLayerOneRiskState,
  registerLayerOneDeploymentEvidence,
  submitLayerOneUnlockRequest,
} from "../api/client";
import type {
  LayerOneAuditPage,
  LayerOneBudgetLevel,
  LayerOneEvidenceType,
  LayerOneMutationReceipt,
  LayerOneRiskStateView,
} from "../api/types";
import {
  BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
  BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
  CONFIRM_DEPLOYMENT_EVIDENCE,
  CONFIRM_INITIALIZE,
  CONFIRM_MANUAL_CEILING,
  CONFIRM_UNLOCK_REQUEST,
  abbreviateId,
  nowIso,
} from "../lib/layerOneConstants";
import { errorMessage } from "../lib/format";

type Props = {
  riskState: LayerOneRiskStateView | null;
  riskError: string | null;
  riskLoading: boolean;
  onRiskStateRefreshed: (state: LayerOneRiskStateView) => void;
  onRiskStateRefreshFailed: (detail: string) => void;
  onMutationSuccess: (receipt: LayerOneMutationReceipt) => void;
  onMutationError: (detail: string) => void;
  mutationError: string | null;
  mutationBusy: boolean;
  setMutationBusy: (busy: boolean) => void;
};

function nonblank(value: string): boolean {
  return value.trim().length > 0;
}

export function LayerOneRiskPanel({
  riskState,
  riskError,
  riskLoading,
  onRiskStateRefreshed,
  onRiskStateRefreshFailed,
  onMutationSuccess,
  onMutationError,
  mutationError,
  mutationBusy,
  setMutationBusy,
}: Props) {
  const initialized = riskState?.initialized === true;
  const lockActive = riskState?.risk_lock_active === true;
  const mutationsAllowed = !riskError && !riskLoading && riskState !== null;
  const showInit = mutationsAllowed && !initialized;
  const showCeiling = mutationsAllowed && initialized;
  const showUnlock = mutationsAllowed && initialized && lockActive;

  async function runMutation(action: () => Promise<LayerOneMutationReceipt>) {
    setMutationBusy(true);
    try {
      const receipt = await action();
      onMutationSuccess(receipt);
      try {
        const refreshed = await getLayerOneRiskState();
        onRiskStateRefreshed(refreshed);
      } catch (refreshError: unknown) {
        onRiskStateRefreshFailed(errorMessage(refreshError));
      }
    } catch (error: unknown) {
      onMutationError(errorMessage(error));
    } finally {
      setMutationBusy(false);
    }
  }

  return (
    <section className="panel layer-one-panel" aria-labelledby="layer-one-risk-heading">
      <h2 id="layer-one-risk-heading">第一层风险控制（研究/人工操作）</h2>
      <p className="muted">
        与排名/回测隔离。本面板只写风险状态审计流，不连接评分、回测、订单或券商。任何成功回执仍保持
        ready_for_trading=false / ready_for_orders=false。
      </p>
      {mutationError ? (
        <div className="error" role="alert">
          <strong>
            {riskError
              ? "变更后无法重新核验风险状态（当前状态已作废）"
              : "变更失败（状态未假定已更新）"}
          </strong>
          <pre>{mutationError}</pre>
        </div>
      ) : null}

      {!mutationsAllowed && !riskLoading ? (
        <p className="muted" role="note">
          风险状态未核验、加载中或缺失时，所有变更表单（含部署证据）已禁用。审计仍可只读加载。
        </p>
      ) : null}

      {showInit ? <InitializeForm busy={mutationBusy} runMutation={runMutation} /> : null}
      {showCeiling ? <ManualCeilingForm busy={mutationBusy} runMutation={runMutation} /> : null}
      {showUnlock ? <UnlockForm busy={mutationBusy} runMutation={runMutation} /> : null}

      {mutationsAllowed ? (
        <details className="layer-one-advanced">
          <summary>高级：部署证据登记（人工证明，非自动核验）</summary>
          <DeploymentEvidenceForm busy={mutationBusy} runMutation={runMutation} />
        </details>
      ) : null}

      <AuditViewer />
    </section>
  );
}

function ConfirmationField({
  id,
  requiredPhrase,
  value,
  onChange,
}: {
  id: string;
  requiredPhrase: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="grow confirm-phrase" htmlFor={id}>
      精确确认短语（须完全一致）
      <input
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
        aria-required="true"
        placeholder={requiredPhrase}
      />
      <span className="confirm-hint">
        须原样输入：<code>{requiredPhrase}</code>
      </span>
    </label>
  );
}

function InitializeForm({
  busy,
  runMutation,
}: {
  busy: boolean;
  runMutation: (action: () => Promise<LayerOneMutationReceipt>) => Promise<void>;
}) {
  const [operator, setOperator] = useState("");
  const [reason, setReason] = useState("");
  const [dataSnapshotId, setDataSnapshotId] = useState("");
  const [phrase, setPhrase] = useState("");
  const ready =
    nonblank(operator) && nonblank(reason) && nonblank(dataSnapshotId) && phrase === CONFIRM_INITIALIZE;

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    void runMutation(() =>
      initializeLayerOneRiskState({
        operator: operator.trim(),
        reason: reason.trim(),
        initialized_at: nowIso(),
        user_confirmed: true,
        two_layer_decision_contract_id: BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id: BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id: dataSnapshotId.trim(),
        contract_schema_version: "1",
        engine_version: "layer-one-regime-engine-v1",
      }),
    );
  }

  return (
    <form className="layer-one-form" onSubmit={onSubmit} aria-label="初始化第一层风险状态">
      <h3>显式初始化</h3>
      <p>
        创建<strong>显式解锁且预算为 0%</strong>的实现状态。不授权交易，不是自动初始化。绑定合约 id：
      </p>
      <ul className="bound-ids">
        <li>
          two-layer <code>{BOUND_TWO_LAYER_DECISION_CONTRACT_ID}</code>
        </li>
        <li>
          protocol <code>{BOUND_LAYER_ONE_INDEX_PROTOCOL_ID}</code>
        </li>
      </ul>
      <div className="row">
        <label>
          操作者
          <input value={operator} onChange={(e) => setOperator(e.target.value)} required />
        </label>
        <label className="grow">
          原因
          <input value={reason} onChange={(e) => setReason(e.target.value)} required />
        </label>
        <label className="grow">
          data_snapshot_id
          <input value={dataSnapshotId} onChange={(e) => setDataSnapshotId(e.target.value)} required />
        </label>
      </div>
      <div className="row">
        <ConfirmationField
          id="confirm-initialize"
          requiredPhrase={CONFIRM_INITIALIZE}
          value={phrase}
          onChange={setPhrase}
        />
        <button type="submit" disabled={!ready || busy}>
          {busy ? "提交中…" : "初始化为 0%（不代表可交易）"}
        </button>
      </div>
    </form>
  );
}

function ManualCeilingForm({
  busy,
  runMutation,
}: {
  busy: boolean;
  runMutation: (action: () => Promise<LayerOneMutationReceipt>) => Promise<void>;
}) {
  const [requestId, setRequestId] = useState("");
  const [ceiling, setCeiling] = useState<LayerOneBudgetLevel>(0);
  const [operator, setOperator] = useState("");
  const [reason, setReason] = useState("");
  const [dataSnapshotId, setDataSnapshotId] = useState("");
  const [histEvidenceId, setHistEvidenceId] = useState("");
  const [anomalyEvidenceId, setAnomalyEvidenceId] = useState("");
  const [phrase, setPhrase] = useState("");

  const ready =
    nonblank(requestId) &&
    nonblank(operator) &&
    nonblank(reason) &&
    nonblank(dataSnapshotId) &&
    phrase === CONFIRM_MANUAL_CEILING &&
    (ceiling !== 0.3 || nonblank(histEvidenceId)) &&
    (ceiling !== 0.6 || nonblank(anomalyEvidenceId));

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    void runMutation(() =>
      authorizeLayerOneManualCeiling({
        request_id: requestId.trim(),
        ceiling,
        authorized_at: nowIso(),
        operator: operator.trim(),
        reason: reason.trim(),
        user_confirmed: true,
        contract_schema_version: "1",
        two_layer_decision_contract_id: BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id: BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id: dataSnapshotId.trim(),
        historical_validation_evidence_id: ceiling === 0.3 ? histEvidenceId.trim() : null,
        no_severe_anomaly_evidence_id: ceiling === 0.6 ? anomalyEvidenceId.trim() : null,
        auto_upgrade: false,
      }),
    );
  }

  return (
    <form className="layer-one-form" onSubmit={onSubmit} aria-label="人工风险上限授权">
      <h3>人工风险上限</h3>
      <p>
        级别仅 0 / 0.3 / 0.6 / 0.9；无自动模式。0→30% 须已登记 historical-validation 证据；30→60% 须≥3
        个日历月且登记覆盖该阶段的 no-severe-anomaly 证据；60→90% 须≥3 个日历月且阶段内无风险锁触发。永不自动。
        12 个月 OOS 不是硬门槛，但成熟前禁止声称完整 OOS。本端点<strong>不验证经济事实</strong>
        ：证据是人工登记的审计记录，后续仍须独立审查。
      </p>
      <div className="row">
        <label>
          request_id
          <input value={requestId} onChange={(e) => setRequestId(e.target.value)} required />
        </label>
        <label>
          ceiling
          <select
            value={String(ceiling)}
            onChange={(e) => setCeiling(Number(e.target.value) as LayerOneBudgetLevel)}
          >
            <option value="0">0</option>
            <option value="0.3">0.3</option>
            <option value="0.6">0.6</option>
            <option value="0.9">0.9</option>
          </select>
        </label>
        <label>
          操作者
          <input value={operator} onChange={(e) => setOperator(e.target.value)} required />
        </label>
        <label className="grow">
          原因
          <input value={reason} onChange={(e) => setReason(e.target.value)} required />
        </label>
        <label className="grow">
          data_snapshot_id
          <input value={dataSnapshotId} onChange={(e) => setDataSnapshotId(e.target.value)} required />
        </label>
      </div>
      {ceiling === 0.3 ? (
        <div className="row">
          <label className="grow">
            historical_validation_evidence_id
            <input value={histEvidenceId} onChange={(e) => setHistEvidenceId(e.target.value)} required />
          </label>
        </div>
      ) : null}
      {ceiling === 0.6 ? (
        <div className="row">
          <label className="grow">
            no_severe_anomaly_evidence_id
            <input
              value={anomalyEvidenceId}
              onChange={(e) => setAnomalyEvidenceId(e.target.value)}
              required
            />
          </label>
        </div>
      ) : null}
      <div className="row">
        <ConfirmationField
          id="confirm-ceiling"
          requiredPhrase={CONFIRM_MANUAL_CEILING}
          value={phrase}
          onChange={setPhrase}
        />
        <button type="submit" disabled={!ready || busy}>
          {busy ? "提交中…" : "提交人工上限授权"}
        </button>
      </div>
    </form>
  );
}

function UnlockForm({
  busy,
  runMutation,
}: {
  busy: boolean;
  runMutation: (action: () => Promise<LayerOneMutationReceipt>) => Promise<void>;
}) {
  const [requestId, setRequestId] = useState("");
  const [operator, setOperator] = useState("");
  const [reason, setReason] = useState("");
  const [dataSnapshotId, setDataSnapshotId] = useState("");
  const [phrase, setPhrase] = useState("");
  const ready =
    nonblank(requestId) &&
    nonblank(operator) &&
    nonblank(reason) &&
    nonblank(dataSnapshotId) &&
    phrase === CONFIRM_UNLOCK_REQUEST;

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    void runMutation(() =>
      submitLayerOneUnlockRequest({
        request: {
          request_id: requestId.trim(),
          operator: operator.trim(),
          reason: reason.trim(),
          requested_at: nowIso(),
          user_confirmed: true,
        },
        two_layer_decision_contract_id: BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id: BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id: dataSnapshotId.trim(),
      }),
    );
  }

  return (
    <form className="layer-one-form" onSubmit={onSubmit} aria-label="提交解锁申请">
      <h3>解锁申请</h3>
      <p className="unlock-warning" role="alert">
        申请本身不会解除锁定。E9a 密封决策与全部恢复规则才决定是否解锁；提交申请 ≠ 解锁。
      </p>
      <div className="row">
        <label>
          request_id
          <input value={requestId} onChange={(e) => setRequestId(e.target.value)} required />
        </label>
        <label>
          操作者
          <input value={operator} onChange={(e) => setOperator(e.target.value)} required />
        </label>
        <label className="grow">
          原因
          <input value={reason} onChange={(e) => setReason(e.target.value)} required />
        </label>
        <label className="grow">
          data_snapshot_id
          <input value={dataSnapshotId} onChange={(e) => setDataSnapshotId(e.target.value)} required />
        </label>
      </div>
      <div className="row">
        <ConfirmationField
          id="confirm-unlock"
          requiredPhrase={CONFIRM_UNLOCK_REQUEST}
          value={phrase}
          onChange={setPhrase}
        />
        <button type="submit" disabled={!ready || busy}>
          {busy ? "提交中…" : "仅提交解锁申请"}
        </button>
      </div>
    </form>
  );
}

function DeploymentEvidenceForm({
  busy,
  runMutation,
}: {
  busy: boolean;
  runMutation: (action: () => Promise<LayerOneMutationReceipt>) => Promise<void>;
}) {
  const [evidenceType, setEvidenceType] = useState<LayerOneEvidenceType>("historical_validation_pass");
  const [observedFrom, setObservedFrom] = useState("");
  const [observedThrough, setObservedThrough] = useState("");
  const [operator, setOperator] = useState("");
  const [summary, setSummary] = useState("");
  const [dataSnapshotId, setDataSnapshotId] = useState("");
  const [typeFlag, setTypeFlag] = useState(false);
  const [phrase, setPhrase] = useState("");

  const ready = useMemo(
    () =>
      nonblank(observedFrom) &&
      nonblank(observedThrough) &&
      nonblank(operator) &&
      nonblank(summary) &&
      nonblank(dataSnapshotId) &&
      typeFlag &&
      phrase === CONFIRM_DEPLOYMENT_EVIDENCE,
    [observedFrom, observedThrough, operator, summary, dataSnapshotId, typeFlag, phrase],
  );

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    const isHist = evidenceType === "historical_validation_pass";
    void runMutation(() =>
      registerLayerOneDeploymentEvidence({
        evidence_type: evidenceType,
        observed_from: observedFrom,
        observed_through: observedThrough,
        recorded_at: nowIso(),
        operator: operator.trim(),
        summary: summary.trim(),
        user_confirmed: true,
        contract_schema_version: "1",
        two_layer_decision_contract_id: BOUND_TWO_LAYER_DECISION_CONTRACT_ID,
        layer_one_index_protocol_id: BOUND_LAYER_ONE_INDEX_PROTOCOL_ID,
        data_snapshot_id: dataSnapshotId.trim(),
        historical_validation_pass: isHist ? true : null,
        no_severe_anomaly: isHist ? null : true,
      }),
    );
  }

  return (
    <form className="layer-one-form" onSubmit={onSubmit} aria-label="登记部署证据">
      <p>
        人工登记证据，不是自动证明，不授权交易。登记后仍须独立审查；本 UI 不声称证据经济事实已被验证。
      </p>
      <div className="row">
        <label>
          evidence_type
          <select
            value={evidenceType}
            onChange={(e) => {
              setEvidenceType(e.target.value as LayerOneEvidenceType);
              setTypeFlag(false);
            }}
          >
            <option value="historical_validation_pass">historical_validation_pass</option>
            <option value="no_severe_anomaly_period">no_severe_anomaly_period</option>
          </select>
        </label>
        <label>
          observed_from
          <input type="date" value={observedFrom} onChange={(e) => setObservedFrom(e.target.value)} required />
        </label>
        <label>
          observed_through
          <input
            type="date"
            value={observedThrough}
            onChange={(e) => setObservedThrough(e.target.value)}
            required
          />
        </label>
        <label>
          操作者
          <input value={operator} onChange={(e) => setOperator(e.target.value)} required />
        </label>
        <label className="grow">
          summary
          <input value={summary} onChange={(e) => setSummary(e.target.value)} required />
        </label>
        <label className="grow">
          data_snapshot_id
          <input value={dataSnapshotId} onChange={(e) => setDataSnapshotId(e.target.value)} required />
        </label>
      </div>
      <label className="checkbox-row">
        <input type="checkbox" checked={typeFlag} onChange={(e) => setTypeFlag(e.target.checked)} />
        {evidenceType === "historical_validation_pass"
          ? "确认 historical_validation_pass=true"
          : "确认 no_severe_anomaly=true"}
      </label>
      <div className="row">
        <ConfirmationField
          id="confirm-evidence"
          requiredPhrase={CONFIRM_DEPLOYMENT_EVIDENCE}
          value={phrase}
          onChange={setPhrase}
        />
        <button type="submit" disabled={!ready || busy}>
          {busy ? "提交中…" : "登记人工证据"}
        </button>
      </div>
    </form>
  );
}

function AuditViewer() {
  const [page, setPage] = useState<LayerOneAuditPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  function loadFirstPage() {
    setLoading(true);
    setError(null);
    getLayerOneAudit({ after_sequence: 0, page_size: 20 })
      .then((data) => {
        setPage(data);
        setLoading(false);
      })
      .catch((err: unknown) => {
        setPage(null);
        setError(errorMessage(err));
        setLoading(false);
      });
  }

  return (
    <div className="audit-viewer" aria-label="审计只读查看">
      <h3>审计只读</h3>
      <p className="muted">首次点击加载最多 20 条。无编辑/删除。</p>
      <button type="button" onClick={loadFirstPage} disabled={loading}>
        {loading ? "加载中…" : "加载审计（最多 20 条）"}
      </button>
      {error ? (
        <div className="error" role="alert">
          <strong>审计加载失败</strong>
          <pre>{error}</pre>
        </div>
      ) : null}
      {page ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>seq</th>
                <th>event</th>
                <th>time</th>
                <th>audit_id</th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((item) => (
                <tr key={item.audit_id}>
                  <td>{item.sequence_no}</td>
                  <td>{item.event_type}</td>
                  <td>{item.recorded_at_utc}</td>
                  <td>
                    <code title={item.audit_id}>{abbreviateId(item.audit_id)}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {page.items.length === 0 ? <p className="empty">无审计记录。</p> : null}
        </div>
      ) : null}
    </div>
  );
}
