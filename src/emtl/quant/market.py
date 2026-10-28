"""Uncapped paginated market discovery with durable request checkpoints."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .data import EastmoneyDailyProvider
from .models import Instrument

SECURITIES_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
DATA_URL = "https://datacenter.eastmoney.com/api/data/v1/get"
PROFILE_COLUMNS = (
    "SECUCODE,SECURITY_NAME_ABBR,LISTING_DATE,EM2016,INDUSTRYCSRC1,SECURITY_TYPE_CODE,TRADE_MARKET"
)
ANNUAL_COLUMNS = "SECUCODE,SECURITY_NAME_ABBR,REPORT_DATE,NOTICE_DATE,UPDATE_DATE,EPSJB,BPS,MGJYXJJE,ROEJQ,ZCFZL,ORG_TYPE"


def canonical(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def stock_instrument(symbol: str) -> Instrument:
    code, market = symbol.split(".")
    if market == "BJ":
        return Instrument(symbol, price_limit=0.30, stamp_tax=True)
    if market == "SH" and code.startswith(("688", "689")):
        return Instrument(symbol, lot_size=200, price_limit=0.20, stamp_tax=True)
    if market == "SZ" and code.startswith(("300", "301")):
        return Instrument(symbol, price_limit=0.20, stamp_tax=True)
    return Instrument(symbol, stamp_tax=True)


@dataclass(frozen=True, slots=True)
class MarketSpec:
    store: Path
    workers: int = 4
    page_size: int = 500
    history_start: dt.date = dt.date(2024, 1, 1)
    fractional_action: str = "floor"

    def __post_init__(self) -> None:
        if type(self.workers) is not int or not 1 <= self.workers <= 32:
            raise ValueError("workers must be 1..32 (concurrency only, not universe size)")
        if type(self.page_size) is not int or not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be 1..500; all pages are always fetched")
        if self.fractional_action not in {"floor", "error"}:
            raise ValueError("fractional_action must be floor or error")


class MarketStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS request_cache (
                scope TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
                digest TEXT NOT NULL, PRIMARY KEY(scope, key)
            );
            CREATE TABLE IF NOT EXISTS catalogs (
                known_on TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS history_availability (
                symbol TEXT PRIMARY KEY, known_on TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL
            );
        """)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cached(self, scope: str, key: str):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload,digest FROM request_cache WHERE scope=? AND key=?", (scope, key)
            ).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
            raise ValueError(f"market cache checksum mismatch: {key}")
        return json.loads(row[0])

    def cache(self, scope: str, key: str, payload) -> None:
        text = canonical(payload)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO request_cache VALUES (?,?,?,?)", (scope, key, text, digest)
            )

    def checkpoint(self, run_id: str, status: str, payload: dict) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?)", (run_id, status, canonical(payload))
            )

    def publish(self, known_on: dt.date, listings: list[dict]) -> None:
        payload = canonical(
            {
                "known_on": str(known_on),
                "listings": sorted(listings, key=lambda r: r["symbol"]),
                "rule_version": 1,
            }
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as connection:
            old = connection.execute(
                "SELECT digest FROM catalogs WHERE known_on=?", (str(known_on),)
            ).fetchone()
            if old and old[0] != digest:
                raise ValueError("same-day market catalog conflict; preserve the old experiment")
            connection.execute(
                "INSERT OR IGNORE INTO catalogs VALUES (?,?,?)", (str(known_on), payload, digest)
            )

    def catalogs(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.connect() as connection:
            rows = list(connection.execute("SELECT payload,digest FROM catalogs ORDER BY known_on"))
        result = []
        for payload, digest in rows:
            if hashlib.sha256(payload.encode()).hexdigest() != digest:
                raise ValueError("market catalog checksum mismatch")
            result.append(json.loads(payload))
        return result

    def publish_history(self, known_on: dt.date, symbols: set[str]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO history_availability VALUES (?,?)",
                [(symbol, str(known_on)) for symbol in sorted(symbols)],
            )

    def history_availability(self) -> dict[str, dt.date]:
        if not self.path.exists():
            return {}
        with self.connect() as connection:
            return {
                symbol: dt.date.fromisoformat(date)
                for symbol, date in connection.execute(
                    "SELECT symbol,known_on FROM history_availability"
                )
            }

    def instruments(self) -> tuple[Instrument, ...]:
        symbols = {r["symbol"] for c in self.catalogs() for r in c["listings"]}
        return tuple(stock_instrument(s) for s in sorted(symbols))


class CatalogHistory:
    def __init__(
        self, catalogs: list[dict], availability: dict[str, dt.date] | None = None
    ) -> None:
        self.catalogs = catalogs
        self.availability = availability

    def members(self, date: dt.date) -> set[str]:
        available = [c for c in self.catalogs if c["known_on"] <= str(date)]
        members = {r["symbol"] for r in available[-1]["listings"]} if available else set()
        if self.availability is not None:
            members &= {
                symbol for symbol, observed in self.availability.items() if observed <= date
            }
        return members


class MarketDownloader:
    """Each successful page/stock request is immediately cached for restart."""

    def __init__(
        self, spec: MarketSpec, scope: str, *, session_factory=None, progress=None
    ) -> None:
        self.spec, self.scope = spec, scope
        self.store = MarketStore(spec.store)
        self.local = threading.local()
        self.session_factory = session_factory or (lambda: EastmoneyDailyProvider().session)
        self.progress = progress or (lambda message: None)

    def get_json(self, url: str, params: dict):
        key = hashlib.sha256(canonical({"url": url, "params": params}).encode()).hexdigest()
        cached = self.store.cached(self.scope, key)
        if cached is not None:
            return cached
        if not hasattr(self.local, "session"):
            self.local.session = self.session_factory()
        response = self.local.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("market response is not an object")
        if "success" in payload and payload["success"] is not True:
            raise ValueError(f"market endpoint rejected request: {payload.get('message')}")
        if "code" in payload and payload["code"] != 0:
            raise ValueError("market data endpoint returned an error code")
        self.store.cache(self.scope, key, payload)
        return payload

    def table(
        self,
        report: str,
        columns: str,
        filter_expression: str,
        *,
        url=SECURITIES_URL,
        sort="SECUCODE",
    ) -> list[dict]:
        params = {
            "reportName": report,
            "columns": columns,
            "filter": filter_expression,
            "pageSize": self.spec.page_size,
            "pageNumber": 1,
            "sortColumns": sort,
            "sortTypes": ",".join("1" for _ in sort.split(",")),
        }
        first = self.get_json(url, params)
        result = first.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError(f"invalid market table: {report}")
        count, pages = result.get("count"), result.get("pages")
        if type(count) is not int or type(pages) is not int or count < 0 or pages < 0:
            raise ValueError(f"missing pagination metadata: {report}")
        if count and (
            not pages or pages != (count + self.spec.page_size - 1) // self.spec.page_size
        ):
            raise ValueError(f"inconsistent page count: {report}")
        data_by_page = {1: result["data"]}
        errors = []

        def fetch(page):
            payload = self.get_json(url, {**params, "pageNumber": page})
            chunk = payload.get("result")
            if (
                not isinstance(chunk, dict)
                or chunk.get("count") != count
                or chunk.get("pages") != pages
                or not isinstance(chunk.get("data"), list)
            ):
                raise ValueError(f"pagination changed while reading {report}, page {page}")
            return chunk["data"]

        with ThreadPoolExecutor(max_workers=self.spec.workers) as pool:
            jobs = {pool.submit(fetch, page): page for page in range(2, pages + 1)}
            for job in as_completed(jobs):
                page = jobs[job]
                try:
                    data_by_page[page] = job.result()
                except Exception as exc:
                    errors.append({"page": page, "error": str(exc)})
                self.progress(
                    {
                        "stage": report,
                        "completed_pages": len(data_by_page),
                        "total_pages": pages,
                        "failed_pages": len(errors),
                    }
                )
        if errors:
            raise ValueError(
                f"{report}: {len(errors)} pages failed; successful pages cached; first error: {errors[0]}"
            )
        rows = [row for page in sorted(data_by_page) for row in data_by_page[page]]
        if len(rows) != count:
            raise ValueError(f"truncated market table {report}: expected {count}, got {len(rows)}")
        fingerprints = [canonical(row) for row in rows]
        if len(set(fingerprints)) != len(rows):
            raise ValueError(f"duplicate rows or repeated pages in {report}")
        self.progress({"stage": report, "rows": len(rows), "status": "complete"})
        return rows


def normalize_listings(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    listings, excluded = [], []
    seen = set()
    for row in rows:
        symbol = row.get("SECUCODE", "")
        market = symbol.rsplit(".", 1)[-1]
        if market not in {"SH", "SZ", "BJ"}:
            excluded.append({"symbol": symbol, "reason": "outside_SH_SZ_BJ"})
            continue
        code = symbol.split(".")[0]
        if len(code) != 6 or not code.isdigit():
            if code.startswith("A") and not row.get("LISTING_DATE"):
                excluded.append({"symbol": symbol, "reason": "unlisted_application"})
                continue
            raise ValueError(f"unrecognized listing identity: {symbol}")
        if not row.get("LISTING_DATE"):
            excluded.append({"symbol": symbol, "reason": "not_listed_yet"})
            continue
        stock_instrument(symbol)
        if symbol in seen:
            raise ValueError(f"duplicate listing identity: {symbol}")
        seen.add(symbol)
        listings.append(
            {
                "symbol": symbol,
                "name": row["SECURITY_NAME_ABBR"],
                "listed_on": str(row["LISTING_DATE"])[:10],
                "industry": row.get("EM2016") or row.get("INDUSTRYCSRC1") or "unknown",
                "market": market,
            }
        )
    markets = {row["market"] for row in listings}
    if markets != {"SH", "SZ", "BJ"}:
        raise ValueError(f"full-market catalog missing exchanges: {markets}")
    return listings, excluded
