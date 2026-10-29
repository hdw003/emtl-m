"""Monthly value replacement, rank retention and executable holding budgets."""

import datetime as dt
from dataclasses import replace

import pytest
from tests.quant.test_value import ASOF, fact

from emtl.quant.config import load_config
from emtl.quant.engine import Backtester
from emtl.quant.models import Bar, Costs, Instrument
from emtl.quant.planning import AccountSnapshot, plan_rebalance
from emtl.quant.portfolio import PortfolioSpec, select_portfolio, target_quantity
from emtl.quant.risk import RiskPolicy
from emtl.quant.strategies import StrategySpec
from emtl.quant.value import ValueSpec, ValueUniverse

A, B = "600000.SH", "000001.SZ"
ZERO = Costs(commission=0, minimum_commission=0, sell_tax=0, slippage_bps=0, participation=1)
RISK = RiskPolicy(max_position=1, max_gross=0.9, max_drawdown=1, stop_loss=1, trailing_stop=1)


def simulator(tmp_path, *, rank_buffer=0, frequency="monthly", known=ASOF):
    spec = ValueSpec(tmp_path / "facts.db", liquidity_window=1, min_daily_amount=0)
    universe = ValueUniverse(
        spec, [fact(A, annual_eps=2, known_on=known), fact(B, annual_eps=1, known_on=known)]
    )
    return Backtester(
        [Instrument(A, price_limit=None), Instrument(B, price_limit=None)],
        StrategySpec(name="buy_hold"),
        costs=ZERO,
        risk=RISK,
        initial_cash=100_000,
        rebalance=frequency,
        universe=universe,
        allocation=PortfolioSpec(max_positions=1, rank_buffer=rank_buffer, minimum_trade_value=0),
    )


def monthly_bars(*, overpriced=True, blocked=False):
    dates = [
        ASOF,
        ASOF + dt.timedelta(days=1),
        dt.date(2024, 1, 31),
        dt.date(2024, 2, 1),
        dt.date(2024, 2, 2),
    ]
    return [
        Bar(
            d,
            s,
            price,
            price,
            price,
            price,
            1_000_000,
            sell_allowed=not (blocked and s == A and d.month == 2),
        )
        for d in dates
        for s in (A, B)
        for price in [60 if s == A and d >= dt.date(2024, 1, 31) and overpriced else 10]
    ]


def test_overpriced_holding_is_replaced_next_open(tmp_path):
    engine = simulator(tmp_path)
    result = engine.run(monthly_bars())
    assert [(f.symbol, f.side, f.date) for f in result.fills] == [
        (A, "buy", dt.date(2024, 1, 3)),
        (A, "sell", dt.date(2024, 2, 2)),
        (B, "buy", dt.date(2024, 2, 2)),
    ]
    assert result.portfolio.positions[A].quantity == 0
    assert result.portfolio.positions[B].quantity > 0
    decision = result.allocation_history[-1]
    assert decision["date"] == "2024-02-01"
    assert decision["selected"] == [B]
    assert decision["exit_reasons"][A] == "valuation_filter"
    assert result.universe_history[-1]["rejected"][A] == "valuation_filter"


def test_no_replacement_before_future_prices_are_known(tmp_path):
    data = monthly_bars()
    prefix = simulator(tmp_path).run([bar for bar in data if bar.date < dt.date(2024, 2, 1)])
    full = simulator(tmp_path).run(data)
    assert prefix.fills == [f for f in full.fills if f.date < dt.date(2024, 2, 1)]
    assert prefix.curve == [p for p in full.curve if p.date < dt.date(2024, 2, 1)]
    assert all(fill.side == "buy" for fill in prefix.fills)


def test_blocked_exit_does_not_buy_an_eleventh_holding(tmp_path):
    result = simulator(tmp_path).run(monthly_bars(blocked=True))
    assert result.portfolio.positions[A].quantity > 0
    assert B not in result.portfolio.positions
    assert any(event["reason"] == "holding_slots_occupied" for event in result.events)


@pytest.mark.parametrize(("buffer", "replace_holding"), [(0, True), (1, False)])
def test_monthly_score_refresh_with_unchanged_pool_and_week(tmp_path, buffer, replace_holding):
    began = dt.date(2024, 1, 29)
    dates = [began, began + dt.timedelta(days=1), dt.date(2024, 2, 1), dt.date(2024, 2, 2)]
    data = [
        Bar(d, s, p, p, p, p, 1_000_000)
        for d in dates
        for s in (A, B)
        for p in [22 if s == A and d.month == 2 else 10]
    ]
    engine = simulator(tmp_path, rank_buffer=buffer, frequency="weekly", known=began)
    result = engine.run(data)
    assert all(set(pool["selected"]) == {A, B} for pool in result.universe_history)
    assert result.allocation_history[-1]["date"] == "2024-02-01"
    assert result.allocation_history[-1]["trigger"] == "value_refresh"
    assert (result.portfolio.positions[A].quantity == 0) == replace_holding
    assert result.allocation_history[-1]["selected"] == ([B] if replace_holding else [A])


def test_no_substitute_means_cash(tmp_path):
    engine = simulator(tmp_path)
    data = [
        replace(bar, open=60, high=60, low=60, close=60)
        if bar.symbol == B and bar.date.month == 2
        else bar
        for bar in monthly_bars()
    ]
    result = engine.run(data)
    assert all(p.quantity == 0 for p in result.portfolio.positions.values())
    assert result.next_weights is None or result.next_weights == {}
    assert result.portfolio.cash == result.curve[-1].equity


@pytest.mark.parametrize("desired", [99.999999999, 99.1, 100, 100.1, 199.1])
def test_positive_target_does_not_liquidate_a_lot(desired):
    policy = PortfolioSpec(minimum_trade_value=0, rebalance_tolerance=0)
    target = target_quantity(
        equity=10000,
        weight=desired * 10 / 10000,
        price=10,
        current=100,
        lot_size=100,
        policy=policy,
    )
    assert target == 100
    assert (
        target_quantity(equity=10000, weight=0, price=10, current=107, lot_size=100, policy=policy)
        == 0
    )


def test_trade_band_minimum_and_meaningful_resize():
    spec = PortfolioSpec()
    args = {"equity": 100_000, "price": 10, "current": 9000, "lot_size": 100, "policy": spec}
    assert target_quantity(weight=0.89, **args) == 9000
    assert target_quantity(weight=0.95, **args) == 9000
    assert target_quantity(weight=0.50, **args) == 5000
    assert target_quantity(weight=0.99, **args) == 9000


def test_uncapped_candidates_are_ranked_into_ten_holdings():
    symbols = [f"{i:06}.SZ" for i in range(1000)]
    raw = dict.fromkeys(symbols, 1 / 1000)
    weights, decision = select_portfolio(
        PortfolioSpec(),
        raw,
        {},
        dict(zip(symbols, range(1000), strict=True)),
        dict.fromkeys(symbols, 10),
        {s: Instrument(s) for s in symbols},
        equity=1_000_000,
        max_gross=0.9,
        max_position=0.15,
    )
    assert decision["eligible_count"] == 1000
    assert len(weights) == 10
    assert set(weights) == set(symbols[-10:])
    assert sum(weights.values()) == pytest.approx(1)


def test_unaffordable_top_rank_does_not_consume_a_slot():
    weights, decision = select_portfolio(
        PortfolioSpec(max_positions=1, minimum_trade_value=0),
        {A: 0.5, B: 0.5},
        {},
        {A: 1, B: 0},
        {A: 10000, B: 10},
        {s: Instrument(s) for s in (A, B)},
        equity=100_000,
        max_gross=0.9,
        max_position=0.15,
    )
    assert weights == {B: 1}
    assert decision["skipped"][A] == "unaffordable_slot_or_minimum_trade"


def test_manual_planner_uses_same_lot_policy():
    now = dt.datetime(2024, 1, 3, tzinfo=dt.UTC)
    account = AccountSnapshot(9001, {A: 100}, {A: 100}, now)
    args = {
        "account": account,
        "prices": {A: 10},
        "instruments": [Instrument(A)],
        "now": now,
        "quote_time": now,
        "costs": ZERO,
        "policy": PortfolioSpec(minimum_trade_value=0),
    }
    assert plan_rebalance({A: 0.099}, **args) == []
    assert plan_rebalance({}, **args)[0].quantity == 100


def test_config_exposes_portfolio_budget_but_not_universe_cap(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'data="bars.csv"\n[value]\nfundamentals="f.db"\ntop_n="all"\nmax_per_industry="all"\n[portfolio]\nmax_positions=10\n[[instruments]]\nsymbol="600000.SH"\n'
    )
    cfg = load_config(path)
    assert cfg.value.top_n is None
    assert cfg.value.max_per_industry is None
    assert cfg.portfolio.max_positions == 10
    assert cfg.fingerprint() != replace(cfg, portfolio=None).fingerprint()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_positions": 0},
        {"rank_buffer": -1},
        {"minimum_trade_value": -1},
        {"rebalance_tolerance": 1},
    ],
)
def test_invalid_portfolio_settings(kwargs):
    with pytest.raises(ValueError, match=next(iter(kwargs))):
        PortfolioSpec(**kwargs)


def test_planner_requires_confirmed_exit_before_replacement():
    now = dt.datetime(2024, 1, 3, tzinfo=dt.UTC)
    account = AccountSnapshot(90000, {A: 100}, {A: 100}, now)
    args = {
        "prices": {A: 10, B: 10},
        "instruments": [Instrument(A), Instrument(B)],
        "now": now,
        "quote_time": now,
        "costs": ZERO,
        "policy": PortfolioSpec(max_positions=1, minimum_trade_value=0),
    }
    orders = plan_rebalance({B: 0.9}, account, **args)
    assert [(o.symbol, o.side) for o in orders] == [(A, "sell")]
    confirmed = AccountSnapshot(91000, {A: 0}, {A: 0}, now)
    orders = plan_rebalance({B: 0.9}, confirmed, **args)
    assert [(o.symbol, o.side) for o in orders] == [(B, "buy")]


def test_volume_limited_buy_respects_minimum_trade_but_stop_exit_does_not(tmp_path):
    engine = simulator(tmp_path)
    engine.allocation = PortfolioSpec(max_positions=1, minimum_trade_value=5000)
    data = [
        Bar(ASOF + dt.timedelta(days=i), s, 10, 10, 10, 10, 100) for i in range(2) for s in (A, B)
    ]
    result = engine.run(data)
    assert result.fills == []
    assert any(e["reason"] == "below_minimum_trade_value" for e in result.events)
    engine.initial_cash = 6000
    engine.risk = replace(RISK, stop_loss=0.05)
    data = [
        Bar(ASOF + dt.timedelta(days=i), s, p, p, p, p, 1_000_000)
        for i in range(4)
        for s in (A, B)
        for p in [9 if s == A and i >= 2 else 10]
    ]
    result = engine.run(data)
    assert result.fills[-1].side == "sell"
    assert result.fills[-1].quantity * result.fills[-1].price < 5000
