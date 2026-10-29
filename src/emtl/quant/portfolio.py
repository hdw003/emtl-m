"""Choose a funded portfolio from an uncapped candidate pool; avoid tiny resizing."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import Instrument


@dataclass(frozen=True, slots=True)
class PortfolioSpec:
    max_positions: int = 10
    rank_buffer: int = 5
    rebalance_tolerance: float = 0.20
    minimum_trade_value: float = 5000

    def __post_init__(self) -> None:
        if type(self.max_positions) is not int or self.max_positions < 1:
            raise ValueError("max_positions must be a positive holding budget, not a candidate cap")
        if type(self.rank_buffer) is not int or self.rank_buffer < 0:
            raise ValueError("rank_buffer must be a nonnegative integer")
        if not math.isfinite(self.rebalance_tolerance) or not 0 <= self.rebalance_tolerance < 1:
            raise ValueError("rebalance_tolerance must be in [0, 1)")
        if not math.isfinite(self.minimum_trade_value) or self.minimum_trade_value < 0:
            raise ValueError("minimum_trade_value must be nonnegative")


def select_portfolio(
    spec: PortfolioSpec,
    raw: dict[str, float],
    held: dict[str, float],
    ranking: dict[str, float],
    prices: dict[str, float],
    instruments: dict[str, Instrument],
    *,
    equity: float,
    max_gross: float,
    max_position: float,
) -> tuple[dict[str, float], dict]:
    if any(not math.isfinite(w) or w < 0 for w in raw.values()):
        raise ValueError("strategy weights must be finite and long-only")
    eligible = {s: w for s, w in raw.items() if w > 0}
    if set(eligible) - set(ranking) or any(not math.isfinite(ranking[s]) for s in eligible):
        raise ValueError("every allocation candidate needs an observed finite value score")
    ordered = sorted(eligible, key=lambda s: (-ranking[s], s))
    ranks = {s: i + 1 for i, s in enumerate(ordered)}
    # Existing holdings get a rank buffer only while BOTH value and timing rules
    # still pass. An overpriced/unsafe asset cannot survive via this buffer.
    retained = [
        s
        for s in ordered
        if held.get(s, 0) > 0 and ranks[s] <= spec.max_positions + spec.rank_buffer
    ][: spec.max_positions]
    chosen = list(retained)
    skipped = {}
    slot_budget = equity * min(max_gross / spec.max_positions, max_position)
    for symbol in ordered:
        if len(chosen) >= spec.max_positions:
            break
        if symbol in chosen:
            continue
        lot_value = prices[symbol] * instruments[symbol].lot_size
        if lot_value > slot_budget or slot_budget < spec.minimum_trade_value:
            skipped[symbol] = "unaffordable_slot_or_minimum_trade"
            continue
        chosen.append(symbol)
    total = sum(eligible[s] for s in chosen)
    weights = {s: eligible[s] / total for s in chosen} if total else {}
    exited = sorted(s for s, w in held.items() if w > 0 and s not in chosen)
    return weights, {
        "eligible_count": len(ordered),
        "holding_budget": spec.max_positions,
        "selected": chosen,
        "retained": retained,
        "entries": sorted(s for s in chosen if held.get(s, 0) <= 0),
        "exits": exited,
        "exit_reasons": {
            s: "value_or_timing_ineligible" if s not in eligible else "rank_deterioration"
            for s in exited
        },
        "selected_ranks": {s: ranks[s] for s in chosen},
        "skipped": skipped,
    }


def target_quantity(
    *,
    equity: float,
    weight: float,
    price: float,
    current: int,
    lot_size: int,
    policy: PortfolioSpec | None = None,
) -> int:
    if policy is None:
        return int(equity * weight / price / lot_size) * lot_size
    if weight <= 0:
        return 0  # Explicit exits bypass bands and minimum notionals, including odd lots.
    desired = equity * weight / price
    if current <= 0:
        target = math.floor(desired / lot_size + 1e-10) * lot_size
        return target if target * price >= policy.minimum_trade_value else 0
    difference = desired - current
    # Round the TRADE down to complete lots, not the entire desired holding.
    # A desired holding of 99.x shares must not liquidate an existing 100 shares.
    lots = math.floor(abs(difference) / lot_size + 1e-10)
    if (
        lots == 0
        or abs(difference) * price < policy.minimum_trade_value
        or abs(difference) / max(desired, 1e-12) < policy.rebalance_tolerance
    ):
        return current
    trade = lots * lot_size
    return current + trade if difference > 0 else current - trade
