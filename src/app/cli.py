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
    )
    typer.echo(f"imported market data from {source_dir}")
    typer.echo(f"source_name={snapshot.source_name} adjustment={snapshot.adjustment}")
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
        f"{'sec':>8}{'alpha':>8}{'crowd':>8}{'exec':>8}"
    )
    for idx, item in enumerate(results[:20], start=1):
        b = item.breakdown
        typer.echo(
            f"{idx:<6}{item.symbol:<10}{item.final_score:8.2f}{b.market_score:8.2f}"
            f"{b.global_score:8.2f}{b.sector_score:8.2f}{b.alpha_score:8.2f}"
            f"{b.crowding_risk:8.2f}{b.execution_risk:8.2f}"
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
    symbols_file: Annotated[Path, typer.Option("--symbols-file", exists=True, dir_okay=False)],
    source_version: Annotated[str | None, typer.Option("--source-version")] = None,
) -> None:
    """Pull Tushare history into the standardized snapshot. Research only; no trading."""
    from app.providers.tushare_client import LiveTushareClient, read_tushare_token
    from app.providers.tushare_fetch import fetch_tushare_and_import, read_symbols_file

    typer.echo("Historical research only. This command does not place orders or connect to a broker.")
    try:
        token = read_tushare_token()
        settings = get_settings()
        config = load_strategy_config(strategy, settings.strategies_dir)
        snapshot = fetch_tushare_and_import(
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            config=config,
            dest_dir=settings.parquet_dir,
            stocks=read_symbols_file(symbols_file),
            source_version=source_version,
            client=LiveTushareClient(token),
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(sanitize_error_message(exc), err=True)
        raise typer.Exit(code=1) from None
    stock_count = snapshot.row_counts.get("instruments", 0)
    typer.echo(f"source_name={snapshot.source_name}")
    typer.echo(f"coverage={snapshot.coverage_start}..{snapshot.coverage_end}")
    typer.echo(f"instruments={stock_count}")
    typer.echo(f"market_index={snapshot.market_index}")
    typer.echo(f"global_symbol={snapshot.global_symbol}")
    typer.echo(f"adjustment={snapshot.adjustment}")
    typer.echo(f"source_version={snapshot.source_version or '-'}")
    typer.echo(f"data_snapshot_id={snapshot.snapshot_id}")


@app.command("list-strategies")
def list_strategies() -> None:
    typer.echo("\n".join(StrategyRegistry.names()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
