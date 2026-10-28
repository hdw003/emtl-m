"""Deterministic synthetic data for offline examples, never investment evidence."""

from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import replace
from pathlib import Path

from .data import load_csv, write_csv
from .models import Bar
from .value import Fundamental, ValueStore

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


def create_value_demo(directory: Path) -> Path:
    """Synthetic, known-date financials combined with the existing price demo."""
    config = create_demo(directory)
    mapping = {"510300.SH": "600004.SH", "510500.SH": "600019.SH", "159915.SZ": "000651.SZ"}
    write_csv(
        directory / "bars.csv",
        [replace(bar, symbol=mapping[bar.symbol]) for bar in load_csv(directory / "bars.csv")],
    )
    text = config.read_text(encoding="utf-8")
    for before, after in mapping.items():
        text = text.replace(before, after)
    text = text.replace("tick_size = 0.001", "tick_size = 0.01").replace(
        "stamp_tax = false", "stamp_tax = true"
    )
    text += '\n[value]\nfundamentals = "fundamentals.sqlite3"\nfrequency = "monthly"\ntop_n = 2\nmax_per_industry = 1\n'
    config.write_text(text, encoding="utf-8")
    facts = []
    for year in range(2021, 2025):
        for index, symbol in enumerate(mapping.values()):
            facts.append(
                Fundamental(
                    symbol,
                    dt.date(year, 12, 31),
                    dt.date(year + 1, 4, 1),
                    dt.date(year + 1, 4, 2),
                    dt.date(2010, 1, 1),
                    f"合成公司{index}",
                    f"合成行业{index}",
                    1 + 0.4 * math.sin(year + index),
                    8 + index,
                    0.12 + 0.02 * index,
                    2,
                    0.4,
                    source="synthetic_demo",
                )
            )
    ValueStore(directory / "fundamentals.sqlite3").append(facts)
    return config
