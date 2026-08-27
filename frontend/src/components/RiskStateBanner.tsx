import type { LayerOneRiskStateView } from "../api/types";
import { LOCK_PERSISTENCE_NOTICE } from "../lib/layerOneConstants";
import { formatPct } from "../lib/format";

export type RiskStateBannerProps = {
  loading: boolean;
  error: string | null;
  state: LayerOneRiskStateView | null;
};

export function RiskStateBanner({ loading, error, state }: RiskStateBannerProps) {
  if (loading && !state && !error) {
    return (
      <section className="risk-banner risk-banner-loading" aria-busy="true" aria-live="polite">
        <p>正在读取第一层风险状态…</p>
      </section>
    );
  }

  if (error) {
    return (
      <section className="risk-banner risk-banner-critical" role="alert">
        <h2 className="risk-banner-heading">风险状态无法核验</h2>
        <p>
          API 或完整性错误：无法核验第一层风险状态。失败关闭（fail closed）：有效股票预算 0%，禁止新开仓。不会隐藏错误，也不会回退为“安全”。
        </p>
        <pre className="risk-banner-detail">{error}</pre>
        <p className="risk-banner-flags">
          ready_for_trading=false · ready_for_orders=false · does_not_trade=true · 研究系统，不交易
        </p>
      </section>
    );
  }

  if (!state) {
    return (
      <section className="risk-banner risk-banner-critical" role="alert">
        <h2 className="risk-banner-heading">风险状态缺失</h2>
        <p>尚未取得风险状态载荷。失败关闭：有效股票预算 0%，禁止新开仓。</p>
      </section>
    );
  }

  if (!state.initialized) {
    return (
      <section className="risk-banner risk-banner-uninitialized" role="alert">
        <h2 className="risk-banner-heading">风险状态未初始化</h2>
        <p>
          流尚未显式初始化。失败关闭（fail closed）：有效股票预算 0%，不能视为已解锁，禁止新开仓。
        </p>
        <p className="risk-banner-flags">
          initialized=false · effective_stock_budget={formatPct(state.effective_stock_budget)} ·
          ready_for_trading=false · ready_for_orders=false · 研究系统，不交易
        </p>
      </section>
    );
  }

  if (state.risk_lock_active === true) {
    return (
      <section className="risk-banner risk-banner-critical" role="alert">
        <h2 className="risk-banner-heading">风险锁定</h2>
        <p>
          触发日：{state.risk_lock_triggered_as_of ?? "—"} · 有效股票预算{" "}
          {formatPct(state.effective_stock_budget)} · 当前 revision {state.revision ?? "—"}
        </p>
        <p>{LOCK_PERSISTENCE_NOTICE}</p>
        <p className="risk-banner-flags">
          risk_lock_active=true · ready_for_trading=false · ready_for_orders=false · does_not_trade=true ·
          研究系统，不交易
        </p>
      </section>
    );
  }

  return (
    <section
      className="risk-banner risk-banner-research"
      role="status"
      aria-label="第一层风险状态（仅研究）"
    >
      <h2 className="risk-banner-heading">第一层风险状态（仅研究 / 实现）</h2>
      <p>
        已初始化且当前未锁定。applied 预算 {formatPct(state.applied_stock_budget)} · 有效预算{" "}
        {formatPct(state.effective_stock_budget)} · 人工上限 {formatPct(state.manual_ceiling)} · revision{" "}
        {state.revision ?? "—"} · 最近决策日 {state.last_decision_target_trading_day ?? "—"}
      </p>
      <p className="risk-banner-flags">
        ready_for_trading=false · ready_for_orders=false · does_not_trade=true · 研究系统，不交易 ·
        本面板不授权交易
      </p>
    </section>
  );
}
