from __future__ import annotations

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
    typer.echo(
        f"strategy={strategy} config_id={results[0].config_id if results else '-'} "
        f"date={as_of} names={len(results)} "
        f"hash={results[0].strategy_config_hash if results else '-'} "
        f"data_snapshot_id={snapshot_id}"
    )
    typer.echo(
        f"{'rank':<6}{'symbol':<10}{'final':>8}{'mkt':>8}{'glb':>8}"
        f"{'sec':>8}{'alpha':>8}{'crowd':>8}{'exec':>8}{'attent':>8}"
    )
    for idx, item in enumerate(results[:20], start=1):
        b = item.breakdown
        typer.echo(
            f"{idx:<6}{item.symbol:<10}{item.final_score:8.2f}{b.market_score:8.2f}"
            f"{b.global_score:8.2f}{b.sector_score:8.2f}{b.alpha_score:8.2f}"
            f"{b.crowding_risk:8.2f}{b.execution_risk:8.2f}{b.attention_risk:8.2f}"
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
    typer.echo(
        f"window signal_end={result.window.signal_end} "
        f"entry_end={result.window.entry_end} valuation_end={result.window.valuation_end}"
    )
    typer.echo(f"open_positions_at_end: {result.open_positions_at_end}")
    for key, value in m.model_dump().items():
        if isinstance(value, float):
            typer.echo(f"{key}: {value:.6f}")
        else:
            typer.echo(f"{key}: {value}")


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
    from app.pipeline import load_store
    from app.preflight import preflight_research

    typer.echo("Offline research only. This command is read-only preflight and does not trade.")
    try:
        start_day = date.fromisoformat(start)
        end_day = date.fromisoformat(end)
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        result = preflight_research(
            store=load_store(settings),
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
    if result.sector_status:
        typer.echo(f"sector_score={result.sector_status}")
    typer.echo(f"signal_ready_start={result.signal_ready_start.isoformat()}")
    typer.echo(f"coverage={result.coverage_start.isoformat()}..{result.coverage_end.isoformat()}")
    typer.echo(f"trading_days={result.trading_days}")
    typer.echo(f"min_history_bars={result.min_history_bars}")
    typer.echo(f"required_history_bars={result.required_history_bars}")
    typer.echo(f"data_snapshot_id={result.snapshot_id}")
    typer.echo("预检只读，不能证明策略收益有效")


@app.command("list-strategies")
def list_strategies() -> None:
    typer.echo("\n".join(StrategyRegistry.names()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
