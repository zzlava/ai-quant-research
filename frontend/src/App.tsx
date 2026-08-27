import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  createBacktest,
  getBacktest,
  getHealth,
  getLayerOneRiskState,
  getRanking,
  getStrategies,
} from "./api/client";
import type {
  BacktestCreated,
  BacktestRecord,
  LayerOneMutationReceipt,
  LayerOneRiskStateView,
  RankingResponse,
  StrategyInfo,
} from "./api/types";
import { EquityChart } from "./components/EquityChart";
import { LayerOneRiskPanel } from "./components/LayerOneRiskPanel";
import { RiskStateBanner } from "./components/RiskStateBanner";
import { errorMessage, formatNumber, formatPct } from "./lib/format";

const DEFAULT_DATE = "2024-01-15";
const DEFAULT_START = "2024-01-02";
const DEFAULT_END = "2024-06-28";

type LoadState<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
};

function emptyState<T>(): LoadState<T> {
  return { data: null, error: null, loading: false };
}

export function App() {
  const [health, setHealth] = useState<LoadState<{ status: string }>>({
    data: null,
    error: null,
    loading: true,
  });
  const [strategies, setStrategies] = useState<LoadState<StrategyInfo[]>>({
    data: null,
    error: null,
    loading: true,
  });
  const [strategy, setStrategy] = useState("baseline_v1");
  const [date, setDate] = useState(DEFAULT_DATE);
  const [top, setTop] = useState(20);
  const [ranking, setRanking] = useState<LoadState<RankingResponse>>(emptyState());
  const [start, setStart] = useState(DEFAULT_START);
  const [end, setEnd] = useState(DEFAULT_END);
  const [backtest, setBacktest] = useState<LoadState<BacktestCreated | BacktestRecord>>(emptyState());
  const [backtestId, setBacktestId] = useState("");
  const [riskState, setRiskState] = useState<LoadState<LayerOneRiskStateView>>({
    data: null,
    error: null,
    loading: true,
  });
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [mutationReceipt, setMutationReceipt] = useState<LayerOneMutationReceipt | null>(null);
  const [mutationBusy, setMutationBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getHealth()
      .then((data) => {
        if (!cancelled) setHealth({ data, error: null, loading: false });
      })
      .catch((error: unknown) => {
        if (!cancelled) setHealth({ data: null, error: errorMessage(error), loading: false });
      });
    getStrategies()
      .then((data) => {
        if (cancelled) return;
        setStrategies({ data, error: null, loading: false });
        if (data[0]?.name) setStrategy(data[0].name);
      })
      .catch((error: unknown) => {
        if (!cancelled) setStrategies({ data: null, error: errorMessage(error), loading: false });
      });
    getLayerOneRiskState()
      .then((data) => {
        if (!cancelled) setRiskState({ data, error: null, loading: false });
      })
      .catch((error: unknown) => {
        if (!cancelled) setRiskState({ data: null, error: errorMessage(error), loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const strategyOptions = useMemo(() => strategies.data ?? [], [strategies.data]);
  const selectedStrategy = strategyOptions.find((item) => item.name === strategy);

  function onRanking(event: FormEvent) {
    event.preventDefault();
    setRanking({ data: null, error: null, loading: true });
    getRanking({ date, strategy, top })
      .then((data) => setRanking({ data, error: null, loading: false }))
      .catch((error: unknown) => setRanking({ data: null, error: errorMessage(error), loading: false }));
  }

  function onBacktest(event: FormEvent) {
    event.preventDefault();
    setBacktest({ data: null, error: null, loading: true });
    createBacktest({ strategy, start, end })
      .then((data) => {
        setBacktestId(data.id);
        setBacktest({ data, error: null, loading: false });
      })
      .catch((error: unknown) => setBacktest({ data: null, error: errorMessage(error), loading: false }));
  }

  function onLookup(event: FormEvent) {
    event.preventDefault();
    if (!backtestId.trim()) {
      setBacktest({ data: null, error: "需要已有的回测 id，界面不会编造记录。", loading: false });
      return;
    }
    setBacktest({ data: null, error: null, loading: true });
    getBacktest(backtestId.trim())
      .then((data) => setBacktest({ data, error: null, loading: false }))
      .catch((error: unknown) => setBacktest({ data: null, error: errorMessage(error), loading: false }));
  }

  const result = backtest.data && "result" in backtest.data ? backtest.data.result : null;
  const status = backtest.data?.status ?? null;

  return (
    <div className="page">
      <header className="banner">
        <p>研究系统，不交易，不构成投资建议。</p>
        <p className="muted">
          排名与回测只反映当前已导入快照上的研究计算。任何收益数字都不是实盘结果，也不能证明策略有效。
        </p>
      </header>

      <RiskStateBanner loading={riskState.loading} error={riskState.error} state={riskState.data} />

      <section className="status-row">
        <div>
          <h1>量化研究仪表盘</h1>
          <p className="muted">只读消费 FastAPI。开发时由 Vite 把 /api 代理到 127.0.0.1:8000。</p>
        </div>
        <HealthBadge state={health} />
      </section>

      <aside className="notice">
        <strong>点时校验不在本 API 中。</strong>
        严格 PIT / warm-up 状态必须用 CLI <code>preflight-research</code> 复核。当前
        GET/POST 接口不返回 signal_ready_start、universe 模式或来源 provenance。本页不会假装已经通过预检。
      </aside>

      <LayerOneRiskPanel
        riskState={riskState.data}
        riskError={riskState.error}
        riskLoading={riskState.loading}
        mutationError={mutationError}
        mutationBusy={mutationBusy}
        setMutationBusy={setMutationBusy}
        onRiskStateRefreshed={(data) => setRiskState({ data, error: null, loading: false })}
        onRiskStateRefreshFailed={(detail) => {
          setRiskState({ data: null, error: detail, loading: false });
          setMutationError(`刷新风险状态失败：${detail}`);
        }}
        onMutationSuccess={(receipt) => {
          setMutationReceipt(receipt);
          setMutationError(null);
        }}
        onMutationError={(detail) => setMutationError(detail)}
      />
      {mutationReceipt ? (
        <aside className="notice" role="status" aria-label="最近一次风险变更回执">
          <strong>最近变更回执（研究/实现 only）</strong>
          <p>
            event={mutationReceipt.event_type} · revision={mutationReceipt.revision} · audit=
            {mutationReceipt.audit_id.slice(0, 8)}… · ready_for_trading=false · ready_for_orders=false ·
            does_not_trade=true
          </p>
        </aside>
      ) : null}

      {strategies.error ? <ErrorBox title="策略列表失败" detail={strategies.error} /> : null}

      <section className="panel">
        <h2>当日排名</h2>
        <form className="row" onSubmit={onRanking}>
          <label>
            策略
            <select value={strategy} onChange={(event) => setStrategy(event.target.value)} disabled={!strategyOptions.length}>
              {strategyOptions.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                  {item.version ? ` (${item.version})` : ""}
                  {item.research_notice ? " · 非严格 PIT" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            日期
            <input type="date" value={date} onChange={(event) => setDate(event.target.value)} required />
          </label>
          <label>
            top
            <input
              type="number"
              min={1}
              max={200}
              value={top}
              onChange={(event) => setTop(Number(event.target.value))}
              required
            />
          </label>
          <button type="submit" disabled={ranking.loading}>
            {ranking.loading ? "请求中…" : "拉取排名"}
          </button>
        </form>
        <ResearchBoundary scope={selectedStrategy?.research_scope} notice={selectedStrategy?.research_notice} />
        {ranking.error ? <ErrorBox title="排名失败" detail={ranking.error} /> : null}
        {ranking.data && ranking.data.items.length === 0 ? (
          <p className="empty">后端返回了空排名。界面不会生成假名次或假分数。</p>
        ) : null}
        {ranking.data && ranking.data.items.length > 0 ? <RankingTable data={ranking.data} /> : null}
      </section>

      <section className="panel">
        <h2>研究回测</h2>
        <form className="row" onSubmit={onBacktest}>
          <label>
            开始
            <input type="date" value={start} onChange={(event) => setStart(event.target.value)} required />
          </label>
          <label>
            结束
            <input type="date" value={end} onChange={(event) => setEnd(event.target.value)} required />
          </label>
          <button type="submit" disabled={backtest.loading}>
            {backtest.loading ? "请求中…" : "提交回测"}
          </button>
        </form>
        <ResearchBoundary scope={selectedStrategy?.research_scope} notice={selectedStrategy?.research_notice} />
        <form className="row" onSubmit={onLookup}>
          <label className="grow">
            已有回测 id
            <input value={backtestId} onChange={(event) => setBacktestId(event.target.value)} placeholder="POST 成功后会填入" />
          </label>
          <button type="submit" disabled={backtest.loading}>
            按 id 查询
          </button>
        </form>
        {backtest.error ? <ErrorBox title="回测失败" detail={backtest.error} /> : null}
        {status ? <p>状态：{status}</p> : null}
        {backtest.data && "error" in backtest.data && backtest.data.error ? (
          <ErrorBox title="回测记录错误" detail={backtest.data.error} />
        ) : null}
        {status === "done" && !result ? <p className="empty">状态为 done，但后端未返回 result。</p> : null}
        {result ? <BacktestView result={result} /> : null}
      </section>
    </div>
  );
}

function HealthBadge({ state }: { state: LoadState<{ status: string }> }) {
  if (state.loading) return <span className="badge">API：检查中</span>;
  if (state.error) return <span className="badge badge-bad">API 不可用：{state.error}</span>;
  return <span className="badge badge-ok">API {state.data?.status ?? "unknown"}</span>;
}

function ErrorBox({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="error" role="alert">
      <strong>{title}</strong>
      <pre>{detail}</pre>
    </div>
  );
}

function ResearchBoundary({
  scope,
  notice,
  reconstructionId,
}: {
  scope?: string;
  notice?: string | null;
  reconstructionId?: string | null;
}) {
  if (!notice) return null;
  return (
    <aside className="research-boundary" role="note">
      <strong>{scope === "public_reconstruction" ? "非严格 PIT 说明性模拟" : "研究限制"}</strong>
      <p>{notice}</p>
      {reconstructionId ? <code>public_reconstruction_id={reconstructionId}</code> : null}
    </aside>
  );
}

function RankingTable({ data }: { data: RankingResponse }) {
  const boundary = data.items[0];
  return (
    <div className="table-wrap">
      <p className="muted">
        {data.date} · {data.strategy}
        {data.data_snapshot_id ? ` · snapshot ${data.data_snapshot_id}` : ""}
      </p>
      <ResearchBoundary
        scope={boundary?.research_scope}
        notice={boundary?.research_notice}
        reconstructionId={boundary?.reconstruction_data_id}
      />
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>symbol</th>
            <th>final</th>
            <th>mkt</th>
            <th>glb</th>
            <th>sec</th>
            <th>alpha</th>
            <th>crowd</th>
            <th>exec</th>
          </tr>
        </thead>
        <tbody>
          {data.items.map((item, index) => (
            <tr key={`${item.symbol}-${item.score_date}`}>
              <td>{index + 1}</td>
              <td>{item.symbol}</td>
              <td>{formatNumber(item.final_score, 2)}</td>
              <td>{formatNumber(item.breakdown.market_score, 2)}</td>
              <td>{formatNumber(item.breakdown.global_score, 2)}</td>
              <td>{formatNumber(item.breakdown.sector_score, 2)}</td>
              <td>{formatNumber(item.breakdown.alpha_score, 2)}</td>
              <td>{formatNumber(item.breakdown.crowding_risk, 2)}</td>
              <td>{formatNumber(item.breakdown.execution_risk, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BacktestView({ result }: { result: NonNullable<BacktestCreated["result"]> }) {
  const m = result.metrics;
  return (
    <div>
      <p className="muted">
        {result.strategy_name} {result.strategy_version} · {result.start}..{result.end}
        {result.data_snapshot_id ? ` · snapshot ${result.data_snapshot_id}` : ""}
      </p>
      <ResearchBoundary
        scope={result.research_scope}
        notice={result.research_notice}
        reconstructionId={result.reconstruction_data_id}
      />
      <p className="muted">
        window signal_end={result.window.signal_end ?? "—"} entry_end={result.window.entry_end} valuation_end=
        {result.window.valuation_end} · 期末持仓 {result.open_positions_at_end}
      </p>
      <dl className="metrics">
        <div>
          <dt>total_return</dt>
          <dd>{formatPct(m.total_return)}</dd>
        </div>
        <div>
          <dt>final_equity</dt>
          <dd>{formatNumber(m.final_equity, 2)}</dd>
        </div>
        <div>
          <dt>max_drawdown</dt>
          <dd>{formatPct(m.max_drawdown)}</dd>
        </div>
        <div>
          <dt>sharpe_ratio</dt>
          <dd>{formatNumber(m.sharpe_ratio)}</dd>
        </div>
        <div>
          <dt>trades</dt>
          <dd>{m.number_of_trades}</dd>
        </div>
        <div>
          <dt>win_rate</dt>
          <dd>{formatPct(m.win_rate)}</dd>
        </div>
      </dl>
      <p className="muted">以上指标直接来自后端 metrics，不是对策略有效性的判断。</p>
      <EquityChart points={result.equity_curve} />
    </div>
  );
}
