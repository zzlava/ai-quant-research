export type HealthResponse = {
  status: string;
};

export type StrategyInfo = {
  name: string;
  version: string | null;
  config_hash: string | null;
  research_scope?: string;
  research_notice?: string | null;
};

export type ScoreBreakdown = {
  market_score: number;
  global_score: number;
  sector_score: number;
  alpha_score: number;
  crowding_risk: number;
  execution_risk: number;
  final_score: number;
};

export type ScoreResult = {
  symbol: string;
  score_date: string;
  strategy_name: string;
  config_id: string;
  strategy_version: string;
  strategy_config_hash: string;
  final_score: number;
  breakdown: ScoreBreakdown;
  sector: string | null;
  data_snapshot_id: string;
  research_scope?: string;
  research_notice?: string | null;
  reconstruction_data_id?: string | null;
};

export type RankingResponse = {
  date: string;
  strategy: string;
  data_snapshot_id: string;
  items: ScoreResult[];
};

export type BacktestMetrics = {
  initial_capital: number;
  final_equity: number;
  total_return: number;
  annualized_return: number | null;
  number_of_trades: number;
  win_rate: number | null;
  average_win: number | null;
  average_loss: number | null;
  profit_factor: number | null;
  expectancy: number | null;
  average_holding_days: number | null;
  max_drawdown: number | null;
  sharpe_ratio: number | null;
  tp_exit_count: number;
  sl_exit_count: number;
  timeout_exit_count: number;
};

export type EquityPoint = {
  date: string;
  cash: number;
  market_value: number;
  equity: number;
};

export type BacktestWindow = {
  start: string;
  signal_end: string | null;
  entry_end: string;
  valuation_end: string;
};

export type BacktestResult = {
  strategy_name: string;
  strategy_version: string;
  strategy_config_hash: string;
  start: string;
  end: string;
  window: BacktestWindow;
  metrics: BacktestMetrics;
  equity_curve: EquityPoint[];
  open_positions_at_end: number;
  data_snapshot_id: string;
  research_scope?: string;
  research_notice?: string | null;
  reconstruction_data_id?: string | null;
};

export type BacktestCreated = {
  id: string;
  status: string;
  result: BacktestResult | null;
};

export type BacktestRecord = {
  id: string;
  status: string;
  strategy_name: string;
  strategy_version: string;
  strategy_config_hash: string;
  start: string;
  end: string;
  error: string | null;
  result: BacktestResult | null;
};

/** Layer-one risk-state API (E9b-1). Research / implementation only; never tradable. */

export type LayerOneEvidenceType = "historical_validation_pass" | "no_severe_anomaly_period";

export type LayerOneAuditEventType =
  | "initialize"
  | "manual_ceiling_authorization"
  | "unlock_request"
  | "decision"
  | "deployment_evidence";

export type LayerOneBudgetLevel = 0 | 0.3 | 0.6 | 0.9;

export type LayerOneRiskStateView = {
  stream_name: string;
  initialized: boolean;
  revision: number | null;
  state_id: string | null;
  applied_stock_budget: number;
  effective_stock_budget: number;
  manual_ceiling: number;
  manual_ceiling_authorization_id: string | null;
  risk_lock_active: boolean | null;
  risk_lock_triggered_as_of: string | null;
  red_line_breached: boolean | null;
  last_decision_id: string | null;
  last_decision_target_trading_day: string | null;
  last_audit_id: string | null;
  data_snapshot_id: string | null;
  two_layer_decision_contract_id: string | null;
  layer_one_index_protocol_id: string | null;
  initialized_at: string | null;
  updated_at: string | null;
  research_only: true;
  implementation_only: true;
  ready_for_orders: false;
  ready_for_trading: false;
  does_not_trade: true;
};

export type LayerOneMutationReceipt = {
  stream_name: string;
  event_type: LayerOneAuditEventType;
  audit_id: string;
  revision: number;
  authorization_id?: string | null;
  unlock_request_id?: string | null;
  unlock_evidence_id?: string | null;
  evidence_id?: string | null;
  decision_id?: string | null;
  state_id?: string | null;
  idempotent_replay?: boolean;
  research_only: true;
  implementation_only: true;
  ready_for_orders: false;
  ready_for_trading: false;
  does_not_trade: true;
};

export type LayerOneInitializeRequest = {
  operator: string;
  reason: string;
  initialized_at: string;
  user_confirmed: boolean;
  two_layer_decision_contract_id: string;
  layer_one_index_protocol_id: string;
  data_snapshot_id: string;
  contract_schema_version?: "1";
  engine_version?: "layer-one-regime-engine-v1";
};

export type LayerOneManualCeilingAuthorizationRequest = {
  request_id: string;
  ceiling: LayerOneBudgetLevel;
  authorized_at: string;
  operator: string;
  reason: string;
  user_confirmed: boolean;
  contract_schema_version?: "1";
  two_layer_decision_contract_id: string;
  layer_one_index_protocol_id: string;
  data_snapshot_id: string;
  historical_validation_evidence_id?: string | null;
  no_severe_anomaly_evidence_id?: string | null;
  auto_upgrade: false;
};

export type LayerOneUnlockRequest = {
  request_id: string;
  operator: string;
  reason: string;
  requested_at: string;
  user_confirmed: boolean;
};

export type LayerOneUnlockRequestSubmission = {
  request: LayerOneUnlockRequest;
  two_layer_decision_contract_id: string;
  layer_one_index_protocol_id: string;
  data_snapshot_id: string;
};

export type LayerOneDeploymentEvidenceRequest = {
  evidence_type: LayerOneEvidenceType;
  observed_from: string;
  observed_through: string;
  recorded_at: string;
  operator: string;
  summary: string;
  user_confirmed: boolean;
  contract_schema_version?: "1";
  two_layer_decision_contract_id: string;
  layer_one_index_protocol_id: string;
  data_snapshot_id: string;
  historical_validation_pass?: true | null;
  no_severe_anomaly?: true | null;
};

export type LayerOneAuditItem = {
  audit_id: string;
  sequence_no: number;
  prior_audit_id: string | null;
  event_type: string;
  recorded_at_utc: string;
  payload_digest?: string;
  payload?: unknown;
  decision_id?: string | null;
  authorization_id?: string | null;
  unlock_request_id?: string | null;
  evidence_id?: string | null;
  revision_after?: number;
};

export type LayerOneAuditPage = {
  stream_name: string;
  items: LayerOneAuditItem[];
  next_after_sequence: number | null;
  page_size: number;
  research_only: true;
  ready_for_orders: false;
  ready_for_trading: false;
  does_not_trade: true;
};
