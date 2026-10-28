"""Execution, causality and accounting checks with deterministic daily bars."""

import datetime as dt
from dataclasses import replace

import pytest

from emtl.quant import Backtester, Bar, Costs, Instrument, RiskPolicy, StrategySpec

SYMBOL = "510300.SH"
ZERO_COST = Costs(commission=0, minimum_commission=0, sell_tax=0, slippage_bps=0, participation=1)
LOOSE_RISK = RiskPolicy(max_position=1, max_gross=1, max_drawdown=1, stop_loss=1, trailing_stop=1)


def bars(prices: list[float], *, symbol: str = SYMBOL) -> list[Bar]:
    return [
        Bar(dt.date(2024, 1, 2) + dt.timedelta(days=i), symbol, p, p, p, p, 1_000_000)
        for i, p in enumerate(prices)
    ]


def engine(**kwargs) -> Backtester:
    return Backtester(
        [Instrument(SYMBOL, price_limit=None)],
        StrategySpec(name="buy_hold"),
        initial_cash=10_000,
        costs=ZERO_COST,
        risk=LOOSE_RISK,
        rebalance="daily",
        **kwargs,
    )


def test_next_open_not_same_close_and_no_negative_cash() -> None:
    data = bars([10, 10, 12])
    data[1] = replace(data[1], open=11, high=11, low=10)
    result = engine().run(data)
    assert result.fills[0].date == data[1].date
    assert result.fills[0].price == 11
    assert result.fills[0].quantity == 900
    assert result.curve[0].equity == 10_000
    assert result.portfolio.cash >= 0


def test_exact_fees_slippage_and_minimum_commission() -> None:
    sim = engine()
    sim.costs = Costs(commission=0.001, minimum_commission=5, slippage_bps=10, participation=1)
    result = sim.run(bars([10, 10]))
    fill = result.fills[0]
    assert fill.price == 10.01
    assert fill.quantity == 900
    assert fill.fee == 9.01
    assert result.portfolio.cash == pytest.approx(981.99)
    assert result.metrics()["fees"] == 9.01


def test_future_data_cannot_change_past_fills_or_curve() -> None:
    sim = engine()
    sim.strategy = StrategySpec(name="dual_ma", fast=2, slow=3)
    data = bars([10, 11, 12, 11, 10, 9, 10, 11])
    prefix = sim.run(data[:5])
    full = sim.run(data)
    assert prefix.curve == full.curve[:5]
    assert prefix.fills == [f for f in full.fills if f.date <= data[4].date]
    truncated = sim.run(data, end=data[4].date)
    assert truncated.curve == prefix.curve
    assert truncated.fills == prefix.fills


def test_missing_bar_and_suspension_do_not_create_fills() -> None:
    data = bars([10, 10, 10])
    data[1] = replace(data[1], volume=0)
    result = engine().run(data)
    assert not result.fills
    assert any(e["reason"] == "missing_or_suspended" for e in result.events)


def test_opening_limit_lock_and_etf_tick_size() -> None:
    sim = engine()
    sim.instruments[SYMBOL] = Instrument(SYMBOL, price_limit=0.10, tick_size=0.001)
    result = sim.run(bars([1.235, 1.359]))
    assert not result.fills
    assert result.events[0]["reason"] == "price_limit"


def test_volume_uses_previous_day_not_future_full_day() -> None:
    data = bars([10, 10])
    data[0] = replace(data[0], volume=200)
    sim = engine()
    sim.costs = replace(ZERO_COST, participation=0.5)
    result = sim.run(data)
    assert result.fills[0].quantity == 100


def test_drawdown_halt_liquidates_next_day_and_stays_halted() -> None:
    sim = engine()
    sim.risk = replace(LOOSE_RISK, max_drawdown=0.1)
    result = sim.run(bars([10, 10, 8, 8, 12, 12]))
    assert result.halted
    assert [f.side for f in result.fills] == ["buy", "sell"]
    assert result.fills[1].date == dt.date(2024, 1, 5)
    assert result.portfolio.positions[SYMBOL].quantity == 0


def test_stop_loss_cooldown_and_lot_accounting() -> None:
    sim = engine()
    sim.risk = replace(LOOSE_RISK, stop_loss=0.1, cooldown=3)
    result = sim.run(bars([10, 10, 8, 8, 9, 9, 9]))
    assert result.fills[0].side == "buy"
    assert result.fills[1].side == "sell"
    assert result.fills[2].date >= dt.date(2024, 1, 8)
    assert all(f.quantity % 100 == 0 for f in result.fills)


def test_split_and_dividend_conserve_equity_and_rescale_signal_history() -> None:
    sim = engine()
    sim.rebalance = "monthly"
    data = bars([10, 10, 4.5])
    data[2] = replace(data[2], split_ratio=2, cash_dividend=1)
    result = sim.run(data)
    assert result.portfolio.cash == 1000
    assert result.portfolio.positions[SYMBOL].quantity == 2000
    assert result.curve[-1].equity == 10_000


def test_sell_tax_only_for_taxable_stock() -> None:
    sim = engine()
    sim.instruments[SYMBOL] = replace(sim.instruments[SYMBOL], stamp_tax=True)
    sim.costs = replace(ZERO_COST, sell_tax=0.001)
    sim.risk = replace(LOOSE_RISK, max_drawdown=0.1)
    result = sim.run(bars([10, 10, 8, 8]))
    assert result.fills[0].fee == 0
    assert result.fills[1].fee == 8


def test_warmup_start_does_not_trade_before_start() -> None:
    data = bars([10, 11, 12, 13, 14])
    result = engine().run(data, start=data[2].date)
    assert result.curve[0].date == data[2].date
    assert result.fills[0].date == data[3].date


def test_duplicate_and_unknown_bars_fail() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        engine().run(bars([10]) * 2)
    with pytest.raises(ValueError, match="unknown instrument"):
        engine().run(bars([10], symbol="600000.SH"))
