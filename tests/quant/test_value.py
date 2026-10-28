"""Value factors must be public at decision time, versioned, and reproducible."""

import csv
import datetime as dt
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from emtl.quant import (
    Bar,
    Costs,
    Instrument,
    QuantConfig,
    RiskPolicy,
    StrategySpec,
    load_csv,
    run_paper,
    write_csv,
)
from emtl.quant.value import (
    Fundamental,
    ValueSpec,
    ValueStore,
    ValueUniverse,
    latest_facts,
    screen_value,
)
from emtl.quant.value_provider import EastmoneyValueProvider

A, B, C = "600000.SH", "000001.SZ", "600004.SH"
ASOF = dt.date(2024, 1, 2)


def fact(symbol=A, **kwargs) -> Fundamental:
    return replace(
        Fundamental(
            symbol,
            dt.date(2022, 12, 31),
            dt.date(2023, 4, 1),
            ASOF,
            dt.date(2000, 1, 1),
            symbol,
            "工业",
            2,
            10,
            0.15,
            3,
            0.4,
        ),
        **kwargs,
    )


def policy(tmp_path: Path, **kwargs) -> ValueSpec:
    return replace(
        ValueSpec(tmp_path / "value.sqlite3", top_n=1, liquidity_window=1, min_daily_amount=0),
        **kwargs,
    )


def screen(spec, facts, date=ASOF):
    symbols = {f.symbol for f in facts}
    return screen_value(
        spec, facts, date, dict.fromkeys(symbols, 10), {s: [1_000_000] for s in symbols}, symbols
    )


def config(tmp_path: Path, facts=None) -> QuantConfig:
    spec = policy(tmp_path)
    ValueStore(spec.fundamentals).append(facts or [fact(), fact(B, annual_eps=1)])
    dates = [
        ASOF,
        ASOF + dt.timedelta(days=1),
        ASOF + dt.timedelta(days=2),
        dt.date(2024, 2, 1),
        dt.date(2024, 2, 2),
    ]
    bars = [Bar(date, symbol, 10, 10, 10, 10, 100000) for date in dates for symbol in (A, B)]
    path = tmp_path / "bars.csv"
    write_csv(path, bars)
    return QuantConfig(
        path,
        (Instrument(A, price_limit=None), Instrument(B, price_limit=None)),
        StrategySpec(name="buy_hold"),
        Costs(commission=0, minimum_commission=0, slippage_bps=0),
        RiskPolicy(max_position=1, max_gross=1, max_drawdown=1, stop_loss=1, trailing_stop=1),
        rebalance="monthly",
        value=spec,
    )


def test_future_fundamentals_are_not_usable(tmp_path: Path) -> None:
    future = fact(known_on=ASOF + dt.timedelta(days=1))
    result = screen(policy(tmp_path), [future])
    assert result["selected"] == []
    assert result["rejected"][A] == "no_known_financials"
    assert screen(policy(tmp_path), [future], future.known_on)["selected"] == [A]


def test_valuation_quality_ranking_and_industry_caps(tmp_path: Path) -> None:
    spec = policy(tmp_path, top_n=3, max_per_industry=1)
    result = screen(spec, [fact(), fact(B, annual_eps=1), fact(C, industry="消费", annual_eps=1)])
    assert result["selected"] == [A, C]
    assert result["ranking"][0]["pe_annual"] == 5
    assert result["ranking"][0]["pb_annual"] == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"is_st": True}, "st_or_inactive"),
        ({"is_active": False}, "st_or_inactive"),
        ({"annual_eps": -1}, "nonpositive_earnings_or_book"),
        ({"book_value_per_share": 0}, "nonpositive_earnings_or_book"),
        ({"roe": 0.01}, "quality_filter"),
        ({"debt_ratio": 0.9}, "quality_filter"),
        ({"operating_cash_flow_per_share": 0}, "quality_filter"),
        ({"annual_eps": None}, "missing_factors"),
        ({"industry": "银行"}, "excluded_industry"),
        ({"listed_on": dt.date(2023, 12, 1)}, "new_listing"),
        ({"annual_eps": 0.1}, "valuation_filter"),
    ],
)
def test_filter_reasons_are_auditable(tmp_path: Path, changes, reason) -> None:
    result = screen(policy(tmp_path), [fact(**changes)])
    assert result["selected"] == []
    assert result["rejected"][A] == reason


def test_store_idempotency_revision_and_transaction_rollback(tmp_path: Path) -> None:
    store = ValueStore(tmp_path / "value.sqlite3")
    assert store.append([fact()]) == 1
    assert store.append([fact()]) == 0
    with pytest.raises(ValueError, match="conflicting observation"):
        store.append([fact(B), fact(annual_eps=9)])
    assert len(store.load()) == 1
    correction = fact(annual_eps=9, known_on=ASOF + dt.timedelta(days=1))
    store.append([correction])
    assert latest_facts(store.load(), ASOF)[A].annual_eps == 2
    assert latest_facts(store.load(), correction.known_on)[A].annual_eps == 9


def test_latest_fiscal_period_wins_over_old_period_restated_later() -> None:
    current = fact(
        period_end=dt.date(2023, 12, 31),
        published_on=dt.date(2024, 3, 1),
        known_on=dt.date(2024, 3, 2),
    )
    old_revision = fact(known_on=dt.date(2024, 4, 1))
    assert latest_facts([current, old_revision], dt.date(2024, 4, 2))[A] == current


def test_monthly_pool_membership_and_next_open_exit(tmp_path: Path) -> None:
    cfg = config(
        tmp_path,
        [
            fact(),
            fact(B, annual_eps=1),
            fact(B, annual_eps=5, known_on=ASOF + dt.timedelta(days=2)),
        ],
    )

    result = cfg.engine().run(load_csv(cfg.data))
    assert result.universe_history[0]["selected"] == [A]
    assert result.universe_history[-1]["selected"] == [B]
    assert [(f.symbol, f.side, f.date) for f in result.fills] == [
        (A, "buy", ASOF + dt.timedelta(days=1)),
        (A, "sell", dt.date(2024, 2, 2)),
        (B, "buy", dt.date(2024, 2, 2)),
    ]


def test_quarterly_update_and_daily_safety_removal(tmp_path: Path) -> None:
    universe = ValueUniverse(
        policy(tmp_path, frequency="quarterly"),
        [fact(), fact(is_st=True, known_on=ASOF + dt.timedelta(days=2))],
    )
    assert universe.update(ASOF, {A: 10}, {A: [100]}, {A})
    assert not universe.update(ASOF + dt.timedelta(days=1), {A: 10}, {A: [100]}, {A})
    assert universe.update(ASOF + dt.timedelta(days=2), {A: 10}, {A: [100]}, {A})
    assert universe.members == set()
    assert universe.snapshots[-1]["reason"] == "safety_removal"


def test_empty_pool_waits_for_next_period_not_daily_value_timing(tmp_path: Path) -> None:
    universe = ValueUniverse(policy(tmp_path), [fact(annual_eps=0.1)])
    universe.update(ASOF, {A: 10}, {A: [100]}, {A})
    assert universe.members == set()
    universe.update(ASOF + dt.timedelta(days=1), {A: 1}, {A: [100]}, {A})
    assert universe.members == set()
    assert len(universe.snapshots) == 1


def test_first_known_financials_can_activate_mid_month(tmp_path: Path) -> None:
    universe = ValueUniverse(policy(tmp_path), [fact(known_on=ASOF + dt.timedelta(days=1))])
    universe.update(ASOF, {A: 10}, {A: [100]}, {A})
    assert universe.update(ASOF + dt.timedelta(days=1), {A: 10}, {A: [100]}, {A})
    assert universe.members == {A}


def test_paper_future_append_keeps_past_ledger_and_old_backfill_rejected(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    database = tmp_path / "paper.db"
    before, _ = run_paper(cfg, database, asof=ASOF + dt.timedelta(days=1))
    store = ValueStore(cfg.value.fundamentals)
    store.append([fact(B, annual_eps=5, known_on=ASOF + dt.timedelta(days=2))])
    after, added = run_paper(cfg, database, asof=ASOF + dt.timedelta(days=1))
    assert added == 0
    assert before.fills == after.fills
    store.append([fact(C, known_on=ASOF)])
    with pytest.raises(ValueError, match="historical data"):
        run_paper(cfg, database, asof=ASOF + dt.timedelta(days=1))


def test_historical_value_csv_import_and_conflict(tmp_path: Path) -> None:
    store = ValueStore(tmp_path / "fundamentals.sqlite3")
    row = json.loads(fact().json())

    path = tmp_path / "fundamentals.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)
    assert store.import_csv(path) == 1
    assert store.import_csv(path) == 0


def test_provider_annual_units_and_observation_date(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url, **kwargs):
        if kwargs["params"]["reportName"] == "RPT_F10_BASIC_ORGINFO":
            data = {
                "result": {
                    "data": [
                        {
                            "SECUCODE": A,
                            "SECURITY_NAME_ABBR": "工业公司",
                            "LISTING_DATE": "2000-01-01 00:00:00",
                            "EM2016": "工业",
                        }
                    ]
                }
            }
        else:
            assert 'REPORT_TYPE="年报"' in kwargs["params"]["filter"]
            data = {
                "result": {
                    "data": [
                        {
                            "SECUCODE": A,
                            "REPORT_DATE": "2023-12-31 00:00:00",
                            "NOTICE_DATE": "2024-04-01 00:00:00",
                            "EPSJB": 2,
                            "BPS": 10,
                            "ROEJQ": 15,
                            "MGJYXJJE": 3,
                            "ZCFZL": 40,
                        }
                    ]
                }
            }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    provider = EastmoneyValueProvider()
    provider.session = SimpleNamespace(get=get)
    facts, evidence = provider.capture((Instrument(A),))
    assert facts[0].roe == 0.15
    assert facts[0].debt_ratio == 0.4
    assert facts[0].known_on == dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    assert facts[0].annual_eps == 2
    assert A in evidence["annual_reports"]


def test_no_fundamental_data_keeps_strategy_in_cash(tmp_path: Path) -> None:
    cfg = config(tmp_path)

    result = cfg.engine(value_facts=[]).run(load_csv(cfg.data))
    assert result.fills == []
    assert result.portfolio.cash == cfg.initial_cash
    assert result.value_status["coverage"] == "none"


def test_cli_value_demo_backtest_paper_and_preview(tmp_path: Path) -> None:
    from emtl.cli import main  # noqa: PLC0415

    directory = tmp_path / "demo"
    assert main(["quant", "value-demo", "--directory", str(directory)]) == 0
    config_path = str(directory / "strategy.toml")
    output = str(tmp_path / "result.json")
    assert main(["quant", "backtest", "--config", config_path, "--output", output]) == 0
    payload = json.loads(Path(output).read_text())
    assert payload["metrics"]["fills"] > 0
    assert payload["value_pools"]
    assert all(len(p["selected"]) <= 2 for p in payload["value_pools"])
    args = [
        "quant",
        "paper",
        "--config",
        config_path,
        "--database",
        str(tmp_path / "paper.sqlite3"),
        "--asof",
        "2025-12-31",
        "--output",
        output,
    ]
    assert main(args) == 0
    assert main(args) == 0
    assert json.loads(Path(output).read_text())["paper"]["new_sessions"] == 0
    assert (
        main(
            [
                "quant",
                "value-pool",
                "--config",
                config_path,
                "--asof",
                "2025-12-31",
                "--output",
                output,
            ]
        )
        == 0
    )
    assert json.loads(Path(output).read_text())["preview_only"]


def test_st_removal_replans_before_price_rebalance(tmp_path: Path) -> None:
    from emtl.quant import Backtester  # noqa: PLC0415

    spec = policy(tmp_path)
    universe = ValueUniverse(spec, [fact(), fact(is_st=True, known_on=ASOF + dt.timedelta(days=2))])
    bars = [Bar(ASOF + dt.timedelta(days=i), A, 10, 10, 10, 10, 100000) for i in range(4)]
    result = Backtester(
        [Instrument(A)], StrategySpec(name="buy_hold"), rebalance="monthly", universe=universe
    ).run(bars)
    assert [f.side for f in result.fills] == ["buy", "sell"]
    assert result.fills[1].date == ASOF + dt.timedelta(days=3)


def test_announcement_lag_and_staleness_are_enforced(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="publication lag"):
        fact(known_on=dt.date(2023, 4, 1))
    result = screen(policy(tmp_path), [fact()], dt.date(2025, 1, 2))
    assert result["rejected"][A] == "stale_financials"
    assert (
        screen(policy(tmp_path, liquidity_window=2), [fact()])["rejected"][A]
        == "illiquid_or_warmup"
    )


def test_failed_capture_preserves_old_financials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from emtl.quant.value_ops import capture_value  # noqa: PLC0415

    cfg = config(tmp_path)
    old = ValueStore(cfg.value.fundamentals).load()

    def fail(self, instruments):
        raise ValueError("incomplete financial capture")

    monkeypatch.setattr(EastmoneyValueProvider, "capture", fail)
    with pytest.raises(ValueError, match="incomplete financial"):
        capture_value(cfg)
    assert ValueStore(cfg.value.fundamentals).load() == old


def test_tencent_raw_units_and_year_windows() -> None:
    from emtl.quant import TencentDailyProvider  # noqa: PLC0415

    calls = []

    def get(url, **kwargs):
        parts = kwargs["params"]["param"].split(",")
        calls.append(parts)
        assert parts[-1] == ""
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "code": 0,
                "data": {"sh600000": {"day": [[parts[2], "10", "11", "12", "9", "20"]]}},
            },
        )

    rows = TencentDailyProvider(session=SimpleNamespace(get=get)).fetch(
        Instrument(A), dt.date(2024, 12, 30), dt.date(2025, 1, 3)
    )
    assert len(calls) == 2
    assert len(rows) == 2
    assert rows[0].volume == 2000
    assert rows[0].close == 11
    assert calls[0][3] == "2024-12-31"


def test_tencent_adjusted_only_data_is_rejected() -> None:
    from emtl.quant import TencentDailyProvider  # noqa: PLC0415

    session = SimpleNamespace(
        get=lambda *a, **k: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"code": 0, "data": {"sh600000": {"qfqday": []}}},
        )
    )
    with pytest.raises(ValueError, match="raw day data missing"):
        TencentDailyProvider(session=session).fetch(Instrument(A), ASOF, ASOF)
