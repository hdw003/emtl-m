"""Restart-safe paper ledger; deterministic replay verified against immutable history."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import QuantConfig
from .data import load_csv
from .engine import BacktestResult


def latest_completed_date(now: dt.datetime | None = None) -> dt.date:
    now = now or dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        raise ValueError("now must have a timezone")
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    return local.date() if local.hour >= 16 else local.date() - dt.timedelta(days=1)


def run_paper(
    config: QuantConfig, database: Path, *, asof: dt.date | None = None
) -> tuple[BacktestResult, int]:
    cutoff = latest_completed_date()
    asof = asof or cutoff
    if asof > cutoff:
        raise ValueError("paper requires completed daily bars (after 16:00 Asia/Shanghai)")
    if config.end and config.end < asof:
        raise ValueError("remove or advance config.end for ongoing paper runs")
    bars = [bar for bar in load_csv(config.data) if bar.date <= asof]
    if not bars or (asof - max(b.date for b in bars)).days > 10:
        raise ValueError(
            "daily dataset is empty or stale by more than 10 calendar days; refresh data"
        )
    # Replay is intentionally simple and auditable for daily strategies. History
    # checksums reject edits to already-booked prices, actions, or fills.
    result = config.engine().run(bars, start=config.start, end=asof)
    last_dates = {bar.symbol: bar.date for bar in bars}
    stale_positions = [
        s
        for s, p in result.portfolio.positions.items()
        if p.quantity and (asof - last_dates[s]).days > 10
    ]
    if stale_positions:
        raise ValueError(f"held-position data is stale by more than 10 days: {stale_positions}")
    digest = hashlib.sha256()
    by_date: dict[dt.date, str] = {}
    for bar in bars:
        digest.update(json.dumps(asdict(bar), sort_keys=True, default=str).encode())
        by_date[bar.date] = digest.hexdigest()
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database, timeout=30) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                date TEXT PRIMARY KEY, digest TEXT NOT NULL,
                equity REAL NOT NULL, cash REAL NOT NULL, drawdown REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                date TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                quantity INTEGER NOT NULL, price REAL NOT NULL, fee REAL NOT NULL,
                PRIMARY KEY (date, symbol, side)
            );
            CREATE TABLE IF NOT EXISTS events (
                date TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (date, sequence)
            );
        """)
        connection.execute("BEGIN IMMEDIATE")
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        fingerprint = config.fingerprint()
        if metadata.get("config_hash", fingerprint) != fingerprint:
            raise ValueError("paper configuration changed; use a new database for a new experiment")
        existing = {
            row[0]: row[1:]
            for row in connection.execute("SELECT date, digest, equity, cash FROM sessions")
        }
        stored_fills = list(
            connection.execute(
                "SELECT date, symbol, side, quantity, price, fee FROM fills ORDER BY date, side, symbol"
            )
        )
        replay_fills = sorted(
            [
                (str(f.date), f.symbol, f.side, f.quantity, f.price, f.fee)
                for f in result.fills
                if str(f.date) in existing
            ],
            key=lambda row: (row[0], row[2], row[1]),
        )
        if stored_fills != replay_fills:
            raise ValueError("historical data or execution fills changed; use a new database")
        if existing and asof < dt.date.fromisoformat(max(existing)):
            raise ValueError("paper asof cannot move backwards")
        replay_dates = {str(point.date) for point in result.curve}
        if set(existing) - replay_dates:
            raise ValueError(
                "historical sessions were removed; refusing to rewrite the paper ledger"
            )
        new_dates: set[str] = set()
        for point in result.curve:
            date = str(point.date)
            if date in existing:
                checksum, equity, cash = existing[date]
                if (
                    checksum != by_date[point.date]
                    or abs(equity - point.equity) > 0.005
                    or abs(cash - point.cash) > 0.005
                ):
                    raise ValueError(
                        f"historical data or engine behavior changed at {date}; use a new database"
                    )
            else:
                new_dates.add(date)
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    (date, by_date[point.date], point.equity, point.cash, point.drawdown),
                )
        for fill in result.fills:
            if str(fill.date) in new_dates:
                connection.execute(
                    "INSERT INTO fills VALUES (?, ?, ?, ?, ?, ?)",
                    (str(fill.date), fill.symbol, fill.side, fill.quantity, fill.price, fill.fee),
                )
        for sequence, event in enumerate(result.events):
            if event["date"] in new_dates:
                connection.execute(
                    "INSERT INTO events VALUES (?, ?, ?)",
                    (event["date"], sequence, json.dumps(event)),
                )
        connection.executemany(
            "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
            [
                ("schema_version", "1"),
                ("config_hash", fingerprint),
                ("last_asof", str(asof)),
                ("halted", json.dumps(result.halted)),
                ("next_weights", json.dumps(result.next_weights)),
            ],
        )
    return result, len(new_dates)
