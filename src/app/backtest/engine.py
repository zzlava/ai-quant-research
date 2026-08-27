from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta

import polars as pl

from app.backtest.costs import apply_slippage, buy_cost, sell_cost, shares_affordable
from app.backtest.limits import is_open_at_limit
from app.backtest.metrics import compute_attribution, compute_metrics
from app.clock import decision_at_utc
from app.models.backtest import BacktestResult, BacktestWindow, EquityPoint, ExitReason, SignalAttribution, TradeFill
from app.models.config import StrategyConfig
from app.models.scores import ScoreResult
from app.research_scope import PUBLIC_RECONSTRUCTION_SCOPE, research_notice
from app.scoring.engine import ScoringEngine
from app.storage.protocol import MarketStore
from app.universe.membership import membership_lookup_options

SignalFn = Callable[[date], list[ScoreResult]]


@dataclass
class OpenPosition:
    symbol: str
    entry_date: date
    entry_price: float
    entry_raw_price: float
    shares: int
    buy_commission: float
    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    exit_eligible_days: int = 0


@dataclass
class PendingBuy:
    symbol: str
    signal_date: date
    atr_pct: float | None = None
    entry_delay_days: int = 0


class BacktestEngine:
    def __init__(
        self,
        store: MarketStore,
        config: StrategyConfig,
        signal_fn: SignalFn | None = None,
    ) -> None:
        self.store = store
        self.config = config
        if config.research_scope == PUBLIC_RECONSTRUCTION_SCOPE and not hasattr(store, "public_reconstruction_id"):
            raise ValueError("public_reconstruction requires a verified public reconstruction overlay")
        self.signal_fn = signal_fn or ScoringEngine(store, config).run

    def run(self, start: date, end: date) -> BacktestResult:
        window = self._window(start, end)
        calendar = self.store.get_calendar(start, window.valuation_end)
        if not calendar:
            raise ValueError("no trading days in backtest window")

        cash = self.config.portfolio.initial_cash
        positions: dict[str, OpenPosition] = {}
        pending: list[PendingBuy] = []
        trades: list[TradeFill] = []
        equity_curve: list[EquityPoint] = []
        bought_today: set[str] = set()
        cooldown_until: dict[str, date] = {}
        signal_audit = SignalAttribution()
        scheduled_signal_days = self._scheduled_signal_days(window)

        daily_all = self.store.get_daily_bars(as_of=window.valuation_end, start=calendar[0])
        daily_all = daily_all.sort(["symbol", "date"]).with_columns(
            pl.col("close").shift(1).over("symbol").alias("prev_close")
        )

        for day in calendar:
            bought_today.clear()
            day_bars = daily_all.filter(pl.col("date") == day)
            bar_map = {str(r["symbol"]): r for r in day_bars.to_dicts()}

            cash, new_positions, pending = self._execute_pending(
                day, pending, positions, bar_map, cash, signal_audit
            )
            for pos in new_positions:
                positions[pos.symbol] = pos
                bought_today.add(pos.symbol)

            proceeds, closed = self._manage_exits(day, positions, bar_map, bought_today, signal_audit)
            cash += proceeds
            trades.extend(closed)
            cooldown_until.update(self._cooldown_dates(closed))

            if (
                window.signal_end is not None
                and day <= window.signal_end
                and (scheduled_signal_days is None or day in scheduled_signal_days)
            ):
                pending = self._generate_orders(day, positions, pending, cooldown_until, signal_audit)

            mtm = self._mark_to_market(positions, bar_map, day)
            equity_curve.append(
                EquityPoint(
                    date=day,
                    cash=cash,
                    market_value=mtm,
                    equity=cash + mtm,
                    open_positions=len(positions),
                    pending_orders=len(pending),
                )
            )

        # A bounded backtest cannot execute orders after valuation_end. Record
        # rather than silently dropping any still-deferred entries.
        signal_audit.deferred_orders_expired += len(pending)

        metrics = compute_metrics(self.config.portfolio.initial_cash, trades, equity_curve, start, end)
        snap = self.store.snapshot()
        return BacktestResult(
            strategy_name=self.config.name,
            strategy_version=self.config.version,
            strategy_config_hash=self.config.config_hash(),
            start=start,
            end=end,
            window=window,
            metrics=metrics,
            trades=trades,
            equity_curve=equity_curve,
            open_positions_at_end=len(positions),
            data_snapshot=snap,
            data_snapshot_id=snap.snapshot_id,
            research_scope=self.config.research_scope,
            research_notice=research_notice(self.config.research_scope),
            reconstruction_data_id=getattr(self.store, "public_reconstruction_id", None),
            attribution=compute_attribution(trades, signal_audit),
        )

    def _window(self, start: date, end: date) -> BacktestWindow:
        calendar = self.store.get_calendar(start, end)
        if not calendar:
            raise ValueError("no trading days in backtest window")
        valuation_end = calendar[-1]
        entry_end = valuation_end
        signal_end: date | None = None
        for day in calendar:
            nxt = self.store.next_trading_day(day)
            if nxt is not None and nxt <= entry_end:
                signal_end = day
        return BacktestWindow(
            start=calendar[0],
            signal_end=signal_end,
            entry_end=entry_end,
            valuation_end=valuation_end,
        )

    def _execute_pending(
        self,
        day: date,
        pending: list[PendingBuy],
        positions: dict[str, OpenPosition],
        bar_map: dict[str, dict[str, object]],
        cash: float,
        signal_audit: SignalAttribution,
    ) -> tuple[float, list[OpenPosition], list[PendingBuy]]:
        opened: list[OpenPosition] = []
        deferred: list[PendingBuy] = []
        slots = self.config.portfolio.max_positions - len(positions)
        if slots <= 0 or not pending:
            return cash, opened, deferred
        candidates = [p for p in pending if p.symbol not in positions]
        equity = cash + self._mark_to_market(positions, bar_map, day)
        target = equity / self.config.portfolio.max_positions
        for order in candidates[:slots]:
            signal_audit.entry_attempts += 1
            bar = bar_map.get(order.symbol)
            if bar is None:
                raise ValueError(f"pending entry {order.symbol} has no daily bar on {day}")
            if bool(bar.get("is_suspended")):
                signal_audit.rejected_suspended += 1
                self._defer_blocked_entry(order, deferred, signal_audit)
                continue
            prev_close = _optional_float(bar.get("prev_close"))
            if is_open_at_limit(bar, prev_close, self.config.trade, "up"):
                signal_audit.rejected_at_limit += 1
                self._defer_blocked_entry(order, deferred, signal_audit)
                continue
            raw_open = float(bar["open"])  # type: ignore[arg-type]
            allocation = min(cash, target)
            shares = shares_affordable(allocation, raw_open, self.config.costs)
            if self.config.trade.require_target_lot_affordability and shares <= 0:
                signal_audit.rejected_unaffordable += 1
                continue
            if shares <= 0:
                shares = shares_affordable(cash, raw_open, self.config.costs)
            if shares <= 0:
                signal_audit.rejected_insufficient_cash += 1
                continue
            fill_price = apply_slippage(raw_open, self.config.costs, "buy")
            total, comm = buy_cost(fill_price, shares, self.config.costs)
            if total > cash + 1e-9:
                signal_audit.rejected_insufficient_cash += 1
                continue
            cash -= total
            take_profit_price, stop_loss_price = self._barrier_prices(fill_price, order.atr_pct)
            opened.append(
                OpenPosition(
                    symbol=order.symbol,
                    entry_date=day,
                    entry_price=fill_price,
                    entry_raw_price=raw_open,
                    shares=shares,
                    buy_commission=comm,
                    take_profit_price=take_profit_price,
                    stop_loss_price=stop_loss_price,
                )
            )
            signal_audit.orders_filled += 1
            # Budget identity (successful fills only):
            # target + overallocated = actual + unallocated.
            # actual includes buy slippage (via fill_price) and buy commission.
            # Cash fallback may spend above target allocation; never let unallocated go negative.
            signal_audit.target_entry_budget_total += allocation
            signal_audit.actual_entry_cash_used_total += total
            signal_audit.unallocated_entry_budget_total += max(allocation - total, 0.0)
            signal_audit.overallocated_entry_budget_total += max(total - allocation, 0.0)
            if order.entry_delay_days > 0:
                signal_audit.orders_filled_after_deferral += 1
        return cash, opened, deferred

    def _defer_blocked_entry(
        self,
        order: PendingBuy,
        deferred: list[PendingBuy],
        signal_audit: SignalAttribution,
    ) -> None:
        trade = self.config.trade
        if trade.blocked_entry_policy != "defer":
            return
        next_delay = order.entry_delay_days + 1
        if next_delay > trade.max_entry_delay_days:
            signal_audit.deferred_orders_expired += 1
            return
        if order.entry_delay_days == 0:
            signal_audit.orders_deferred += 1
        signal_audit.entry_deferral_days += 1
        deferred.append(replace(order, entry_delay_days=next_delay))

    def _manage_exits(
        self,
        day: date,
        positions: dict[str, OpenPosition],
        bar_map: dict[str, dict[str, object]],
        bought_today: set[str],
        signal_audit: SignalAttribution,
    ) -> tuple[float, list[TradeFill]]:
        proceeds = 0.0
        closed: list[TradeFill] = []
        to_delete: list[str] = []
        for symbol, pos in positions.items():
            if symbol in bought_today:
                continue
            bar = bar_map.get(symbol)
            if bar is None:
                raise ValueError(f"open position {symbol} has no daily bar on {day}")
            pos.exit_eligible_days += 1
            if bool(bar.get("is_suspended")):
                # Count only true blocked exits: past min hold and would exit if tradable.
                if (
                    pos.exit_eligible_days >= self.config.trade.min_holding_days
                    and self._exit_decision(pos, bar) is not None
                ):
                    signal_audit.exit_blocked_suspended_days += 1
                continue
            if pos.exit_eligible_days < self.config.trade.min_holding_days:
                continue
            decision = self._exit_decision(pos, bar)
            if decision is None:
                continue
            prev_close = _optional_float(bar.get("prev_close"))
            if is_open_at_limit(bar, prev_close, self.config.trade, "down"):
                signal_audit.exit_blocked_limit_down_days += 1
                continue
            reason, raw_price = decision
            fill = apply_slippage(raw_price, self.config.costs, "sell")
            net, comm, tax = sell_cost(fill, pos.shares, self.config.costs, day)
            proceeds += net
            cost_basis = pos.entry_price * pos.shares + pos.buy_commission
            pnl = net - cost_basis
            closed.append(
                TradeFill(
                    symbol=symbol,
                    entry_date=pos.entry_date,
                    exit_date=day,
                    entry_price=pos.entry_price,
                    exit_price=fill,
                    shares=pos.shares,
                    pnl=pnl,
                    return_pct=pnl / cost_basis if cost_basis else 0.0,
                    holding_days=pos.exit_eligible_days,
                    exit_reason=reason,
                    buy_commission=pos.buy_commission,
                    sell_commission=comm,
                    stamp_tax=tax,
                    entry_raw_price=pos.entry_raw_price,
                    exit_raw_price=raw_price,
                    buy_slippage=(pos.entry_price - pos.entry_raw_price) * pos.shares,
                    sell_slippage=(raw_price - fill) * pos.shares,
                    gross_pnl=(raw_price - pos.entry_raw_price) * pos.shares,
                )
            )
            to_delete.append(symbol)
        for symbol in to_delete:
            del positions[symbol]
        return proceeds, closed

    def _exit_decision(
        self,
        pos: OpenPosition,
        bar: dict[str, object],
    ) -> tuple[ExitReason, float] | None:
        open_ = float(bar["open"])  # type: ignore[arg-type]
        high = float(bar["high"])  # type: ignore[arg-type]
        low = float(bar["low"])  # type: ignore[arg-type]
        close = float(bar["close"])  # type: ignore[arg-type]
        if self.config.trade.exit_policy == "fixed_horizon":
            if pos.exit_eligible_days >= self.config.trade.max_holding_days:
                return "timeout", close
            return None
        tp_price = pos.take_profit_price or pos.entry_price * (1.0 + self.config.trade.take_profit)
        sl_price = pos.stop_loss_price or pos.entry_price * (1.0 + self.config.trade.stop_loss)
        if open_ <= sl_price:
            return "stop_loss", open_
        if open_ >= tp_price:
            return "take_profit", open_
        hit_tp = high >= tp_price
        hit_sl = low <= sl_price
        if hit_tp and hit_sl:
            return "stop_loss", sl_price
        if hit_sl:
            return "stop_loss", sl_price
        if hit_tp:
            return "take_profit", tp_price
        if pos.exit_eligible_days >= self.config.trade.max_holding_days:
            return "timeout", close
        return None

    def _scheduled_signal_days(self, window: BacktestWindow) -> set[date] | None:
        trade = self.config.trade
        if trade.signal_interval_days == 1:
            return None
        anchor = trade.signal_anchor_date
        if anchor is None:  # Defensive; TradeConfig validates this combination.
            raise ValueError("scheduled signals require signal_anchor_date")
        if window.signal_end is None or anchor > window.signal_end:
            return set()
        schedule = self.store.get_calendar(anchor, window.signal_end)
        if not schedule or schedule[0] != anchor:
            raise ValueError(f"signal_anchor_date {anchor} is not a trading day in the snapshot")
        return set(schedule[:: trade.signal_interval_days])

    def _generate_orders(
        self,
        day: date,
        positions: dict[str, OpenPosition],
        pending: list[PendingBuy],
        cooldown_until: dict[str, date],
        signal_audit: SignalAttribution,
    ) -> list[PendingBuy]:
        signal_audit.scheduled_signal_days += 1
        ranked = self.signal_fn(day)
        if not ranked:
            signal_audit.empty_ranking_days += 1
            return pending
        signal_audit.scoring_days += 1
        signal_audit.names_ranked += len(ranked)
        breakdown = ranked[0].breakdown
        regime_score = breakdown.regime_score if breakdown.regime_score is not None else breakdown.market_score
        allowed = self.config.gate_max_new_positions(regime_score)
        free_slots = self.config.portfolio.max_positions - len(positions) - len(pending)
        take = min(allowed, max(free_slots, 0))
        if take <= 0:
            if allowed <= 0:
                signal_audit.rejected_by_regime_gate += len(ranked)
                signal_audit.regime_blocked_days += 1
            else:
                signal_audit.rejected_by_capacity += len(ranked)
                signal_audit.capacity_blocked_days += 1
            return pending
        held = set(positions) | {order.symbol for order in pending}
        lookup = membership_lookup_options(self.config.universe)
        members = self.store.get_universe_members(
            self.config.universe.id,
            day,
            decision_at_utc(day, self.config.data),
            expected_constituents=lookup["expected_constituents"],
            require_available_cross_section=bool(lookup["require_available_cross_section"]),
        )
        picks: list[PendingBuy] = []
        return_series = self._correlation_return_series(day)
        for index, result in enumerate(ranked):
            minimum = (
                self.config.balanced_ranking.min_score
                if self.config.balanced_ranking is not None
                else self.config.fundamental_ranking.min_score
                if self.config.fundamental_ranking is not None
                else self.config.ranking.min_score
                if self.config.ranking is not None
                else 0.0
            )
            if result.final_score < minimum:
                signal_audit.rejected_by_ranking_threshold += 1
                continue
            if result.symbol in held:
                signal_audit.rejected_already_held_or_pending += 1
                continue
            if result.symbol not in members:
                signal_audit.rejected_not_in_membership += 1
                continue
            until = cooldown_until.get(result.symbol)
            if until is not None and day <= until:
                signal_audit.rejected_by_cooldown += 1
                continue
            comparison = held | {pick.symbol for pick in picks}
            if not self._passes_correlation_cap(
                result.symbol,
                comparison,
                return_series,
            ):
                signal_audit.rejected_by_correlation_cap += 1
                continue
            atr_pct: float | None = None
            if result.feature is not None and result.feature.close > 0:
                atr_pct = result.feature.atr_14 / result.feature.close
            picks.append(PendingBuy(symbol=result.symbol, signal_date=day, atr_pct=atr_pct))
            if len(picks) >= take:
                remaining = len(ranked) - index - 1
                if remaining > 0:
                    signal_audit.not_evaluated_after_order_limit += remaining
                break
        signal_audit.orders_generated += len(picks)
        return [*pending, *picks]

    def _correlation_return_series(self, as_of: date) -> dict[str, dict[date, float]]:
        lookback = self.config.portfolio.correlation_lookback_days
        if self.config.portfolio.max_pairwise_correlation is None:
            return {}
        calendar = self.store.get_calendar(
            as_of - timedelta(days=max(lookback * 3, 120)), as_of
        )
        if len(calendar) < lookback + 1:
            return {}
        start = calendar[-(lookback + 1)]
        daily = self.store.get_daily_bars(as_of=as_of, start=start)
        if daily.is_empty() or "adj_close" not in daily.columns:
            return {}
        returns = (
            daily.select(["symbol", "date", "adj_close"])
            .drop_nulls()
            .sort(["symbol", "date"])
            .with_columns(
                (pl.col("adj_close") / pl.col("adj_close").shift(1).over("symbol") - 1.0)
                .alias("return")
            )
            .drop_nulls("return")
        )
        out: dict[str, dict[date, float]] = {}
        for row in returns.iter_rows(named=True):
            symbol = str(row["symbol"])
            day = row["date"]
            value = row["return"]
            if isinstance(day, date) and isinstance(value, int | float) and math.isfinite(float(value)):
                out.setdefault(symbol, {})[day] = float(value)
        return out

    def _passes_correlation_cap(
        self,
        candidate: str,
        comparison: set[str],
        series: dict[str, dict[date, float]],
    ) -> bool:
        cap = self.config.portfolio.max_pairwise_correlation
        if cap is None or not comparison:
            return True
        needed = self.config.portfolio.correlation_lookback_days
        candidate_values = series.get(candidate)
        if candidate_values is None:
            return False
        for other in comparison:
            other_values = series.get(other)
            if other_values is None:
                return False
            common = sorted(candidate_values.keys() & other_values.keys())
            if len(common) < needed:
                return False
            dates = common[-needed:]
            correlation = _pearson(
                [candidate_values[day] for day in dates],
                [other_values[day] for day in dates],
            )
            if correlation is None or correlation > cap:
                return False
        return True

    def _barrier_prices(self, entry_price: float, atr_pct: float | None) -> tuple[float | None, float | None]:
        trade = self.config.trade
        if trade.take_profit_atr is None or trade.stop_loss_atr is None:
            return None, None
        if atr_pct is None or atr_pct <= 0:
            raise ValueError("ATR-priced exits require a finite positive signal ATR")
        return (
            entry_price * (1.0 + trade.take_profit_atr * atr_pct),
            entry_price * (1.0 - trade.stop_loss_atr * atr_pct),
        )

    def _cooldown_dates(self, trades: list[TradeFill]) -> dict[str, date]:
        days = self.config.trade.cooldown_days
        if days <= 0:
            return {}
        out: dict[str, date] = {}
        for trade in trades:
            blocked = self.store.trading_days_after(trade.exit_date, days)
            if len(blocked) == days:
                out[trade.symbol] = blocked[-1]
        return out

    def _mark_to_market(
        self,
        positions: dict[str, OpenPosition],
        bar_map: dict[str, dict[str, object]],
        day: date,
    ) -> float:
        value = 0.0
        for symbol, pos in positions.items():
            bar = bar_map.get(symbol)
            if bar is None:
                raise ValueError(f"open position {symbol} has no daily bar on {day}")
            price = _require_finite_positive_close(bar.get("close"), symbol=symbol, day=day)
            value += price * pos.shares
        return value


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _require_finite_positive_close(value: object, *, symbol: str, day: date) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"open position {symbol} has invalid close on {day}")
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"open position {symbol} has invalid close on {day}")
    return price


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)
    )
    left_ss = sum((value - left_mean) ** 2 for value in left)
    right_ss = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_ss * right_ss)
    if denominator <= 0:
        return None
    return numerator / denominator
