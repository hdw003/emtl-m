"""Point-in-time annual value factors, append-only storage and pool selection."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .models import Instrument


@dataclass(frozen=True, slots=True)
class Fundamental:
    symbol: str
    period_end: dt.date
    published_on: dt.date
    known_on: dt.date
    listed_on: dt.date
    name: str
    industry: str
    annual_eps: float | None
    book_value_per_share: float | None
    roe: float | None
    operating_cash_flow_per_share: float | None
    debt_ratio: float | None
    is_st: bool = False
    is_active: bool = True
    source: str = "import"

    def __post_init__(self) -> None:
        Instrument(self.symbol)
        if self.period_end.month != 12 or self.period_end.day != 31:
            raise ValueError("value factors require annual reports ending December 31")
        if not self.period_end < self.published_on < self.known_on:
            raise ValueError(
                "require period_end < published_on < known_on (one-day publication lag)"
            )
        if self.listed_on > self.known_on or not self.name or not self.industry or not self.source:
            raise ValueError("invalid listing date or missing identity/source")
        for key in (
            "annual_eps",
            "book_value_per_share",
            "roe",
            "operating_cash_flow_per_share",
            "debt_ratio",
        ):
            value = getattr(self, key)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{key} must be finite or missing")
        if self.debt_ratio is not None and not 0 <= self.debt_ratio <= 1:
            raise ValueError("debt_ratio must be a fraction in [0, 1]")
        if type(self.is_st) is not bool or type(self.is_active) is not bool:
            raise ValueError("is_st/is_active must be booleans")

    def json(self) -> str:
        return json.dumps(
            asdict(self), sort_keys=True, default=str, ensure_ascii=False, allow_nan=False
        )

    @classmethod
    def parse(cls, raw: dict) -> Fundamental:
        raw = dict(raw)
        for key in ("period_end", "published_on", "known_on", "listed_on"):
            raw[key] = dt.date.fromisoformat(raw[key])
        for key in (
            "annual_eps",
            "book_value_per_share",
            "roe",
            "operating_cash_flow_per_share",
            "debt_ratio",
        ):
            raw[key] = float(raw[key]) if raw.get(key) not in {None, ""} else None
        for key, default in (("is_st", False), ("is_active", True)):
            value = raw.get(key, default)
            if isinstance(value, str):
                if value.lower() not in {"true", "false", "1", "0"}:
                    raise ValueError(f"invalid {key}")
                value = value.lower() in {"true", "1"}
            raw[key] = value
        return cls(**raw)


class ValueStore:
    """Transactions preserve old observations; revisions require a new known_on."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS facts (
                symbol TEXT NOT NULL, period_end TEXT NOT NULL, known_on TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(symbol, period_end, known_on)
            );
            CREATE TABLE IF NOT EXISTS captures (
                digest TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pools (
                digest TEXT PRIMARY KEY, asof TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def append(self, facts: list[Fundamental], *, evidence: dict | None = None) -> int:
        count = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for fact in facts:
                key = fact.symbol, str(fact.period_end), str(fact.known_on)
                old = connection.execute(
                    "SELECT payload FROM facts WHERE symbol=? AND period_end=? AND known_on=?", key
                ).fetchone()
                payload = fact.json()
                if old and old[0] != payload:
                    raise ValueError(
                        f"conflicting observation {key}; preserve old data and use a new known_on"
                    )
                if not old:
                    connection.execute("INSERT INTO facts VALUES (?, ?, ?, ?)", (*key, payload))
                    count += 1
            if evidence is not None:
                payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False, allow_nan=False)
                digest = hashlib.sha256(payload.encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO captures VALUES (?, ?)", (digest, payload)
                )
        return count

    def load(self) -> list[Fundamental]:
        if not self.path.exists():
            return []
        with self._connect() as connection:
            return [
                Fundamental.parse(json.loads(row[0]))
                for row in connection.execute(
                    "SELECT payload FROM facts ORDER BY known_on, symbol, period_end"
                )
            ]

    def import_csv(self, path: Path) -> int:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            unknown = set(reader.fieldnames or []) - {f.name for f in fields(Fundamental)}
            if unknown:
                raise ValueError(f"unknown fundamental columns: {sorted(unknown)}")
            try:
                facts = [Fundamental.parse(row) for row in reader]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid fundamentals CSV: {exc}") from exc
        if not facts:
            raise ValueError("fundamentals CSV is empty")
        return self.append(facts)

    def save_pool(self, payload: dict) -> str:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO pools VALUES (?, ?, ?)", (digest, payload["date"], text)
            )
        return digest


@dataclass(frozen=True, slots=True)
class ValueSpec:
    fundamentals: Path
    frequency: str = "monthly"
    top_n: int = 10
    max_pe: float = 25
    max_pb: float = 3
    min_roe: float = 0.08
    max_debt_ratio: float = 0.70
    min_cash_flow_per_share: float = 0
    min_listing_days: int = 365
    max_financial_age_days: int = 550
    liquidity_window: int = 20
    min_daily_amount: float = 20_000_000
    max_per_industry: int = 3
    earnings_weight: float = 0.4
    book_weight: float = 0.3
    quality_weight: float = 0.3
    excluded_industries: tuple[str, ...] = ("银行", "保险", "证券")

    def __post_init__(self) -> None:
        if self.frequency not in {"monthly", "quarterly"}:
            raise ValueError("value frequency must be monthly or quarterly")
        for key in (
            "top_n",
            "min_listing_days",
            "max_financial_age_days",
            "liquidity_window",
            "max_per_industry",
        ):
            if type(getattr(self, key)) is not int or getattr(self, key) < 1:
                raise ValueError(f"{key} must be a positive integer")
        for key in (
            "max_pe",
            "max_pb",
            "min_roe",
            "max_debt_ratio",
            "min_cash_flow_per_share",
            "min_daily_amount",
            "earnings_weight",
            "book_weight",
            "quality_weight",
        ):
            if not math.isfinite(getattr(self, key)) or getattr(self, key) < 0:
                raise ValueError(f"invalid value parameter {key}")
        if self.max_pe == 0 or self.max_pb == 0 or not 0 <= self.max_debt_ratio <= 1:
            raise ValueError("positive PE/PB limits and debt fraction <= 1 required")
        if not math.isclose(self.earnings_weight + self.book_weight + self.quality_weight, 1):
            raise ValueError("value score weights must sum to 1")


def latest_facts(facts: list[Fundamental], date: dt.date) -> dict[str, Fundamental]:
    result: dict[str, Fundamental] = {}
    for fact in facts:
        if fact.known_on <= date and (
            fact.symbol not in result
            or (fact.period_end, fact.known_on)
            > (result[fact.symbol].period_end, result[fact.symbol].known_on)
        ):
            result[fact.symbol] = fact
    return result


def screen_value(
    spec: ValueSpec,
    facts: list[Fundamental],
    date: dt.date,
    prices: dict[str, float],
    amounts: dict[str, list[float]],
    tradable: set[str],
) -> dict:
    known = latest_facts(facts, date)
    ranked = []
    rejected: dict[str, str] = {}
    for symbol in sorted(prices):
        fact = known.get(symbol)
        reason = None
        if fact is None:
            reason = "no_known_financials"
        elif fact.is_st or not fact.is_active:
            reason = "st_or_inactive"
        elif (date - fact.listed_on).days < spec.min_listing_days:
            reason = "new_listing"
        elif (date - fact.period_end).days > spec.max_financial_age_days:
            reason = "stale_financials"
        elif any(part in fact.industry for part in spec.excluded_industries):
            reason = "excluded_industry"
        elif any(
            getattr(fact, key) is None
            for key in (
                "annual_eps",
                "book_value_per_share",
                "roe",
                "operating_cash_flow_per_share",
                "debt_ratio",
            )
        ):
            reason = "missing_factors"
        elif fact.annual_eps <= 0 or fact.book_value_per_share <= 0:
            reason = "nonpositive_earnings_or_book"
        elif (
            fact.roe < spec.min_roe
            or fact.debt_ratio > spec.max_debt_ratio
            or fact.operating_cash_flow_per_share <= spec.min_cash_flow_per_share
        ):
            reason = "quality_filter"
        elif symbol not in tradable:
            reason = "not_tradable"
        elif (
            len(amounts.get(symbol, [])) < spec.liquidity_window
            or sum(amounts[symbol][-spec.liquidity_window :]) / spec.liquidity_window
            < spec.min_daily_amount
        ):
            reason = "illiquid_or_warmup"
        elif not math.isfinite(prices[symbol]) or prices[symbol] <= 0:
            raise ValueError("value prices must be positive and finite")
        if reason:
            rejected[symbol] = reason
            continue
        assert fact is not None
        assert fact.annual_eps is not None
        assert fact.book_value_per_share is not None
        pe, pb = prices[symbol] / fact.annual_eps, prices[symbol] / fact.book_value_per_share
        if pe > spec.max_pe or pb > spec.max_pb:
            rejected[symbol] = "valuation_filter"
            continue
        ranked.append(
            {
                "symbol": symbol,
                "name": fact.name,
                "industry": fact.industry,
                "pe_annual": pe,
                "pb_annual": pb,
                "roe": fact.roe,
                "known_on": str(fact.known_on),
                "period_end": str(fact.period_end),
                "score": 0.0,
            }
        )
    # Average empirical ranks preserve equal scores for equal factor values.
    n = len(ranked)
    for key, weight, smaller in (
        ("pe_annual", spec.earnings_weight, True),
        ("pb_annual", spec.book_weight, True),
        ("roe", spec.quality_weight, False),
    ):
        for row in ranked:
            worse = sum(
                (other[key] > row[key]) if smaller else (other[key] < row[key]) for other in ranked
            )
            ties = sum(other[key] == row[key] for other in ranked)
            row["score"] += weight * (worse + (ties - 1) / 2) / max(1, n - 1)
    ranked.sort(key=lambda row: (-row["score"], row["symbol"]))
    counts: Counter = Counter()
    selected = []
    for row in ranked:
        if len(selected) < spec.top_n and counts[row["industry"]] < spec.max_per_industry:
            selected.append(row["symbol"])
            counts[row["industry"]] += 1
    digest = hashlib.sha256(
        "\n".join(f.json() for f in sorted(known.values(), key=lambda f: f.symbol)).encode()
    ).hexdigest()
    return {
        "date": str(date),
        "selected": selected,
        "ranking": ranked,
        "rejected": rejected,
        "rejection_counts": dict(Counter(rejected.values())),
        "facts_digest": digest,
        "valuation_basis": "latest known annual EPS/BVPS, not TTM",
    }


class ValueUniverse:
    def __init__(self, spec: ValueSpec, facts: list[Fundamental]) -> None:
        self.spec, self.facts = spec, facts
        self.reset()

    def reset(self) -> None:
        self.members: set[str] = set()
        self.period = None
        self.snapshots: list[dict] = []
        self.seen_financials = False

    def update(
        self,
        date: dt.date,
        prices: dict[str, float],
        amounts: dict[str, list[float]],
        tradable: set[str],
    ) -> bool:
        period = (
            date.year,
            date.month if self.spec.frequency == "monthly" else (date.month - 1) // 3,
        )
        previous = self.members.copy()
        known = latest_facts(self.facts, date)
        first_known = bool(set(known) & set(prices)) and not self.seen_financials
        self.seen_financials = self.seen_financials or first_known
        unsafe = {
            s
            for s in self.members
            if s not in known
            or known[s].is_st
            or not known[s].is_active
            or (date - known[s].period_end).days > self.spec.max_financial_age_days
        }
        if period != self.period or first_known:
            snapshot = screen_value(self.spec, self.facts, date, prices, amounts, tradable)
            self.members = set(snapshot["selected"])
            self.snapshots.append(snapshot)
            self.period = period
        elif unsafe:
            self.members -= unsafe
            self.snapshots.append(
                {
                    "date": str(date),
                    "selected": sorted(self.members),
                    "reason": "safety_removal",
                    "removed": sorted(unsafe),
                }
            )
        return self.members != previous
