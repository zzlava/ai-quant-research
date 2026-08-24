from __future__ import annotations

from datetime import date

from app.demo.generator import generate_demo_market
from app.providers.demo_provider import DemoProvider
from app.research.ic import analyze_ic, write_ic_report
from app.storage.memory import InMemoryStore
from tests.helpers import load_test_config


def test_ic_analysis_uses_as_of_scores_and_writes_a_reproducible_report(tmp_path) -> None:
    bundle = generate_demo_market(seed=7, n_stocks=12, start=date(2023, 1, 3), end=date(2024, 3, 29))
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))
    start = bundle.calendar[60]
    end = bundle.calendar[80]

    progress: list[tuple[int, int, date]] = []
    report = analyze_ic(
        store=store,
        config=load_test_config(),
        start=start,
        end=end,
        horizons=[1, 5],
        rolling_window_days=10,
        rolling_step_days=5,
        progress=lambda done, total, day: progress.append((done, total, day)),
    )

    assert report.data_snapshot_id == store.snapshot().snapshot_id
    assert report.horizons == [1, 5]
    assert len(report.summaries) == 22
    assert report.annual_periods[0].label == "2023"
    assert report.rolling_periods[0].start == start
    assert report.rolling_periods[0].end == bundle.calendar[69]
    assert any(item.observations > 0 for item in report.summaries)
    assert progress[0] == (1, 21, start)
    assert progress[-1] == (21, 21, end)
    output = tmp_path / "ic.json"
    write_ic_report(report, output)
    assert '"data_snapshot_id"' in output.read_text(encoding="utf-8")


def test_ic_analysis_rejects_partial_rolling_configuration() -> None:
    bundle = generate_demo_market(seed=7, n_stocks=12, start=date(2023, 1, 3), end=date(2024, 3, 29))
    store = InMemoryStore.from_provider(DemoProvider(bundle=bundle))

    try:
        analyze_ic(
            store=store,
            config=load_test_config(),
            start=bundle.calendar[60],
            end=bundle.calendar[80],
            horizons=[5],
            rolling_window_days=10,
        )
    except ValueError as exc:
        assert "configured together" in str(exc)
    else:
        raise AssertionError("partial rolling configuration must fail")
