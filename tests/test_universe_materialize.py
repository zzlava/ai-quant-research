from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml
from typer.testing import CliRunner

from app.cli import app as cli_app
from app.errors import DataQualityError
from app.models.config import StrategyConfig
from app.providers.tushare_client import TOKEN_ENV
from app.storage.quality import validate_universe_membership
from app.universe.materialize import (
    build_universe_membership,
    materialize_daily_membership,
    read_trade_calendar_file,
    read_universe_snapshots_file,
)
from app.universe.membership import read_universe_membership_file
from tests.helpers import CONFIG_DIR, PROJECT_ROOT, load_test_config, weekdays

A = "000001.SZ"
B = "600000.SH"
C = "600519.SH"


def _hist_config(expected: int | None = 2) -> StrategyConfig:
    config = load_test_config()
    config.universe.mode = "historical_membership"
    config.universe.id = "csi300"
    config.universe.expected_constituents = expected
    return config


def _write_csv(path: Path, header: str, rows: list[str]) -> Path:
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _write_calendar(path: Path, days: list[date]) -> Path:
    return _write_csv(path, "date", [day.isoformat() for day in days])


def _write_snapshots(path: Path, rows: list[str]) -> Path:
    return _write_csv(
        path,
        "universe_id,effective_from,symbol,available_at,weight",
        rows,
    )


def _snapshot_rows(
    effective_from: date,
    members: list[tuple[str, str]],
    available_at: str,
) -> list[str]:
    return [
        f"csi300,{effective_from.isoformat()},{symbol},{available_at},{weight}"
        for symbol, weight in members
    ]


def test_forward_hold_then_rebalance(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 8)
    start, end = calendar[1], calendar[6]
    baseline = date(2023, 12, 29)
    switch = calendar[4]
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(baseline, [(A, "0.42"), (B, "0.38")], "2023-12-28T16:00:00Z")
        + _snapshot_rows(switch, [(A, "0.41"), (C, "0.39")], "2024-01-04T16:00:00Z"),
    )
    cal = _write_calendar(tmp_path / "cal.csv", [baseline, *calendar])
    result = build_universe_membership(
        snapshots_file=snapshots,
        calendar_file=cal,
        config=_hist_config(),
        start=start,
        end=end,
        output=tmp_path / "out.csv",
    )
    frame = result.frame
    days = [day for day in calendar if start <= day <= end]
    assert sorted({day for day in frame["as_of_date"].to_list()}) == days
    assert frame.filter(pl.col("as_of_date") < switch)["symbol"].unique().sort().to_list() == [A, B]
    assert frame.filter(pl.col("as_of_date") >= switch)["symbol"].unique().sort().to_list() == [A, C]
    early_stamp = frame.filter(pl.col("as_of_date") == start)["available_at"].to_list()[0]
    late_stamp = frame.filter(pl.col("as_of_date") == switch)["available_at"].to_list()[0]
    assert early_stamp != late_stamp
    assert result.snapshot_count == 2
    assert result.trading_days == len(days)
    assert result.members_per_day == "2"


def test_late_available_at_does_not_switch_early(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    switch = calendar[2]
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(calendar[0], [(A, "0.5"), (B, "0.5")], "2024-01-01T16:00:00Z")
        + _snapshot_rows(switch, [(A, "0.5"), (C, "0.5")], "2024-01-04T08:00:00Z"),
    )
    # 2024-01-04 08:00 UTC is after 15:00 Asia/Shanghai (07:00 UTC) on 2024-01-04.
    assert switch == date(2024, 1, 4)
    frame = materialize_daily_membership(
        read_universe_snapshots_file(snapshots),
        read_trade_calendar_file(_write_calendar(tmp_path / "cal.csv", calendar)),
        _hist_config(),
        calendar[0],
        calendar[-1],
    )
    assert set(frame.filter(pl.col("as_of_date") == switch)["symbol"].to_list()) == {A, B}
    assert set(frame.filter(pl.col("as_of_date") == calendar[3])["symbol"].to_list()) == {A, C}


def test_available_at_microseconds_are_preserved_across_decision_boundary(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 6)
    switch = date(2024, 1, 4)
    assert switch in calendar
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(calendar[0], [(A, "0.5"), (B, "0.5")], "2024-01-01T16:00:00Z")
        + _snapshot_rows(switch, [(A, "0.5"), (C, "0.5")], "2024-01-04T07:00:00.000001Z"),
    )
    output = tmp_path / "out.csv"
    result = build_universe_membership(
        snapshots_file=snapshots,
        calendar_file=_write_calendar(tmp_path / "cal.csv", calendar),
        config=_hist_config(),
        start=calendar[0],
        end=calendar[-1],
        output=output,
    )
    assert set(result.frame.filter(pl.col("as_of_date") == switch)["symbol"].to_list()) == {A, B}
    assert set(result.frame.filter(pl.col("as_of_date") == calendar[3])["symbol"].to_list()) == {A, C}
    text = output.read_text(encoding="utf-8")
    assert "2024-01-04T07:00:00.000001Z" in text
    loaded = read_universe_membership_file(output)
    late = loaded.filter(pl.col("as_of_date") == calendar[3])["available_at"].to_list()[0]
    assert late.microsecond == 1
    assert set(loaded.filter(pl.col("as_of_date") == switch)["symbol"].to_list()) == {A, B}


def test_future_snapshot_is_not_backfilled(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 5)
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(calendar[2], [(A, ""), (B, "")], "2024-01-03T16:00:00Z"),
    )
    with pytest.raises(DataQualityError, match=calendar[0].isoformat()):
        materialize_daily_membership(
            read_universe_snapshots_file(snapshots),
            read_trade_calendar_file(_write_calendar(tmp_path / "cal.csv", calendar)),
            _hist_config(),
            calendar[0],
            calendar[-1],
        )


def test_missing_baseline_names_first_unbuildable_day(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 4)
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(calendar[0], [(A, ""), (B, "")], "2024-01-02T08:00:00Z"),
    )
    with pytest.raises(DataQualityError, match=f"cannot be built for {calendar[0].isoformat()}"):
        materialize_daily_membership(
            read_universe_snapshots_file(snapshots),
            read_trade_calendar_file(_write_calendar(tmp_path / "cal.csv", calendar)),
            _hist_config(),
            calendar[0],
            calendar[-1],
        )


def test_expected_constituents_and_mixed_available_at(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    short = _write_snapshots(
        tmp_path / "short.csv",
        [f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,0.5"],
    )
    with pytest.raises(DataQualityError, match="expected_constituents=2"):
        materialize_daily_membership(
            read_universe_snapshots_file(short),
            read_trade_calendar_file(_write_calendar(tmp_path / "cal.csv", calendar)),
            _hist_config(2),
            calendar[0],
            calendar[-1],
        )
    mixed = _write_snapshots(
        tmp_path / "mixed.csv",
        [
            f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,0.5",
            f"csi300,{calendar[0].isoformat()},{B},2024-01-01T17:00:00Z,0.5",
        ],
    )
    with pytest.raises(DataQualityError, match="mixed available_at"):
        read_universe_snapshots_file(mixed)


def test_snapshot_and_calendar_quality_rejections(tmp_path: Path) -> None:
    calendar = weekdays(date(2024, 1, 2), 3)
    cal = _write_calendar(tmp_path / "cal.csv", calendar)
    dup = _write_snapshots(
        tmp_path / "dup.csv",
        [
            f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,",
            f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,",
        ],
    )
    with pytest.raises(DataQualityError, match="duplicate primary key"):
        read_universe_snapshots_file(dup)
    with pytest.raises(DataQualityError, match="suffixes are not inferred"):
        read_universe_snapshots_file(
            _write_snapshots(tmp_path / "sym.csv", [f"csi300,{calendar[0].isoformat()},000001,2024-01-01T16:00:00Z,"])
        )
    with pytest.raises(DataQualityError, match="invalid"):
        read_universe_snapshots_file(
            _write_snapshots(tmp_path / "date.csv", [f"csi300,not-a-date,{A},2024-01-01T16:00:00Z,"])
        )
    with pytest.raises(DataQualityError, match="available_at"):
        read_universe_snapshots_file(
            _write_snapshots(tmp_path / "tz.csv", [f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00-05:00,"])
        )
    with pytest.raises(DataQualityError, match="finite float"):
        read_universe_snapshots_file(
            _write_snapshots(tmp_path / "wt.csv", [f"csi300,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,inf"])
        )
    with pytest.raises(DataQualityError, match="does not match"):
        materialize_daily_membership(
            read_universe_snapshots_file(
                _write_snapshots(
                    tmp_path / "ids.csv",
                    [
                        f"other,{calendar[0].isoformat()},{A},2024-01-01T16:00:00Z,",
                        f"other,{calendar[0].isoformat()},{B},2024-01-01T16:00:00Z,",
                    ],
                )
            ),
            read_trade_calendar_file(cal),
            _hist_config(),
            calendar[0],
            calendar[-1],
        )
    with pytest.raises(DataQualityError, match="duplicate date"):
        read_trade_calendar_file(_write_csv(tmp_path / "dupcal.csv", "date", ["2024-01-02", "2024-01-02"]))
    with pytest.raises(DataQualityError, match="not covered by the trade calendar"):
        materialize_daily_membership(
            read_universe_snapshots_file(
                _write_snapshots(
                    tmp_path / "ok.csv",
                    _snapshot_rows(calendar[0], [(A, ""), (B, "")], "2024-01-01T16:00:00Z"),
                )
            ),
            read_trade_calendar_file(cal),
            _hist_config(),
            date(2023, 12, 1),
            calendar[-1],
        )


def _write_hist_yaml(dest: Path, expected: int = 2) -> Path:
    with (CONFIG_DIR / "baseline_v1.yaml").open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    payload["universe"]["mode"] = "historical_membership"
    payload["universe"]["id"] = "csi300"
    payload["universe"]["expected_constituents"] = expected
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / "hist_two.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return dest


def test_cli_rejects_manual_static_and_does_not_read_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    monkeypatch.setenv(TOKEN_ENV, "should-not-be-read")
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(PROJECT_ROOT / "config"))
    calendar = weekdays(date(2024, 1, 2), 3)
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(calendar[0], [(A, ""), (B, "")], "2024-01-01T16:00:00Z"),
    )
    cal = _write_calendar(tmp_path / "cal.csv", calendar)
    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "build-universe-membership",
            "--snapshots-file",
            str(snapshots),
            "--calendar-file",
            str(cal),
            "--start",
            calendar[0].isoformat(),
            "--end",
            calendar[-1].isoformat(),
            "--strategy",
            "baseline_real_cn_v1",
            "--output",
            str(tmp_path / "out.csv"),
        ],
    )
    assert result.exit_code != 0
    combined = ((result.stdout or "") + (result.stderr or "")).lower()
    assert "manual_static" in combined or "historical_membership" in combined
    assert "should-not-be-read" not in combined


def test_cli_default_refuses_overwrite_and_success_is_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", boom)
    strategies = _write_hist_yaml(tmp_path / "config" / "strategies")
    monkeypatch.setenv("AIQ_CONFIG_DIR", str(tmp_path / "config"))
    calendar = weekdays(date(2024, 1, 2), 4)
    snapshots = _write_snapshots(
        tmp_path / "snap.csv",
        _snapshot_rows(calendar[0], [(A, "0.5"), (B, "0.5")], "2024-01-01T16:00:00Z"),
    )
    cal = _write_calendar(tmp_path / "cal.csv", calendar)
    output = tmp_path / "members.csv"
    args = [
        "build-universe-membership",
        "--snapshots-file",
        str(snapshots),
        "--calendar-file",
        str(cal),
        "--start",
        calendar[0].isoformat(),
        "--end",
        calendar[-1].isoformat(),
        "--strategy",
        "hist_two",
        "--output",
        str(output),
    ]
    runner = CliRunner()
    first = runner.invoke(cli_app, args)
    assert first.exit_code == 0, first.stdout + (first.stderr or "")
    assert "仅生成离线研究成员文件，不交易" in (first.stdout or "")
    assert "universe_id=csi300" in (first.stdout or "")
    loaded = read_universe_membership_file(output)
    validate_universe_membership(
        loaded,
        calendar,
        pl.DataFrame({"symbol": [A, B], "is_index": [False, False], "is_global": [False, False]}),
        universe_id="csi300",
        expected_constituents=2,
    )
    blocked = runner.invoke(cli_app, args)
    assert blocked.exit_code != 0
    assert "already exists" in ((blocked.stdout or "") + (blocked.stderr or "")).lower()
    output.write_text(output.read_text(encoding="utf-8") + "# stale\n", encoding="utf-8")
    overwritten = runner.invoke(cli_app, [*args, "--overwrite"])
    assert overwritten.exit_code == 0, overwritten.stdout + (overwritten.stderr or "")
    again = read_universe_membership_file(output)
    assert again.height == loaded.height
    assert strategies.exists()
