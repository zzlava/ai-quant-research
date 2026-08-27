from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import typer

from app.demo.generator import DEMO_SEED, generate_demo_market, write_demo_parquet
from app.errors import sanitize_error_message
from app.pipeline import run_backtest, run_score
from app.research.position_utilization import summarize_position_utilization
from app.settings import get_settings
from app.storage.import_market import import_market_data
from app.strategies.loader import load_strategy_config
from app.strategies.registry import StrategyRegistry

app = typer.Typer(help="A-share research scoring and historical backtest CLI.")


@app.command("generate-demo")
def generate_demo() -> None:
    settings = get_settings()
    bundle = generate_demo_market(seed=DEMO_SEED)
    snapshot = write_demo_parquet(bundle, settings.parquet_dir)
    typer.echo(
        f"wrote demo parquet to {settings.parquet_dir} "
        f"({len(bundle.calendar)} trading days, "
        f"{sum(1 for i in bundle.instruments if not i.is_index and not i.is_global)} stocks)"
    )
    typer.echo(f"data_snapshot_id={snapshot.snapshot_id}")


@app.command("import-market-data")
def import_market_data_cmd(
    source_dir: Annotated[Path, typer.Option("--source-dir", exists=True, file_okay=False)],
    source_name: Annotated[str, typer.Option("--source-name")] = "local",
    adjustment: Annotated[Literal["forward", "backward", "none"], typer.Option("--adjustment")] = "forward",
    price_basis: Annotated[
        Literal["raw_ohlc_plus_adjusted_features"], typer.Option("--price-basis")
    ] = "raw_ohlc_plus_adjusted_features",
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
    market_index: Annotated[str | None, typer.Option("--market-index")] = None,
    global_symbol: Annotated[str | None, typer.Option("--global-symbol")] = None,
    universe_membership_file: Annotated[Path | None, typer.Option("--universe-membership-file", dir_okay=False)] = None,
) -> None:
    settings = get_settings()
    snapshot = import_market_data(
        source_dir,
        settings.parquet_dir,
        source_name=source_name,
        adjustment=adjustment,
        price_basis=price_basis,
        source_version=source_version,
        market_index=market_index,
        global_symbol=global_symbol,
        membership_file=universe_membership_file,
    )
    typer.echo(f"imported market data from {source_dir}")
    typer.echo(f"source_name={snapshot.source_name} adjustment={snapshot.adjustment}")
    typer.echo(f"universe_membership_rows={snapshot.row_counts.get('universe_membership', 0)}")
    typer.echo(f"data_snapshot_id={snapshot.snapshot_id}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")


@app.command()
def score(
    date_: Annotated[str, typer.Option("--date", help="YYYY-MM-DD")],
    strategy: Annotated[str, typer.Option("--strategy")] = "baseline_v1",
) -> None:
    as_of = date.fromisoformat(date_)
    results = run_score(as_of, strategy)
    snapshot_id = results[0].data_snapshot_id if results else ""
    if results and results[0].research_notice:
        typer.echo(f"research_scope={results[0].research_scope}")
        typer.echo(f"研究限制：{results[0].research_notice}")
        typer.echo(f"public_reconstruction_id={results[0].reconstruction_data_id}")
    typer.echo(
        f"strategy={strategy} config_id={results[0].config_id if results else '-'} "
        f"date={as_of} names={len(results)} "
        f"hash={results[0].strategy_config_hash if results else '-'} "
        f"data_snapshot_id={snapshot_id}"
    )
    typer.echo(
        f"{'rank':<6}{'symbol':<10}{'final':>8}{'mkt':>8}{'glb':>8}"
        f"{'sec':>8}{'alpha':>8}{'qual':>8}{'impr':>8}{'value':>8}"
        f"{'mom':>8}{'size':>8}{'inst':>8}"
        f"{'crowd':>8}{'exec':>8}{'attent':>8}{'regime':>8}"
    )
    for idx, item in enumerate(results[:20], start=1):
        b = item.breakdown
        institutional = "-" if b.institutional_score is None else f"{b.institutional_score:.2f}"
        typer.echo(
            f"{idx:<6}{item.symbol:<10}{item.final_score:8.2f}{b.market_score:8.2f}"
            f"{b.global_score:8.2f}{b.sector_score:8.2f}{b.alpha_score:8.2f}"
            f"{b.quality_score:8.2f}{b.improvement_score:8.2f}{b.value_score:8.2f}"
            f"{b.momentum_score:8.2f}{b.size_score:8.2f}{institutional:>8}"
            f"{b.crowding_risk:8.2f}{b.execution_risk:8.2f}{b.attention_risk:8.2f}"
            f"{(b.regime_score if b.regime_score is not None else b.market_score):8.2f}"
        )


@app.command()
def backtest(
    strategy: Annotated[str, typer.Option("--strategy")] = "baseline_v1",
    start: Annotated[str, typer.Option("--start")] = "2024-01-02",
    end: Annotated[str, typer.Option("--end")] = "2024-06-28",
) -> None:
    result = run_backtest(strategy, date.fromisoformat(start), date.fromisoformat(end))
    m = result.metrics
    typer.echo(f"strategy={result.strategy_name} version={result.strategy_version}")
    typer.echo(f"config_hash={result.strategy_config_hash}")
    typer.echo(f"data_snapshot_id={result.data_snapshot_id}")
    typer.echo(f"research_scope={result.research_scope}")
    if result.research_notice:
        typer.echo(f"研究限制：{result.research_notice}")
        typer.echo(f"public_reconstruction_id={result.reconstruction_data_id}")
    typer.echo(
        f"window signal_end={result.window.signal_end} "
        f"entry_end={result.window.entry_end} valuation_end={result.window.valuation_end}"
    )
    typer.echo(f"open_positions_at_end: {result.open_positions_at_end}")
    if result.open_positions_at_end:
        typer.echo(
            "WARNING: final_equity includes marked-to-market open positions; future liquidation costs are not included"
        )
    for key, value in m.model_dump().items():
        if isinstance(value, float):
            typer.echo(f"{key}: {value:.6f}")
        else:
            typer.echo(f"{key}: {value}")
    attribution = result.attribution
    typer.echo(f"gross_realized_pnl: {attribution.gross_realized_pnl:.6f}")
    typer.echo(f"net_realized_pnl: {attribution.net_realized_pnl:.6f}")
    typer.echo(f"explicit_costs: {attribution.explicit_costs:.6f}")
    typer.echo(f"estimated_slippage: {attribution.estimated_slippage:.6f}")
    typer.echo(f"total_trading_costs: {attribution.total_trading_costs:.6f}")
    typer.echo(f"signal_orders_generated: {attribution.signal.orders_generated}")
    typer.echo(f"signal_orders_filled: {attribution.signal.orders_filled}")
    typer.echo(f"signal_rejected_by_correlation_cap: {attribution.signal.rejected_by_correlation_cap}")
    typer.echo(f"signal_orders_deferred: {attribution.signal.orders_deferred}")
    typer.echo(f"signal_entry_deferral_days: {attribution.signal.entry_deferral_days}")
    typer.echo(f"signal_orders_filled_after_deferral: {attribution.signal.orders_filled_after_deferral}")
    typer.echo(f"signal_deferred_orders_expired: {attribution.signal.deferred_orders_expired}")
    signal = attribution.signal
    typer.echo("--- position funnel diagnostics (read-only) ---")
    typer.echo(f"scheduled_signal_days: {signal.scheduled_signal_days}")
    typer.echo(f"scoring_days: {signal.scoring_days}")
    typer.echo(f"empty_ranking_days: {signal.empty_ranking_days}")
    typer.echo(f"regime_blocked_days: {signal.regime_blocked_days}")
    typer.echo(f"capacity_blocked_days: {signal.capacity_blocked_days}")
    typer.echo(f"rejected_by_capacity: {signal.rejected_by_capacity}")
    typer.echo(f"rejected_already_held_or_pending: {signal.rejected_already_held_or_pending}")
    typer.echo(f"rejected_not_in_membership: {signal.rejected_not_in_membership}")
    typer.echo(f"not_evaluated_after_order_limit: {signal.not_evaluated_after_order_limit}")
    typer.echo(f"entry_attempts: {signal.entry_attempts}")
    typer.echo(f"rejected_insufficient_cash: {signal.rejected_insufficient_cash}")
    typer.echo(f"rejected_unaffordable: {signal.rejected_unaffordable}")
    typer.echo(f"rejected_suspended: {signal.rejected_suspended}")
    typer.echo(f"rejected_at_limit: {signal.rejected_at_limit}")
    typer.echo(f"exit_blocked_suspended_days: {signal.exit_blocked_suspended_days}")
    typer.echo(f"exit_blocked_limit_down_days: {signal.exit_blocked_limit_down_days}")
    typer.echo(f"target_entry_budget_total: {signal.target_entry_budget_total:.6f}")
    typer.echo(f"actual_entry_cash_used_total: {signal.actual_entry_cash_used_total:.6f}")
    typer.echo(f"unallocated_entry_budget_total: {signal.unallocated_entry_budget_total:.6f}")
    typer.echo(f"overallocated_entry_budget_total: {signal.overallocated_entry_budget_total:.6f}")
    settings = get_settings()
    config = load_strategy_config(strategy, settings.strategies_dir)
    utilization = summarize_position_utilization(result, max_positions=config.portfolio.max_positions)
    typer.echo("--- position utilization diagnostics (read-only) ---")
    typer.echo(f"utilization_available: {utilization.available}")
    if utilization.unavailable_reason:
        typer.echo(f"utilization_unavailable_reason: {utilization.unavailable_reason}")
    else:
        typer.echo(f"trading_days: {utilization.trading_days}")
        typer.echo(f"zero_position_days: {utilization.zero_position_days}")
        typer.echo(f"underfilled_days: {utilization.underfilled_days}")
        typer.echo(f"average_open_positions: {utilization.average_open_positions:.6f}")
        typer.echo(f"peak_open_positions: {utilization.peak_open_positions}")
        typer.echo(f"average_invested_fraction: {utilization.average_invested_fraction:.6f}")
        typer.echo(f"peak_invested_fraction: {utilization.peak_invested_fraction:.6f}")
        typer.echo(f"average_cash_fraction: {utilization.average_cash_fraction:.6f}")
    if utilization.fill_rate is None:
        typer.echo("fill_rate: null")
    else:
        typer.echo(f"fill_rate: {utilization.fill_rate:.6f}")
    if utilization.budget_utilization is None:
        typer.echo("budget_utilization: null")
    else:
        typer.echo(f"budget_utilization: {utilization.budget_utilization:.6f}")


@app.command("fetch-tushare")
def fetch_tushare_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    strategy: Annotated[str, typer.Option("--strategy")],
    symbols_file: Annotated[Path | None, typer.Option("--symbols-file", dir_okay=False)] = None,
    universe_membership_file: Annotated[Path | None, typer.Option("--universe-membership-file", dir_okay=False)] = None,
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
) -> None:
    """Pull Tushare history into the standardized snapshot. Research only; no trading."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_fetch import fetch_tushare_and_import
    from app.universe.membership import resolve_fetch_universe

    typer.echo("Historical research only. This command does not place orders or connect to a broker.")
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        stocks, membership = resolve_fetch_universe(
            config,
            symbols_file=symbols_file,
            membership_file=universe_membership_file,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
        )
        token = read_tushare_token()
        snapshot = fetch_tushare_and_import(
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            config=config,
            dest_dir=settings.parquet_dir,
            stocks=stocks,
            membership=membership,
            source_version=source_version,
            client=LiveTushareClient(token),
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    stock_count = snapshot.row_counts.get("instruments", 0)
    typer.echo(f"source_name={snapshot.source_name}")
    typer.echo(f"universe_id={config.universe.id}")
    typer.echo(f"universe_mode={config.universe.mode}")
    typer.echo(f"universe_membership_rows={snapshot.row_counts.get('universe_membership', 0)}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"instruments={stock_count}")
    typer.echo(f"market_index={snapshot.market_index}")
    typer.echo(f"global_symbol={snapshot.global_symbol}")
    typer.echo(f"adjustment={snapshot.adjustment}")
    typer.echo(f"source_version={snapshot.source_version or '-'}")
    typer.echo(f"data_snapshot_id={snapshot.snapshot_id}")


@app.command("fetch-tushare-fundamentals")
def fetch_tushare_fundamentals_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    symbols_file: Annotated[Path, typer.Option("--symbols-file", exists=True, dir_okay=False)],
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Build an audited PIT fundamental/valuation overlay. Research only."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_fetch import read_symbols_file
    from app.providers.tushare_fundamentals import fetch_tushare_fundamentals

    typer.echo(
        "Historical research only. Financial reports use conservative announcement-date availability; "
        "daily valuation is available at 17:00 Asia/Shanghai. This command does not trade."
    )
    try:
        settings = get_settings()
        target = output_dir or settings.fundamental_dir
        if target is None:
            raise ValueError("set AIQ_FUNDAMENTAL_DIR or pass --output-dir")
        symbols = read_symbols_file(symbols_file)
        token = read_tushare_token()

        def progress(done: int, total: int) -> None:
            if done == total or done % 25 == 0:
                typer.echo(f"progress={done}/{total}")

        snapshot = fetch_tushare_fundamentals(
            client=LiveTushareClient(token),
            symbols=symbols,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            dest_dir=target,
            source_version=source_version,
            replace_existing=replace_existing,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"source_name={snapshot.source_name}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"fundamental_report_rows={snapshot.row_counts['fundamental_reports']}")
    typer.echo(f"daily_valuation_rows={snapshot.row_counts['daily_valuation']}")
    typer.echo(f"fundamental_snapshot_id={snapshot.snapshot_id}")


@app.command("fetch-tushare-latest-all-a-share")
def fetch_tushare_latest_all_a_share_cmd(
    as_of: Annotated[str, typer.Option("--as-of", help="Requested YYYY-MM-DD; resolves to the latest open SSE day")],
    strategy: Annotated[str, typer.Option("--strategy")] = "all_a_share_latest_v1",
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
    replace_existing: Annotated[
        bool,
        typer.Option("--replace-existing", help="Explicitly replace a non-empty target parquet directory"),
    ] = False,
) -> None:
    """Build a current listed A-share research snapshot. It cannot create a historical backtest universe."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_latest_market import fetch_latest_all_a_share_and_import

    typer.echo(
        "Latest-market research only. This command creates one current all-A-share ranking snapshot; "
        "it does not trade or create a historical backtest universe."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        if config.research_scope != "latest_market_snapshot":
            raise ValueError("fetch-tushare-latest-all-a-share requires research_scope=latest_market_snapshot")
        token = read_tushare_token()
        result = fetch_latest_all_a_share_and_import(
            requested_as_of=date.fromisoformat(as_of),
            config=config,
            dest_dir=settings.parquet_dir,
            source_version=source_version,
            replace_existing=replace_existing,
            client=LiveTushareClient(token),
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    snapshot = result.snapshot
    typer.echo(f"source_name={snapshot.source_name}")
    typer.echo(f"research_scope={config.research_scope}")
    typer.echo(f"universe_id={config.universe.id}")
    typer.echo(f"requested_as_of={result.requested_as_of}")
    typer.echo(f"resolved_as_of={result.as_of}")
    typer.echo(f"current_a_share_candidates={result.candidate_count}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"instruments={snapshot.row_counts.get('instruments', 0)}")
    typer.echo(f"market_index={snapshot.market_index}")
    typer.echo(f"global_symbol={snapshot.global_symbol}")
    typer.echo(f"adjustment={snapshot.adjustment}")
    typer.echo(f"data_snapshot_id={snapshot.snapshot_id}")


@app.command("fetch-bigquant-public-membership")
def fetch_bigquant_public_membership_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    output_dir: Annotated[Path, typer.Option("--output-dir", file_okay=False)],
) -> None:
    """Collect third-party public CSI300 candidates. It never creates a PIT membership file."""
    from app.providers.bigquant_client import LiveBigQuantClient, read_bigquant_credentials
    from app.universe.public_reconstruction import collect_bigquant_public_reconstruction

    typer.echo(
        "Public reconstruction only. This command does not create historical_membership, "
        "does not infer available_at, and does not trade."
    )
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        access_key, secret_key = read_bigquant_credentials()
        result = collect_bigquant_public_reconstruction(
            client=LiveBigQuantClient(access_key, secret_key),
            start=start_day,
            end=end_day,
            output_dir=output_dir,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"raw_rows={result.raw_rows}")
    typer.echo(f"source_dates={result.source_dates}")
    typer.echo(f"complete_dates={result.complete_dates}")
    typer.echo(f"incomplete_dates={result.incomplete_dates}")
    typer.echo(f"eligible_for_public_reconstruction={result.eligible_for_public_reconstruction}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")
    if result.candidate_membership_path is not None:
        typer.echo(f"candidate_membership={result.candidate_membership_path}")


@app.command("collect-tushare-all-a-share-history")
def collect_tushare_all_a_share_history_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    staging_dir: Annotated[Path, typer.Option("--staging-dir", file_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")] = "all_a_share_historical_value_quality_v1",
) -> None:
    """Collect resumable historical all-A raw inputs by trading date. Research only."""
    from app.providers.tushare_all_market_history import collect_tushare_all_a_share_history
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token

    typer.echo(
        "Historical research only. Raw all-A inputs are checkpointed by trading date; "
        "this command does not trade or overwrite a normalized market snapshot."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        token = read_tushare_token()

        def progress(api_name: str, done: int, total: int, reused: bool) -> None:
            if done == total or done % 50 == 0:
                typer.echo(f"progress={done}/{total} api={api_name} partition={'reused' if reused else 'fetched'}")

        result = collect_tushare_all_a_share_history(
            client=LiveTushareClient(token),
            config=config,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            staging_dir=staging_dir,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"source_coverage={result.source_start}..{result.coverage_end}")
    typer.echo(f"research_coverage={result.coverage_start}..{result.coverage_end}")
    typer.echo(f"trading_days={result.trading_days}")
    typer.echo(f"selected_stocks={result.selected_stocks}")
    typer.echo(f"completed_partitions={result.completed_partitions}")
    typer.echo(f"reused_partitions={result.reused_partitions}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")


@app.command("materialize-tushare-all-a-share-history")
def materialize_tushare_all_a_share_history_cmd(
    staging_dir: Annotated[Path, typer.Option("--staging-dir", exists=True, file_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")] = "all_a_share_historical_value_quality_v1",
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Verify, normalize, and derive a PIT liquid membership from a completed collection."""
    from app.providers.tushare_all_market_history import materialize_tushare_all_a_share_history

    typer.echo(
        "Offline research only. This verifies staged hashes, normalizes prices, and derives daily membership; "
        "it does not place orders."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        result = materialize_tushare_all_a_share_history(
            staging_dir=staging_dir,
            dest_dir=output_dir or settings.parquet_dir,
            config=config,
            source_version=source_version,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    snapshot = result.snapshot
    typer.echo(f"source_name={snapshot.source_name}")
    typer.echo(f"research_scope={config.research_scope}")
    typer.echo(f"universe_id={config.universe.id}")
    typer.echo(f"selected_stocks={result.selected_stocks}")
    typer.echo(f"universe_membership_rows={result.membership_rows}")
    typer.echo(f"members_per_day={result.min_members}..{result.max_members}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"adjustment={snapshot.adjustment}")
    typer.echo(f"data_snapshot_id={snapshot.snapshot_id}")


@app.command("collect-tushare-all-a-share-fundamentals")
def collect_tushare_all_a_share_fundamentals_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD open trading day")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD open trading day")],
    staging_dir: Annotated[Path, typer.Option("--staging-dir", file_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")] = "all_a_share_historical_value_quality_v1",
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
) -> None:
    """Collect resumable full-market financial reports and daily valuation. Research only."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_fundamental_history import (
        collect_tushare_all_a_share_fundamentals,
    )

    typer.echo(
        "Historical research only. Financial reports are checkpointed by symbol and valuation by "
        "trading date; this command does not trade or overwrite the market snapshot."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        token = read_tushare_token()

        def progress(api_name: str, done: int, total: int, reused: bool) -> None:
            if done == total or done % 50 == 0:
                typer.echo(f"progress={done}/{total} api={api_name} partition={'reused' if reused else 'fetched'}")

        result = collect_tushare_all_a_share_fundamentals(
            client=LiveTushareClient(token),
            market_dir=market_dir or settings.parquet_dir,
            config=config,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            staging_dir=staging_dir,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"base_market_snapshot_id={result.base_market_snapshot_id}")
    typer.echo(f"report_period_start={result.report_period_start}")
    typer.echo(f"coverage={result.coverage_start}..{result.coverage_end}")
    typer.echo(f"trading_days={result.trading_days}")
    typer.echo(f"requested_stocks={result.requested_stocks}")
    typer.echo(f"completed_partitions={result.completed_partitions}")
    typer.echo(f"reused_partitions={result.reused_partitions}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")


@app.command("materialize-tushare-all-a-share-fundamentals")
def materialize_tushare_all_a_share_fundamentals_cmd(
    staging_dir: Annotated[Path, typer.Option("--staging-dir", exists=True, file_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")] = "all_a_share_historical_value_quality_v1",
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Verify and atomically materialize a market-snapshot-bound fundamental overlay."""
    from app.providers.tushare_fundamental_history import (
        materialize_tushare_all_a_share_fundamentals,
    )

    typer.echo(
        "Offline research only. This verifies staged hashes and binds the overlay to the exact "
        "market snapshot; it does not place orders."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        target = output_dir or settings.fundamental_dir
        if target is None:
            raise ValueError("set AIQ_FUNDAMENTAL_DIR or pass --output-dir")
        result = materialize_tushare_all_a_share_fundamentals(
            staging_dir=staging_dir,
            market_dir=market_dir or settings.parquet_dir,
            config=config,
            dest_dir=target,
            source_version=source_version,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"source_name={result.snapshot.source_name}")
    typer.echo(f"base_market_snapshot_id={result.snapshot.base_market_snapshot_id}")
    typer.echo(f"request_id={result.snapshot.collection_request_id}")
    typer.echo(f"requested_stocks={result.requested_stocks}")
    typer.echo(f"covered_report_symbols={result.covered_report_symbols}")
    typer.echo(f"covered_valuation_symbols={result.covered_valuation_symbols}")
    typer.echo(f"fundamental_report_rows={result.report_rows}")
    typer.echo(f"daily_valuation_rows={result.valuation_rows}")
    typer.echo(f"coverage={result.snapshot.coverage_start}..{result.snapshot.coverage_end}")
    typer.echo(f"fundamental_snapshot_id={result.snapshot.snapshot_id}")


@app.command("collect-tushare-all-a-share-ownership")
def collect_tushare_all_a_share_ownership_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    staging_dir: Annotated[Path, typer.Option("--staging-dir", file_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")] = ("all_a_share_balanced_multifactor_v1"),
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    fundamental_dir: Annotated[Path | None, typer.Option("--fundamental-dir", file_okay=False)] = None,
) -> None:
    """Collect resumable top-ten-float-holder disclosures. Research only."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_ownership_history import (
        collect_tushare_all_a_share_ownership,
    )

    typer.echo(
        "Historical research only. This collects a disclosed-holder proxy; it does not "
        "claim complete institutional ownership and does not trade."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        fundamentals = fundamental_dir or settings.fundamental_dir
        if fundamentals is None:
            raise ValueError("set AIQ_FUNDAMENTAL_DIR or pass --fundamental-dir")
        token = read_tushare_token()

        def progress(done: int, total: int, reused: bool) -> None:
            if done == total or done % 50 == 0:
                typer.echo(
                    f"progress={done}/{total} api=top10_floatholders partition={'reused' if reused else 'fetched'}"
                )

        result = collect_tushare_all_a_share_ownership(
            client=LiveTushareClient(token),
            market_dir=market_dir or settings.parquet_dir,
            fundamental_dir=fundamentals,
            config=config,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            staging_dir=staging_dir,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"base_market_snapshot_id={result.base_market_snapshot_id}")
    typer.echo(f"fundamental_snapshot_id={result.fundamental_snapshot_id}")
    typer.echo(f"requested_stocks={result.requested_stocks}")
    typer.echo(f"completed_partitions={result.completed_partitions}")
    typer.echo(f"reused_partitions={result.reused_partitions}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")


@app.command("materialize-tushare-all-a-share-ownership")
def materialize_tushare_all_a_share_ownership_cmd(
    staging_dir: Annotated[Path, typer.Option("--staging-dir", exists=True, file_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")] = ("all_a_share_balanced_multifactor_v1"),
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    fundamental_dir: Annotated[Path | None, typer.Option("--fundamental-dir", file_okay=False)] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Verify, bind and atomically materialize the ownership proxy overlay."""
    from app.providers.tushare_ownership_history import (
        materialize_tushare_all_a_share_ownership,
    )

    typer.echo(
        "Offline research only. This verifies all hashes and exact market/fundamental "
        "bindings; it does not place orders."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        fundamentals = fundamental_dir or settings.fundamental_dir
        if fundamentals is None:
            raise ValueError("set AIQ_FUNDAMENTAL_DIR or pass --fundamental-dir")
        target = output_dir or settings.ownership_dir
        if target is None:
            raise ValueError("set AIQ_OWNERSHIP_DIR or pass --output-dir")
        result = materialize_tushare_all_a_share_ownership(
            staging_dir=staging_dir,
            market_dir=market_dir or settings.parquet_dir,
            fundamental_dir=fundamentals,
            config=config,
            dest_dir=target,
            source_version=source_version,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"source_name={result.snapshot.source_name}")
    typer.echo(f"base_market_snapshot_id={result.snapshot.base_market_snapshot_id}")
    typer.echo(f"fundamental_snapshot_id={result.snapshot.fundamental_snapshot_id}")
    typer.echo(f"requested_stocks={result.requested_stocks}")
    typer.echo(f"covered_symbols={result.covered_symbols}")
    typer.echo(f"complete_groups={result.complete_groups}")
    typer.echo(f"rows={result.rows}")
    typer.echo(f"ownership_snapshot_id={result.snapshot.snapshot_id}")


@app.command("export-bigquant-public-symbols")
def export_bigquant_public_symbols_cmd(
    collection_dir: Annotated[Path, typer.Option("--collection-dir", exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option("--output", dir_okay=False)],
) -> None:
    """Export unique member codes only for downloading an isolated base price snapshot."""
    from app.universe.public_replay import export_public_reconstruction_symbols, load_public_reconstruction_pack

    typer.echo("Public reconstruction only. This writes a download symbol list, not a PIT membership file.")
    try:
        pack = load_public_reconstruction_pack(collection_dir, expected_constituents=300)
        path = export_public_reconstruction_symbols(pack, output)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"collection_id={pack.collection_id}")
    typer.echo(f"source_date_coverage={pack.coverage_start.isoformat()}..{pack.coverage_end.isoformat()}")
    typer.echo(f"unique_symbols={len(set(pack.memberships['symbol'].to_list()))}")
    typer.echo(f"output={path}")


@app.command("materialize-a-share-event-overlay")
def materialize_a_share_event_overlay_cmd(
    source_dir: Annotated[Path, typer.Option("--source-dir", exists=True, file_okay=False)],
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Build the five-table, market-bound A-share event overlay from offline exports."""
    from app.providers.tushare_event_history import materialize_tushare_event_overlay

    typer.echo(
        "Offline research only. This command verifies source hashes, normalizes date-only "
        "announcement availability, and does not fetch data, score stocks, or trade."
    )
    try:
        settings = get_settings()
        target = output_dir or settings.event_dir
        if target is None:
            raise ValueError("set AIQ_EVENT_DIR or pass --output-dir")
        result = materialize_tushare_event_overlay(
            source_dir=source_dir,
            market_dir=market_dir or settings.parquet_dir,
            dest_dir=target,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    snapshot = result.snapshot
    typer.echo(f"source_name={snapshot.source_name}")
    typer.echo(f"source_version={snapshot.source_version or '-'}")
    typer.echo(f"base_market_snapshot_id={snapshot.base_market_snapshot_id}")
    typer.echo(f"source_manifest_sha256={snapshot.source_manifest_sha256}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"covered_symbols={snapshot.covered_symbols}")
    for name, count in snapshot.row_counts.items():
        typer.echo(f"{name}_rows={count}")
    typer.echo(f"event_snapshot_id={snapshot.snapshot_id}")


@app.command("collect-tushare-all-a-share-events")
def collect_tushare_all_a_share_events_cmd(
    start: Annotated[str, typer.Option("--start", help="Announcement coverage YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="Announcement coverage YYYY-MM-DD")],
    staging_dir: Annotated[Path, typer.Option("--staging-dir", file_okay=False)],
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
) -> None:
    """Collect five resumable, market-bound Tushare event sources. Research only."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_event_collection import collect_tushare_a_share_events

    typer.echo(
        "Historical research only. Five event endpoints are checkpointed by stock. "
        "Collection time is provenance, never historical available_at; this command does not score or trade."
    )
    try:
        settings = get_settings()
        token = read_tushare_token()

        def progress(api_name: str, done: int, total: int, reused: bool) -> None:
            if done == total or done % 50 == 0:
                typer.echo(f"progress={done}/{total} api={api_name} partition={'reused' if reused else 'fetched'}")

        def fallback_progress(symbol: str, done: int, total: int, day: date) -> None:
            if done == 1 or done == total or done % 50 == 0:
                typer.echo(f"share_float_fallback={done}/{total} symbol={symbol} ann_date={day.isoformat()}")

        result = collect_tushare_a_share_events(
            client=LiveTushareClient(token),
            market_dir=market_dir or settings.parquet_dir,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            staging_dir=staging_dir,
            source_version=source_version,
            progress=progress,
            fallback_progress=fallback_progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"base_market_snapshot_id={result.base_market_snapshot_id}")
    typer.echo(f"coverage={result.coverage_start}..{result.coverage_end}")
    typer.echo(f"requested_stocks={result.requested_stocks}")
    typer.echo(f"completed_partitions={result.completed_partitions}")
    typer.echo(f"reused_partitions={result.reused_partitions}")
    typer.echo(f"source_manifest={result.source_manifest_path}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")


@app.command("verify-a-share-event-overlay")
def verify_a_share_event_overlay_cmd(
    event_dir: Annotated[Path | None, typer.Option("--event-dir", file_okay=False)] = None,
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
) -> None:
    """Verify all event bytes, PIT timestamps, provenance, and market binding offline."""
    from app.storage.event_io import load_verified_event_snapshot
    from app.storage.snapshot_io import load_verified_snapshot

    typer.echo("Offline verification only. This command does not fetch data, score stocks, or trade.")
    try:
        settings = get_settings()
        target = event_dir or settings.event_dir
        if target is None:
            raise ValueError("set AIQ_EVENT_DIR or pass --event-dir")
        market = load_verified_snapshot(market_dir or settings.parquet_dir)
        snapshot, _ = load_verified_event_snapshot(
            target,
            expected_market_snapshot_id=market.snapshot_id,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"verified_event_snapshot_id={snapshot.snapshot_id}")
    typer.echo(f"base_market_snapshot_id={snapshot.base_market_snapshot_id}")
    typer.echo(f"source_manifest_sha256={snapshot.source_manifest_sha256}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"covered_symbols={snapshot.covered_symbols}")
    for name, count in snapshot.row_counts.items():
        typer.echo(f"{name}_rows={count}")


@app.command("diagnose-a-share-event-overlay")
def diagnose_a_share_event_overlay_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    as_of: Annotated[str, typer.Option("--as-of", help="decision date YYYY-MM-DD")],
    event_dir: Annotated[Path | None, typer.Option("--event-dir", file_okay=False)] = None,
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Build a read-only PIT event diagnostic snapshot; never score or trade."""
    from app.models.events import EventSourceManifest
    from app.research.event_diagnostics import (
        build_event_diagnostics,
        write_event_diagnostics_atomically,
    )
    from app.storage.event_io import load_verified_event_snapshot
    from app.storage.snapshot_io import load_verified_snapshot

    typer.echo(
        "Offline diagnostic only. The output contains PIT event observations, not risk "
        "thresholds, exclusions, scores, orders, or trades."
    )
    try:
        settings = get_settings()
        decision_day = date.fromisoformat(as_of)
        resolved_market = market_dir or settings.parquet_dir
        resolved_event = event_dir or settings.event_dir
        if resolved_event is None:
            raise ValueError("set AIQ_EVENT_DIR or pass --event-dir")
        market = load_verified_snapshot(resolved_market)
        event_snapshot, tables = load_verified_event_snapshot(
            resolved_event,
            expected_market_snapshot_id=market.snapshot_id,
        )
        event_source_bytes = (resolved_event / "source_manifest.json").read_bytes()
        if hashlib.sha256(event_source_bytes).hexdigest() != event_snapshot.source_manifest_sha256:
            raise ValueError("event source manifest changed during diagnostic loading")
        event_source_manifest = EventSourceManifest.model_validate_json(event_source_bytes)
        config = load_strategy_config(strategy, settings.strategies_dir)
        report, frame = build_event_diagnostics(
            market_dir=resolved_market,
            event_snapshot=event_snapshot,
            event_source_manifest=event_source_manifest,
            event_tables=tables,
            config=config,
            as_of=decision_day,
        )
        destination = output_dir or (settings.data_dir / "event-diagnostics" / f"{strategy}-{decision_day.isoformat()}")
        report = write_event_diagnostics_atomically(
            destination,
            report,
            frame,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"market_snapshot_id={report.market_snapshot_id}")
    typer.echo(f"event_snapshot_id={report.event_snapshot_id}")
    typer.echo(f"as_of={report.as_of_date} decision_at_utc={report.decision_at_utc}")
    typer.echo(f"rows={report.rows}")
    for name, count in report.visible_event_rows.items():
        typer.echo(f"visible_{name}_rows={count}")
    for name, count in report.observed_symbol_counts.items():
        typer.echo(f"observed_{name}_symbols={count}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo(f"output={destination}")


@app.command("diagnose-a-share-event-candidates")
def diagnose_a_share_event_candidates_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    start: Annotated[str, typer.Option("--start", help="inclusive window start YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="inclusive window end YYYY-MM-DD")],
    event_dir: Annotated[Path | None, typer.Option("--event-dir", file_okay=False)] = None,
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Measure development-window event-candidate coverage and direction; never score or trade."""
    from app.models.events import EventSourceManifest
    from app.research.event_candidate_diagnostics import (
        DEVELOPMENT_WINDOW_END,
        DEVELOPMENT_WINDOW_START,
        build_event_candidate_diagnostics,
        write_event_candidate_diagnostics_atomically,
    )
    from app.storage.event_io import load_verified_event_snapshot
    from app.storage.snapshot_io import load_verified_snapshot

    typer.echo(
        "Offline development-window event-candidate diagnostics only. Output is candidate "
        "evidence for coverage, missingness, direction, and 2022/2023 stability; it "
        "authorizes no score, IC, exclusion, portfolio, order, trade, or alpha claim. "
        "Labels stop at 2023-12-31; 2024 is already observed and must not be used for selection."
    )
    try:
        settings = get_settings()
        window_start = date.fromisoformat(start)
        window_end = date.fromisoformat(end)
        if window_start != DEVELOPMENT_WINDOW_START or window_end != DEVELOPMENT_WINDOW_END:
            raise ValueError(
                "diagnose-a-share-event-candidates only allows "
                f"{DEVELOPMENT_WINDOW_START.isoformat()}..{DEVELOPMENT_WINDOW_END.isoformat()}"
            )
        resolved_market = market_dir or settings.parquet_dir
        resolved_event = event_dir or settings.event_dir
        if resolved_event is None:
            raise ValueError("set AIQ_EVENT_DIR or pass --event-dir")
        market = load_verified_snapshot(resolved_market)
        event_snapshot, tables = load_verified_event_snapshot(
            resolved_event,
            expected_market_snapshot_id=market.snapshot_id,
        )
        event_source_bytes = (resolved_event / "source_manifest.json").read_bytes()
        if hashlib.sha256(event_source_bytes).hexdigest() != event_snapshot.source_manifest_sha256:
            raise ValueError("event source manifest changed during candidate diagnostic loading")
        event_source_manifest = EventSourceManifest.model_validate_json(event_source_bytes)
        config = load_strategy_config(strategy, settings.strategies_dir)
        report, observations, summary = build_event_candidate_diagnostics(
            market_dir=resolved_market,
            event_snapshot=event_snapshot,
            event_source_manifest=event_source_manifest,
            event_tables=tables,
            config=config,
            window_start=window_start,
            window_end=window_end,
        )
        destination = output_dir or (settings.data_dir / "event-candidate-diagnostics" / report.diagnostic_version)
        report = write_event_candidate_diagnostics_atomically(
            destination,
            report,
            observations,
            summary,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"diagnostic_version={report.diagnostic_version}")
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"market_snapshot_id={report.market_snapshot_id}")
    typer.echo(f"event_snapshot_id={report.event_snapshot_id}")
    typer.echo(f"window={report.window_start}..{report.window_end}")
    typer.echo(f"label_hard_end={report.label_hard_end}")
    typer.echo(f"benchmark_symbol={report.benchmark_symbol}")
    typer.echo(f"observation_rows={report.observation_rows}")
    typer.echo(f"summary_rows={report.summary_rows}")
    typer.echo(f"report_id={report.report_id}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("development_only=true")
    typer.echo(f"output={destination}")


@app.command("verify-a-share-event-candidate-freeze")
def verify_a_share_event_candidate_freeze_cmd(
    freeze_file: Annotated[
        Path | None,
        typer.Option("--freeze-file", dir_okay=False, help="Frozen research protocol JSON"),
    ] = None,
    diagnostic_dir: Annotated[
        Path | None,
        typer.Option(
            "--diagnostic-dir",
            file_okay=False,
            help="Sealed development-window event-candidate diagnostic directory",
        ),
    ] = None,
) -> None:
    """Verify the development-only event-candidate OOS freeze; never score or trade."""
    from app.research.event_candidate_freeze import (
        DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH,
        load_verified_event_candidate_oos_freeze,
        verify_event_candidate_oos_freeze,
    )

    typer.echo(
        "Offline freeze verification only. This command does not inspect 2024/2025+ returns, "
        "run preflight, score, analyze-ic, backtest, or trade."
    )
    try:
        resolved_freeze = freeze_file or DEFAULT_EVENT_CANDIDATE_OOS_FREEZE_PATH
        contract = load_verified_event_candidate_oos_freeze(resolved_freeze)
        resolved_diagnostic = diagnostic_dir or Path(contract.bound_diagnostic.artifact_dir)
        contract = verify_event_candidate_oos_freeze(
            freeze_path=resolved_freeze,
            diagnostic_dir=resolved_diagnostic,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    nominated = ",".join(contract.nominated_hypothesis_ids) or "(none)"
    typer.echo(f"freeze_version={contract.freeze_version}")
    typer.echo(f"freeze_id={contract.freeze_id}")
    typer.echo(f"bound_report_id={contract.bound_diagnostic.report_id}")
    typer.echo(f"market_snapshot_id={contract.bound_diagnostic.market_snapshot_id}")
    typer.echo(f"event_snapshot_id={contract.bound_diagnostic.event_snapshot_id}")
    typer.echo(f"strategy_config_hash={contract.bound_diagnostic.strategy_config_hash}")
    typer.echo(
        "primary_oos_endpoint="
        f"{contract.primary_oos_endpoint.observation_field} vs "
        f"{contract.primary_oos_endpoint.benchmark_symbol}"
    )
    typer.echo(f"nominated_hypothesis_ids={nominated}")
    typer.echo(f"nominated_count={contract.nominated_count}")
    typer.echo("multiplicity_reported=true")
    for item in contract.hypothesis_nominations:
        typer.echo(f"hypothesis={item.hypothesis_id} passed={str(item.passed).lower()} reason={item.reason}")
    typer.echo(f"oos_evaluation_mode={contract.oos_policy.evaluation_mode}")
    typer.echo(f"authorized_oos_window={contract.oos_policy.authorized_oos_window}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("human_review_required=true")


@app.command("verify-all-a-share-portfolio-oos-freeze")
def verify_all_a_share_portfolio_oos_freeze_cmd(
    freeze_file: Annotated[
        Path | None,
        typer.Option("--freeze-file", dir_okay=False, help="Frozen portfolio OOS protocol JSON"),
    ] = None,
    project_root: Annotated[
        Path | None,
        typer.Option(
            "--project-root",
            file_okay=False,
            help="Repository root used to resolve relative freeze bindings",
        ),
    ] = None,
) -> None:
    """Verify the development-only p10_h20 portfolio OOS freeze; never score or trade."""
    from app.research.portfolio_oos_freeze import (
        DEFAULT_PORTFOLIO_OOS_FREEZE_PATH,
        assert_committed_portfolio_oos_freeze_bindings,
        verify_portfolio_oos_freeze,
    )

    typer.echo(
        "Offline portfolio OOS freeze verification only. This command reads the freeze "
        "contract, development evidence JSON, strategy YAML, four manifests, and two "
        "calendar Parquet files. It does not read daily/index/global bars, fundamental "
        "tables, or any 2025+ preflight/score/IC/backtest/return/trade/portfolio result."
    )
    try:
        resolved_freeze = freeze_file or DEFAULT_PORTFOLIO_OOS_FREEZE_PATH
        contract = verify_portfolio_oos_freeze(
            freeze_path=resolved_freeze,
            project_root=project_root,
        )
        assert_committed_portfolio_oos_freeze_bindings(contract)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"freeze_version={contract.freeze_version}")
    typer.echo(f"freeze_id={contract.freeze_id}")
    typer.echo(f"candidate_id={contract.bound_strategy.candidate_id}")
    typer.echo(f"strategy_config_hash={contract.bound_strategy.config_hash}")
    typer.echo(f"selection_report_sha256={contract.bound_selection.report_sha256}")
    typer.echo(f"robustness_status={contract.bound_robustness.status}")
    typer.echo(f"oos_market_snapshot_id={contract.bound_oos_data.market_snapshot_id}")
    typer.echo(f"oos_fundamental_snapshot_id={contract.bound_oos_data.fundamental_snapshot_id}")
    typer.echo(f"runtime_equivalent_anchor={contract.calendar_equivalence.runtime_equivalent_anchor.isoformat()}")
    typer.echo(f"first_2025_plus_signal={contract.calendar_equivalence.first_2025_plus_signal.isoformat()}")
    typer.echo(f"signal_cutoff={contract.evaluation_window.signal_cutoff.isoformat()}")
    typer.echo(f"last_scheduled_exit={contract.evaluation_window.last_scheduled_exit.isoformat()}")
    typer.echo(f"oos_evaluation_mode={contract.oos_policy.evaluation_mode}")
    typer.echo(f"authorized={str(contract.authorized).lower()}")
    typer.echo(f"one_shot_required={str(contract.one_shot_required).lower()}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("human_review_required=true")


@app.command("verify-research-trial-ledger")
def verify_research_trial_ledger_cmd(
    ledger_file: Annotated[
        Path | None,
        typer.Option("--ledger-file", dir_okay=False, help="Research trial ledger JSON"),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used to resolve relative evidence/receipt paths",
        ),
    ] = None,
) -> None:
    """Verify the research trial ledger; never score, backtest, or trade."""
    from app.research.experiment_ledger import (
        DEFAULT_RESEARCH_TRIAL_LEDGER_PATH,
        verify_research_trial_ledger,
    )

    typer.echo(
        "Read-only research trial ledger verification only. This command checks "
        "self-hash, trial graph, evidence/receipt paths, and OOS consumption rules. "
        "It does not score, backtest, analyze IC, run phase diagnostics, or trade."
    )
    try:
        resolved_ledger = ledger_file or DEFAULT_RESEARCH_TRIAL_LEDGER_PATH
        _ledger, summary = verify_research_trial_ledger(
            ledger_path=resolved_ledger,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"ledger_id={summary.ledger_id}")
    typer.echo(f"complete={str(summary.complete).lower()}")
    typer.echo(f"historical_backfill={str(summary.historical_backfill).lower()}")
    typer.echo(f"trial_count={summary.trial_count}")
    typer.echo(f"trial_count_is_lower_bound={str(summary.trial_count_is_lower_bound).lower()}")
    typer.echo(f"oos_consumed_count={summary.oos_consumed_count}")
    typer.echo(
        "declared_before_observation="
        f"yes={summary.declared_before_observation_yes},"
        f"no={summary.declared_before_observation_no},"
        f"unknown={summary.declared_before_observation_unknown}"
    )
    for family_id, count in summary.counts_by_family.items():
        typer.echo(f"family_count.{family_id}={count}")
    for stage, count in summary.counts_by_stage.items():
        typer.echo(f"stage_count.{stage}={count}")
    for status, count in summary.counts_by_status.items():
        typer.echo(f"status_count.{status}={count}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("does_not_score=true")
    typer.echo("does_not_backtest=true")
    typer.echo("does_not_trade=true")


@app.command("audit-deflated-sharpe")
def audit_deflated_sharpe_cmd(
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Bind available DSR inputs and fail closed when multiplicity inputs are absent."""
    from app.research.deflated_sharpe_audit import write_deflated_sharpe_audit

    typer.echo(
        "Offline Deflated Sharpe audit only. This binds the audited daily return moments "
        "and trial-ledger lower bound. Missing comparable-trial dispersion or effective "
        "independence stays null; no DSR number is invented. No OOS, scoring, backtest, or trade."
    )
    try:
        report = write_deflated_sharpe_audit(repo_root=repo_root or Path.cwd())
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"audit_id={report.audit_id}")
    typer.echo(f"observed_annualized_sharpe={report.observed_annualized_sharpe:.8f}")
    typer.echo(f"return_observations={report.n_return_observations}")
    typer.echo(f"trial_count_lower_bound={report.registered_trial_count_lower_bound}")
    typer.echo(f"status={report.status}")
    typer.echo(f"missing_bindings={','.join(report.missing_bindings)}")
    typer.echo("numeric_dsr=null")
    typer.echo("ready_for_trading=false")


@app.command("verify-deflated-sharpe-audit")
def verify_deflated_sharpe_audit_cmd(
    audit_file: Annotated[
        Path,
        typer.Option("--audit-file", dir_okay=False),
    ] = Path("data/research/deflated-sharpe-audit-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Fully recompute the fail-closed Deflated Sharpe audit."""
    from app.research.deflated_sharpe_audit import (
        verify_deflated_sharpe_audit_file,
    )

    try:
        report = verify_deflated_sharpe_audit_file(repo_root=repo_root or Path.cwd(), path=audit_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"audit_id={report.audit_id}")
    typer.echo(f"status={report.status}")
    typer.echo("full_recomputation=passed")
    typer.echo("numeric_dsr=null")
    typer.echo("ready_for_trading=false")


@app.command("review-statistical-power")
def review_statistical_power_cmd(
    output: Annotated[
        Path,
        typer.Option("--output", dir_okay=False, help="Destination for the sealed read-only power review JSON"),
    ],
    protocol_file: Annotated[
        Path | None,
        typer.Option("--protocol-file", dir_okay=False, help="Frozen statistical power gate protocol JSON"),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False, help="Repository root used to resolve the bound diagnostic"),
    ] = None,
) -> None:
    """Build a retrospective variance/power calibration; never infer alpha or consume OOS."""
    from app.research.statistical_power_gate import (
        DEFAULT_STATISTICAL_POWER_GATE_PATH,
        build_retrospective_power_review,
        write_power_review,
    )

    typer.echo(
        "Offline statistical-power calibration only. This command reads one hash-bound "
        "development diagnostic, computes a planning MDE, and writes a sealed review. "
        "It does not reinterpret results, select factors, score, backtest, consume OOS, or trade."
    )
    try:
        resolved_protocol = protocol_file or DEFAULT_STATISTICAL_POWER_GATE_PATH
        review = build_retrospective_power_review(
            protocol_path=resolved_protocol,
            repo_root=repo_root,
        )
        sealed = write_power_review(output, review)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={sealed.protocol_id}")
    typer.echo(f"source_diagnostic_report_id={sealed.source_diagnostic_report_id}")
    for row in sealed.rows:
        typer.echo(
            f"endpoint={row.endpoint_id} mde={row.normal_approximation_mde:.6f} "
            f"minimum_effect={row.minimum_effect_of_interest:.6f} outcome={row.outcome}"
        )
    typer.echo(f"family_outcome={sealed.family_outcome}")
    typer.echo(f"review_id={sealed.review_id}")
    typer.echo(f"output={output}")
    typer.echo("retrospective_calibration_only=true")
    typer.echo("consumes_oos=false")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")


@app.command("verify-statistical-power-review")
def verify_statistical_power_review_cmd(
    review_file: Annotated[
        Path,
        typer.Option("--review-file", exists=True, dir_okay=False, help="Sealed statistical power review JSON"),
    ],
    protocol_file: Annotated[
        Path | None,
        typer.Option("--protocol-file", dir_okay=False, help="Frozen statistical power gate protocol JSON"),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False, help="Repository root used for full recomputation"),
    ] = None,
) -> None:
    """Fully recompute a statistical-power review; never score, backtest, or trade."""
    from app.research.statistical_power_gate import (
        DEFAULT_STATISTICAL_POWER_GATE_PATH,
        verify_power_review,
    )

    typer.echo(
        "Read-only statistical-power review verification. This command checks self-hash, "
        "protocol/source bindings, and full MDE recomputation. It does not score, backtest, "
        "consume OOS, or trade."
    )
    try:
        review = verify_power_review(
            review_path=review_file,
            protocol_path=protocol_file or DEFAULT_STATISTICAL_POWER_GATE_PATH,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={review.protocol_id}")
    typer.echo(f"review_id={review.review_id}")
    typer.echo(f"endpoints_evaluable={review.endpoints_evaluable}")
    typer.echo(f"endpoints_not_evaluable={review.endpoints_not_evaluable}")
    typer.echo(f"family_outcome={review.family_outcome}")
    typer.echo("retrospective_calibration_only=true")
    typer.echo("consumes_oos=false")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")


@app.command("run-layer-two-evaluation-machine")
def run_layer_two_evaluation_machine_cmd(
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Build development-only random controls, left-tail metrics and IC decay."""
    from app.research.layer_two_evaluation_machine import write_evaluation_machine

    typer.echo(
        "Offline 2022-2023 evaluation-machine diagnostic only. This command separates "
        "random safety and tilt arms, evaluates left-tail classification and IC decay. "
        "It does not read 2025+, retune weights, score, backtest, construct an executable "
        "portfolio, place orders, connect to a broker, or trade."
    )
    try:
        report = write_evaluation_machine(repo_root=repo_root or Path.cwd())
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"anchors={report.four_arm['anchor_count']}")
    typer.echo(f"safety_increment_mean={report.four_arm['safety_increment']['mean']:.8f}")
    typer.echo(f"tilt_percentile_vs_r1_random={report.four_arm['tilt_percentile_vs_r1_random']:.8f}")
    typer.echo(f"confirmatory_status={report.confirmatory_status}")
    typer.echo("consumed_oos_reused=false")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-layer-two-evaluation-machine")
def verify_layer_two_evaluation_machine_cmd(
    report_file: Annotated[
        Path,
        typer.Option("--report-file", dir_okay=False),
    ] = Path("data/all-a-share-historical-v1/research/layer-two-evaluation-machine-v1/report.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Fully recompute the frozen development-only evaluation machine."""
    from app.research.layer_two_evaluation_machine import (
        verify_evaluation_machine_file,
    )

    typer.echo(
        "Read-only full recomputation of the 2022-2023 evaluation machine. "
        "No 2025+ data, scoring, backtest, portfolio construction, orders, broker, or trade."
    )
    try:
        report = verify_evaluation_machine_file(repo_root=repo_root or Path.cwd(), report_path=report_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"monte_carlo_sha256={report.monte_carlo_sha256}")
    typer.echo(f"ic_decay_sha256={report.ic_decay_sha256}")
    typer.echo(f"left_tail_sha256={report.left_tail_sha256}")
    typer.echo("full_disk_recomputation=passed")
    typer.echo("confirmatory_status=not_evaluable")
    typer.echo("ready_for_trading=false")


@app.command("verify-two-layer-decision-contract")
def verify_two_layer_decision_contract_cmd(
    draft_file: Annotated[
        Path | None,
        typer.Option(
            "--draft-file",
            dir_okay=False,
            help="Two-layer strategy decision draft JSON",
        ),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used to resolve and verify the bound research trial ledger",
        ),
    ] = None,
) -> None:
    """Verify the two-layer decision draft; never score, backtest, or trade."""
    from app.research.two_layer_contract import (
        DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH,
        verify_two_layer_decision_draft_file,
    )

    typer.echo(
        "Read-only two-layer decision contract verification only. This command checks "
        "self-hash, bound research trial ledger path/id, status, ready flags, "
        "pending_user_decision count, and categorized evidence blockers. "
        "It does not invent economic defaults, score, backtest, trade, or auto-deploy."
    )
    try:
        resolved_draft = draft_file or DEFAULT_TWO_LAYER_DECISION_DRAFT_PATH
        resolved_root = repo_root or Path.cwd()
        _draft, result = verify_two_layer_decision_draft_file(
            draft_path=resolved_draft,
            repo_root=resolved_root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"contract_id={result.contract_id}")
    typer.echo(f"schema_version={result.schema_version}")
    typer.echo(f"contract_version={result.contract_version}")
    typer.echo(f"status={result.status}")
    typer.echo(f"research_trial_ledger_path={result.research_trial_ledger_path}")
    typer.echo(f"research_trial_ledger_id={result.research_trial_ledger_id}")
    typer.echo(f"research_trial_ledger_binding_ok={str(result.research_trial_ledger_binding_ok).lower()}")
    typer.echo(f"user_decisions_resolved={str(result.user_decisions_resolved).lower()}")
    typer.echo(f"pending_user_decision_count={result.pending_user_decision_count}")
    typer.echo(f"resolved={str(result.resolved).lower()}")
    typer.echo(f"blocker_count={len(result.blockers)}")
    for blocker in result.blockers:
        typer.echo(f"blocker={blocker}")
    for evidence in result.evidence_blockers:
        typer.echo(f"evidence_blocker={evidence.category}:{evidence.path}")
    typer.echo(f"confirmed_initial_cash={result.confirmed_initial_cash}")
    typer.echo("initial_cash_is_blocker=false")
    typer.echo("consumed_oos_reuse_forbidden=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("does_not_score=true")
    typer.echo("does_not_backtest=true")
    typer.echo("does_not_trade=true")


@app.command("verify-pit-industry-source")
def verify_pit_industry_source_cmd(
    history_file: Annotated[
        Path,
        typer.Option("--history-file", dir_okay=False, help="PIT industry history CSV"),
    ],
    manifest_file: Annotated[
        Path,
        typer.Option("--manifest-file", dir_okay=False, help="PIT industry history JSON manifest"),
    ],
) -> None:
    """Verify an offline PIT industry history CSV + manifest; never score, backtest, or trade."""
    from app.research.industry_history_contract import verify_industry_history_source

    typer.echo(
        "Read-only PIT industry history source verification only. This command checks "
        "manifest self-hash, CSV SHA-256, required columns, UTC timestamps, "
        "effective intervals, and coverage declarations. "
        "It does not invent industry history, score, backtest, or trade."
    )
    try:
        _manifest, _records, summary = verify_industry_history_source(
            history_file=history_file,
            manifest_file=manifest_file,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"manifest_id={summary.manifest_id}")
    typer.echo(f"source_name={summary.source_name}")
    typer.echo(f"industry_scheme={summary.industry_scheme}")
    typer.echo(f"industry_version={summary.industry_version}")
    typer.echo(f"history_file_sha256={summary.history_file_sha256}")
    typer.echo(f"coverage={summary.coverage_start.isoformat()}..{summary.coverage_end.isoformat()}")
    typer.echo(f"row_count={summary.row_count}")
    typer.echo(f"covered_symbols={summary.covered_symbols}")
    typer.echo(f"complete={str(summary.complete).lower()}")
    typer.echo(f"pit_semantics={summary.pit_semantics}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("does_not_score=true")
    typer.echo("does_not_backtest=true")
    typer.echo("does_not_trade=true")


@app.command("verify-csi-all-share-index-identity")
def verify_csi_all_share_index_identity_cmd(
    contract_file: Annotated[
        Path,
        typer.Option(
            "--contract-file",
            dir_okay=False,
            help="Sealed CSI All Share index identity contract JSON",
        ),
    ],
    repo_root: Annotated[
        Path,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used for strict regular-file resolution",
        ),
    ],
) -> None:
    """Verify the factual index identity contract; never fetch, score, backtest, or trade."""
    from app.research.csi_all_share_index_identity import verify_contract_file

    typer.echo(
        "Read-only CSI All Share index identity verification only. This command checks "
        "the sealed CSI/Tushare evidence identity, source hashes, fixed probe metadata, "
        "self-hash, canonical factory binding, and readiness gates. It does not open "
        "network access, materialize history, fill missing dates, score, backtest, or trade."
    )
    try:
        contract, result = verify_contract_file(
            repo_root=repo_root,
            contract_path=contract_file,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    identities = {identity.return_definition: identity for identity in contract.identities}
    typer.echo(f"contract_id={result.contract_id}")
    typer.echo(f"price_ts_code={identities['price_index'].tushare_ts_code}")
    typer.echo(f"total_return_ts_code={identities['total_return'].tushare_ts_code}")
    typer.echo(f"net_return_ts_code={identities['net_return'].tushare_ts_code}")
    typer.echo(f"factual_identity_verified={str(result.factual_identity_verified).lower()}")
    typer.echo(
        "price_series_ready_for_long_history_materialization="
        f"{str(result.price_series_ready_for_long_history_materialization).lower()}"
    )
    typer.echo(
        "total_return_series_ready_for_strict_long_history_materialization="
        f"{str(result.total_return_series_ready_for_strict_long_history_materialization).lower()}"
    )
    typer.echo(f"blocker={result.blocker}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")


@app.command("verify-research-plan-stop-rule")
def verify_research_plan_stop_rule_cmd(
    contract_file: Annotated[
        Path,
        typer.Option("--contract-file", dir_okay=False),
    ] = Path("config/research/research-plan-stop-rule-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify the source-bound no-go and research moratorium contract."""
    from app.research.research_plan_stop_rule import verify_research_plan_stop_rule

    typer.echo(
        "Read-only plan-level stop-rule verification. It never restarts research, "
        "unlocks capital, scores, backtests, orders, connects to a broker, or trades."
    )
    try:
        contract = verify_research_plan_stop_rule(repo_root=repo_root or Path.cwd(), path=contract_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"contract_id={contract.contract_id}")
    typer.echo(f"deployment_decision={contract.deployment_decision}")
    typer.echo(
        "individual_stock_alpha_moratorium_ends_not_before="
        f"{contract.individual_stock_alpha_moratorium['ends_not_before']}"
    )
    typer.echo("prominent_manual_restart_confirmation_required=true")
    typer.echo("automatic_restart_forbidden=true")
    typer.echo("capital_unlock_authorized=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-index-etf-risk-budget-protocol")
def verify_index_etf_risk_budget_protocol_cmd(
    protocol_file: Annotated[
        Path,
        typer.Option("--protocol-file", dir_okay=False),
    ] = Path("config/research/index-etf-risk-budget-research-protocol-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify the design-only index ETF risk-budget protocol."""
    from app.research.index_etf_risk_budget_protocol import (
        verify_index_etf_risk_budget_protocol,
    )

    typer.echo(
        "Read-only sealed-protocol verification. This command does not restart research, "
        "replay history, consume OOS, select products, construct a portfolio, order, connect, or trade."
    )
    try:
        protocol = verify_index_etf_risk_budget_protocol(
            repo_root=repo_root or Path.cwd(), path=protocol_file
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={protocol.protocol_id}")
    typer.echo(f"status={protocol.status}")
    typer.echo(f"research_family_id={protocol.research_family['family_id']}")
    typer.echo("registered_dynamic_hypotheses=2")
    typer.echo("primary_endpoint=net_of_cost_calmar_difference_vs_best_static_grid_arm")
    typer.echo("implementation_complete=true")
    typer.echo("ready_for_authorized_historical_replay=true")
    typer.echo("historical_replay_authorized=false")
    typer.echo("prospective_evaluation_authorized=false")
    typer.echo("prominent_manual_run_confirmation_required=true")
    typer.echo("ready_for_trading=false")


@app.command("verify-index-product-cost-contract")
def verify_index_product_cost_contract_cmd(
    contract_file: Annotated[
        Path,
        typer.Option("--contract-file", dir_okay=False),
    ] = Path("config/research/index-research-product-cost-contract-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify the sealed index-proxy product boundary and cost envelope."""
    from app.research.index_research_product_cost_contract import (
        verify_index_research_product_cost_contract,
    )

    typer.echo(
        "Read-only index product/cost verification. Synthetic research penalties are not "
        "live product fees; this command never selects a product, orders, connects, or trades."
    )
    try:
        contract = verify_index_research_product_cost_contract(
            repo_root=repo_root or Path.cwd(), path=contract_file
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"contract_id={contract.contract_id}")
    typer.echo("exact_equity_linked_products_found=0")
    typer.echo("exact_defensive_linked_products_found=0")
    typer.echo("ready_for_index_level_historical_replay=true")
    typer.echo("ready_for_live_product_mapping=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-index-risk-budget-power-review")
def verify_index_risk_budget_power_review_cmd(
    review_file: Annotated[
        Path,
        typer.Option("--review-file", dir_okay=False),
    ] = Path("data/research/index-risk-budget-power-v1/review.json"),
    protocol_file: Annotated[
        Path,
        typer.Option("--protocol-file", dir_okay=False),
    ] = Path("config/research/index-risk-budget-power-protocol-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Recompute and verify the static-control-only MDE review."""
    from app.research.index_risk_budget_power import verify_index_risk_budget_power_review

    typer.echo(
        "Static-control-only power verification. Dynamic candidates are not loaded or evaluated; "
        "this is not OOS, scoring, backtesting, ordering, or trading."
    )
    try:
        review = verify_index_risk_budget_power_review(
            repo_root=repo_root or Path.cwd(),
            review_path=review_file,
            protocol_path=protocol_file,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"review_id={review.review_id}")
    typer.echo(f"calibration_observations={review.calibration_observations}")
    typer.echo(f"sealed_mde_calmar_difference={review.sealed_mde_calmar_difference:.8f}")
    typer.echo(f"family_outcome={review.family_outcome}")
    typer.echo("dynamic_candidate_returns_loaded=false")
    typer.echo("consumes_oos=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-index-risk-budget-run-readiness")
def verify_index_risk_budget_run_readiness_cmd(
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify every automatic prerequisite while leaving the manual run gate closed."""
    from app.research.defensive_leg_history import verify_official_defensive_leg_history
    from app.research.index_etf_risk_budget_protocol import (
        verify_index_etf_risk_budget_protocol,
    )
    from app.research.index_research_product_cost_contract import (
        verify_index_research_product_cost_contract,
    )
    from app.research.index_risk_budget_evaluator import load_verified_index_inputs
    from app.research.index_risk_budget_power import verify_index_risk_budget_power_review
    from app.research.index_time_series_trial_ledger import (
        verify_index_time_series_trial_ledger,
    )

    typer.echo(
        "Read-only full readiness verification. It does not create run authorization, replay "
        "candidates, consume OOS, score, construct a portfolio, order, connect, or trade."
    )
    root = repo_root or Path.cwd()
    try:
        protocol = verify_index_etf_risk_budget_protocol(repo_root=root)
        ledger = verify_index_time_series_trial_ledger(repo_root=root)
        defensive = verify_official_defensive_leg_history(repo_root=root)
        costs = verify_index_research_product_cost_contract(repo_root=root)
        power = verify_index_risk_budget_power_review(repo_root=root)
        _dates, _risk, _equity, _defensive, equity_snapshot_id, defensive_snapshot_id = (
            load_verified_index_inputs(repo_root=root)
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={protocol.protocol_id}")
    typer.echo(f"trial_ledger_id={ledger.ledger_id}")
    typer.echo(f"equity_snapshot_id={equity_snapshot_id}")
    typer.echo(f"defensive_snapshot_id={defensive_snapshot_id}")
    typer.echo(f"verified_defensive_snapshot_id={defensive['snapshot_id']}")
    typer.echo(f"product_cost_contract_id={costs.contract_id}")
    typer.echo(f"power_review_id={power.review_id}")
    typer.echo("automatic_prerequisites_complete=true")
    typer.echo("PROMINENT_MANUAL_CONFIRMATION_REQUIRED=true")
    typer.echo("ready_for_historical_replay=false")
    typer.echo("ready_for_live_product_mapping=false")
    typer.echo("ready_for_trading=false")


@app.command("run-index-risk-budget-historical-replay")
def run_index_risk_budget_historical_replay_cmd(
    authorization_file: Annotated[
        Path,
        typer.Option("--authorization-file", dir_okay=False),
    ] = Path("config/research/index-risk-budget-run-authorization-v1.json"),
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", file_okay=False),
    ] = Path("data/research/index-risk-budget-historical-replay-v1"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Run the single-use seen-history replay only after exact manual authorization."""
    from app.research.index_risk_budget_evaluator import (
        build_index_risk_budget_historical_replay,
    )

    typer.echo(
        "PROMINENT MANUAL CONFIRMATION GATE. This command is seen-history research only; "
        "it is not OOS, scoring, stock selection, a portfolio instruction, an order, or trading."
    )
    try:
        report = build_index_risk_budget_historical_replay(
            repo_root=repo_root or Path.cwd(),
            authorization_path=authorization_file,
            output_dir=output_dir,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    for item in report.candidate_comparisons:
        typer.echo(
            f"candidate={item.candidate_id} all_hard_gates_pass="
            f"{str(item.all_hard_gates_pass).lower()}"
        )
    typer.echo("oos_claim=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-index-risk-budget-closeout")
def verify_index_risk_budget_closeout_cmd(
    protocol_file: Annotated[
        Path,
        typer.Option("--protocol-file", dir_okay=False),
    ] = Path("config/research/index-risk-budget-closeout-protocol-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify the permanent family closure and unfunded annual-allocation policy."""
    from app.research.index_risk_budget_closeout import verify_index_risk_budget_closeout

    typer.echo(
        "Read-only closeout verification. This command does not reuse consumed history, "
        "restart research, select products, construct a portfolio, order, connect, or trade."
    )
    try:
        protocol = verify_index_risk_budget_closeout(
            repo_root=repo_root or Path.cwd(), path=protocol_file
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"closeout_id={protocol.closeout_id}")
    typer.echo(f"family_id={protocol.family_closure['family_id']}")
    typer.echo(f"outcome={protocol.family_closure['outcome']}")
    typer.echo("maximum_drawdown_utility_budget=-0.30")
    typer.echo("policy_starting_allocation=30_percent_equity_70_percent_defensive")
    typer.echo("rebalance_policy=annual_calendar_unvalidated")
    typer.echo("consumed_history_reuse_forbidden=true")
    typer.echo("capital_deployment_authorized=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-index-shadow-execution-protocol")
def verify_index_shadow_execution_protocol_cmd(
    protocol_file: Annotated[
        Path,
        typer.Option("--protocol-file", dir_okay=False),
    ] = Path("config/research/index-shadow-execution-protocol-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify the sealed shadow-only ETF mapping and execution boundary."""
    from app.research.index_shadow_execution import verify_index_shadow_execution_protocol

    typer.echo(
        "SHADOW ONLY. Read-only protocol verification; no capital, broker credentials, "
        "broker connection, order submission, or trading is authorized."
    )
    try:
        protocol = verify_index_shadow_execution_protocol(
            repo_root=repo_root or Path.cwd(), path=protocol_file
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={protocol.protocol_id}")
    typer.echo(f"equity_shadow_surrogate={protocol.product_mappings['equity'].symbol}")
    typer.echo(f"defensive_shadow_surrogate={protocol.product_mappings['defensive'].symbol}")
    typer.echo("exact_research_proxy_match=false")
    typer.echo("ready_for_shadow_initialization=true")
    typer.echo("capital_deployment_authorized=false")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")


@app.command("initialize-index-shadow-execution")
def initialize_index_shadow_execution_cmd(
    protocol_file: Annotated[
        Path,
        typer.Option("--protocol-file", dir_okay=False),
    ] = Path("config/research/index-shadow-execution-protocol-v1.json"),
    output_file: Annotated[
        Path,
        typer.Option("--output-file", dir_okay=False),
    ] = Path("data/shadow/index-risk-budget-shadow-v1/initialization-20260827.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Create a sealed hypothetical board-lot allocation from official close quotes."""
    from app.research.index_shadow_execution import materialize_index_shadow_initialization

    typer.echo(
        "⚠️ SHADOW ONLY: this writes a local hypothetical ledger artifact. It does not "
        "access broker credentials, connect to a broker, submit an order, deploy capital, or trade."
    )
    try:
        report = materialize_index_shadow_initialization(
            repo_root=repo_root or Path.cwd(),
            protocol_path=protocol_file,
            output_path=output_file,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"observed_at_cst={report.observed_at_cst.isoformat()}")
    for leg in report.legs:
        typer.echo(
            f"shadow_leg={leg.role} symbol={leg.symbol} quantity={leg.quantity} "
            f"assumed_fill_price={leg.assumed_fill_price} initial_weight={leg.initial_weight}"
        )
    typer.echo(f"residual_virtual_cash_cny={report.residual_virtual_cash_cny}")
    typer.echo(f"estimated_total_commission_cny={report.estimated_total_commission_cny}")
    typer.echo("capital_deployment_authorized=false")
    typer.echo("broker_connection_authorized=false")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")
    typer.echo(str(report.prominent_warning))


@app.command("verify-index-shadow-initialization")
def verify_index_shadow_initialization_cmd(
    report_file: Annotated[
        Path,
        typer.Option("--report-file", dir_okay=False),
    ] = Path("data/shadow/index-risk-budget-shadow-v1/initialization-20260827.json"),
    protocol_file: Annotated[
        Path,
        typer.Option("--protocol-file", dir_okay=False),
    ] = Path("config/research/index-shadow-execution-protocol-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify a shadow initialization against the sealed official inputs."""
    from app.research.index_shadow_execution import verify_index_shadow_initialization_report

    typer.echo(
        "SHADOW ONLY. Read-only report verification; no broker access, order, capital, or trading."
    )
    try:
        report = verify_index_shadow_initialization_report(
            repo_root=repo_root or Path.cwd(),
            report_path=report_file,
            protocol_path=protocol_file,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo("verification=passed")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")


@app.command("collect-csi-all-share-long-history")
def collect_csi_all_share_long_history_cmd(
    staging_dir: Annotated[
        Path,
        typer.Option("--staging-dir", file_okay=False),
    ] = Path("data/raw/csi-all-share-index-2005-2024-v1"),
    identity_contract: Annotated[
        Path,
        typer.Option("--identity-contract", dir_okay=False),
    ] = Path("config/research/csi-all-share-index-identity-v1.json"),
) -> None:
    """Collect hash-bound CSI/Tushare index history; never score, backtest, or trade."""
    from app.providers.csi_all_share_long_history import (
        LiveCSIHistoryBytesClient,
        collect_csi_all_share_long_history,
    )
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token

    typer.echo(
        "Historical index source collection only. This command reads the configured Tushare "
        "credential without printing it, fetches sealed CSI/Tushare sources, and writes a "
        "hash-bound raw collection. It does not materialize strategy inputs, score, backtest, "
        "trade, or connect to a broker."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        token = read_tushare_token()

        def report_progress(family: str, done: int, total: int, reused: bool) -> None:
            typer.echo(f"progress={done}/{total} source={family} partition={'reused' if reused else 'fetched'}")

        result = collect_csi_all_share_long_history(
            tushare_client=LiveTushareClient(token),
            csi_client=LiveCSIHistoryBytesClient(),
            repo_root=repo_root,
            staging_dir=staging_dir,
            identity_contract_path=identity_contract,
            progress=report_progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"collection_id={result.collection_id}")
    typer.echo(f"identity_contract_id={result.identity_contract_id}")
    typer.echo(f"raw_file_count={result.raw_file_count}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-csi-all-share-long-history-collection")
def verify_csi_all_share_long_history_collection_cmd(
    staging_dir: Annotated[
        Path,
        typer.Option("--staging-dir", exists=True, file_okay=False),
    ] = Path("data/raw/csi-all-share-index-2005-2024-v1"),
    identity_contract: Annotated[
        Path,
        typer.Option("--identity-contract", dir_okay=False),
    ] = Path("config/research/csi-all-share-index-identity-v1.json"),
) -> None:
    """Verify every raw source and manifest hash without network access."""
    from app.providers.csi_all_share_long_history import (
        verify_csi_all_share_long_history_collection,
    )

    typer.echo(
        "Offline CSI index collection verification only. This command does not read a token, "
        "use network access, materialize, score, backtest, or trade."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = verify_csi_all_share_long_history_collection(
            repo_root=repo_root,
            staging_dir=staging_dir,
            identity_contract_path=identity_contract,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"collection_id={result.collection_id}")
    typer.echo(f"identity_contract_id={result.identity_contract_id}")
    typer.echo(f"raw_file_count={result.raw_file_count}")
    typer.echo("verification=passed")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")


@app.command("materialize-csi-all-share-long-history")
def materialize_csi_all_share_long_history_cmd(
    staging_dir: Annotated[
        Path,
        typer.Option("--staging-dir", exists=True, file_okay=False),
    ] = Path("data/raw/csi-all-share-index-2005-2024-v1"),
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", file_okay=False),
    ] = Path("data/research/csi-all-share-index-2005-2024-v1"),
    identity_contract: Annotated[
        Path,
        typer.Option("--identity-contract", dir_okay=False),
    ] = Path("config/research/csi-all-share-index-identity-v1.json"),
) -> None:
    """Materialize the strict long-history index snapshot from verified raw sources."""
    from app.providers.csi_all_share_long_history import materialize_csi_all_share_long_history

    typer.echo(
        "Offline index materialization only. This command recomputes all source bindings and "
        "applies only the sealed two-date official override. It does not read a token, use "
        "network access, score, backtest, trade, or connect to a broker."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = materialize_csi_all_share_long_history(
            repo_root=repo_root,
            staging_dir=staging_dir,
            output_dir=output_dir,
            identity_contract_path=identity_contract,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"snapshot_id={result.snapshot_id}")
    typer.echo(f"collection_id={result.collection_id}")
    typer.echo(f"coverage_rows={result.calendar_rows}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-csi-all-share-long-history-snapshot")
def verify_csi_all_share_long_history_snapshot_cmd(
    staging_dir: Annotated[
        Path,
        typer.Option("--staging-dir", exists=True, file_okay=False),
    ] = Path("data/raw/csi-all-share-index-2005-2024-v1"),
    snapshot_dir: Annotated[
        Path,
        typer.Option("--snapshot-dir", exists=True, file_okay=False),
    ] = Path("data/research/csi-all-share-index-2005-2024-v1"),
    identity_contract: Annotated[
        Path,
        typer.Option("--identity-contract", dir_okay=False),
    ] = Path("config/research/csi-all-share-index-identity-v1.json"),
) -> None:
    """Recompute and verify the materialized snapshot from sealed raw sources."""
    from app.providers.csi_all_share_long_history import (
        verify_csi_all_share_long_history_snapshot,
    )

    typer.echo(
        "Offline full-recomputation CSI index snapshot verification only. This command does not "
        "read a token, use network access, score, backtest, or trade."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = verify_csi_all_share_long_history_snapshot(
            repo_root=repo_root,
            staging_dir=staging_dir,
            snapshot_dir=snapshot_dir,
            identity_contract_path=identity_contract,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"snapshot_id={result.snapshot_id}")
    typer.echo(f"collection_id={result.collection_id}")
    typer.echo(f"coverage_rows={result.calendar_rows}")
    typer.echo("verification=passed")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-layer-one-index-protocol")
def verify_layer_one_index_protocol_cmd(
    protocol_file: Annotated[
        Path,
        typer.Option(
            "--protocol-file",
            dir_okay=False,
            help="Layer-one index development protocol draft JSON (required; no silent default)",
        ),
    ],
    repo_root: Annotated[
        Path,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used to resolve and verify the bound research trial ledger",
        ),
    ],
) -> None:
    """Verify layer-one index development protocol; never score, backtest, or trade."""
    from app.research.layer_one_index_protocol import verify_layer_one_index_protocol_draft_file

    typer.echo(
        "Read-only layer-one index development protocol verification only. This command "
        "checks self-hash, bound research trial ledger path/id, bound two-layer decision "
        "contract path/id (schema v2), status, ready flags, pending_user_decision count, "
        "categorized evidence blockers, development/validation non-overlap, and "
        "consumed-OOS non-reuse. It does not invent economic defaults, open market data, "
        "score, backtest, or trade."
    )
    try:
        _draft, result = verify_layer_one_index_protocol_draft_file(
            protocol_path=protocol_file,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={result.protocol_id}")
    typer.echo(f"schema_version={result.schema_version}")
    typer.echo(f"protocol_version={result.protocol_version}")
    typer.echo(f"status={result.status}")
    typer.echo(f"research_trial_ledger_path={result.research_trial_ledger_path}")
    typer.echo(f"research_trial_ledger_id={result.research_trial_ledger_id}")
    typer.echo(f"research_trial_ledger_binding_ok={str(result.research_trial_ledger_binding_ok).lower()}")
    typer.echo(f"two_layer_decision_contract_path={result.two_layer_decision_contract_path}")
    typer.echo(f"two_layer_decision_contract_id={result.two_layer_decision_contract_id}")
    typer.echo(f"two_layer_decision_contract_binding_ok={str(result.two_layer_decision_contract_binding_ok).lower()}")
    typer.echo(f"user_decisions_resolved={str(result.user_decisions_resolved).lower()}")
    typer.echo(f"pending_user_decision_count={result.pending_user_decision_count}")
    typer.echo(f"resolved={str(result.resolved).lower()}")
    typer.echo(f"blocker_count={len(result.blockers)}")
    for blocker in result.blockers:
        typer.echo(f"blocker={blocker}")
    for evidence in result.evidence_blockers:
        typer.echo(f"evidence_blocker={evidence.category}:{evidence.path}")
    typer.echo(f"windows_overlap={str(result.windows_overlap).lower()}")
    typer.echo(f"consumed_oos_reuse_check_ok={str(result.consumed_oos_reuse_check_ok).lower()}")
    typer.echo("consumed_oos_reuse_forbidden=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")
    typer.echo("does_not_score=true")
    typer.echo("does_not_backtest=true")
    typer.echo("does_not_trade=true")


@app.command("verify-tranche-evaluation-protocol")
def verify_tranche_evaluation_protocol_cmd(
    protocol_file: Annotated[
        Path,
        typer.Option(
            "--protocol-file",
            dir_okay=False,
            help="Tranche evaluation protocol draft JSON (required; no silent default)",
        ),
    ],
    repo_root: Annotated[
        Path,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used to resolve and verify bound ledger/contracts",
        ),
    ],
) -> None:
    """Verify tranche evaluation protocol; never score, backtest, or trade."""
    from app.research.tranche_evaluation_protocol import verify_tranche_evaluation_protocol_draft_file

    typer.echo(
        "Read-only tranche evaluation protocol verification only. This command "
        "checks self-hash, structural consistency, bound research trial ledger path/id, "
        "bound two-layer contract path/id, bound layer-one protocol path/id, status, "
        "user_decisions_resolved vs overall resolved, categorized evidence blockers, "
        "window non-overlap, and consumed-OOS non-reuse. It does not invent "
        "tranche_count=40 as an active count, open market data, score, backtest, or trade."
    )
    try:
        _draft, result = verify_tranche_evaluation_protocol_draft_file(
            protocol_path=protocol_file,
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"protocol_id={result.protocol_id}")
    typer.echo(f"schema_version={result.schema_version}")
    typer.echo(f"protocol_version={result.protocol_version}")
    typer.echo(f"status={result.status}")
    typer.echo(f"structural_ok={str(result.structural_ok).lower()}")
    typer.echo(f"research_trial_ledger_path={result.research_trial_ledger_path}")
    typer.echo(f"research_trial_ledger_id={result.research_trial_ledger_id}")
    typer.echo(f"research_trial_ledger_binding_ok={str(result.research_trial_ledger_binding_ok).lower()}")
    typer.echo(f"two_layer_decision_contract_path={result.two_layer_decision_contract_path}")
    typer.echo(f"two_layer_decision_contract_id={result.two_layer_decision_contract_id}")
    typer.echo(f"two_layer_decision_contract_binding_ok={str(result.two_layer_decision_contract_binding_ok).lower()}")
    typer.echo(f"layer_one_index_protocol_path={result.layer_one_index_protocol_path}")
    typer.echo(f"layer_one_index_protocol_id={result.layer_one_index_protocol_id}")
    typer.echo(f"layer_one_index_protocol_binding_ok={str(result.layer_one_index_protocol_binding_ok).lower()}")
    typer.echo(f"user_decisions_resolved={str(result.user_decisions_resolved).lower()}")
    typer.echo(f"pending_user_decision_count={result.pending_user_decision_count}")
    typer.echo(f"resolved={str(result.resolved).lower()}")
    typer.echo(f"blocker_count={len(result.blockers)}")
    for blocker in result.blockers:
        typer.echo(f"blocker={blocker}")
    for evidence in result.evidence_blockers:
        typer.echo(f"evidence_blocker={evidence.category}:{evidence.path}")
    typer.echo(f"windows_overlap={str(result.windows_overlap).lower()}")
    typer.echo(f"consumed_oos_reuse_check_ok={str(result.consumed_oos_reuse_check_ok).lower()}")
    typer.echo("consumed_oos_reuse_forbidden=true")
    typer.echo(f"confirmed_initial_cash={result.confirmed_initial_cash}")
    typer.echo("initial_cash_is_blocker=false")
    typer.echo("research_only=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")
    typer.echo("does_not_score=true")
    typer.echo("does_not_backtest=true")
    typer.echo("does_not_trade=true")


@app.command("verify-rolling-tranche-schedule-report")
def verify_rolling_tranche_schedule_report_cmd(
    report_file: Annotated[
        Path,
        typer.Option(
            "--report-file",
            dir_okay=False,
            help="Sealed rolling-tranche schedule diagnostic JSON (required local fixture/file)",
        ),
    ],
) -> None:
    """Verify a sealed rolling-tranche schedule report; never loads market data or trades."""
    from app.research.rolling_tranche_schedule import verify_rolling_tranche_schedule_report_file

    typer.echo(
        "Read-only rolling-tranche schedule report verification only. This command checks "
        "self-hash, calendar/window consistency, and fixed diagnostic gate flags on an "
        "explicitly supplied local JSON file. It does not open repository market snapshots, "
        "invent N/H, score, backtest, or trade."
    )
    try:
        report = verify_rolling_tranche_schedule_report_file(report_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"start={report.start.isoformat()}")
    typer.echo(f"end={report.end.isoformat()}")
    typer.echo(f"tranche_count={report.tranche_count}")
    typer.echo(f"holding_period_bars={report.holding_period_bars}")
    typer.echo(f"initial_capital={report.initial_capital}")
    typer.echo(f"per_tranche_capital={report.per_tranche_capital}")
    typer.echo(f"total_scheduled_decisions={report.total_scheduled_decisions}")
    typer.echo(f"warm_up_day_count={report.warm_up_day_count}")
    typer.echo(f"tail_effect_day_count={report.tail_effect_day_count}")
    typer.echo("diagnostic_only=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")


@app.command("verify-index-risk-feature-report")
def verify_index_risk_feature_report_cmd(
    report_file: Annotated[
        Path,
        typer.Option(
            "--report-file",
            dir_okay=False,
            help="Sealed index risk feature diagnostic JSON (required local fixture/file)",
        ),
    ],
) -> None:
    """Verify a sealed index risk feature report; never loads market data or trades."""
    from app.research.index_risk_features import verify_index_risk_feature_report_file

    typer.echo(
        "Read-only index risk feature report verification only. This command checks "
        "self-hash and fixed diagnostic gate flags on an explicitly supplied local "
        "JSON file. It does not open repository market snapshots, invent lookbacks, "
        "score, backtest, or trade."
    )
    try:
        report = verify_index_risk_feature_report_file(report_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo(f"index_symbol={report.index_symbol}")
    typer.echo(f"as_of={report.as_of.isoformat()}")
    typer.echo(f"trend_lookback_bars={report.trend_lookback_bars}")
    typer.echo(f"volatility_lookback_bars={report.volatility_lookback_bars}")
    typer.echo(f"drawdown_lookback_bars={report.drawdown_lookback_bars}")
    typer.echo(f"latest_close={report.latest_close}")
    typer.echo(f"close_to_sma_ratio={report.close_to_sma_ratio}")
    typer.echo(f"realized_volatility_annualized={report.realized_volatility_annualized}")
    typer.echo(f"drawdown={report.drawdown}")
    typer.echo("diagnostic_only=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")


@app.command("verify-layer-one-index-data-evidence")
def verify_layer_one_index_data_evidence_cmd(
    evidence_file: Annotated[
        Path,
        typer.Option(
            "--evidence-file",
            dir_okay=False,
            help="Sealed layer-one CSI All-Share data evidence JSON",
        ),
    ] = Path("config/research/layer-one-index-data-evidence-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used for full raw/snapshot/contract disk verification",
        ),
    ] = None,
) -> None:
    """Verify the sealed CSI All-Share data evidence; never scores or trades."""
    from app.research.layer_one_index_data_evidence import (
        verify_layer_one_index_data_evidence_file,
    )

    typer.echo(
        "Read-only layer-one index data evidence verification. This recomputes the "
        "materialized snapshot from hash-bound raw sources and verifies exact index "
        "identity plus the historical stamp-tax contract. It does not score stocks, "
        "place orders, connect to a broker, or trade."
    )
    try:
        root = repo_root or Path.cwd()
        evidence = verify_layer_one_index_data_evidence_file(
            evidence_path=evidence_file,
            repo_root=root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"evidence_id={evidence.evidence_id}")
    typer.echo(f"snapshot_id={evidence.snapshot_manifest.artifact_id}")
    typer.echo(f"risk_state_symbol={evidence.risk_state_index.symbol}")
    typer.echo(f"performance_benchmark_symbol={evidence.performance_benchmark.symbol}")
    typer.echo(f"coverage={evidence.risk_state_index.coverage_start}..{evidence.risk_state_index.coverage_end}")
    typer.echo(f"calendar_rows={evidence.calendar_rows}")
    typer.echo("exact_symbol_identity_verified=true")
    typer.echo("snapshot_full_raw_recomputation_verified=true")
    typer.echo("ready_for_layer_one_historical_evaluation=true")
    typer.echo("ready_for_stock_scoring=false")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")


@app.command("verify-layer-one-regime-decision")
def verify_layer_one_regime_decision_cmd(
    decision_file: Annotated[
        Path,
        typer.Option(
            "--decision-file",
            dir_okay=False,
            help="Sealed layer-one regime decision JSON (local file only)",
        ),
    ],
    repo_root: Annotated[
        Path | None,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root used to bind upstream two-layer / layer-one protocol files",
        ),
    ] = None,
) -> None:
    """Verify a sealed layer-one regime decision; never loads market data or trades."""
    from app.research.layer_one_regime import verify_layer_one_regime_decision_file

    typer.echo(
        "Read-only layer-one regime decision verification only. This command checks "
        "self-hash, embedded sealed index-risk feature report binding, offline "
        "recomputation of derived caps/lock/unlock/new_state from that report, "
        "calendar and evidence id bindings, fixed research/implementation gate flags, "
        "and upstream disk contract/protocol id bindings on an explicitly supplied "
        "local JSON file. It does not open market snapshots, score, backtest, or trade."
    )
    try:
        resolved_root = repo_root or Path.cwd()
        report = verify_layer_one_regime_decision_file(decision_file, repo_root=resolved_root)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"decision_id={report.decision_id}")
    typer.echo(f"target_trading_day={report.target_trading_day.isoformat()}")
    typer.echo(f"as_of={report.as_of.isoformat()}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo(f"index_risk_feature_report_id={report.index_risk_feature_report_id}")
    typer.echo(f"market_calendar_id={report.market_calendar_id}")
    typer.echo(f"account_equity_evidence_id={report.account_equity_evidence_id}")
    typer.echo(f"manual_ceiling_authorization_id={report.manual_ceiling_authorization_id}")
    typer.echo(f"prior_state_id={report.prior_state_id}")
    typer.echo(f"applied_stock_budget={report.applied_stock_budget}")
    typer.echo(f"raw_target_budget={report.raw_target_budget}")
    typer.echo(f"risk_lock_new_active={str(report.risk_lock_new_active).lower()}")
    typer.echo(f"red_line_breached={str(report.red_line_breached).lower()}")
    typer.echo(f"two_layer_decision_contract_id={report.two_layer_decision_contract_id}")
    typer.echo(f"layer_one_index_protocol_id={report.layer_one_index_protocol_id}")
    typer.echo("research_only=true")
    typer.echo("implementation_only=true")
    typer.echo("exact_symbol_identity_verified=true")
    typer.echo(f"layer_one_index_data_evidence_id={report.layer_one_index_data_evidence_id}")
    typer.echo("snapshot_full_raw_recomputation_verified=true")
    typer.echo("ready_for_historical_evaluation=true")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")
    typer.echo("does_not_trade=true")


@app.command("verify-account-execution-diagnostic-report-integrity")
def verify_account_execution_diagnostic_report_integrity_cmd(
    report_file: Annotated[
        Path,
        typer.Option(
            "--report-file",
            dir_okay=False,
            help="Sealed account-execution diagnostic JSON (required local fixture/file)",
        ),
    ],
) -> None:
    """Integrity-only verify of an account-execution report; never loads BacktestResult."""
    from app.research.account_execution_diagnostics import (
        verify_account_execution_diagnostic_report_integrity_only,
    )

    typer.echo(
        "Read-only account-execution diagnostic integrity verification only. This command "
        "checks self-hash, sealed-field consistency, and fixed diagnostic gate flags on an "
        "explicitly supplied local JSON file. It is integrity-only: it does not embed or "
        "recompute from a BacktestResult, open repository market snapshots, score, backtest, "
        "or trade. Full economic validation requires diagnose_account_execution on the source "
        "result."
    )
    try:
        report = verify_account_execution_diagnostic_report_integrity_only(report_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"source_result_hash={report.source_result_hash}")
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo(f"closed_trade_count={report.closed_trade_count}")
    typer.echo(f"file_verifier_scope={report.file_verifier_scope}")
    typer.echo("diagnostic_only=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")


@app.command("run-layer-one-historical-validation")
def run_layer_one_historical_validation_cmd(
    repo_root: Annotated[
        Path | None,
        typer.Option(
            "--repo-root",
            file_okay=False,
            help="Repository root containing the frozen contracts and index snapshot",
        ),
    ] = None,
) -> None:
    """Build the frozen 2013-2021 layer-one historical evidence; never OOS/trading."""
    from app.research.layer_one_historical_validation import (
        write_layer_one_historical_validation,
    )

    typer.echo(
        "Historical validation only (2013-2021; not OOS). This command fully verifies "
        "the sealed CSI All-Share sources, replays the frozen layer-one budget with "
        "no invented manual unlock, and writes read-only evidence. It does not score "
        "stocks, run consumed/new OOS, place orders, connect to a broker, or trade."
    )
    try:
        root = repo_root or Path.cwd()
        report = write_layer_one_historical_validation(repo_root=root)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo(f"window={report.validation_start}..{report.validation_end}")
    typer.echo(f"daily_rows={report.daily_row_count}")
    typer.echo(f"combined_annualized_return={report.combined.annualized_return_after_cost:.8f}")
    typer.echo(f"combined_max_drawdown={report.combined.max_drawdown:.8f}")
    typer.echo(f"combined_calmar={report.combined.calmar}")
    typer.echo(f"risk_lock_trigger_dates={','.join(day.isoformat() for day in report.risk_lock_trigger_dates)}")
    typer.echo(f"all_hard_gates_pass={str(report.gates.all_hard_gates_pass).lower()}")
    typer.echo("historical_validation_only=true")
    typer.echo("oos_claim=false")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-layer-one-risk-lock-recovery-policy")
def verify_layer_one_risk_lock_recovery_policy_cmd(
    policy_file: Annotated[
        Path,
        typer.Option("--policy-file", dir_okay=False),
    ] = Path("config/research/layer-one-risk-lock-recovery-policy-v1.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Verify the audited risk-lock recovery overlay; never unlocks a live state."""
    from app.research.layer_one_risk_lock_recovery_policy import verify_policy_file

    typer.echo(
        "Read-only risk-lock recovery policy verification. This command never changes "
        "risk state, resets account equity, scores, places orders, connects to a broker, or trades."
    )
    try:
        root = repo_root or Path.cwd()
        policy = verify_policy_file(repo_root=root, policy_path=policy_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"policy_id={policy.policy_id}")
    typer.echo("explicit_user_confirmation_required=true")
    typer.echo("new_epoch_peak_equals_current_equity=true")
    typer.echo("first_reentry_budget_cap=0.3")
    typer.echo("pre_reset_peak_and_drawdown_remain_in_audit=true")
    typer.echo("auto_clear_forbidden=true")
    typer.echo("ready_for_orders=false")
    typer.echo("ready_for_trading=false")


@app.command("run-layer-one-recovery-counterfactual")
def run_layer_one_recovery_counterfactual_cmd(
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Evaluate the sealed recovery overlay on 2013-2021; never changes live state."""
    from app.research.layer_one_recovery_counterfactual import (
        write_recovery_counterfactual,
    )

    typer.echo(
        "Historical counterfactual only (2013-2021; not OOS). Explicit confirmations "
        "are simulated under the sealed recovery policy and are not real user actions. "
        "This command never changes live state, scores stocks, backtests a stock strategy, "
        "places orders, connects to a broker, or trades."
    )
    try:
        report = write_recovery_counterfactual(repo_root=repo_root or Path.cwd())
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"window={report.validation_start}..{report.validation_end}")
    typer.echo(f"simulated_confirmations={report.simulated_confirmation_count}")
    typer.echo(f"combined_annualized_return={report.combined.annualized_return_after_cost:.8f}")
    typer.echo(f"combined_max_drawdown={report.combined.max_drawdown:.8f}")
    typer.echo(f"combined_calmar={report.combined.calmar}")
    typer.echo(f"all_hard_gates_pass={str(report.gates.all_hard_gates_pass).lower()}")
    typer.echo("simulated_confirmation_is_not_observed_user_action=true")
    typer.echo("oos_claim=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-layer-one-recovery-counterfactual")
def verify_layer_one_recovery_counterfactual_cmd(
    report_file: Annotated[
        Path,
        typer.Option("--report-file", dir_okay=False),
    ] = Path("data/research/layer-one-recovery-counterfactual-v1/report.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Fully recompute the sealed recovery counterfactual."""
    from app.research.layer_one_recovery_counterfactual import (
        verify_recovery_counterfactual_file,
    )

    typer.echo(
        "Read-only full recomputation of the 2013-2021 recovery counterfactual. "
        "It does not mutate live state, consume OOS, score, order, connect, or trade."
    )
    try:
        report = verify_recovery_counterfactual_file(repo_root=repo_root or Path.cwd(), report_path=report_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"daily_table_content_sha256={report.daily_table_content_sha256}")
    typer.echo(f"all_hard_gates_pass={str(report.gates.all_hard_gates_pass).lower()}")
    typer.echo("full_disk_recomputation=passed")
    typer.echo("oos_claim=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-layer-one-historical-validation")
def verify_layer_one_historical_validation_cmd(
    report_file: Annotated[
        Path,
        typer.Option("--report-file", dir_okay=False),
    ] = Path("data/research/layer-one-historical-validation-v1/report.json"),
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", file_okay=False),
    ] = None,
) -> None:
    """Full disk recomputation of the frozen layer-one historical evidence."""
    from app.research.layer_one_historical_validation import (
        verify_layer_one_historical_validation_file,
    )

    typer.echo(
        "Read-only full recomputation of 2013-2021 layer-one historical validation. "
        "This is not OOS and does not score, place orders, connect to a broker, or trade."
    )
    try:
        root = repo_root or Path.cwd()
        report = verify_layer_one_historical_validation_file(
            repo_root=root,
            report_path=report_file,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"daily_table_content_sha256={report.daily_table_content_sha256}")
    typer.echo(f"daily_rows={report.daily_row_count}")
    typer.echo(f"all_hard_gates_pass={str(report.gates.all_hard_gates_pass).lower()}")
    typer.echo("full_disk_recomputation=passed")
    typer.echo("oos_claim=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-candidate-lot-affordability-report")
def verify_candidate_lot_affordability_report_cmd(
    report_file: Annotated[
        Path,
        typer.Option(
            "--report-file",
            dir_okay=False,
            help="Sealed candidate-lot affordability diagnostic JSON (required local fixture/file)",
        ),
    ],
) -> None:
    """Verify a sealed candidate-lot affordability report by recomputing from embedded inputs."""
    from app.research.account_execution_diagnostics import (
        verify_candidate_lot_affordability_report_file,
    )

    typer.echo(
        "Read-only candidate-lot affordability report verification only. This command checks "
        "self-hash, recomputes diagnose() from embedded candidate inputs, and fixed diagnostic "
        "gate flags on an explicitly supplied local JSON file. It does not open repository "
        "market snapshots, universes, score, backtest, or trade."
    )
    try:
        report = verify_candidate_lot_affordability_report_file(report_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"candidate_count={len(report.candidates)}")
    typer.echo(f"cash_per_slice={report.cash_per_slice}")
    typer.echo(f"lot_size={report.lot_size}")
    typer.echo("diagnostic_only=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_apply=false")


@app.command("evaluate-all-a-share-portfolio-oos-one-shot")
def evaluate_all_a_share_portfolio_oos_one_shot_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    authorization_file: Annotated[
        Path | None,
        typer.Option(
            "--authorization-file",
            dir_okay=False,
            help="Sealed one-shot portfolio OOS authorization JSON",
        ),
    ] = None,
    freeze_file: Annotated[
        Path | None,
        typer.Option("--freeze-file", dir_okay=False, help="Frozen portfolio OOS protocol JSON"),
    ] = None,
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    fundamental_dir: Annotated[Path | None, typer.Option("--fundamental-dir", file_okay=False)] = None,
) -> None:
    """Run the authorized one-shot 2025+ p10_h20 portfolio OOS evaluation; never deploy."""
    from app.research.portfolio_oos_authorization import (
        DEFAULT_PORTFOLIO_OOS_AUTH_PATH,
        assert_committed_authorization_bindings,
        load_verified_portfolio_oos_authorization,
    )
    from app.research.portfolio_oos_evaluation import (
        evaluate_and_write_portfolio_oos_one_shot,
    )
    from app.research.portfolio_oos_freeze import (
        assert_committed_portfolio_oos_freeze_bindings,
        verify_portfolio_oos_freeze,
    )

    typer.echo(
        "Authorized one-shot 2025+ portfolio OOS evaluation only. This command does not "
        "mutate the authorization contract, does not overwrite prior output/receipt, does "
        "not connect to a broker or network token, and authorizes no auto scoring, paper "
        "trading, live trading, p-value, or IC claim."
    )
    try:
        resolved_auth = authorization_file or DEFAULT_PORTFOLIO_OOS_AUTH_PATH
        authorization = load_verified_portfolio_oos_authorization(resolved_auth)
        assert_committed_authorization_bindings(authorization)
        resolved_freeze = freeze_file or Path(authorization.freeze_file)
        if freeze_file is not None and Path(freeze_file) != Path(authorization.freeze_file):
            if Path(freeze_file).resolve() != (Path.cwd() / authorization.freeze_file).resolve():
                raise ValueError("--freeze-file does not match the authorization freeze_file")
        freeze = verify_portfolio_oos_freeze(freeze_path=resolved_freeze)
        assert_committed_portfolio_oos_freeze_bindings(freeze)
        resolved_market = market_dir or Path(authorization.market_dir)
        if market_dir is not None:
            expected_market = Path(authorization.market_dir)
            if (
                market_dir.resolve() != expected_market.resolve()
                and market_dir.resolve() != (Path.cwd() / expected_market).resolve()
            ):
                raise ValueError("--market-dir does not match the authorization market_dir")
        resolved_fundamental = fundamental_dir or Path(authorization.fundamental_dir)
        if fundamental_dir is not None:
            expected_fundamental = Path(authorization.fundamental_dir)
            if fundamental_dir.resolve() != expected_fundamental.resolve() and (
                fundamental_dir.resolve() != (Path.cwd() / expected_fundamental).resolve()
            ):
                raise ValueError("--fundamental-dir does not match the authorization fundamental_dir")
        if strategy != authorization.strategy_config_id:
            raise ValueError("--strategy does not match the authorization strategy_config_id")
        strategy_path = Path(authorization.strategy_path)
        if not strategy_path.is_file():
            strategy_path = Path.cwd() / authorization.strategy_path

        def progress(stage: str, done: int, total: int) -> None:
            typer.echo(f"progress={done}/{total} stage={stage}")

        report, receipt, destination = evaluate_and_write_portfolio_oos_one_shot(
            authorization=authorization,
            freeze_path=resolved_freeze,
            strategy_path=strategy_path,
            market_dir=resolved_market,
            fundamental_dir=resolved_fundamental,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"evaluation_version={report.evaluation_version}")
    typer.echo(f"authorization_id={report.authorization_id}")
    typer.echo(f"freeze_id={report.freeze_id}")
    typer.echo(f"frozen_config_hash={report.frozen_config_hash}")
    typer.echo(f"runtime_config_hash={report.runtime_config_hash}")
    typer.echo(f"market_snapshot_id={report.market_snapshot_id}")
    typer.echo(f"fundamental_snapshot_id={report.fundamental_snapshot_id}")
    typer.echo(f"composite_store_snapshot_id={report.composite_store_snapshot_id}")
    typer.echo(f"evaluation_window={report.evaluation_start}..{report.evaluation_end}")
    typer.echo(f"signal_cutoff={report.signal_cutoff}")
    typer.echo(f"preflight_passed={str(report.preflight_passed).lower()}")
    typer.echo(f"outcome={report.outcome}")
    typer.echo(f"outcome_reason={report.outcome_reason}")
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"receipt_id={receipt.receipt_id}")
    typer.echo("one_shot=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("human_review_required=true")
    typer.echo(f"output={destination}")


@app.command("verify-all-a-share-portfolio-oos-one-shot")
def verify_all_a_share_portfolio_oos_one_shot_cmd(
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", file_okay=False, help="Sealed one-shot evaluation directory"),
    ] = None,
    receipt_file: Annotated[
        Path | None,
        typer.Option(
            "--receipt-file",
            dir_okay=False,
            help="Sealed one-shot consumption receipt JSON",
        ),
    ] = None,
    authorization_file: Annotated[
        Path | None,
        typer.Option(
            "--authorization-file",
            dir_okay=False,
            help="Committed one-shot portfolio OOS authorization JSON",
        ),
    ] = None,
    freeze_file: Annotated[
        Path | None,
        typer.Option("--freeze-file", dir_okay=False, help="Committed portfolio OOS freeze JSON"),
    ] = None,
) -> None:
    """Verify a sealed portfolio OOS one-shot report+receipt; never execute evaluation."""
    from app.research.portfolio_oos_authorization import (
        AUTHORIZED_OUTPUT_DIR,
        AUTHORIZED_RECEIPT_PATH,
        DEFAULT_PORTFOLIO_OOS_AUTH_PATH,
    )
    from app.research.portfolio_oos_evaluation import verify_sealed_portfolio_oos_one_shot
    from app.research.portfolio_oos_freeze import DEFAULT_PORTFOLIO_OOS_FREEZE_PATH

    typer.echo(
        "Read-only portfolio OOS one-shot verification only. This command loads the "
        "committed authorization and freeze, may replay preflight read-only against the "
        "authorized store, recomputes output hashes, rebuilds scenario summaries from "
        "sealed BacktestResult files, and recomputes the full gate list. It does not run "
        "score, backtest, or trade."
    )
    try:
        resolved_output = output_dir or Path(AUTHORIZED_OUTPUT_DIR)
        resolved_receipt = receipt_file or Path(AUTHORIZED_RECEIPT_PATH)
        resolved_auth = authorization_file or DEFAULT_PORTFOLIO_OOS_AUTH_PATH
        resolved_freeze = freeze_file or DEFAULT_PORTFOLIO_OOS_FREEZE_PATH
        report, receipt = verify_sealed_portfolio_oos_one_shot(
            output_dir=resolved_output,
            receipt_path=resolved_receipt,
            authorization_path=resolved_auth,
            freeze_path=resolved_freeze,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"evaluation_version={report.evaluation_version}")
    typer.echo(f"authorization_id={report.authorization_id}")
    typer.echo(f"freeze_id={report.freeze_id}")
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"receipt_id={receipt.receipt_id}")
    typer.echo(f"outcome={report.outcome}")
    typer.echo("one_shot=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("human_review_required=true")
    typer.echo("verified=true")


@app.command("evaluate-a-share-event-candidate-oos-one-shot")
def evaluate_a_share_event_candidate_oos_one_shot_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    authorization_file: Annotated[
        Path | None,
        typer.Option(
            "--authorization-file",
            dir_okay=False,
            help="Sealed one-shot OOS authorization JSON",
        ),
    ] = None,
    freeze_file: Annotated[
        Path | None,
        typer.Option("--freeze-file", dir_okay=False, help="Frozen research protocol JSON"),
    ] = None,
    event_dir: Annotated[Path | None, typer.Option("--event-dir", file_okay=False)] = None,
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
) -> None:
    """Run the authorized one-shot 2025+ event-candidate OOS diagnostic; never score or trade."""
    from app.research.event_candidate_oos_authorization import (
        DEFAULT_EVENT_CANDIDATE_OOS_AUTH_PATH,
        assert_committed_authorization_bindings,
        load_verified_event_candidate_oos_authorization,
    )
    from app.research.event_candidate_oos_evaluation import (
        evaluate_and_write_event_candidate_oos_one_shot,
    )

    typer.echo(
        "Authorized one-shot 2025+ OOS directional replication only. This command does not "
        "mutate the authorization contract, does not overwrite prior output/receipt, and "
        "authorizes no score, IC, exclusion, portfolio, order, trade, p-value, or alpha claim."
    )
    try:
        settings = get_settings()
        resolved_auth = authorization_file or DEFAULT_EVENT_CANDIDATE_OOS_AUTH_PATH
        authorization = load_verified_event_candidate_oos_authorization(resolved_auth)
        assert_committed_authorization_bindings(authorization)
        resolved_freeze = freeze_file or Path(authorization.freeze_file)
        if freeze_file is not None and Path(freeze_file) != Path(authorization.freeze_file):
            if Path(freeze_file).resolve() != (Path.cwd() / authorization.freeze_file).resolve():
                raise ValueError("--freeze-file does not match the authorization freeze_file")
        resolved_market = market_dir or Path(authorization.market_dir)
        if market_dir is not None:
            expected_market = Path(authorization.market_dir)
            if (
                market_dir.resolve() != expected_market.resolve()
                and market_dir.resolve() != (Path.cwd() / expected_market).resolve()
            ):
                raise ValueError("--market-dir does not match the authorization market_dir")
        resolved_event = event_dir or Path(authorization.event_dir)
        if event_dir is not None:
            expected_event = Path(authorization.event_dir)
            if (
                event_dir.resolve() != expected_event.resolve()
                and event_dir.resolve() != (Path.cwd() / expected_event).resolve()
            ):
                raise ValueError("--event-dir does not match the authorization event_dir")
        if strategy != authorization.strategy_config_id:
            raise ValueError("--strategy does not match the authorization strategy_config_id")
        config = load_strategy_config(strategy, settings.strategies_dir)
        report, receipt, destination = evaluate_and_write_event_candidate_oos_one_shot(
            authorization=authorization,
            freeze_path=resolved_freeze,
            market_dir=resolved_market,
            event_dir=resolved_event,
            config=config,
            strategy_config_id=strategy,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"evaluation_version={report.evaluation_version}")
    typer.echo(f"authorization_id={report.authorization_id}")
    typer.echo(f"freeze_id={report.freeze_id}")
    typer.echo(f"market_snapshot_id={report.market_snapshot_id}")
    typer.echo(f"event_snapshot_id={report.event_snapshot_id}")
    typer.echo(f"announcement_window={report.announcement_window_start}..{report.announcement_window_end}")
    typer.echo(f"label_hard_end={report.label_hard_end}")
    typer.echo(f"benchmark_symbol={report.benchmark_symbol}")
    typer.echo(f"candidate_multiplicity={report.candidate_multiplicity}")
    typer.echo(f"observation_rows={report.observation_rows}")
    typer.echo(f"candidate_summary_rows={report.candidate_summary_rows}")
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"receipt_id={receipt.receipt_id}")
    for hypothesis_id, outcome in report.candidate_outcomes.items():
        typer.echo(f"hypothesis={hypothesis_id} outcome={outcome}")
    typer.echo("one_shot=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo("auto_deploy=false")
    typer.echo("human_review_required=true")
    typer.echo(f"output={destination}")


@app.command("review-a-share-event-overlay")
def review_a_share_event_overlay_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    start: Annotated[str, typer.Option("--start", help="inclusive window start YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="inclusive window end YYYY-MM-DD")],
    source_collection_dir: Annotated[
        Path,
        typer.Option(
            "--source-collection-dir",
            file_okay=False,
            help=(
                "Verified offline collection directory containing collection_manifest.json, "
                "source_manifest.json, and quality_report.json"
            ),
        ),
    ],
    event_dir: Annotated[Path | None, typer.Option("--event-dir", file_okay=False)] = None,
    market_dir: Annotated[Path | None, typer.Option("--market-dir", file_okay=False)] = None,
    output_dir: Annotated[Path | None, typer.Option("--output-dir", file_okay=False)] = None,
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Build an offline coverage/PIT review of a verified event overlay; never score or trade."""
    from app.models.events import EventSourceManifest
    from app.research.event_overlay_review import (
        build_event_overlay_review,
        write_event_overlay_review_atomically,
    )
    from app.storage.event_io import load_verified_event_snapshot
    from app.storage.snapshot_io import load_verified_snapshot

    typer.echo(
        "Offline event-overlay review only. Output covers coverage, revisions, missingness, "
        "audit-text distribution, and PIT availability; it authorizes no risk rule, score, "
        "exclusion, order, trade, or alpha claim. Raw source missingness comes from the "
        "verified collector quality_report, never inferred from the canonical overlay."
    )
    try:
        settings = get_settings()
        window_start = date.fromisoformat(start)
        window_end = date.fromisoformat(end)
        resolved_market = market_dir or settings.parquet_dir
        resolved_event = event_dir or settings.event_dir
        if resolved_event is None:
            raise ValueError("set AIQ_EVENT_DIR or pass --event-dir")
        market = load_verified_snapshot(resolved_market)
        event_snapshot, tables = load_verified_event_snapshot(
            resolved_event,
            expected_market_snapshot_id=market.snapshot_id,
        )
        event_source_bytes = (resolved_event / "source_manifest.json").read_bytes()
        if hashlib.sha256(event_source_bytes).hexdigest() != event_snapshot.source_manifest_sha256:
            raise ValueError("event source manifest changed during review loading")
        event_source_manifest = EventSourceManifest.model_validate_json(event_source_bytes)
        config = load_strategy_config(strategy, settings.strategies_dir)
        report, annual = build_event_overlay_review(
            market_dir=resolved_market,
            event_snapshot=event_snapshot,
            event_source_manifest=event_source_manifest,
            event_tables=tables,
            config=config,
            window_start=window_start,
            window_end=window_end,
            source_collection_dir=source_collection_dir,
        )
        destination = output_dir or (
            settings.data_dir
            / "event-overlay-reviews"
            / f"{strategy}-{window_start.isoformat()}_{window_end.isoformat()}"
        )
        report = write_event_overlay_review_atomically(
            destination,
            report,
            annual,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"market_snapshot_id={report.market_snapshot_id}")
    typer.echo(f"event_snapshot_id={report.event_snapshot_id}")
    typer.echo(f"source_manifest_sha256={report.source_manifest_sha256}")
    typer.echo(f"collection_source_manifest_sha256={report.collection_source_manifest_sha256}")
    typer.echo(f"collection_quality_report_sha256={report.collection_quality_report_sha256}")
    typer.echo(f"window={report.window_start}..{report.window_end}")
    typer.echo(f"annual_source_rows={len(report.annual_by_source)}")
    typer.echo(f"pit_probes={len(report.pit_availability_probes)}")
    typer.echo(f"raw_collection_holder_rows={report.holder_count_missingness.raw_collection_holder_rows}")
    typer.echo(
        f"raw_collection_holder_num_blank_rows={report.holder_count_missingness.raw_collection_holder_num_blank_rows}"
    )
    typer.echo(f"canonical_holder_rows_in_window={report.holder_count_missingness.canonical_holder_rows_in_window}")
    typer.echo(
        "symbols_with_no_observable_canonical_holder_data="
        f"{report.holder_count_missingness.symbols_with_no_observable_canonical_holder_data}"
    )
    typer.echo(
        f"raw_collection_float_ratio_blank_rows={report.unlock_ratio_coverage.raw_collection_float_ratio_blank_rows}"
    )
    typer.echo(f"canonical_float_ratio_known_rows={report.unlock_ratio_coverage.canonical_float_ratio_known_rows}")
    typer.echo(f"canonical_float_ratio_missing_rows={report.unlock_ratio_coverage.canonical_float_ratio_missing_rows}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_trading=false")
    typer.echo(f"output={destination}")


@app.command("build-universe-membership")
def build_universe_membership_cmd(
    snapshots_file: Annotated[Path, typer.Option("--snapshots-file", exists=True, dir_okay=False)],
    calendar_file: Annotated[Path, typer.Option("--calendar-file", exists=True, dir_okay=False)],
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    strategy: Annotated[str, typer.Option("--strategy")],
    output: Annotated[Path, typer.Option("--output", dir_okay=False)],
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
) -> None:
    """Materialize a daily PIT membership file from offline constituent snapshots. Research only."""
    from app.universe.materialize import build_universe_membership

    typer.echo("Offline research only. This command only writes a membership file and does not trade.")
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        if end_day < start_day:
            raise ValueError("end date must be on or after start date")
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        result = build_universe_membership(
            snapshots_file=snapshots_file,
            calendar_file=calendar_file,
            config=config,
            start=start_day,
            end=end_day,
            output=output,
            overwrite=overwrite,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"universe_id={result.universe_id}")
    typer.echo(f"trading_days={result.trading_days}")
    typer.echo(f"members_per_day={result.members_per_day}")
    typer.echo(f"output_rows={result.row_count}")
    typer.echo(f"input_snapshots={result.snapshot_count}")
    typer.echo(f"output={result.path}")
    typer.echo("仅生成离线研究成员文件，不交易")


@app.command("verify-universe-source")
def verify_universe_source_cmd(
    snapshots_file: Annotated[Path, typer.Option("--snapshots-file", exists=True, dir_okay=False)],
    provenance_file: Annotated[Path, typer.Option("--provenance-file", exists=True, dir_okay=False)],
    strategy: Annotated[str, typer.Option("--strategy")],
) -> None:
    """Verify an offline membership-source manifest against snapshot bytes. Research only."""
    from app.universe.provenance import verify_universe_source

    typer.echo("Offline research only. This command only verifies a user-supplied source manifest.")
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        result = verify_universe_source(
            snapshots_file=snapshots_file,
            provenance_file=provenance_file,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"provenance_schema_version={result.schema_version}")
    typer.echo(f"universe_id={result.universe_id}")
    typer.echo(f"source_name={result.source_name}")
    typer.echo(f"snapshots_file_sha256={result.snapshots_file_sha256}")
    typer.echo(
        f"effective_from_coverage={result.effective_from_start.isoformat()}..{result.effective_from_end.isoformat()}"
    )
    typer.echo(f"snapshot_count={result.snapshot_count}")
    typer.echo(f"expected_constituents={result.expected_constituents}")
    if result.event_evidence_count is not None:
        typer.echo(f"event_evidence_count={result.event_evidence_count}")
        typer.echo(f"event_evidence_ledger_sha256={result.event_evidence_ledger_sha256}")
    typer.echo("来源清单由用户/可信来源提供，本命令只验证，不下载/不生成/不把下载时间当 available_at")


@app.command("preflight-research")
def preflight_research_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
) -> None:
    """Read-only research window check. Does not trade or prove strategy returns."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research

    typer.echo("Offline research only. This command is read-only preflight and does not trade.")
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        result = preflight_research(
            store=load_research_store(settings, strategy),
            config=config,
            start=start_day,
            end=end_day,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"universe_id={result.universe_id}")
    typer.echo(f"universe_mode={result.universe_mode}")
    typer.echo(f"research_mode={result.research_mode}")
    if result.research_notice:
        typer.echo(f"研究限制：{result.research_notice}")
    if result.sector_status:
        typer.echo(f"sector_score={result.sector_status}")
    typer.echo(f"signal_ready_start={result.signal_ready_start.isoformat()}")
    typer.echo(f"coverage={result.coverage_start.isoformat()}..{result.coverage_end.isoformat()}")
    typer.echo(f"trading_days={result.trading_days}")
    typer.echo(f"min_history_bars={result.min_history_bars}")
    typer.echo(f"required_history_bars={result.required_history_bars}")
    typer.echo(f"data_snapshot_id={result.snapshot_id}")
    typer.echo("预检只读，不能证明策略收益有效")


def _format_ic_summary_row(item: object) -> str:
    from app.research.ic import ICFactorSummary

    if not isinstance(item, ICFactorSummary):
        raise TypeError("expected ICFactorSummary")
    mean = "-" if item.mean_spearman_ic is None else f"{item.mean_spearman_ic:.6f}"
    icir = "-" if item.icir is None else f"{item.icir:.6f}"
    naive_t = "-" if item.t_stat is None else f"{item.t_stat:.4f}"
    hac_t = "-" if item.hac_t_stat is None else f"{item.hac_t_stat:.4f}"
    hac_lag = "-" if item.hac_lag is None else str(item.hac_lag)
    return (
        f"{item.horizon_days:>7} {item.factor:<18} {item.observations:>12} "
        f"{mean:>16} {icir:>10} {naive_t:>8} {hac_t:>8} {hac_lag:>7}"
    )


def _format_quantile_summary_row(item: object) -> str:
    from app.research.quantile_portfolios import QuantileFactorSummary

    if not isinstance(item, QuantileFactorSummary):
        raise TypeError("expected QuantileFactorSummary")
    avg_names = "-" if item.average_names is None else f"{item.average_names:.2f}"
    min_names = "-" if item.minimum_names is None else str(item.minimum_names)
    mean_high = "-" if item.mean_highest_quantile_return is None else f"{item.mean_highest_quantile_return:.6f}"
    mean_low = "-" if item.mean_lowest_quantile_return is None else f"{item.mean_lowest_quantile_return:.6f}"
    mean_spread = "-" if item.mean_spread is None else f"{item.mean_spread:.6f}"
    spread_ir = "-" if item.spread_ir is None else f"{item.spread_ir:.6f}"
    naive_t = "-" if item.t_stat is None else f"{item.t_stat:.4f}"
    hac_t = "-" if item.hac_t_stat is None else f"{item.hac_t_stat:.4f}"
    hac_lag = "-" if item.hac_lag is None else str(item.hac_lag)
    return (
        f"{item.horizon_days:>7} {item.factor:<18} {item.scoring_days:>12} "
        f"{avg_names:>9} {min_names:>9} {mean_high:>12} {mean_low:>12} "
        f"{mean_spread:>12} {spread_ir:>10} {naive_t:>8} {hac_t:>8} {hac_lag:>7} "
        f"{item.skipped_insufficient_cross_section:>8}"
    )


@app.command("analyze-ic")
def analyze_ic_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    start: Annotated[str, typer.Option("--start", help="first signal date YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="last signal date YYYY-MM-DD")],
    horizons: Annotated[str, typer.Option("--horizons", help="comma-separated trading-day horizons")] = "1,5,10",
    rolling_window: Annotated[
        int, typer.Option("--rolling-window", min=0, help="rolling scoring-day window; 0 disables")
    ] = 0,
    rolling_step: Annotated[
        int, typer.Option("--rolling-step", min=0, help="rolling scoring-day step; 0 disables")
    ] = 0,
    scheduled_only: Annotated[
        bool,
        typer.Option(
            "--scheduled-only",
            help="sample only the strategy's declared signal schedule",
        ),
    ] = False,
    quantiles: Annotated[
        int,
        typer.Option(
            "--quantiles",
            min=2,
            max=10,
            help="cross-section factor quantile count for top-minus-bottom spread (2..10)",
        ),
    ] = 5,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Read-only IC and quantile-spread diagnostic; forward returns are labels only."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research
    from app.research.ic import analyze_ic, write_ic_report

    typer.echo("Offline research only. Forward returns are diagnostic labels and are never trading inputs.")
    typer.echo("Quantile long/short spreads diagnose factors only; A-share short legs are not tradable here.")
    try:
        parsed_horizons = [int(value.strip()) for value in horizons.split(",") if value.strip()]
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        store = load_research_store(settings, strategy)
        preflight_research(store=store, config=config, start=start_day, end=end_day)

        def progress(done: int, total: int, day: date) -> None:
            if done == total or done % 25 == 0:
                typer.echo(f"progress={done}/{total} decision_date={day.isoformat()}")

        report = analyze_ic(
            store=store,
            config=config,
            start=start_day,
            end=end_day,
            horizons=parsed_horizons,
            rolling_window_days=rolling_window,
            rolling_step_days=rolling_step,
            scheduled_only=scheduled_only,
            quantiles=quantiles,
            progress=progress,
        )
        if output is not None:
            write_ic_report(report, output)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo(f"decision_schedule={report.decision_schedule}")
    typer.echo(f"diagnostic_only={str(report.diagnostic_only).lower()}")
    typer.echo(f"tradable_long_short={str(report.tradable_long_short).lower()}")
    typer.echo(f"ready_for_scoring={str(report.ready_for_scoring).lower()}")
    typer.echo(f"ready_for_trading={str(report.ready_for_trading).lower()}")
    typer.echo(f"quantile_count={report.quantile_count}")
    typer.echo(f"spread_definition={report.spread_definition}")
    typer.echo("ic_table")
    typer.echo("horizon factor observations mean_spearman_ic icir naive_t hac_t hac_lag")
    for item in report.summaries:
        typer.echo(_format_ic_summary_row(item))
    for period in [*report.annual_periods, *report.rolling_periods]:
        typer.echo(f"ic_period={period.label} coverage={period.start.isoformat()}..{period.end.isoformat()}")
        for item in period.summaries:
            typer.echo(_format_ic_summary_row(item))
    typer.echo("quantile_spread_table")
    typer.echo(
        "horizon factor scoring_days avg_names min_names mean_high mean_low "
        "mean_spread spread_ir naive_t hac_t hac_lag skipped_cs"
    )
    for quantile_item in report.quantile_summaries:
        typer.echo(_format_quantile_summary_row(quantile_item))
    for quantile_period in [
        *report.annual_quantile_periods,
        *report.rolling_quantile_periods,
    ]:
        typer.echo(
            "quantile_period="
            f"{quantile_period.label} coverage="
            f"{quantile_period.start.isoformat()}..{quantile_period.end.isoformat()}"
        )
        for quantile_item in quantile_period.summaries:
            typer.echo(_format_quantile_summary_row(quantile_item))
    if output is not None:
        typer.echo(f"report={output}")


@app.command("analyze-portfolio-signal")
def analyze_portfolio_signal_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    start: Annotated[str, typer.Option("--start", help="first signal date YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="last signal date YYYY-MM-DD")],
    horizons: Annotated[str, typer.Option("--horizons", help="comma-separated holding horizons")] = "5,10",
    factor: Annotated[Literal["final_score", "alpha_score"], typer.Option("--factor")] = "final_score",
    top_k: Annotated[int, typer.Option("--top-k", min=1)] = 3,
    quantiles: Annotated[int, typer.Option("--quantiles", min=2)] = 10,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Read-only execution-aligned Top-K and quantile signal diagnostic."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research
    from app.research.portfolio_signal import analyze_portfolio_signal, write_portfolio_signal_report

    typer.echo("Offline research only. Future entry/exit prices are diagnostic labels, never signal inputs.")
    try:
        parsed_horizons = [int(value.strip()) for value in horizons.split(",") if value.strip()]
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        store = load_research_store(settings, strategy)
        preflight_research(store=store, config=config, start=start_day, end=end_day)
        report = analyze_portfolio_signal(
            store=store,
            config=config,
            start=start_day,
            end=end_day,
            horizons=parsed_horizons,
            factor=factor,
            top_k=top_k,
            quantiles=quantiles,
        )
        if output is not None:
            write_portfolio_signal_report(report, output)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo("horizon days names top_k_gross top_k_net net_t top_quantile bottom_quantile spread turnover")
    for item in report.summaries:
        values = (
            item.average_labeled_names,
            item.mean_top_k_gross_return,
            item.mean_top_k_estimated_net_return,
            item.top_k_net_t_stat,
            item.mean_top_quantile_gross_return,
            item.mean_bottom_quantile_gross_return,
            item.mean_long_short_spread,
            item.mean_top_k_turnover,
        )
        rendered = ["-" if value is None else f"{value:.6f}" for value in values]
        typer.echo(f"{item.horizon_days:>7} {item.scoring_days:>4} " + " ".join(rendered))
    if output is not None:
        typer.echo(f"report={output}")


@app.command("analyze-portfolio-construction")
def analyze_portfolio_construction_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    positions: Annotated[str, typer.Option("--positions", help="comma-separated maximum position counts")] = "3,5,8,10",
    holding_days: Annotated[
        str, typer.Option("--holding-days", help="comma-separated fixed holding/signal intervals")
    ] = "10,20,40",
    training_start: Annotated[str, typer.Option("--training-start")] = "2022-04-01",
    training_end: Annotated[str, typer.Option("--training-end")] = "2022-12-30",
    validation_start: Annotated[str, typer.Option("--validation-start")] = "2023-01-03",
    validation_end: Annotated[str, typer.Option("--validation-end")] = "2023-12-29",
    holdout_start: Annotated[str, typer.Option("--holdout-start")] = "2024-01-02",
    holdout_end: Annotated[str, typer.Option("--holdout-end")] = "2024-12-31",
    minimum_training_trades: Annotated[int, typer.Option("--minimum-training-trades", min=1)] = 4,
    liquidation_buffer_days: Annotated[int, typer.Option("--liquidation-buffer-days", min=0)] = 10,
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir", file_okay=False)] = None,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
    selected_config_output: Annotated[Path | None, typer.Option("--selected-config-output", dir_okay=False)] = None,
) -> None:
    """Select on 2023 after a 2022 screen; evaluate only the frozen winner on 2024."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research
    from app.research.portfolio_construction import (
        CachedScoreProvider,
        evaluate_holdout,
        evaluate_price_index_benchmark,
        select_portfolio_construction,
        write_portfolio_construction_report,
        write_selected_config,
    )

    typer.echo(
        "Offline research only. Initial capital and ranking inputs are frozen; "
        "the holdout is evaluated once for the selected construction."
    )
    try:
        parsed_positions = [int(value.strip()) for value in positions.split(",") if value.strip()]
        parsed_horizons = [int(value.strip()) for value in holding_days.split(",") if value.strip()]
        train_start = date.fromisoformat(training_start)
        train_end = date.fromisoformat(training_end)
        valid_start = date.fromisoformat(validation_start)
        valid_end = date.fromisoformat(validation_end)
        test_start = date.fromisoformat(holdout_start)
        test_end = date.fromisoformat(holdout_end)
        if valid_end >= test_start:
            raise ValueError("validation and holdout windows must not overlap")
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        store = load_research_store(settings, strategy)
        preflight_research(store=store, config=config, start=train_start, end=test_end)
        resolved_cache = cache_dir or settings.data_dir / "research-cache" / "portfolio-construction"
        resolved_report = output or settings.data_dir / "portfolio-construction-v2.json"
        resolved_config = selected_config_output or (settings.strategies_dir / f"{strategy}_portfolio_selected_v2.yaml")
        scores = CachedScoreProvider(store=store, config=config, cache_root=resolved_cache)

        def progress(stage: str, done: int, total: int, candidate_id: str) -> None:
            typer.echo(f"progress={done}/{total} stage={stage} candidate={candidate_id}")

        report, selected_config = select_portfolio_construction(
            store=store,
            base_config=config,
            positions=parsed_positions,
            holding_days=parsed_horizons,
            training_start=train_start,
            training_end=train_end,
            validation_start=valid_start,
            validation_end=valid_end,
            minimum_training_trades=minimum_training_trades,
            liquidation_buffer_days=liquidation_buffer_days,
            score_fn=scores,
            progress=progress,
        )
        # Freeze the exact winner before exposing the untouched holdout to it.
        write_selected_config(selected_config, resolved_config)
        report.selected_config_path = str(resolved_config)
        typer.echo(
            f"selected={report.selected_candidate_id} "
            f"config_hash={report.selected_config_hash} frozen_config={resolved_config}"
        )
        report.holdout = evaluate_holdout(
            store=store,
            selected_config=selected_config,
            start=test_start,
            end=test_end,
            maximum_candidate_horizon=max(parsed_horizons),
            liquidation_buffer_days=liquidation_buffer_days,
            score_fn=scores,
        )
        report.holdout_benchmark = evaluate_price_index_benchmark(
            store=store,
            symbol=config.data.market_index,
            start=test_start,
            end=test_end,
        )
        report.holdout_return_minus_benchmark = (
            report.holdout.period.total_return - report.holdout_benchmark.total_return
        )
        report.score_cache_hits = scores.hits
        report.score_cache_misses = scores.misses
        write_portfolio_construction_report(report, resolved_report)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    selected = next(item for item in report.evaluations if item.candidate.candidate_id == report.selected_candidate_id)
    typer.echo(
        f"training return={selected.training.total_return:.6f} "
        f"sharpe={selected.training.sharpe_ratio} dd={selected.training.max_drawdown}"
    )
    typer.echo(
        f"validation return={selected.validation.total_return:.6f} "
        f"sharpe={selected.validation.sharpe_ratio} dd={selected.validation.max_drawdown}"
    )
    if report.holdout is not None:
        test = report.holdout.period
        typer.echo(
            f"holdout return={test.total_return:.6f} sharpe={test.sharpe_ratio} "
            f"dd={test.max_drawdown} trades={test.number_of_trades}"
        )
    if report.holdout_benchmark is not None:
        benchmark = report.holdout_benchmark
        typer.echo(
            f"benchmark={benchmark.symbol} type={benchmark.benchmark_type} "
            f"return={benchmark.total_return:.6f} sharpe={benchmark.sharpe_ratio} "
            f"dd={benchmark.max_drawdown}"
        )
        typer.echo(f"holdout_return_minus_benchmark={report.holdout_return_minus_benchmark:.6f}")
    typer.echo(f"score_cache_hits={report.score_cache_hits} misses={report.score_cache_misses}")
    typer.echo(f"report={resolved_report}")


@app.command("analyze-frozen-portfolio-robustness")
def analyze_frozen_portfolio_robustness_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    selection_report_path: Annotated[Path, typer.Option("--selection-report", exists=True, dir_okay=False)],
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir", file_okay=False)] = None,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Audit the frozen winner; never select or change portfolio parameters."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research
    from app.research.portfolio_construction import CachedScoreProvider, PortfolioConstructionReport
    from app.research.portfolio_robustness import (
        analyze_frozen_portfolio_robustness,
        write_frozen_portfolio_robustness_report,
    )

    typer.echo(
        "Offline diagnostic only. The selected config is hash-locked; this command does not "
        "select parameters, alter signals, trade, or connect to a broker."
    )
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        selection = PortfolioConstructionReport.model_validate_json(selection_report_path.read_text(encoding="utf-8"))
        selected = next(
            item for item in selection.evaluations if item.candidate.candidate_id == selection.selected_candidate_id
        )
        if selection.holdout is None:
            raise ValueError("selection report does not contain the frozen holdout period")
        start_day = selected.training.start
        end_day = selection.holdout.period.end
        store = load_research_store(settings, strategy)
        preflight_research(store=store, config=config, start=start_day, end=end_day)
        resolved_cache = cache_dir or settings.data_dir / "research-cache" / "portfolio-construction"
        resolved_output = output or settings.data_dir / "frozen-portfolio-robustness-v2.json"
        scores = CachedScoreProvider(store=store, config=config, cache_root=resolved_cache)

        def progress(stage: str, done: int, total: int) -> None:
            typer.echo(f"progress={done}/{total} stage={stage}")

        report = analyze_frozen_portfolio_robustness(
            store=store,
            config=config,
            selection_report=selection,
            selection_report_path=selection_report_path,
            score_fn=scores,
            progress=progress,
        )
        report.score_cache_hits = scores.hits
        report.score_cache_misses = scores.misses
        write_frozen_portfolio_robustness_report(report, resolved_output)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    for period in report.periods:
        typer.echo(
            f"period={period.label} return={period.strategy.total_return:.6f} "
            f"benchmark={period.benchmark.total_return:.6f} "
            f"relative={period.return_minus_benchmark:.6f} "
            f"avg_invested={period.exposure.average_invested_fraction:.6f}"
        )
    training = next(item for item in report.periods if item.label == "training")
    for item in training.symbols[:5]:
        typer.echo(f"training_loss symbol={item.symbol} sector={item.sector} net_pnl={item.net_pnl:.2f}")
    for scenario in report.cost_scenarios:
        rendered = ",".join(f"{period.label}:{period.total_return:.6f}" for period in scenario.periods)
        typer.echo(f"cost_scenario={scenario.scenario_id} returns={rendered}")
    for gate in report.gates:
        typer.echo(f"gate={gate.gate} passed={gate.passed} observed={gate.observed}")
    for warning in report.warnings:
        typer.echo(f"warning={warning}")
    typer.echo(f"status={report.status} reason={report.status_reason}")
    typer.echo(f"score_cache_hits={report.score_cache_hits} misses={report.score_cache_misses}")
    typer.echo(f"report={resolved_output}")


@app.command("analyze-signal-phase-sensitivity")
def analyze_signal_phase_sensitivity_cmd(
    strategy: Annotated[str, typer.Option("--strategy")],
    start: Annotated[str, typer.Option("--start", help="evaluation window start YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="evaluation window end YYYY-MM-DD")],
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir", file_okay=False)] = None,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Full-phase fixed-interval sensitivity diagnostic; never selects a winning phase."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research
    from app.research.portfolio_construction import CachedScoreProvider
    from app.research.signal_phase_sensitivity import (
        analyze_signal_phase_sensitivity,
        write_signal_phase_sensitivity_report,
    )
    from app.scoring.engine import ScoringEngine

    typer.echo(
        "Offline diagnostic only. Runs every phase_offset for signal_interval_days; "
        "does not select a best phase, alter strategy weights, score via the score CLI, "
        "or call the backtest CLI."
    )
    typer.echo("Development-window use only under the current process; first 2025+ run requires new authorization.")
    try:
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        store = load_research_store(settings, strategy)
        preflight_research(store=store, config=config, start=start_day, end=end_day)
        resolved_cache = cache_dir or settings.data_dir / "research-cache" / "signal-phase-sensitivity"
        resolved_output = output or settings.data_dir / "research" / "signal-phase-sensitivity.json"
        from app.models.scores import ScoreResult

        if config.trade.exit_policy == "fixed_horizon":
            provider = CachedScoreProvider(store=store, config=config, cache_root=resolved_cache)

            def score_fn(as_of: date) -> list[ScoreResult]:
                return provider(as_of)
        else:
            engine = ScoringEngine(store, config)

            def score_fn(as_of: date) -> list[ScoreResult]:
                return engine.run(as_of)

        def _phase_progress(offset_done: int, total: int, anchor: date) -> None:
            typer.echo(f"phase_progress {offset_done}/{total} anchor={anchor.isoformat()}")

        report = analyze_signal_phase_sensitivity(
            store=store,
            config=config,
            start=start_day,
            end=end_day,
            score_fn=score_fn,
            progress=_phase_progress,
        )
        write_signal_phase_sensitivity_report(report, resolved_output)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    for warning in report.warnings:
        typer.echo(f"warning={warning}")
    typer.echo(f"base_config_hash={report.base_config_hash}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo(f"signal_interval_days={report.signal_interval_days}")
    typer.echo(f"original_anchor={report.original_anchor.isoformat()}")
    typer.echo(f"phase_count={report.phase_count}")
    typer.echo(f"diagnostic_only={str(report.diagnostic_only).lower()}")
    typer.echo(f"parameter_selection_forbidden={str(report.parameter_selection_forbidden).lower()}")
    typer.echo("selected_phase=null")
    typer.echo(f"ready_for_scoring={str(report.ready_for_scoring).lower()}")
    typer.echo(f"ready_for_trading={str(report.ready_for_trading).lower()}")
    for phase in report.phases:
        typer.echo(
            f"phase offset={phase.phase_offset} anchor={phase.signal_anchor_date.isoformat()} "
            f"return={phase.total_return:.6f} sharpe="
            f"{'null' if phase.sharpe_ratio is None else f'{phase.sharpe_ratio:.6f}'} "
            f"trades={phase.trades} exposure={phase.average_invested_fraction:.6f}"
        )
    summary = report.summary
    typer.echo(
        "summary return "
        f"min={summary.total_return.min} median={summary.total_return.median} "
        f"max={summary.total_return.max} range={summary.total_return.range}"
    )
    typer.echo(f"independence_note={summary.independence_note}")
    typer.echo(f"report={resolved_output}")


@app.command("inventory-layer-two-alpha-diagnostic-inputs")
def inventory_layer_two_alpha_diagnostic_inputs_cmd(
    market_dir: Annotated[Path, typer.Option("--market-dir", exists=True, file_okay=False)],
    fundamental_dir: Annotated[Path, typer.Option("--fundamental-dir", exists=True, file_okay=False)],
    output: Annotated[Path, typer.Option("--output", dir_okay=False)],
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Build a content-addressed input inventory for the E11b-0b run contract.

    Offline/read-only: verifies market and fundamental data, binds available
    slots, marks missing derived slots as blocked, and writes the sealed
    inventory atomically. Does not modify source data.
    """
    from app.research.layer_two_alpha_diagnostic_input_inventory import (
        build_input_inventory,
        write_inventory,
    )

    typer.echo("Offline read-only inventory builder. Does not score, backtest, trade, or modify source data.")
    try:
        repo_root = Path(__file__).resolve().parents[2]
        inventory = build_input_inventory(
            market_dir=market_dir,
            fundamental_dir=fundamental_dir,
            repo_root=repo_root,
        )
        written = write_inventory(Path(output), inventory, repo_root=repo_root, replace_existing=replace_existing)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"inventory_id={written.inventory_id}")
    typer.echo(f"contract_id={written.contract_id}")
    bound_count = sum(1 for s in written.slots if s.state == "bound")
    blocked_count = sum(1 for s in written.slots if s.state == "blocked_missing")
    typer.echo(f"bound_slots={bound_count}")
    typer.echo(f"blocked_slots={blocked_count}")
    typer.echo(f"ready_for_data={str(written.readiness.ready_for_data).lower()}")
    typer.echo(f"research_only={str(written.readiness.research_only).lower()}")
    typer.echo(f"read_only={str(written.readiness.read_only).lower()}")
    typer.echo(f"output={output}")


@app.command("review-layer-two-alpha-input-feasibility")
def review_layer_two_alpha_input_feasibility_cmd(
    output: Annotated[
        Path,
        typer.Option("--output", dir_okay=False),
    ] = Path("data/all-a-share-historical-v1/research/layer-two-alpha-input-feasibility-v1.json"),
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Seal an optimistic, fail-closed coverage feasibility review.

    This command verifies all bound inputs and computes only the maximum
    possible factor cross-section. It never computes IC, forward returns,
    scores, portfolios, backtests, orders, or trades.
    """
    from app.research.layer_two_alpha_input_feasibility import (
        build_feasibility_report,
        write_feasibility_report,
    )

    typer.echo(
        "Offline feasibility review only. No IC, forward returns, score, "
        "backtest, portfolio, order, or trading operation is performed."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        report = build_feasibility_report(repo_root=repo_root)
        write_feasibility_report(output, report, replace_existing=replace_existing)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"coverage={report.coverage_start.isoformat()}..{report.coverage_end.isoformat()}")
    typer.echo(f"trading_dates={report.trading_date_count}")
    typer.echo(f"development_primary_valid_date_upper_bound={report.development_primary_valid_date_upper_bound}")
    for row in report.yearly:
        typer.echo(
            f"year={row.year} primary_valid_date_upper_bound="
            f"{row.primary_valid_date_upper_bound} median_eligible_upper_bound="
            f"{row.median_alpha_eligible_upper_bound:.1f} "
            f"min_candidate_eligible={row.min_candidate_eligible_rows}"
        )
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo(f"output={output}")


@app.command("verify-layer-two-alpha-input-feasibility")
def verify_layer_two_alpha_input_feasibility_cmd(
    report_file: Annotated[Path, typer.Option("--report-file", exists=True, dir_okay=False)],
) -> None:
    """Fully recompute a sealed layer-two alpha input feasibility report."""
    from app.research.layer_two_alpha_input_feasibility import (
        verify_feasibility_report_file,
    )

    typer.echo(
        "Offline full-recomputation verification only. No IC, score, backtest, "
        "portfolio, order, or trading operation is performed."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        report = verify_feasibility_report_file(report_file, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"development_primary_valid_date_upper_bound={report.development_primary_valid_date_upper_bound}")
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo("verification=passed")


@app.command("freeze-layer-two-alpha-coverage-separation-policy")
def freeze_layer_two_alpha_coverage_separation_policy_cmd(
    output: Annotated[
        Path,
        typer.Option("--output", dir_okay=False),
    ] = Path("config/research/layer-two-alpha-coverage-separation-policy-v1.json"),
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Freeze the pre-outcome alpha/financial coverage separation correction."""
    from app.research.layer_two_alpha_coverage_separation_policy import (
        build_policy,
        write_policy,
    )

    typer.echo(
        "Protocol freeze only. The base E11a file remains immutable; this does not "
        "run IC, labels, scoring, backtests, portfolios, orders, or trading."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        policy = build_policy(repo_root=repo_root)
        write_policy(output, policy, replace_existing=replace_existing)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"policy_id={policy.policy_id}")
    typer.echo(f"feasibility_report_id={policy.source_binding.feasibility_report_id}")
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo(f"output={output}")


@app.command("verify-layer-two-alpha-coverage-separation-policy")
def verify_layer_two_alpha_coverage_separation_policy_cmd(
    policy_file: Annotated[Path, typer.Option("--policy-file", exists=True, dir_okay=False)],
) -> None:
    """Verify the separation policy and fully recompute its feasibility source."""
    from app.research.layer_two_alpha_coverage_separation_policy import (
        verify_policy_file,
    )

    typer.echo(
        "Offline full-source verification only. No IC, labels, scoring, backtest, "
        "portfolio, order, or trading operation is performed."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        policy = verify_policy_file(policy_file, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"policy_id={policy.policy_id}")
    typer.echo("base_e11a_remains_immutable=true")
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo("verification=passed")


@app.command("freeze-layer-two-alpha-v2-contracts")
def freeze_layer_two_alpha_v2_contracts_cmd(
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Freeze the additive v2 trial registration, protocol, and run contract."""
    from app.research.layer_two_alpha_v2_freeze_bundle import freeze_bundle

    typer.echo(
        "Freeze-only registration chain. No factor outcome, forward return, IC, "
        "score, backtest, portfolio, order, or trading operation is performed."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        registration, protocol, contract = freeze_bundle(
            repo_root=repo_root,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"registration_id={registration.registration_id}")
    typer.echo(f"protocol_id={protocol.protocol_id}")
    typer.echo(f"contract_id={contract.contract_id}")
    typer.echo("unbound_required=statistical_cluster_companion_reports")
    typer.echo("ready_for_alpha_diagnostic_execution=false")


@app.command("verify-layer-two-alpha-v2-contracts")
def verify_layer_two_alpha_v2_contracts_cmd() -> None:
    """Verify the additive v2 registration chain against all bound sources."""
    from app.research.layer_two_alpha_v2_freeze_bundle import verify_bundle

    typer.echo(
        "Offline freeze-chain verification only. No factor outcome, IC, score, "
        "backtest, portfolio, order, or trading operation is performed."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        registration, protocol, contract = verify_bundle(repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"registration_id={registration.registration_id}")
    typer.echo(f"protocol_id={protocol.protocol_id}")
    typer.echo(f"contract_id={contract.contract_id}")
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo("verification=passed")


@app.command("materialize-layer-two-statistical-cluster-pack-v2")
def materialize_layer_two_statistical_cluster_pack_v2_cmd(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", file_okay=False),
    ] = Path("data/all-a-share-historical-v1/research/layer-two-statistical-cluster-pack-v2"),
) -> None:
    """Materialize the compact monthly PIT statistical-risk-cluster pack."""
    from app.research.layer_two_statistical_cluster_pack_v2 import (
        materialize_cluster_pack,
    )

    typer.echo(
        "Offline statistical-risk proxy materialization only; not industry, alpha, "
        "scoring, backtest, portfolio, order, or trading output."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        manifest = materialize_cluster_pack(repo_root=repo_root, output_dir=output_dir)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"pack_id={manifest.pack_id}")
    typer.echo(f"anchors={manifest.integrity.anchor_count}")
    typer.echo(f"rows={manifest.integrity.row_count}")
    typer.echo(f"status_counts={json.dumps(manifest.status_counts, sort_keys=True)}")
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo(f"output={output_dir}")


@app.command("verify-layer-two-statistical-cluster-pack-v2")
def verify_layer_two_statistical_cluster_pack_v2_cmd(
    pack_dir: Annotated[Path, typer.Option("--pack-dir", exists=True, file_okay=False)],
) -> None:
    """Fully recompute the compact monthly statistical-risk-cluster pack."""
    from app.research.layer_two_statistical_cluster_pack_v2 import (
        verify_cluster_pack,
    )

    typer.echo(
        "Offline full-recomputation verification only; not industry, alpha, score, "
        "backtest, portfolio, order, or trading output."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        manifest = verify_cluster_pack(repo_root=repo_root, pack_dir=pack_dir)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"pack_id={manifest.pack_id}")
    typer.echo(f"anchors={manifest.integrity.anchor_count}")
    typer.echo(f"rows={manifest.integrity.row_count}")
    typer.echo("ready_for_alpha_diagnostic_execution=false")
    typer.echo("verification=passed")


@app.command("bind-layer-two-alpha-input-bundle-v2")
def bind_layer_two_alpha_input_bundle_v2_cmd(
    output: Annotated[
        Path,
        typer.Option("--output", dir_okay=False),
    ] = Path("data/all-a-share-historical-v1/research/layer-two-alpha-input-bundle-v2.json"),
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Bind all six verified v2 alpha diagnostic inputs."""
    from app.research.layer_two_alpha_input_bundle_v2 import (
        build_input_bundle,
        write_input_bundle,
    )

    typer.echo(
        "Offline input binding for the frozen alpha diagnostic only. This does not "
        "run factor outcomes, scoring, backtests, portfolios, orders, or trading."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        bundle = build_input_bundle(repo_root=repo_root)
        write_input_bundle(output, bundle, replace_existing=replace_existing)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"bundle_id={bundle.bundle_id}")
    typer.echo("all_six_slots_bound=true")
    typer.echo("ready_for_frozen_alpha_diagnostic_execution=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo(f"output={output}")


@app.command("verify-layer-two-alpha-input-bundle-v2")
def verify_layer_two_alpha_input_bundle_v2_cmd(
    bundle_file: Annotated[Path, typer.Option("--bundle-file", exists=True, dir_okay=False)],
) -> None:
    """Verify the complete v2 alpha input bundle against its bound sources."""
    from app.research.layer_two_alpha_input_bundle_v2 import verify_input_bundle

    typer.echo(
        "Offline input-binding verification only. No factor outcome, score, backtest, "
        "portfolio, order, or trading operation is performed."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        bundle = verify_input_bundle(repo_root=repo_root, path=bundle_file)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"bundle_id={bundle.bundle_id}")
    typer.echo("ready_for_frozen_alpha_diagnostic_execution=true")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("verification=passed")


@app.command("run-layer-two-alpha-diagnostic-v2")
def run_layer_two_alpha_diagnostic_v2_cmd(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", file_okay=False),
    ] = Path("data/all-a-share-historical-v1/research/layer-two-alpha-diagnostic-v2"),
    replace_existing: Annotated[bool, typer.Option("--replace-existing")] = False,
) -> None:
    """Run the frozen 2022-2024 layer-two alpha development diagnostic."""
    from app.research.layer_two_alpha_diagnostic_v2 import (
        build_diagnostic,
        write_diagnostic,
    )

    typer.echo(
        "Offline frozen alpha diagnostic only. Selection uses 2022-2023; 2024 is "
        "report-only. No 2025+ data, score, backtest, portfolio, order, or trade."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        report, daily, summary, size_summary = build_diagnostic(repo_root=repo_root)
        sealed = write_diagnostic(
            output_dir=output_dir,
            report=report,
            daily=daily,
            summary=summary,
            size_summary=size_summary,
            replace_existing=replace_existing,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={sealed.report_id}")
    typer.echo(f"selected_factor_ids={json.dumps(sealed.selected_factor_ids)}")
    typer.echo(f"robustness_2024_pass={str(bool(sealed.robustness_2024['robustness_pass'])).lower()}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("new_oos_authorized=false")
    typer.echo(f"output={output_dir}")


@app.command("verify-layer-two-alpha-diagnostic-v2")
def verify_layer_two_alpha_diagnostic_v2_cmd(
    report_dir: Annotated[Path, typer.Option("--report-dir", exists=True, file_okay=False)],
) -> None:
    """Fully recompute the frozen layer-two alpha v2 diagnostic."""
    from app.research.layer_two_alpha_diagnostic_v2 import verify_diagnostic

    typer.echo(
        "Offline full-recomputation verification only. No 2025+ data, score, backtest, portfolio, order, or trade."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        report = verify_diagnostic(repo_root=repo_root, output_dir=report_dir)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"report_id={report.report_id}")
    typer.echo(f"selected_factor_ids={json.dumps(report.selected_factor_ids)}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("new_oos_authorized=false")
    typer.echo("verification=passed")


@app.command("materialize-layer-two-candidate-eligibility-pack")
def materialize_layer_two_candidate_eligibility_pack_cmd(
    output_dir: Annotated[Path, typer.Option("--output-dir", file_okay=False)],
) -> None:
    """Materialize a full-window E10a candidate eligibility Parquet pack.

    Research-only. Does not score, backtest, trade, or modify source data.
    Writes eligibility_verdicts.parquet and manifest.json into output_dir.
    Refuses if output_dir already exists. Uses atomic directory rename.
    """
    from app.research.layer_two_candidate_eligibility_pack import (
        build_candidate_eligibility_pack,
    )

    typer.echo(
        "Offline research pack materializer. Does not score, backtest, trade, "
        "or modify source data. ready_for_scoring=false ready_for_trading=false"
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]

        def _progress(done: int, total: int, current_date: date) -> None:
            if done % 50 == 0 or done == total - 1:
                typer.echo(f"progress={done + 1}/{total} as_of={current_date.isoformat()}")

        manifest = build_candidate_eligibility_pack(
            repo_root=repo_root,
            output_dir=Path(output_dir),
            progress_callback=_progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"pack_id={manifest.pack_id}")
    typer.echo(f"row_count={manifest.integrity.row_count}")
    typer.echo(f"trading_dates={manifest.coverage.trading_date_count}")
    typer.echo(f"year_2022={manifest.row_counts.year_2022}")
    typer.echo(f"year_2023={manifest.row_counts.year_2023}")
    typer.echo(f"year_2024={manifest.row_counts.year_2024}")
    typer.echo(f"ready_for_scoring={str(manifest.readiness.ready_for_scoring).lower()}")
    typer.echo(f"ready_for_trading={str(manifest.readiness.ready_for_trading).lower()}")
    typer.echo(f"output_dir={output_dir}")


@app.command("verify-layer-two-candidate-eligibility-pack")
def verify_layer_two_candidate_eligibility_pack_cmd(
    pack_dir: Annotated[Path, typer.Option("--pack-dir", exists=True, file_okay=False)],
) -> None:
    """Verify an existing candidate eligibility pack (read-only).

    Full recomputation verifier: checks manifest seal, Parquet integrity,
    source bindings against disk, module provenance, and E10a field parity
    for every row. Does not modify any files.
    """
    from app.research.layer_two_candidate_eligibility_pack import (
        verify_candidate_eligibility_pack,
    )

    typer.echo("Read-only full-recomputation pack verifier. Does not modify files.")
    try:
        repo_root = Path(__file__).resolve().parents[2]
        manifest = verify_candidate_eligibility_pack(
            Path(pack_dir),
            repo_root=repo_root,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"pack_id={manifest.pack_id}")
    typer.echo(f"row_count={manifest.integrity.row_count}")
    typer.echo(f"trading_dates={manifest.coverage.trading_date_count}")
    typer.echo(f"parquet_sha256={manifest.integrity.parquet_file_sha256}")
    typer.echo(f"pack_module_sha256={manifest.pack_module_sha256}")
    typer.echo(f"ready_for_scoring={str(manifest.readiness.ready_for_scoring).lower()}")
    typer.echo(f"ready_for_trading={str(manifest.readiness.ready_for_trading).lower()}")
    typer.echo("verification=passed")


@app.command("verify-financial-negative-list-collection-run-contract")
def verify_financial_negative_list_collection_run_contract_cmd(
    run_contract: Annotated[Path, typer.Option("--run-contract", dir_okay=False)] = Path(
        "config/research/financial-negative-list-collection-run-contract-v3.json"
    ),
    require_authorized: Annotated[bool, typer.Option("--require-authorized")] = False,
    authorization_file: Annotated[Path | None, typer.Option("--authorization-file", dir_okay=False)] = None,
) -> None:
    """Offline verifier for prepared FN list collection run contract and optional authorization gate."""
    from app.research.layer_two_financial_negative_list_collection_authorization import (
        verify_collection_authorization_file,
    )
    from app.research.layer_two_financial_negative_list_collection_run_contract import (
        verify_run_contract_file,
    )

    typer.echo(
        "Offline gate check only. Verifies prepared contract integrity; this command does not "
        "read token, perform network collection, score, backtest, or trade."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        contract_result = None
        if require_authorized or authorization_file is not None:
            if require_authorized and authorization_file is None:
                raise ValueError("--require-authorized requires --authorization-file")
            resolved_authorization_file = authorization_file
            if resolved_authorization_file is None:
                raise ValueError("--authorization-file is required when authorization verification is requested")
            contract, contract_result = verify_run_contract_file(
                run_contract_path=Path(run_contract),
                repo_root=repo_root,
            )
            if require_authorized and contract.network_authorized:
                raise ValueError("prepared run contract must keep network_authorized=false")
            _, auth_result = verify_collection_authorization_file(
                authorization_path=Path(resolved_authorization_file),
                repo_root=repo_root,
                run_contract_path=Path(run_contract),
                preverified_run_contract=contract,
                preverified_run_contract_result=contract_result,
            )
            typer.echo(f"authorization_id={auth_result.authorization_id}")
            if require_authorized:
                typer.echo(f"authorization_staging_dir={auth_result.staging_dir}")
                typer.echo(f"network_collection_allowed={str(auth_result.network_collection_allowed).lower()}")
            else:
                typer.echo("authorization_binding=verified")
        else:
            _, contract_result = verify_run_contract_file(
                run_contract_path=Path(run_contract),
                repo_root=repo_root,
            )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    if contract_result is None:
        raise typer.Exit(code=1)
    typer.echo(f"run_contract_id={contract_result.run_contract_id}")
    typer.echo(f"run_contract_version={contract_result.run_contract_version}")
    typer.echo(f"status={contract_result.status}")
    typer.echo(f"network_authorized={str(contract_result.network_authorized).lower()}")
    typer.echo(f"requires_fresh_user_authorization={str(contract_result.requires_fresh_user_authorization).lower()}")
    if contract_result.response_boundary_policy_id is not None:
        typer.echo(f"response_boundary_policy_id={contract_result.response_boundary_policy_id}")
    if contract_result.response_boundary_policy_path is not None:
        typer.echo(f"response_boundary_policy_path={contract_result.response_boundary_policy_path}")
    if contract_result.response_boundary_reason_code is not None:
        typer.echo(f"response_boundary_reason_code={contract_result.response_boundary_reason_code}")
    typer.echo(f"symbol_count={contract_result.canonical_symbol_count}")
    typer.echo(f"expected_partition_count={contract_result.expected_partition_count}")
    typer.echo("verification=passed")


@app.command("collect-tushare-financial-negative-list")
def collect_tushare_financial_negative_list_cmd(
    authorization_file: Annotated[Path, typer.Option("--authorization-file", dir_okay=False)],
    run_contract: Annotated[Path, typer.Option("--run-contract", dir_okay=False)] = Path(
        "config/research/financial-negative-list-collection-run-contract-v3.json"
    ),
    staging_dir: Annotated[Path | None, typer.Option("--staging-dir", file_okay=False)] = None,
) -> None:
    """Collect historical financial-negative-list raw partitions with explicit authorization."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_financial_negative_list_collection import (
        collect_tushare_financial_negative_list,
    )
    from app.research.layer_two_financial_negative_list_collection_authorization import (
        verify_collection_authorization_file,
    )

    typer.echo(
        "Historical data collection only. collected_at is provenance metadata and never available_at. "
        "This command is not scoring/backtest/trading authorization."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        authorization, authorization_result = verify_collection_authorization_file(
            authorization_path=Path(authorization_file),
            repo_root=repo_root,
            run_contract_path=Path(run_contract),
        )
        if staging_dir is not None and Path(staging_dir).as_posix() != authorization.staging_dir:
            raise ValueError("provided --staging-dir must exactly match contract/authorization staging_dir binding")
        resolved_staging = Path(staging_dir) if staging_dir is not None else Path(authorization.staging_dir)

        def _progress(endpoint: str, done: int, total: int, endpoint_done: int, endpoint_total: int) -> None:
            if done % 50 == 0 or endpoint_done == endpoint_total:
                typer.echo(f"collection_progress endpoint={endpoint} done={done}/{total}")

        token = read_tushare_token()
        result = collect_tushare_financial_negative_list(
            client=LiveTushareClient(token),
            repo_root=repo_root,
            staging_dir=resolved_staging,
            collection_authorization_id=str(authorization.authorization_id),
            verified_run_contract_id=authorization_result.run_contract_id,
            verified_run_contract_version=authorization_result.run_contract_version,
            verified_response_boundary_policy_id=authorization_result.response_boundary_policy_id,
            verified_response_boundary_policy_file_sha256=(authorization_result.response_boundary_policy_file_sha256),
            verified_response_boundary_reason_code=authorization_result.response_boundary_reason_code,
            progress_callback=_progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"collection_authorization_id={result.collection_authorization_id}")
    typer.echo(f"protocol_id={result.protocol_id}")
    typer.echo(f"requested_symbols={result.requested_symbols}")
    typer.echo(f"partition_count={result.partition_count}")
    typer.echo(f"completed_partitions={result.completed_partitions}")
    typer.echo(f"reused_partitions={result.reused_partitions}")
    typer.echo(f"staging_dir={result.staging_dir}")
    typer.echo(f"source_manifest={result.source_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-tushare-financial-negative-list-collection")
def verify_tushare_financial_negative_list_collection_cmd(
    staging_dir: Annotated[Path, typer.Option("--staging-dir", file_okay=False)],
) -> None:
    """Offline verifier for collected financial-negative-list staging artifacts."""
    from app.providers.tushare_financial_negative_list_collection import (
        verify_financial_negative_list_collection,
    )

    typer.echo(
        "Offline verification only. Does not read token, does not perform network access, "
        "and does not score/backtest/trade."
    )
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = verify_financial_negative_list_collection(
            repo_root=repo_root,
            staging_dir=Path(staging_dir),
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"request_id={result.request_id}")
    typer.echo(f"protocol_id={result.protocol_id}")
    typer.echo(f"collection_authorization_id={result.collection_authorization_id}")
    typer.echo(f"requested_symbols={result.requested_symbols}")
    typer.echo(f"partition_count={result.partition_count}")
    typer.echo(f"collection_request={result.staging_dir / 'collection_request.json'}")
    typer.echo(f"source_manifest={result.source_manifest_path}")
    typer.echo(f"quality_report={result.quality_report_path}")
    typer.echo(f"collection_manifest={result.collection_manifest_path}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_trading=false")
    typer.echo("verification=passed")


@app.command("materialize-financial-negative-list-verdict-overlay")
def materialize_financial_negative_list_verdict_overlay_cmd(
    collection_dir: Annotated[
        Path,
        typer.Option("--collection-dir", file_okay=False),
    ] = Path("data/raw/a-share-financial-negative-list-20200101-20241231-v3"),
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", file_okay=False),
    ] = Path("data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1"),
) -> None:
    """Materialize the isolated PIT financial-negative-list verdict overlay."""
    from app.research.layer_two_financial_negative_list_overlay import (
        materialize_financial_negative_list_verdict_overlay,
    )

    typer.echo(
        "Offline research only. This materializes an isolated PIT verdict overlay; "
        "it does not read a token, use network access, score, backtest, or trade."
    )

    def report_progress(partitions: int, rows: int, decision_date: str) -> None:
        if partitions == 1 or partitions % 250 == 0:
            typer.echo(f"progress_partitions={partitions} rows={rows} decision_date={decision_date}")

    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = materialize_financial_negative_list_verdict_overlay(
            repo_root=repo_root,
            collection_dir=Path(collection_dir),
            output_dir=Path(output_dir),
            progress_callback=report_progress,
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"overlay_id={result.overlay_id}")
    typer.echo(f"financial_collection_id={result.collection_id}")
    typer.echo(f"coverage={result.coverage_start}..{result.coverage_end}")
    typer.echo(f"row_count={result.row_count}")
    typer.echo(f"partition_count={result.partition_count}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo(f"coverage_review={result.coverage_review_path}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_portfolio_construction=false")
    typer.echo("ready_for_trading=false")


@app.command("verify-financial-negative-list-verdict-overlay")
def verify_financial_negative_list_verdict_overlay_cmd(
    overlay_dir: Annotated[Path, typer.Option("--overlay-dir", file_okay=False)] = Path(
        "data/all-a-share-historical-v1/research/financial-negative-list-verdict-overlay-v1"
    ),
) -> None:
    """Verify the sealed financial-negative-list verdict overlay offline."""
    from app.research.layer_two_financial_negative_list_overlay import (
        verify_financial_negative_list_verdict_overlay,
    )

    typer.echo("Offline verification only. Does not read a token, perform network access, score, backtest, or trade.")
    try:
        repo_root = Path(__file__).resolve().parents[2]
        result = verify_financial_negative_list_verdict_overlay(
            repo_root=repo_root,
            overlay_dir=Path(overlay_dir),
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"overlay_id={result.overlay_id}")
    typer.echo(f"financial_collection_id={result.collection_id}")
    typer.echo(f"coverage={result.coverage_start}..{result.coverage_end}")
    typer.echo(f"row_count={result.row_count}")
    typer.echo(f"partition_count={result.partition_count}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo(f"coverage_review={result.coverage_review_path}")
    typer.echo("ready_for_scoring=false")
    typer.echo("ready_for_backtest=false")
    typer.echo("ready_for_portfolio_construction=false")
    typer.echo("ready_for_trading=false")
    typer.echo("verification=passed")


@app.command("list-strategies")
def list_strategies() -> None:
    typer.echo("\n".join(StrategyRegistry.names()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
