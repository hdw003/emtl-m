"""Strict CSV daily bars and an optional public Eastmoney daily data provider."""

from __future__ import annotations

import csv
import datetime as dt
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Bar, Instrument

FIELDS = (
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "buy_allowed",
    "sell_allowed",
    "split_ratio",
    "cash_dividend",
)


class DailyProvider(Protocol):
    def fetch(self, instrument: Instrument, start: dt.date, end: dt.date) -> list[Bar]: ...


def _boolean(value: str) -> bool:
    if value not in {"0", "1", "true", "false"}:
        raise ValueError("boolean must be 0/1/true/false")
    return value in {"1", "true"}


def load_csv(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    seen: set[tuple[dt.date, str]] = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = set(FIELDS[:7])
        if not required <= set(reader.fieldnames or []):
            raise ValueError(f"CSV requires columns: {', '.join(FIELDS[:7])}")
        unknown = set(reader.fieldnames or []) - set(FIELDS)
        if unknown:
            raise ValueError(f"unknown CSV columns: {sorted(unknown)}")
        for line, row in enumerate(reader, 2):
            try:
                symbol = row["symbol"].strip()
                Instrument(symbol)
                bar = Bar(
                    dt.date.fromisoformat(row["date"]),
                    symbol,
                    *(float(row[name]) for name in ("open", "high", "low", "close", "volume")),
                    buy_allowed=_boolean(row.get("buy_allowed") or "1"),
                    sell_allowed=_boolean(row.get("sell_allowed") or "1"),
                    split_ratio=float(row.get("split_ratio") or "1"),
                    cash_dividend=float(row.get("cash_dividend") or "0"),
                )
                key = (bar.date, bar.symbol)
                if key in seen:
                    raise ValueError(f"duplicate bar: {key}")
                seen.add(key)
                bars.append(bar)
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"{path}:{line}: {exc}") from exc
    if not bars:
        raise ValueError("CSV has no bars")
    return sorted(bars, key=lambda b: (b.date, b.symbol))


def write_csv(path: Path, bars: Sequence[Bar]) -> None:
    """Replace only after the complete download succeeds; never leave partial data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".bars-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            for b in sorted(bars, key=lambda b: (b.date, b.symbol)):
                writer.writerow(
                    {
                        **{name: getattr(b, name) for name in FIELDS},
                        "date": b.date.isoformat(),
                        "buy_allowed": int(b.buy_allowed),
                        "sell_allowed": int(b.sell_allowed),
                    }
                )
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class EastmoneyDailyProvider:
    """Unadjusted daily OHLCV, volume converted from lots to shares.

    Corporate actions are not supplied by this endpoint. Annotate raw CSV with
    split_ratio/cash_dividend before relying on multi-year raw-price results.
    """

    def __init__(self, session: requests.Session | None = None, timeout: float = 20) -> None:
        self.session = session or requests.Session()
        if session is None:
            retries = Retry(
                total=3,
                backoff_factor=0.5,
                backoff_max=5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset({"GET"}),
            )
            self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.timeout = timeout

    def fetch(self, instrument: Instrument, start: dt.date, end: dt.date) -> list[Bar]:
        if start > end:
            raise ValueError("start must be <= end")
        code, market = instrument.symbol.split(".")
        response = self.session.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": f"{1 if market == 'SH' else 0}.{code}",
                "klt": 101,
                "fqt": 0,
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("rc", 0) != 0:
            raise ValueError(f"invalid daily-data response for {instrument.symbol}")
        data = payload.get("data")
        rows = data.get("klines") if isinstance(data, dict) else None
        if data is None or rows == []:
            return []  # Holiday, future listing date, or empty date range.
        if not isinstance(rows, list):
            raise ValueError(f"invalid daily bars for {instrument.symbol}")
        bars = []
        for row in rows:
            date, opening, close, high, low, volume = row.split(",")
            bar = Bar(
                dt.date.fromisoformat(date),
                instrument.symbol,
                float(opening),
                float(high),
                float(low),
                float(close),
                float(volume) * 100,
            )
            if start <= bar.date <= end:
                bars.append(bar)
        if len({b.date for b in bars}) != len(bars):
            raise ValueError("provider returned duplicate dates")
        return sorted(bars, key=lambda b: b.date)


class TencentDailyProvider:
    """Explicit alternative raw daily source; yearly windows avoid row truncation."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 20) -> None:
        self.session = session or EastmoneyDailyProvider().session
        self.timeout = timeout

    def fetch(self, instrument: Instrument, start: dt.date, end: dt.date) -> list[Bar]:
        if start > end:
            raise ValueError("start must be <= end")
        ticker = instrument.symbol[-2:].lower() + instrument.symbol[:6]
        bars = []
        cursor = start
        while cursor <= end:
            stop = min(end, dt.date(cursor.year, 12, 31))
            response = self.session.get(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                params={"param": f"{ticker},day,{cursor},{stop},640,"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise ValueError(f"invalid Tencent response for {instrument.symbol}")
            series = (payload.get("data") or {}).get(ticker)
            if not isinstance(series, dict) or "day" not in series:
                raise ValueError(
                    f"raw day data missing for {instrument.symbol}; adjusted data not accepted"
                )
            for row in series["day"]:
                date, opening, close, high, low, volume = row[:6]
                bar = Bar(
                    dt.date.fromisoformat(date),
                    instrument.symbol,
                    float(opening),
                    float(high),
                    float(low),
                    float(close),
                    float(volume) * 100,
                )
                if cursor <= bar.date <= stop:
                    bars.append(bar)
            cursor = stop + dt.timedelta(days=1)
        if len({b.date for b in bars}) != len(bars):
            raise ValueError("Tencent returned duplicate daily bars")
        return sorted(bars, key=lambda b: b.date)
