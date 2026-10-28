"""Deterministic synthetic data for offline examples, never investment evidence."""

from __future__ import annotations

import datetime as dt
import math
import random
from pathlib import Path

from .data import write_csv
from .models import Bar

DEMO_CONFIG = """# SYNTHETIC DATA ONLY: generated for functional verification, not performance claims.
data = "bars.csv"
initial_cash = 1000000
rebalance = "monthly"
start = "2023-01-01"

[strategy]
name = "momentum_rotation"
lookback = 126
skip = 21
top_n = 2
absolute_filter = true

[risk]
max_position = 0.45
max_gross = 0.90
max_drawdown = 0.20
stop_loss = 0.12
trailing_stop = 0.15
cooldown = 20

[costs]
commission = 0.0003
minimum_commission = 5.0
sell_tax = 0.0005
slippage_bps = 5.0
participation = 0.10

[[instruments]]
symbol = "510300.SH"
price_limit = 0.10
stamp_tax = false
tick_size = 0.001

[[instruments]]
symbol = "510500.SH"
price_limit = 0.10
stamp_tax = false
tick_size = 0.001

[[instruments]]
symbol = "159915.SZ"
price_limit = 0.20
stamp_tax = false
tick_size = 0.001
"""


def create_demo(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / "strategy.toml"
    data = directory / "bars.csv"
    if config.exists() or data.exists():
        raise ValueError("demo directory already contains strategy.toml or bars.csv")
    rng = random.Random(20261028)  # noqa: S311 -- reproducible synthetic market, not security
    bars: list[Bar] = []
    symbols = ("510300.SH", "510500.SH", "159915.SZ")
    prices = dict.fromkeys(symbols, 10.0)
    date = dt.date(2022, 1, 3)
    for index in range(1461):
        date = dt.date(2022, 1, 3) + dt.timedelta(days=index)
        if date.weekday() >= 5:
            continue
        for offset, symbol in enumerate(symbols):
            opening = prices[symbol] * (1 + rng.gauss(0, 0.002))
            daily_return = (
                0.00025 + 0.0015 * math.sin(index / 90 + offset * 2) + rng.gauss(0, 0.007)
            )
            close = opening * (1 + daily_return)
            bars.append(
                Bar(
                    date,
                    symbol,
                    opening,
                    max(opening, close) * 1.002,
                    min(opening, close) * 0.998,
                    close,
                    10_000_000,
                )
            )
            prices[symbol] = close
    write_csv(data, bars)
    config.write_text(DEMO_CONFIG, encoding="utf-8")
    return config
