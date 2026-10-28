"""Daily research and execution value objects; no network or broker dependencies."""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass, field
from typing import Literal


def finite(value: float, name: str, *, minimum: float = 0) -> None:
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be finite and >= {minimum}")


@dataclass(frozen=True, slots=True)
class Instrument:
    symbol: str
    lot_size: int = 100
    settlement_days: int = 1
    price_limit: float | None = 0.10
    stamp_tax: bool = False
    tick_size: float = 0.01

    def __post_init__(self) -> None:
        parts = self.symbol.split(".")
        if (
            len(parts) != 2
            or len(parts[0]) != 6
            or not parts[0].isdigit()
            or parts[1] not in {"SH", "SZ"}
        ):
            raise ValueError("symbol must have an explicit market, e.g. 510300.SH")
        if (
            type(self.lot_size) is not int
            or self.lot_size < 1
            or type(self.settlement_days) is not int
            or self.settlement_days not in {0, 1}
        ):
            raise ValueError("lot_size >= 1; settlement_days must be 0 or 1")
        if self.price_limit is not None and not 0 < self.price_limit < 1:
            raise ValueError("price_limit must be in (0, 1), or None")
        finite(self.tick_size, "tick_size", minimum=1e-12)


@dataclass(frozen=True, slots=True)
class Bar:
    date: dt.date
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_allowed: bool = True
    sell_allowed: bool = True
    split_ratio: float = 1.0
    cash_dividend: float = 0.0

    def __post_init__(self) -> None:
        for name in ("open", "high", "low", "close"):
            finite(getattr(self, name), name, minimum=1e-12)
        for name in ("volume", "cash_dividend"):
            finite(getattr(self, name), name)
        finite(self.split_ratio, "split_ratio", minimum=1e-12)
        if (
            self.low > min(self.open, self.close)
            or self.high < max(self.open, self.close)
            or self.low > self.high
        ):
            raise ValueError(f"invalid OHLC for {self.symbol} {self.date}")


@dataclass(frozen=True, slots=True)
class Costs:
    commission: float = 0.0003
    minimum_commission: float = 5.0
    sell_tax: float = 0.0005
    slippage_bps: float = 5.0
    participation: float = 0.10

    def __post_init__(self) -> None:
        for name in ("commission", "minimum_commission", "sell_tax", "slippage_bps"):
            finite(getattr(self, name), name)
        if self.commission >= 1 or self.sell_tax >= 1 or self.slippage_bps >= 10000:
            raise ValueError("cost rates must be below 100%")
        if not 0 < self.participation <= 1:
            raise ValueError("participation must be in (0, 1]")

    def fee(self, value: float, *, sell: bool, taxable: bool) -> float:
        return round(
            max(self.minimum_commission, value * self.commission)
            + (value * self.sell_tax if sell and taxable else 0),
            2,
        )


@dataclass(slots=True)
class Position:
    quantity: int = 0
    average_cost: float = 0.0
    peak: float = 0.0


@dataclass(slots=True)
class Portfolio:
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            p.quantity * prices[s] for s, p in self.positions.items() if p.quantity
        )

    def weights(self, prices: dict[str, float]) -> dict[str, float]:
        equity = self.equity(prices)
        return (
            {s: p.quantity * prices[s] / equity for s, p in self.positions.items() if p.quantity}
            if equity > 0
            else {}
        )


@dataclass(frozen=True, slots=True)
class Fill:
    date: dt.date
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    price: float
    fee: float

    def as_dict(self) -> dict:
        return {**asdict(self), "date": self.date.isoformat()}


@dataclass(frozen=True, slots=True)
class EquityPoint:
    date: dt.date
    equity: float
    cash: float
    drawdown: float


@dataclass(frozen=True, slots=True)
class OrderIntent:
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    limit_price: float

    def __post_init__(self) -> None:
        Instrument(self.symbol)
        if self.side not in {"buy", "sell"} or type(self.quantity) is not int or self.quantity < 1:
            raise ValueError("invalid side or quantity")
        finite(self.limit_price, "limit_price", minimum=1e-12)
