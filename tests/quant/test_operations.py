"""Daily operations, concurrent restart, risk and data-provider boundary checks."""

import datetime as dt
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from emtl.cli import main
from emtl.quant import (
    AccountSnapshot,
    Backtester,
    Bar,
    Costs,
    EastmoneyDailyProvider,
    Instrument,
    RiskPolicy,
    StrategySpec,
    load_config,
    plan_rebalance,
    run_paper,
)
from emtl.quant.demo import DEMO_CONFIG


def test_every_strategy_runs_with_real_fills_on_synthetic_market(tmp_path: Path) -> None:
    assert main(["quant", "demo", "--directory", str(tmp_path / "demo")]) == 0
    for strategy in (
        "buy_hold",
        "dual_ma",
        "time_series_momentum",
        "momentum_rotation",
        "donchian",
        "bollinger",
        "inverse_volatility",
    ):
        output = tmp_path / f"{strategy}.json"
        assert (
            main(
                [
                    "quant",
                    "backtest",
                    "--config",
                    str(tmp_path / "demo/strategy.toml"),
                    "--strategy",
                    strategy,
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
        payload = json.loads(output.read_text())
        assert payload["metrics"]["sessions"] > 700
        assert payload["metrics"]["fills"] > 0
        assert payload["metrics"]["cash"] >= 0
        assert all(fill["quantity"] % 100 == 0 for fill in payload["fills"])


def test_concurrent_paper_retries_share_one_ledger(tmp_path: Path) -> None:
    main(["quant", "demo", "--directory", str(tmp_path / "demo")])
    config = load_config(tmp_path / "demo/strategy.toml")
    database = tmp_path / "paper.db"

    def run():
        return run_paper(config, database, asof=dt.date(2025, 12, 31))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(count for _, count in results)[0] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM fills").fetchone()[0] == len(
            results[0][0].fills
        )


def test_daily_fetch_append_then_paper_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "strategy.toml").write_text(
        DEMO_CONFIG.replace('start = "2023-01-01"', 'start = "2024-01-01"')
    )
    calls = []

    def fetch(self, instrument, start, end):
        calls.append((instrument.symbol, start, end))
        return [Bar(dt.date(2024, 1, 2), instrument.symbol, 10, 10, 10, 10, 10000)]

    monkeypatch.setattr(EastmoneyDailyProvider, "fetch", fetch)
    args = [
        "quant",
        "daily",
        "--config",
        str(tmp_path / "strategy.toml"),
        "--start",
        "2024-01-01",
        "--asof",
        "2024-01-02",
        "--database",
        str(tmp_path / "paper.db"),
        "--output",
        str(tmp_path / "paper.json"),
    ]
    assert main(args) == 0
    assert len(calls) == 3
    assert main(args) == 0
    assert len(calls) == 3
    assert json.loads((tmp_path / "paper.json").read_text())["paper"]["new_sessions"] == 0


def test_partial_download_failure_does_not_write_partial_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "strategy.toml"
    path.write_text(DEMO_CONFIG)
    calls = []

    def fetch(self, instrument, start, end):
        calls.append(instrument.symbol)
        if len(calls) == 2:
            raise ValueError("provider unavailable")
        return [Bar(dt.date(2024, 1, 2), instrument.symbol, 10, 10, 10, 10, 10000)]

    monkeypatch.setattr(EastmoneyDailyProvider, "fetch", fetch)
    assert (
        main(
            [
                "quant",
                "fetch",
                "--config",
                str(path),
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-03",
            ]
        )
        == 1
    )
    assert not (tmp_path / "bars.csv").exists()


def test_provider_empty_holiday_and_bad_shape() -> None:
    def provider(payload):
        return EastmoneyDailyProvider(
            session=SimpleNamespace(
                get=lambda *a, **k: SimpleNamespace(
                    raise_for_status=lambda: None, json=lambda: payload
                )
            )
        )

    instrument = Instrument("510300.SH")
    date = dt.date(2024, 1, 1)
    assert provider({"rc": 0, "data": None}).fetch(instrument, date, date) == []
    with pytest.raises(ValueError, match="invalid daily-data"):
        provider([]).fetch(instrument, date, date)


def test_order_plan_reserves_fees_and_sellable_odd_lots() -> None:
    now = dt.datetime(2024, 1, 2, tzinfo=dt.UTC)
    account = AccountSnapshot(1000, {}, {}, now)
    orders = plan_rebalance(
        {"510300.SH": 1},
        account,
        {"510300.SH": 1},
        [Instrument("510300.SH")],
        now=now,
        quote_time=now,
        cash_reserve=0,
    )
    assert orders[0].quantity == 900
    account = AccountSnapshot(0, {"510300.SH": 150}, {"510300.SH": 150}, now)
    orders = plan_rebalance(
        {}, account, {"510300.SH": 1}, [Instrument("510300.SH")], now=now, quote_time=now
    )
    assert orders[0].quantity == 150


def test_future_corporate_action_does_not_affect_past_equity() -> None:
    instrument = Instrument("510300.SH", price_limit=None)
    data = [
        Bar(dt.date(2024, 1, i), instrument.symbol, 10, 10, 10, 10, 100000) for i in range(2, 7)
    ]
    sim = Backtester(
        [instrument],
        StrategySpec(name="buy_hold"),
        risk=RiskPolicy(max_position=1, max_gross=1),
        costs=Costs(slippage_bps=0, commission=0, minimum_commission=0),
    )
    prefix = sim.run(data[:4])
    data[4] = replace(data[4], open=5, high=5, low=5, close=5, split_ratio=2)
    complete = sim.run(data)
    assert prefix.curve == complete.curve[:4]


def test_config_typos_are_errors(tmp_path: Path) -> None:
    path = tmp_path / "strategy.toml"
    path.write_text(DEMO_CONFIG + "\n[typo]\nlookback = 20\n")
    with pytest.raises(ValueError, match="unknown config"):
        load_config(path)
