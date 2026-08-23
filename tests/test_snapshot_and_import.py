from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from app.api.main import app
from app.cli import app as cli_app
from app.demo.generator import generate_demo_market, write_demo_parquet
from app.errors import DataQualityError, MissingBenchmarkError, SnapshotError
from app.pipeline import run_backtest, run_score
from app.providers._frames import bars_to_frame, global_to_frame, instruments_to_frame
from app.storage.hashing import build_snapshot
from app.storage.import_market import import_market_data
from app.storage.snapshot_io import load_verified_snapshot
from tests.helpers import PROJECT_ROOT


def _bundle():
    return generate_demo_market(
        seed=42,
        n_stocks=12,
        start=date(2023, 1, 3),
        end=date(2024, 3, 29),
    )


def _tables_from_bundle(bundle) -> dict[str, pl.DataFrame]:
    return {
        "daily_bars": bars_to_frame(bundle.daily_bars),
        "index_bars": bars_to_frame(bundle.index_bars),
        "global_bars": global_to_frame(bundle.global_bars),
        "instruments": instruments_to_frame(bundle.instruments),
        "calendar": pl.DataFrame({"date": bundle.calendar}).with_columns(pl.col("date").cast(pl.Date)),
    }


def _write_source(dest: Path, tables: dict[str, pl.DataFrame], fmt: str = "csv") -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        if fmt == "csv":
            frame.write_csv(dest / f"{name}.csv")
        else:
            frame.write_parquet(dest / f"{name}.parquet")
    return dest


def test_snapshot_id_changes_when_content_changes() -> None:
    tables = _tables_from_bundle(_bundle())
    base = build_snapshot(tables, adjustment="forward", source_name="demo")
    price = tables["daily_bars"].with_columns(pl.col("close") + 0.01)
    avail = tables["global_bars"].with_columns(pl.col("available_at") + pl.duration(hours=1))
    inst = tables["instruments"].with_columns(pl.col("name") + "_x")
    cal = tables["calendar"].filter(pl.col("date") != tables["calendar"]["date"].min())
    price_id = build_snapshot({**tables, "daily_bars": price}, adjustment="forward", source_name="demo")
    avail_id = build_snapshot({**tables, "global_bars": avail}, adjustment="forward", source_name="demo")
    inst_id = build_snapshot({**tables, "instruments": inst}, adjustment="forward", source_name="demo")
    cal_id = build_snapshot({**tables, "calendar": cal}, adjustment="forward", source_name="demo")
    assert price_id.snapshot_id != base.snapshot_id
    assert avail_id.snapshot_id != base.snapshot_id
    assert inst_id.snapshot_id != base.snapshot_id
    assert cal_id.snapshot_id != base.snapshot_id


def test_snapshot_id_stable_across_row_order_and_fetched_at() -> None:
    tables = _tables_from_bundle(_bundle())
    shuffled = {
        name: frame.sample(fraction=1.0, shuffle=True, seed=7) if frame.height else frame
        for name, frame in tables.items()
    }
    first = build_snapshot(tables, adjustment="forward", source_name="a")
    second = build_snapshot(shuffled, adjustment="forward", source_name="b")
    assert first.snapshot_id == second.snapshot_id
    assert first.content_hash == second.content_hash


def test_reimport_same_content_same_snapshot_id(tmp_path: Path) -> None:
    tables = _tables_from_bundle(_bundle())
    src = _write_source(tmp_path / "src", tables)
    first = import_market_data(src, tmp_path / "out1", source_name="local", adjustment="forward")
    second = import_market_data(src, tmp_path / "out2", source_name="other", adjustment="forward")
    assert first.snapshot_id == second.snapshot_id


def test_failed_import_does_not_break_existing_snapshot(tmp_path: Path) -> None:
    tables = _tables_from_bundle(_bundle())
    good = _write_source(tmp_path / "good", tables)
    dest = tmp_path / "parquet"
    snapshot = import_market_data(good, dest, source_name="local", adjustment="forward")
    stored = load_verified_snapshot(dest)
    assert stored.snapshot_id == snapshot.snapshot_id

    bad = tmp_path / "bad"
    bad.mkdir()
    for name in ("daily_bars", "index_bars", "instruments", "calendar"):
        tables[name].write_csv(bad / f"{name}.csv")
    with pytest.raises(DataQualityError, match="missing required table"):
        import_market_data(bad, dest, source_name="local", adjustment="forward")

    after = load_verified_snapshot(dest)
    assert after.snapshot_id == snapshot.snapshot_id
    assert (dest / "daily_bars.parquet").exists()
    assert (dest / "manifest.json").exists()


def test_missing_or_tampered_manifest_fails(tmp_path: Path) -> None:
    tables = _tables_from_bundle(_bundle())
    dest = tmp_path / "parquet"
    import_market_data(_write_source(tmp_path / "src", tables), dest, source_name="local", adjustment="forward")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SnapshotError, match="missing manifest"):
        load_verified_snapshot(empty)

    manifest = dest / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "deadbeef"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotError, match="does not match"):
        load_verified_snapshot(dest)


def test_import_rejects_missing_benchmark_and_available_at(tmp_path: Path) -> None:
    tables = _tables_from_bundle(_bundle())
    src = _write_source(tmp_path / "src", tables)
    with pytest.raises(MissingBenchmarkError, match="NO_INDEX"):
        import_market_data(
            src,
            tmp_path / "out",
            source_name="local",
            adjustment="forward",
            market_index="NO_INDEX",
        )

    broken = tables["global_bars"].drop("available_at")
    bad = _write_source(tmp_path / "bad", {**tables, "global_bars": broken})
    with pytest.raises(DataQualityError, match="available_at"):
        import_market_data(bad, tmp_path / "out2", source_name="local", adjustment="forward")


def test_score_backtest_cli_and_api_share_snapshot_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AIQ_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("AIQ_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")

    tables = _tables_from_bundle(_bundle())
    source = _write_source(tmp_path / "normalized", tables)
    imported = import_market_data(
        source,
        data_dir / "parquet",
        source_name="fixture",
        adjustment="forward",
        market_index="IDX_CSI300",
        global_symbol="GLB_SPX",
    )

    scores = run_score(date(2024, 1, 15), "baseline_v1")
    assert scores
    assert {row.data_snapshot_id for row in scores} == {imported.snapshot_id}

    persisted = pl.read_parquet(data_dir / "scores" / "scores.parquet")
    assert set(persisted["data_snapshot_id"].to_list()) == {imported.snapshot_id}

    bt = run_backtest("baseline_v1", date(2024, 1, 2), date(2024, 1, 31))
    assert bt.data_snapshot_id == imported.snapshot_id
    assert bt.data_snapshot is not None
    assert bt.data_snapshot.snapshot_id == imported.snapshot_id

    runner = CliRunner()
    scored = runner.invoke(cli_app, ["score", "--date", "2024-01-15", "--strategy", "baseline_v1"])
    assert scored.exit_code == 0, scored.output
    assert imported.snapshot_id in scored.output

    backed = runner.invoke(
        cli_app,
        ["backtest", "--strategy", "baseline_v1", "--start", "2024-01-02", "--end", "2024-01-31"],
    )
    assert backed.exit_code == 0, backed.output
    assert imported.snapshot_id in backed.output

    client = TestClient(app)
    ranking = client.get("/ranking", params={"date": "2024-01-15", "strategy": "baseline_v1", "top": 5})
    assert ranking.status_code == 200
    body = ranking.json()
    assert body["data_snapshot_id"] == imported.snapshot_id
    assert body["items"][0]["data_snapshot_id"] == imported.snapshot_id

    created = client.post(
        "/backtests",
        json={"strategy": "baseline_v1", "start": "2024-01-02", "end": "2024-01-31"},
    )
    assert created.status_code == 200
    result = created.json()["result"]
    assert result["data_snapshot_id"] == imported.snapshot_id
    fetched = client.get(f"/backtests/{created.json()['id']}")
    assert fetched.json()["result"]["data_snapshot_id"] == imported.snapshot_id


def test_missing_snapshot_fails_score_and_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    monkeypatch.setenv("AIQ_DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    with pytest.raises(SnapshotError, match="missing manifest"):
        run_score(date(2024, 1, 15), "baseline_v1")
    client = TestClient(app)
    ranking = client.get("/ranking", params={"date": "2024-01-15"})
    assert ranking.status_code == 400
    assert "token" not in ranking.json()["detail"].lower()
    assert "/Users/" not in ranking.json()["detail"]


def test_cli_import_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIQ_DATA_DIR", str(tmp_path / "data"))
    source = _write_source(tmp_path / "src", _tables_from_bundle(_bundle()))
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "import-market-data",
            "--source-dir",
            str(source),
            "--source-name",
            "offline",
            "--adjustment",
            "forward",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "data_snapshot_id=" in result.output
    verified = load_verified_snapshot(tmp_path / "data" / "parquet")
    assert verified.snapshot_id in result.output


def test_write_demo_parquet_is_verifiable(tmp_path: Path) -> None:
    snapshot = write_demo_parquet(_bundle(), tmp_path / "parquet")
    loaded = load_verified_snapshot(tmp_path / "parquet")
    assert loaded.snapshot_id == snapshot.snapshot_id


def test_one_ulp_price_change_changes_snapshot_id() -> None:
    tables = _tables_from_bundle(_bundle())
    daily = tables["daily_bars"]
    original = float(daily["close"][0])
    ulp = math.nextafter(original, math.inf)
    assert ulp != original
    changed = daily.with_columns(
        pl.when(pl.int_range(0, pl.len()) == 0).then(pl.lit(ulp)).otherwise(pl.col("close")).alias("close")
    )
    base = build_snapshot(tables, adjustment="forward", source_name="demo")
    other = build_snapshot({**tables, "daily_bars": changed}, adjustment="forward", source_name="demo")
    assert other.snapshot_id != base.snapshot_id
    assert other.table_hashes["daily_bars"] != base.table_hashes["daily_bars"]


def test_import_rejects_minus_five_offset_available_at(tmp_path: Path) -> None:
    tables = _tables_from_bundle(_bundle())
    offset = tables["global_bars"].with_columns(pl.lit("2024-01-02T16:00:00-05:00").alias("available_at"))
    src = _write_source(tmp_path / "src", {**tables, "global_bars": offset})
    with pytest.raises(DataQualityError, match="non-zero offsets"):
        import_market_data(src, tmp_path / "out", source_name="local", adjustment="forward")


def test_import_accepts_zulu_available_at_as_utc(tmp_path: Path) -> None:
    tables = _tables_from_bundle(_bundle())
    zulu = tables["global_bars"].with_columns(
        pl.col("available_at").dt.strftime("%Y-%m-%dT%H:%M:%SZ").alias("available_at")
    )
    src = _write_source(tmp_path / "src", {**tables, "global_bars": zulu})
    snapshot = import_market_data(src, tmp_path / "out", source_name="local", adjustment="forward")
    stored = pl.read_parquet(tmp_path / "out" / "global_bars.parquet")
    first = stored["available_at"][0]
    assert first.tzinfo is None
    assert snapshot.snapshot_id
