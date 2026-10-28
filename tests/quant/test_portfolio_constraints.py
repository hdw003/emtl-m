"""Risk limits must apply to actual holdings after blocked liquidations."""

import datetime as dt

from emtl.quant import Backtester, Bar, Costs, Instrument, RiskPolicy


def test_failed_sell_does_not_free_gross_exposure_for_new_buys() -> None:
    a, b = "510300.SH", "510500.SH"

    class Rotate:
        def weights(self, history, held):
            return {a: 0.5} if len(history[a]) == 1 else {b: 0.9}

    bars = []
    for day in range(2, 5):
        for symbol in (a, b):
            bars.append(
                Bar(
                    dt.date(2024, 1, day),
                    symbol,
                    10,
                    10,
                    10,
                    10,
                    100000,
                    sell_allowed=not (day == 4 and symbol == a),
                )
            )
    result = Backtester(
        [Instrument(a), Instrument(b)],
        Rotate(),
        initial_cash=10000,
        rebalance="daily",
        risk=RiskPolicy(max_position=1, max_gross=0.9),
        costs=Costs(commission=0, minimum_commission=0, slippage_bps=0),
    ).run(bars)
    portfolio = result.portfolio
    assert portfolio.positions[a].quantity == 500
    assert portfolio.positions[b].quantity == 400
    assert sum(portfolio.weights({a: 10, b: 10}).values()) <= 0.9
