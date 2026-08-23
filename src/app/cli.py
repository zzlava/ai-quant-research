from __future__ import annotations

from datetime import date
from typing import Annotated

import typer

from app.demo.generator import DEMO_SEED, generate_demo_market, write_demo_parquet
from app.pipeline import run_backtest, run_score
from app.settings import get_settings
from app.strategies.registry import StrategyRegistry

app = typer.Typer(help="A-share research scoring and historical backtest CLI.")


@app.command("generate-demo")
def generate_demo() -> None:
    settings = get_settings()
    bundle = generate_demo_market(seed=DEMO_SEED)
    write_demo_parquet(bundle, settings.parquet_dir)
    typer.echo(
        f"wrote demo parquet to {settings.parquet_dir} "
        f"({len(bundle.calendar)} trading days, "
        f"{sum(1 for i in bundle.instruments if not i.is_index and not i.is_global)} stocks)"
    )


@app.command()
def score(
    date_: Annotated[str, typer.Option("--date", help="YYYY-MM-DD")],
    strategy: Annotated[str, typer.Option("--strategy")] = "baseline_v1",
) -> None:
    as_of = date.fromisoformat(date_)
    results = run_score(as_of, strategy)
    typer.echo(
        f"strategy={strategy} date={as_of} names={len(results)} "
        f"hash={results[0].strategy_config_hash if results else '-'}"
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


@app.command("list-strategies")
def list_strategies() -> None:
    typer.echo("\n".join(StrategyRegistry.names()))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
