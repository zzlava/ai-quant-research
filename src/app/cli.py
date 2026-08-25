from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import typer

from app.demo.generator import DEMO_SEED, generate_demo_market, write_demo_parquet
from app.errors import sanitize_error_message
from app.pipeline import run_backtest, run_score
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
    universe_membership_file: Annotated[
        Path | None, typer.Option("--universe-membership-file", dir_okay=False)
    ] = None,
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
        f"{'crowd':>8}{'exec':>8}{'attent':>8}{'regime':>8}"
    )
    for idx, item in enumerate(results[:20], start=1):
        b = item.breakdown
        typer.echo(
            f"{idx:<6}{item.symbol:<10}{item.final_score:8.2f}{b.market_score:8.2f}"
                f"{b.global_score:8.2f}{b.sector_score:8.2f}{b.alpha_score:8.2f}"
                f"{b.quality_score:8.2f}{b.improvement_score:8.2f}{b.value_score:8.2f}"
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
            "WARNING: final_equity includes marked-to-market open positions; "
            "future liquidation costs are not included"
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
    typer.echo(f"signal_orders_deferred: {attribution.signal.orders_deferred}")
    typer.echo(f"signal_entry_deferral_days: {attribution.signal.entry_deferral_days}")
    typer.echo(
        "signal_orders_filled_after_deferral: "
        f"{attribution.signal.orders_filled_after_deferral}"
    )
    typer.echo(
        "signal_deferred_orders_expired: "
        f"{attribution.signal.deferred_orders_expired}"
    )


@app.command("fetch-tushare")
def fetch_tushare_cmd(
    start: Annotated[str, typer.Option("--start", help="YYYY-MM-DD")],
    end: Annotated[str, typer.Option("--end", help="YYYY-MM-DD")],
    strategy: Annotated[str, typer.Option("--strategy")],
    symbols_file: Annotated[Path | None, typer.Option("--symbols-file", dir_okay=False)] = None,
    universe_membership_file: Annotated[
        Path | None, typer.Option("--universe-membership-file", dir_okay=False)
    ] = None,
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
            raise ValueError(
                "fetch-tushare-latest-all-a-share requires research_scope=latest_market_snapshot"
            )
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
                typer.echo(
                    f"progress={done}/{total} api={api_name} "
                    f"partition={'reused' if reused else 'fetched'}"
                )

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
    staging_dir: Annotated[
        Path, typer.Option("--staging-dir", exists=True, file_okay=False)
    ],
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
                typer.echo(
                    f"progress={done}/{total} api={api_name} "
                    f"partition={'reused' if reused else 'fetched'}"
                )

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
                typer.echo(
                    f"progress={done}/{total} api={api_name} "
                    f"partition={'reused' if reused else 'fetched'}"
                )

        def fallback_progress(symbol: str, done: int, total: int, day: date) -> None:
            if done == 1 or done == total or done % 50 == 0:
                typer.echo(
                    f"share_float_fallback={done}/{total} symbol={symbol} "
                    f"ann_date={day.isoformat()}"
                )

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

    typer.echo(
        "Offline verification only. This command does not fetch data, score stocks, or trade."
    )
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
        destination = output_dir or (
            settings.data_dir / "event-diagnostics" / f"{strategy}-{decision_day.isoformat()}"
        )
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
        destination = output_dir or (
            settings.data_dir / "event-candidate-diagnostics" / report.diagnostic_version
        )
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
            if market_dir.resolve() != expected_market.resolve() and market_dir.resolve() != (
                Path.cwd() / expected_market
            ).resolve():
                raise ValueError("--market-dir does not match the authorization market_dir")
        resolved_event = event_dir or Path(authorization.event_dir)
        if event_dir is not None:
            expected_event = Path(authorization.event_dir)
            if event_dir.resolve() != expected_event.resolve() and event_dir.resolve() != (
                Path.cwd() / expected_event
            ).resolve():
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
    typer.echo(
        f"announcement_window={report.announcement_window_start}..{report.announcement_window_end}"
    )
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
    typer.echo(
        "collection_source_manifest_sha256="
        f"{report.collection_source_manifest_sha256}"
    )
    typer.echo(
        "collection_quality_report_sha256="
        f"{report.collection_quality_report_sha256}"
    )
    typer.echo(f"window={report.window_start}..{report.window_end}")
    typer.echo(f"annual_source_rows={len(report.annual_by_source)}")
    typer.echo(f"pit_probes={len(report.pit_availability_probes)}")
    typer.echo(
        "raw_collection_holder_rows="
        f"{report.holder_count_missingness.raw_collection_holder_rows}"
    )
    typer.echo(
        "raw_collection_holder_num_blank_rows="
        f"{report.holder_count_missingness.raw_collection_holder_num_blank_rows}"
    )
    typer.echo(
        "canonical_holder_rows_in_window="
        f"{report.holder_count_missingness.canonical_holder_rows_in_window}"
    )
    typer.echo(
        "symbols_with_no_observable_canonical_holder_data="
        f"{report.holder_count_missingness.symbols_with_no_observable_canonical_holder_data}"
    )
    typer.echo(
        "raw_collection_float_ratio_blank_rows="
        f"{report.unlock_ratio_coverage.raw_collection_float_ratio_blank_rows}"
    )
    typer.echo(
        "canonical_float_ratio_known_rows="
        f"{report.unlock_ratio_coverage.canonical_float_ratio_known_rows}"
    )
    typer.echo(
        "canonical_float_ratio_missing_rows="
        f"{report.unlock_ratio_coverage.canonical_float_ratio_missing_rows}"
    )
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
        f"effective_from_coverage={result.effective_from_start.isoformat()}.."
        f"{result.effective_from_end.isoformat()}"
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
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
) -> None:
    """Read-only IC diagnostic; forward returns are labels, not signal inputs."""
    from app.pipeline import load_research_store
    from app.preflight import preflight_research
    from app.research.ic import analyze_ic, write_ic_report

    typer.echo("Offline research only. Forward returns are diagnostic labels and are never trading inputs.")
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
            progress=progress,
        )
        if output is not None:
            write_ic_report(report, output)
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"strategy_config_hash={report.strategy_config_hash}")
    typer.echo(f"data_snapshot_id={report.data_snapshot_id}")
    typer.echo("horizon factor observations mean_spearman_ic t_stat")
    for item in report.summaries:
        mean = "-" if item.mean_spearman_ic is None else f"{item.mean_spearman_ic:.6f}"
        t_stat = "-" if item.t_stat is None else f"{item.t_stat:.4f}"
        typer.echo(f"{item.horizon_days:>7} {item.factor:<18} {item.observations:>12} {mean:>16} {t_stat:>8}")
    for period in [*report.annual_periods, *report.rolling_periods]:
        typer.echo(f"period={period.label} coverage={period.start.isoformat()}..{period.end.isoformat()}")
        for item in period.summaries:
            mean = "-" if item.mean_spearman_ic is None else f"{item.mean_spearman_ic:.6f}"
            t_stat = "-" if item.t_stat is None else f"{item.t_stat:.4f}"
            typer.echo(
                f"{item.horizon_days:>7} {item.factor:<18} {item.observations:>12} {mean:>16} {t_stat:>8}"
            )
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
    typer.echo(
        "horizon days names top_k_gross top_k_net net_t top_quantile bottom_quantile spread turnover"
    )
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
    positions: Annotated[
        str, typer.Option("--positions", help="comma-separated maximum position counts")
    ] = "3,5,8,10",
    holding_days: Annotated[
        str, typer.Option("--holding-days", help="comma-separated fixed holding/signal intervals")
    ] = "10,20,40",
    training_start: Annotated[str, typer.Option("--training-start")] = "2022-04-01",
    training_end: Annotated[str, typer.Option("--training-end")] = "2022-12-30",
    validation_start: Annotated[str, typer.Option("--validation-start")] = "2023-01-03",
    validation_end: Annotated[str, typer.Option("--validation-end")] = "2023-12-29",
    holdout_start: Annotated[str, typer.Option("--holdout-start")] = "2024-01-02",
    holdout_end: Annotated[str, typer.Option("--holdout-end")] = "2024-12-31",
    minimum_training_trades: Annotated[
        int, typer.Option("--minimum-training-trades", min=1)
    ] = 4,
    liquidation_buffer_days: Annotated[
        int, typer.Option("--liquidation-buffer-days", min=0)
    ] = 10,
    cache_dir: Annotated[Path | None, typer.Option("--cache-dir", file_okay=False)] = None,
    output: Annotated[Path | None, typer.Option("--output", dir_okay=False)] = None,
    selected_config_output: Annotated[
        Path | None, typer.Option("--selected-config-output", dir_okay=False)
    ] = None,
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
        parsed_horizons = [
            int(value.strip()) for value in holding_days.split(",") if value.strip()
        ]
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
        resolved_config = selected_config_output or (
            settings.strategies_dir / f"{strategy}_portfolio_selected_v2.yaml"
        )
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

    selected = next(
        item for item in report.evaluations if item.candidate.candidate_id == report.selected_candidate_id
    )
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
    selection_report_path: Annotated[
        Path, typer.Option("--selection-report", exists=True, dir_okay=False)
    ],
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
        selection = PortfolioConstructionReport.model_validate_json(
            selection_report_path.read_text(encoding="utf-8")
        )
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
        typer.echo(
            f"training_loss symbol={item.symbol} sector={item.sector} net_pnl={item.net_pnl:.2f}"
        )
    for scenario in report.cost_scenarios:
        rendered = ",".join(
            f"{period.label}:{period.total_return:.6f}" for period in scenario.periods
        )
        typer.echo(f"cost_scenario={scenario.scenario_id} returns={rendered}")
    for gate in report.gates:
        typer.echo(f"gate={gate.gate} passed={gate.passed} observed={gate.observed}")
    for warning in report.warnings:
        typer.echo(f"warning={warning}")
    typer.echo(f"status={report.status} reason={report.status_reason}")
    typer.echo(f"score_cache_hits={report.score_cache_hits} misses={report.score_cache_misses}")
    typer.echo(f"report={resolved_output}")


@app.command("list-strategies")
def list_strategies() -> None:
    typer.echo("\n".join(StrategyRegistry.names()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
