"""Deterministic close-signal / next-open daily simulator with explicit costs."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from statistics import mean, stdev

from .models import Bar, Costs, EquityPoint, Fill, Instrument, Portfolio, Position, finite
from .portfolio import PortfolioSpec, select_portfolio, target_quantity
from .risk import RiskPolicy
from .strategies import Strategy
from .value import ValueUniverse


@dataclass(slots=True)
class BacktestResult:
    initial_cash: float
    portfolio: Portfolio
    curve: list[EquityPoint] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    next_weights: dict[str, float] | None = None
    halted: bool = False
    universe_history: list[dict] = field(default_factory=list)
    value_status: dict | None = None
    holdings: list[tuple[dt.date, frozenset[str]]] = field(default_factory=list)
    allocation_history: list[dict] = field(default_factory=list)

    def metrics(self) -> dict[str, float | int | None]:
        if not self.curve:
            raise ValueError("no sessions in the selected range")
        equities = [self.initial_cash, *(p.equity for p in self.curve)]
        returns = [b / a - 1 for a, b in pairwise(equities)]
        volatility = stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0
        days = (self.curve[-1].date - self.curve[0].date).days
        ratio = equities[-1] / self.initial_cash
        exponent = math.log(ratio) * 365.25 / days if days and ratio > 0 else None
        annual_return = math.expm1(exponent) if exponent is not None and exponent < 700 else None
        return {
            "sessions": len(self.curve),
            "total_return": ratio - 1,
            "annualized_return": annual_return,
            "annualized_volatility": volatility,
            "sharpe_zero_rate": mean(returns) * 252 / volatility if volatility else None,
            "max_drawdown": max(p.drawdown for p in self.curve),
            "final_equity": equities[-1],
            "cash": self.portfolio.cash,
            "fills": len(self.fills),
            "fees": sum(f.fee for f in self.fills),
            "traded_notional": sum(f.quantity * f.price for f in self.fills),
        }


def _period(date: dt.date, frequency: str) -> object:
    match frequency:
        case "daily":
            return date
        case "weekly":
            return date.isocalendar()[:2]
        case "monthly":
            return date.year, date.month
        case _:
            raise ValueError("rebalance must be daily, weekly, or monthly")


class Backtester:
    def __init__(
        self,
        instruments: Sequence[Instrument],
        strategy: Strategy,
        *,
        initial_cash: float = 1_000_000,
        costs: Costs | None = None,
        risk: RiskPolicy | None = None,
        rebalance: str = "monthly",
        universe: ValueUniverse | None = None,
        fractional_action: str = "error",
        allocation: PortfolioSpec | None = None,
        selection_ranking: dict[str, float] | None = None,
    ) -> None:
        finite(initial_cash, "initial_cash", minimum=1)
        self.instruments = {i.symbol: i for i in instruments}
        if not instruments or len(self.instruments) != len(instruments):
            raise ValueError("instrument universe must be nonempty and unique")
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.costs = costs or Costs()
        self.risk = risk or RiskPolicy()
        _period(dt.date(2020, 1, 1), rebalance)
        self.rebalance = rebalance
        self.universe = universe
        if fractional_action not in {"error", "floor"}:
            raise ValueError("fractional_action must be error or floor")
        self.fractional_action = fractional_action
        self.allocation = allocation
        self.selection_ranking = selection_ranking
        if allocation and universe is None and selection_ranking is None:
            raise ValueError(
                "value-ranked allocation requires observed or explicitly frozen scores"
            )

    def run(
        self, bars: Sequence[Bar], *, start: dt.date | None = None, end: dt.date | None = None
    ) -> BacktestResult:
        if start and end and start > end:
            raise ValueError("start must be <= end")
        grouped: dict[dt.date, dict[str, Bar]] = defaultdict(dict)
        for bar in bars:
            if end is not None and bar.date > end:
                continue
            if bar.symbol not in self.instruments:
                raise ValueError(f"unknown instrument {bar.symbol}")
            if bar.symbol in grouped[bar.date]:
                raise ValueError(f"duplicate bar: {bar.date} {bar.symbol}")
            grouped[bar.date][bar.symbol] = bar
        portfolio = Portfolio(self.initial_cash)
        if self.universe:
            self.universe.reset()
        result = BacktestResult(self.initial_cash, portfolio)
        history: dict[str, list[float]] = {s: [] for s in self.instruments}
        amounts: dict[str, list[float]] = {s: [] for s in self.instruments}
        prices: dict[str, float] = {}
        volumes: dict[str, float] = {}
        pending: dict[str, float] | None = None
        blocked_until: dict[str, int] = {}
        peak = self.initial_cash
        last_period: object = None
        for index, (date, today) in enumerate(sorted(grouped.items())):
            # Corporate actions are applied only on the ex-date, before opening.
            # Historical signal prices are rescaled then, never using future events.
            for symbol, bar in today.items():
                position = portfolio.positions.get(symbol)
                previous = prices.get(symbol)
                if bar.split_ratio != 1 or bar.cash_dividend:
                    if previous is None or previous <= bar.cash_dividend:
                        raise ValueError(
                            f"corporate action needs a valid previous close: {symbol} {date}"
                        )
                    factor = (previous - bar.cash_dividend) / previous / bar.split_ratio
                    history[symbol] = [p * factor for p in history[symbol]]
                    prices[symbol] *= factor
                    if position and position.quantity:
                        quantity = position.quantity * bar.split_ratio
                        if not math.isclose(quantity, round(quantity), abs_tol=1e-6):
                            if self.fractional_action == "error":
                                raise ValueError(
                                    "fractional corporate-action shares require explicit handling"
                                )
                            result.events.append(
                                {
                                    "date": str(date),
                                    "symbol": symbol,
                                    "reason": "fractional_shares_discarded",
                                    "shares": quantity - math.floor(quantity),
                                }
                            )
                            quantity = math.floor(quantity)
                        portfolio.cash = round(
                            portfolio.cash + position.quantity * bar.cash_dividend, 2
                        )
                        position.quantity = round(quantity)
                        position.average_cost = (
                            max(0, position.average_cost - bar.cash_dividend) / bar.split_ratio
                        )
                        position.peak *= factor
            previous_prices = dict(prices)
            opening = {**prices, **{s: b.open for s, b in today.items()}}
            if pending is not None:
                self._execute(date, today, previous_prices, volumes, opening, pending, result)
            prices.update({s: b.close for s, b in today.items()})
            for symbol, values in amounts.items():
                values.append(today[symbol].close * today[symbol].volume if symbol in today else 0)
            for symbol, values in history.items():
                if symbol in prices:
                    values.append(prices[symbol])
            volumes.update({s: b.volume for s, b in today.items()})
            if start is not None and date < start:
                continue
            equity = portfolio.equity(prices)
            peak = max(peak, equity)
            drawdown = 1 - equity / peak
            result.curve.append(EquityPoint(date, equity, portfolio.cash, drawdown))
            result.holdings.append(
                (date, frozenset(s for s, p in portfolio.positions.items() if p.quantity))
            )
            pending = None
            pool_changed = False
            if self.universe:
                pool_changed = self.universe.update(
                    date, prices, amounts, {s for s, b in today.items() if b.volume > 0}
                )
            forced: set[str] = set()
            if drawdown >= self.risk.max_drawdown and not result.halted:
                result.halted = True
                result.events.append(
                    {"date": str(date), "reason": "drawdown_halt", "drawdown": drawdown}
                )
            for symbol, position in portfolio.positions.items():
                if not position.quantity:
                    continue
                position.peak = max(position.peak, prices[symbol])
                if prices[symbol] <= position.average_cost * (1 - self.risk.stop_loss) or prices[
                    symbol
                ] <= position.peak * (1 - self.risk.trailing_stop):
                    forced.add(symbol)
                    blocked_until[symbol] = index + self.risk.cooldown
                    result.events.append(
                        {"date": str(date), "symbol": symbol, "reason": "position_stop"}
                    )
            period = _period(date, self.rebalance)
            if result.halted:
                pending = {}
            elif (
                period != last_period
                or pool_changed
                or (self.allocation and self.universe and self.universe.refreshed)
            ):
                # Do not open new positions from stale or missing bars.
                eligible = {
                    s: values
                    for s, values in history.items()
                    if s in today
                    and today[s].volume > 0
                    and (self.universe is None or s in self.universe.members)
                }
                raw = self.strategy.weights(eligible, portfolio.weights(prices))
                if set(raw) - set(eligible):
                    raise ValueError("strategy returned an unknown or ineligible symbol")
                if self.allocation:
                    raw = {
                        s: w
                        for s, w in raw.items()
                        if index >= blocked_until.get(s, -1) and s not in forced
                    }
                    raw, decision = select_portfolio(
                        self.allocation,
                        raw,
                        portfolio.weights(prices),
                        self.universe.ranking if self.universe else self.selection_ranking,
                        prices,
                        self.instruments,
                        equity=equity,
                        max_gross=self.risk.max_gross,
                        max_position=self.risk.max_position,
                    )
                    if self.universe:
                        for symbol in decision["exits"]:
                            if symbol not in self.universe.members:
                                decision["exit_reasons"][symbol] = self.universe.rejections.get(
                                    symbol, "outside_value_universe"
                                )
                    decision.update(
                        {
                            "date": str(date),
                            "trigger": "value_refresh"
                            if self.universe and self.universe.refreshed
                            else "scheduled",
                        }
                    )
                    result.allocation_history.append(decision)
                pending = self.risk.constrain(raw, history)
                pending = {s: w for s, w in pending.items() if index >= blocked_until.get(s, -1)}
            if forced:
                pending = portfolio.weights(prices) if pending is None else pending
                pending = {s: w for s, w in pending.items() if s not in forced}
            last_period = period
        if not result.curve:
            raise ValueError("no sessions in the selected range")
        result.next_weights = pending
        if self.universe:
            result.universe_history = list(self.universe.snapshots)
            dates = [f.known_on for f in self.universe.facts if f.symbol in self.instruments]
            first_known = min(dates) if dates else None
            covered = sum(p.date >= first_known for p in result.curve) if first_known else 0
            result.value_status = {
                "first_known_on": str(first_known) if first_known else None,
                "sessions_with_observations": covered,
                "total_sessions": len(result.curve),
                "coverage": "none"
                if covered == 0
                else "partial"
                if covered < len(result.curve)
                else "observations_available_unverified",
                "note": "No historical value performance can be inferred from sessions before first_known_on; availability does not certify completeness or correctness.",
            }
        return result

    def _execute(
        self,
        date: dt.date,
        today: dict[str, Bar],
        previous: dict[str, float],
        volumes: dict[str, float],
        opening: dict[str, float],
        weights: dict[str, float],
        result: BacktestResult,
    ) -> None:
        portfolio = result.portfolio
        equity = portfolio.equity(opening)
        targets = {
            s: target_quantity(
                equity=equity,
                weight=weights.get(s, 0),
                price=price,
                current=portfolio.positions.get(s, Position()).quantity,
                lot_size=self.instruments[s].lot_size,
                policy=self.allocation,
            )
            for s, price in opening.items()
        }
        # Sell first. Each symbol gets at most one fill per session. Combined
        # with next-session execution this enforces T+1 without intraday reuse.
        for side in ("sell", "buy"):
            for symbol in sorted(targets):
                instrument = self.instruments[symbol]
                current = portfolio.positions.get(symbol, Position()).quantity
                delta = targets[symbol] - current
                if (side == "buy" and delta <= 0) or (side == "sell" and delta >= 0):
                    continue
                bar = today.get(symbol)
                reason = None
                if bar is None or bar.volume == 0:
                    reason = "missing_or_suspended"
                elif not (bar.buy_allowed if side == "buy" else bar.sell_allowed):
                    reason = "direction_blocked"
                elif instrument.price_limit is not None and symbol in previous:
                    bound = previous[symbol] * (
                        1 + instrument.price_limit * (1 if side == "buy" else -1)
                    )
                    tick = Decimal(str(instrument.tick_size))
                    bound = float(
                        (Decimal(str(bound)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                        * tick
                    )
                    if (side == "buy" and bar.open >= bound - 1e-9) or (
                        side == "sell" and bar.open <= bound + 1e-9
                    ):
                        reason = "price_limit"
                if reason:
                    result.events.append(
                        {"date": str(date), "symbol": symbol, "side": side, "reason": reason}
                    )
                    continue
                assert bar is not None
                # Use previously observed volume, not the future full-day volume
                # of the opening fill session, to cap participation.
                capacity = int(volumes.get(symbol, 0) * self.costs.participation)
                quantity = min(abs(delta), capacity)
                if not (side == "sell" and quantity == current):
                    quantity = quantity // instrument.lot_size * instrument.lot_size
                price = bar.open * (
                    1 + self.costs.slippage_bps / 10000 * (1 if side == "buy" else -1)
                )
                tick = Decimal(str(instrument.tick_size))
                price = float(
                    (Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                    * tick
                )
                if price <= 0:
                    raise ValueError("execution price is below instrument tick size")
                if instrument.price_limit is not None and symbol in previous:
                    boundary = previous[symbol] * (
                        1 + instrument.price_limit * (1 if side == "buy" else -1)
                    )
                    boundary = float(
                        (Decimal(str(boundary)) / tick).quantize(
                            Decimal("1"), rounding=ROUND_HALF_UP
                        )
                        * tick
                    )
                    price = min(price, boundary) if side == "buy" else max(price, boundary)
                if side == "buy":
                    if (
                        self.allocation
                        and not current
                        and sum(p.quantity > 0 for p in portfolio.positions.values())
                        >= self.allocation.max_positions
                    ):
                        result.events.append(
                            {
                                "date": str(date),
                                "symbol": symbol,
                                "side": side,
                                "reason": "holding_slots_occupied",
                            }
                        )
                        continue
                    # A blocked sell must not be treated as freed exposure.
                    # Limit buys against the actual portfolio after sell fills.
                    equity_now = portfolio.equity(opening)
                    gross_now = equity_now - portfolio.cash
                    risk_budget = max(0, self.risk.max_gross * equity_now - gross_now)
                    denominator = bar.open + self.risk.max_gross * (
                        max(0, price - bar.open) + price * self.costs.commission
                    )
                    risk_quantity = max(
                        0,
                        int(
                            (risk_budget - self.risk.max_gross * self.costs.minimum_commission)
                            / denominator
                            / instrument.lot_size
                        )
                        * instrument.lot_size,
                    )
                    quantity = min(quantity, risk_quantity)
                    budget = min(
                        portfolio.cash / (1 + self.costs.commission),
                        portfolio.cash - self.costs.minimum_commission,
                    )
                    affordable = max(
                        0, int(budget / price / instrument.lot_size) * instrument.lot_size
                    )
                    quantity = min(quantity, affordable)
                    while (
                        quantity > 0
                        and quantity * price
                        + self.costs.fee(quantity * price, sell=False, taxable=False)
                        > portfolio.cash
                    ):
                        quantity -= instrument.lot_size
                if (
                    self.allocation
                    and quantity > 0
                    and weights.get(symbol, 0) > 0
                    and quantity * price < self.allocation.minimum_trade_value
                ):
                    result.events.append(
                        {
                            "date": str(date),
                            "symbol": symbol,
                            "side": side,
                            "reason": "below_minimum_trade_value",
                        }
                    )
                    continue
                if quantity <= 0:
                    result.events.append(
                        {
                            "date": str(date),
                            "symbol": symbol,
                            "side": side,
                            "reason": "cash_or_volume_or_lot",
                        }
                    )
                    continue
                fee = self.costs.fee(
                    quantity * price, sell=side == "sell", taxable=instrument.stamp_tax
                )
                position = portfolio.positions.setdefault(symbol, Position())
                if side == "buy":
                    position.average_cost = (
                        position.quantity * position.average_cost + quantity * price + fee
                    ) / (position.quantity + quantity)
                    position.quantity += quantity
                    position.peak = max(position.peak, price)
                    portfolio.cash = round(portfolio.cash - quantity * price - fee, 2)
                else:
                    position.quantity -= quantity
                    portfolio.cash = round(portfolio.cash + quantity * price - fee, 2)
                    if not position.quantity:
                        position.average_cost = position.peak = 0
                result.fills.append(Fill(date, symbol, side, quantity, price, fee))
