"""Archive implemented distributions and annotate unadjusted daily bars."""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_left
from collections import defaultdict
from dataclasses import replace

from .market import DATA_URL, MarketDownloader
from .models import Bar


def annotate_actions(bars: list[Bar], rows: list[dict]) -> tuple[list[Bar], dict]:
    dates = defaultdict(list)
    for bar in bars:
        dates[bar.symbol].append(bar.date)
    for values in dates.values():
        values.sort()
    actions, delayed, seen = {}, [], set()
    applied_exdates = {}
    for row in rows:
        if row.get("ASSIGN_PROGRESS") != "实施分配":
            continue
        symbol = row["SECUCODE"]
        exdate = dt.date.fromisoformat(row["EX_DIVIDEND_DATE"][:10])
        if symbol not in dates or not dates[symbol][0] < exdate <= dates[symbol][-1]:
            continue
        cash = float(row.get("PRETAX_BONUS_RMB") or 0) / 10
        stock = float(row.get("BONUS_IT_RATIO") or 0) / 10
        components = (float(row.get("BONUS_RATIO") or 0) + float(row.get("IT_RATIO") or 0)) / 10
        if not all(
            math.isfinite(n) and n >= 0 for n in (cash, stock, components)
        ) or not math.isclose(stock, components, abs_tol=1e-6):
            raise ValueError(f"invalid distribution units: {symbol} {exdate}")
        identity = symbol, str(row.get("REPORT_DATE")), str(exdate)
        if identity in seen:
            raise ValueError(f"duplicate implemented distribution: {identity}")
        seen.add(identity)
        if not cash and not stock:
            continue
        applied = dates[symbol][bisect_left(dates[symbol], exdate)]
        if applied != exdate:
            delayed.append({"symbol": symbol, "ex_date": str(exdate), "applied_on": str(applied)})
        key = symbol, applied
        if key in applied_exdates and applied_exdates[key] != exdate:
            raise ValueError(
                f"multiple ex-dates delayed to one bar require explicit accounting: {symbol} {applied}"
            )
        applied_exdates[key] = exdate
        oldcash, oldstock = actions.get(key, (0, 0))
        actions[key] = oldcash + cash, oldstock + stock
    enriched = []
    for bar in bars:
        event = actions.get((bar.symbol, bar.date))
        if event:
            cash, stock = event
            if (bar.cash_dividend and not math.isclose(bar.cash_dividend, cash)) or (
                bar.split_ratio != 1 and not math.isclose(bar.split_ratio, 1 + stock)
            ):
                raise ValueError(
                    f"corporate action conflicts with archived bars: {bar.symbol} {bar.date}"
                )
            bar = replace(bar, cash_dividend=cash, split_ratio=1 + stock)
        enriched.append(bar)
    return enriched, {
        "annotated_sessions": len(actions),
        "delayed_until_next_observed_bar": delayed,
        "limitations": [
            "Single-vendor distributions; rights issues and other reorganizations are not modeled.",
            "Gross dividends credited on ex-date (or next observed bar), not actual payment date; holding-period dividend tax omitted.",
            "Fractional stock distributions floor shares without cash compensation; missing bars carry old valuation until next observation.",
        ],
    }


def fetch_actions(
    downloader: MarketDownloader, bars: list[Bar], start: dt.date, end: dt.date
) -> tuple[list[Bar], dict]:
    rows = downloader.table(
        "RPT_SHAREBONUS_DET",
        "ALL",
        f"(EX_DIVIDEND_DATE>='{start}')(EX_DIVIDEND_DATE<='{end}')(ASSIGN_PROGRESS=\"实施分配\")",
        url=DATA_URL,
        sort="SECUCODE,EX_DIVIDEND_DATE",
    )
    return annotate_actions(bars, rows)
