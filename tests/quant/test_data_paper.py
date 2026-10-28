"""Data integrity, restart idempotency and transactional replay tests."""

import datetime as dt
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from emtl.cli import main
from emtl.quant import (
    Bar,
    Costs,
    EastmoneyDailyProvider,
    Instrument,
    QuantConfig,
    RiskPolicy,
    StrategySpec,
    load_csv,
    run_paper,
    write_csv,
)
from emtl.quant.paper import latest_completed_date


def config(tmp_path: Path) -> QuantConfig:
    data = [Bar(dt.date(2024, 1, i), "510300.SH", 10, 10, 10, 10, 1_000_000) for i in range(2, 7)]
    path = tmp_path / "bars.csv"
    write_csv(path, data)
    return QuantConfig(
        path,
        (Instrument("510300.SH"),),
        StrategySpec(name="buy_hold"),
        Costs(),
        RiskPolicy(),
        rebalance="daily",
    )


def test_csv_roundtrip_and_bad_rows(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    data = load_csv(cfg.data)
    assert len(data) == 5
    write_csv(cfg.data, data * 2)
    with pytest.raises(ValueError, match="duplicate"):
        load_csv(cfg.data)
    with pytest.raises(ValueError, match="OHLC"):
        replace(data[0], low=11)
    with pytest.raises(ValueError, match="finite"):
        replace(data[0], close=float("nan"))


def test_provider_maps_market_and_share_volume_without_network() -> None:
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": {"klines": ["2024-01-02,10,11,12,9,100"]}},
        )

    provider = EastmoneyDailyProvider(session=SimpleNamespace(get=get))
    result = provider.fetch(Instrument("510300.SH"), dt.date(2024, 1, 1), dt.date(2024, 1, 3))
    assert calls[0][1]["params"]["secid"] == "1.510300"
    assert result[0].volume == 10000
    assert result[0].close == 11


def test_paper_resume_and_retry_do_not_duplicate_fills(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    database = tmp_path / "paper.db"
    first, count = run_paper(cfg, database, asof=dt.date(2024, 1, 4))
    assert count == 3
    again, count = run_paper(cfg, database, asof=dt.date(2024, 1, 4))
    assert count == 0
    assert first.fills == again.fills
    resumed, count = run_paper(cfg, database, asof=dt.date(2024, 1, 6))
    assert count == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM fills").fetchone()[0] == len(resumed.fills)
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 5


def test_paper_rejects_changed_history_config_or_backwards_time(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    database = tmp_path / "paper.db"
    run_paper(cfg, database, asof=dt.date(2024, 1, 5))
    with pytest.raises(ValueError, match="configuration changed"):
        run_paper(replace(cfg, initial_cash=5000), database, asof=dt.date(2024, 1, 6))
    with pytest.raises(ValueError, match="backwards"):
        run_paper(cfg, database, asof=dt.date(2024, 1, 4))
    data = load_csv(cfg.data)
    write_csv(cfg.data, [replace(data[0], volume=12), *data[1:]])
    with pytest.raises(ValueError, match="historical data"):
        run_paper(cfg, database, asof=dt.date(2024, 1, 6))
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 4


def test_completed_bar_cutoff_uses_shanghai() -> None:
    assert latest_completed_date(dt.datetime(2024, 1, 2, 7, tzinfo=dt.UTC)) == dt.date(2024, 1, 1)
    assert latest_completed_date(dt.datetime(2024, 1, 2, 8, tzinfo=dt.UTC)) == dt.date(2024, 1, 2)


def test_cli_demo_backtest_and_paper_need_no_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_login(*args, **kwargs):
        pytest.fail("quant commands must not log in")

    monkeypatch.setattr("emtl._client.EMTClient.login", forbid_login)
    directory = tmp_path / "demo"
    assert main(["quant", "demo", "--directory", str(directory)]) == 0
    config_path = str(directory / "strategy.toml")
    output = str(tmp_path / "report.json")
    assert main(["quant", "backtest", "--config", config_path, "--output", output]) == 0
    assert (
        main(
            [
                "quant",
                "paper",
                "--config",
                config_path,
                "--database",
                str(tmp_path / "paper.db"),
                "--asof",
                "2025-12-31",
                "--output",
                output,
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "quant",
                "paper",
                "--config",
                config_path,
                "--database",
                str(tmp_path / "paper.db"),
                "--asof",
                "2025-12-31",
                "--output",
                output,
            ]
        )
        == 0
    )
