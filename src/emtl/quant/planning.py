"""Pure target-weight to limit-order planning from explicitly reconciled balances."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

from .models import Costs, Instrument, OrderIntent, finite
from .portfolio import PortfolioSpec, target_quantity


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash: float
    quantities: dict[str, int]
    available: dict[str, int]
    captured_at: dt.datetime


def plan_rebalance(
    weights: dict[str, float],
    account: AccountSnapshot,
    prices: dict[str, float],
    instruments: list[Instrument],
    *,
    now: dt.datetime,
    quote_time: dt.datetime,
    max_age_seconds: int = 60,
    cash_reserve: float = 100,
    costs: Costs | None = None,
    policy: PortfolioSpec | None = None,
) -> list[OrderIntent]:
    """No network. Values must come from fresh, externally reconciled account data.

    Do not spend hypothetical proceeds from pending sells. The caller must
    reconcile confirmed fills and replan before using released funds.
    """
    if any(time.tzinfo is None for time in (account.captured_at, now, quote_time)):
        raise ValueError("account and quote timestamps must be timezone-aware")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    for timestamp in (account.captured_at, quote_time):
        if not 0 <= (now - timestamp).total_seconds() <= max_age_seconds:
            raise ValueError("stale or future account/quote timestamp")
    finite(account.cash, "cash")
    finite(cash_reserve, "cash_reserve")
    universe = {i.symbol: i for i in instruments}
    if len(universe) != len(instruments):
        raise ValueError("duplicate instruments")
    symbols = set(weights) | set(account.quantities)
    if symbols - set(universe) or symbols - set(prices):
        raise ValueError("every position and target needs an explicit instrument and price")
    if (
        any(not math.isfinite(w) or w < 0 for w in weights.values())
        or sum(weights.values()) > 1 + 1e-12
    ):
        raise ValueError("weights must be long-only without leverage")
    for symbol in symbols:
        finite(prices[symbol], "price", minimum=1e-12)
        quantity, available = account.quantities.get(symbol, 0), account.available.get(symbol, 0)
        if (
            type(quantity) is not int
            or type(available) is not int
            or not 0 <= available <= quantity
        ):
            raise ValueError("invalid quantity or sellable balance")
    equity = account.cash + sum(q * prices[s] for s, q in account.quantities.items())
    budget = max(0, account.cash - cash_reserve)
    costs = costs or Costs()
    orders: list[OrderIntent] = []
    occupied = {s for s, q in account.quantities.items() if q > 0}
    for side in ("sell", "buy"):
        for symbol in sorted(symbols):
            instrument, price = universe[symbol], prices[symbol]
            quantity = account.quantities.get(symbol, 0)
            target = target_quantity(
                equity=equity,
                weight=weights.get(symbol, 0),
                price=price,
                current=quantity,
                lot_size=instrument.lot_size,
                policy=policy,
            )
            delta = target - quantity
            if side == "sell" and delta < 0:
                sell = min(-delta, account.available.get(symbol, 0))
                if sell != quantity:
                    sell = sell // instrument.lot_size * instrument.lot_size
                if (
                    sell
                    and policy
                    and weights.get(symbol, 0) > 0
                    and sell * price < policy.minimum_trade_value
                ):
                    continue
                if sell:
                    orders.append(OrderIntent(symbol, "sell", sell, price))
            elif side == "buy" and delta > 0:
                if policy and symbol not in occupied and len(occupied) >= policy.max_positions:
                    continue
                value_budget = max(
                    0, min(budget / (1 + costs.commission), budget - costs.minimum_commission)
                )
                buy = min(
                    delta, int(value_budget / price / instrument.lot_size) * instrument.lot_size
                )
                if buy and policy and buy * price < policy.minimum_trade_value:
                    continue
                if buy:
                    occupied.add(symbol)
                    orders.append(OrderIntent(symbol, "buy", buy, price))
                    budget -= buy * price + costs.fee(buy * price, sell=False, taxable=False)
    return orders
