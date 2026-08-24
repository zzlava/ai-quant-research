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
