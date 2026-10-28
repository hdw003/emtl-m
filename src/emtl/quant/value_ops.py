"""Capture and inspect a value pool independently of paper execution."""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from zoneinfo import ZoneInfo

from .config import QuantConfig
from .data import load_csv
from .value import ValueStore, screen_value
from .value_provider import EastmoneyValueProvider


def capture_value(config: QuantConfig, *, force: bool = False) -> dict:
    if config.value is None:
        raise ValueError("configure [value] before using value commands")
    today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    store = ValueStore(config.value.fundamentals)
    previous = {
        f.symbol
        for f in store.load()
        if f.known_on == today and f.source == "eastmoney_observed_annual"
    }
    if not force and previous >= {i.symbol for i in config.instruments}:
        return {"inserted": 0, "known_on": str(today), "status": "already_captured"}
    facts, evidence = EastmoneyValueProvider().capture(config.instruments)
    inserted = store.append(facts, evidence=evidence)
    return {
        "inserted": inserted,
        "known_on": str(today),
        "status": "captured",
        "coverage": len(facts),
    }


def inspect_pool(config: QuantConfig, asof: dt.date) -> dict:
    if config.value is None:
        raise ValueError("configure [value] before selecting a value pool")
    today = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if asof > today:
        raise ValueError("value pool asof cannot be in the future")
    prices, amounts, last_dates, last_volumes, grouped = {}, {}, {}, {}, {}
    for bar in load_csv(config.data):
        if bar.date > asof:
            continue
        prices[bar.symbol] = bar.close
        grouped.setdefault(bar.date, {})[bar.symbol] = bar
        last_dates[bar.symbol] = bar.date
        last_volumes[bar.symbol] = bar.volume
    if not prices or set(prices) - {i.symbol for i in config.instruments}:
        raise ValueError("value prices missing or contain unconfigured stocks")
    # Preview can use the previous completed close, but never silently price a
    # stale asset; engine selection itself requires a current tradable daily bar.
    for _date, bars in sorted(grouped.items()):
        for symbol in prices:
            amounts.setdefault(symbol, []).append(
                bars[symbol].close * bars[symbol].volume if symbol in bars else 0
            )
    latest = max(last_dates.values())
    tradable = {
        s
        for s, date in last_dates.items()
        if date == latest and (asof - date).days <= 5 and last_volumes[s] > 0
    }
    result = screen_value(
        config.value, ValueStore(config.value.fundamentals).load(), asof, prices, amounts, tradable
    )
    result["pricing_dates"] = {s: str(d) for s, d in last_dates.items()}
    result["preview_only"] = True
    result["policy"] = {
        key: str(value) if key == "fundamentals" else value
        for key, value in asdict(config.value).items()
    }
    result["snapshot_id"] = ValueStore(config.value.fundamentals).save_pool(result)
    return result
