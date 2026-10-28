"""Separate trading, portfolio membership changes, and value-pool refreshes."""

from __future__ import annotations

from itertools import pairwise
from statistics import mean

from .engine import BacktestResult


def activity(result: BacktestResult, scheduled: str, pool_frequency: str | None = None) -> dict:
    dates = {point.date: index for index, point in enumerate(result.curve)}
    traded = sorted({fill.date for fill in result.fills})
    intervals = [dates[b] - dates[a] for a, b in pairwise(traded)]
    previous = frozenset()
    entered_at, closed, changes = {}, [], []
    for date, held in result.holdings:
        entries, exits = held - previous, previous - held
        for symbol in entries:
            entered_at[symbol] = (dates[date], date)
        for symbol in exits:
            index, began = entered_at.pop(symbol)
            closed.append(
                {
                    "symbol": symbol,
                    "entry": str(began),
                    "exit": str(date),
                    "sessions": dates[date] - index,
                    "calendar_days": (date - began).days,
                }
            )
        if entries or exits:
            changes.append({"date": str(date), "entries": sorted(entries), "exits": sorted(exits)})
        previous = held
    pool_changes = []
    prior_pool = set()
    for snapshot in result.universe_history:
        current = set(snapshot["selected"])
        if current != prior_pool:
            pool_changes.append(
                {
                    "date": snapshot["date"],
                    "entries": sorted(current - prior_pool),
                    "exits": sorted(prior_pool - current),
                }
            )
        prior_pool = current
    return {
        "scheduled_signal_frequency": scheduled,
        "scheduled_pool_frequency": pool_frequency,
        "actual_trading_days": len(traded),
        "trading_days_per_252_sessions": len(traded) * 252 / len(dates) if dates else 0,
        "mean_sessions_between_trading_days": mean(intervals) if intervals else None,
        "portfolio_membership_change_days": len(changes),
        "stock_entry_episodes": sum(len(c["entries"]) for c in changes),
        "stock_full_exit_episodes": len(closed),
        "replacement_days": sum(bool(c["entries"] and c["exits"]) for c in changes),
        "same_day_replacement_pairs": sum(min(len(c["entries"]), len(c["exits"])) for c in changes),
        "mean_completed_holding_sessions": mean(c["sessions"] for c in closed) if closed else None,
        "mean_completed_holding_calendar_days": mean(c["calendar_days"] for c in closed)
        if closed
        else None,
        "open_holding_episodes": len(entered_at),
        "pool_refresh_observations": len(result.universe_history),
        "pool_membership_change_days": len(pool_changes),
        "membership_changes": changes,
        "pool_changes": pool_changes,
        "completed_holdings": closed,
        "definitions": "Trading days include resizing existing positions. Full exits require quantity reaching zero; replacements pair entries/exits on the same day. Initial entries are not replacements. Holding means exclude still-open positions.",
    }
