"""Whole-market value pipeline: bulk financials, uncapped screening, resumable bars."""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .config import QuantConfig
from .data import EastmoneyDailyProvider, TencentDailyProvider, load_csv, write_csv
from .market import (
    ANNUAL_COLUMNS,
    DATA_URL,
    PROFILE_COLUMNS,
    MarketDownloader,
    normalize_listings,
    stock_instrument,
)
from .market_actions import fetch_actions
from .paper import latest_completed_date
from .report import write_json
from .value import Fundamental, ValueStore, screen_value


class CachedSession:
    def __init__(self, downloader: MarketDownloader) -> None:
        self.downloader = downloader

    def get(self, url, *, params, timeout):
        payload = self.downloader.get_json(url, params)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def normalize_facts(
    listings: list[dict], annuals: list[dict], known_on: dt.date
) -> tuple[list[Fundamental], dict]:
    by_symbol = {}
    for row in annuals:
        if not row.get("NOTICE_DATE") or str(row["NOTICE_DATE"])[:10] >= str(known_on):
            continue
        symbol = row["SECUCODE"]
        prior = by_symbol.get(symbol)
        key = (str(row.get("REPORT_DATE")), str(row.get("UPDATE_DATE")), str(row["NOTICE_DATE"]))
        if prior is None or key > (
            str(prior.get("REPORT_DATE")),
            str(prior.get("UPDATE_DATE")),
            str(prior["NOTICE_DATE"]),
        ):
            by_symbol[symbol] = row
    facts, unavailable = [], {}
    for listing in listings:
        symbol = listing["symbol"]
        row = by_symbol.get(symbol)
        if row is None:
            unavailable[symbol] = "no_published_annual_report"
            continue

        def number(key, row=row):
            return float(row[key]) if row.get(key) not in {None, "", "-"} else None

        try:
            industry = (
                row["ORG_TYPE"]
                if row.get("ORG_TYPE") in {"银行", "保险", "证券"}
                else listing["industry"]
            )
            facts.append(
                Fundamental(
                    symbol,
                    dt.date.fromisoformat(row["REPORT_DATE"][:10]),
                    dt.date.fromisoformat(row["NOTICE_DATE"][:10]),
                    known_on,
                    dt.date.fromisoformat(listing["listed_on"]),
                    listing["name"],
                    industry,
                    number("EPSJB"),
                    number("BPS"),
                    number("ROEJQ") / 100 if number("ROEJQ") is not None else None,
                    number("MGJYXJJE"),
                    number("ZCFZL") / 100 if number("ZCFZL") is not None else None,
                    is_st="ST" in listing["name"].upper()
                    or "退" in listing["name"]
                    or listing["name"].startswith("PT"),
                    is_active="退" not in listing["name"] and not listing["name"].startswith("PT"),
                    source="eastmoney_full_market_observed_annual",
                )
            )
        except (ValueError, TypeError, KeyError) as exc:
            unavailable[symbol] = f"invalid_financial_record: {exc}"
    return facts, unavailable


def scan_market(config: QuantConfig, *, asof: dt.date | None = None, progress=None) -> dict:
    if config.market is None or config.value is None:
        raise ValueError("market-scan requires [market] and [value]")
    asof = asof or latest_completed_date()
    if asof > latest_completed_date():
        raise ValueError("market-scan prices must be completed daily data")
    now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    known_on = now.date()
    notify = progress or (lambda payload: None)
    downloader = MarketDownloader(config.market, str(asof), progress=notify)
    store = downloader.store
    run_id = f"{asof}:{config.fingerprint()}"
    manifest = {
        "run_id": run_id,
        "price_date": str(asof),
        "known_on": str(known_on),
        "candidate_cap": None,
        "value_pool_cap": config.value.top_n,
        "status": "running",
    }
    store.checkpoint(run_id, "running", manifest)
    try:
        profiles = downloader.table(
            "RPT_F10_BASIC_ORGINFO", PROFILE_COLUMNS, '(SECURITY_TYPE_CODE="058001001")'
        )
        listings, out_of_scope = normalize_listings(profiles)
        annuals = []
        for year in (known_on.year - 1, known_on.year - 2):
            annuals.extend(
                downloader.table(
                    "RPT_F10_FINANCE_MAINFINADATA", ANNUAL_COLUMNS, f"(REPORT_DATE='{year}-12-31')"
                )
            )
        facts, unavailable = normalize_facts(listings, annuals, known_on)
        price_rows = downloader.table(
            "RPT_VALUEANALYSIS_DET",
            "SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,CLOSE_PRICE",
            f"(TRADE_DATE='{asof}')",
            url=DATA_URL,
        )
        prices = {}
        for row in price_rows:
            if str(row.get("TRADE_DATE", ""))[:10] != str(asof):
                raise ValueError("bulk market price date mismatch")
            value = row.get("CLOSE_PRICE")
            if value is not None and math.isfinite(float(value)) and float(value) > 0:
                if row["SECUCODE"] in prices:
                    raise ValueError("duplicate bulk market price identity")
                prices[row["SECUCODE"]] = float(value)
        if {s.split(".")[-1] for s in prices} != {"SH", "SZ", "BJ"}:
            raise ValueError("bulk prices do not cover all SH/SZ/BJ exchanges")
        listing_by_symbol = {r["symbol"]: r for r in listings}
        historical_listings = {
            row["symbol"]: row for catalog in store.catalogs() for row in catalog["listings"]
        }
        historical_listings.update(listing_by_symbol)
        prices = {s: p for s, p in prices.items() if s in listing_by_symbol}
        uncapped = replace(config.value, top_n=None, max_per_industry=None)
        preliminary = screen_value(
            uncapped, facts, known_on, prices, {}, set(prices), check_liquidity=False
        )
        # No top-N history shortcut: EVERY financially/valuation-eligible stock
        # proceeds to liquidity and indicator history checks.
        existing = load_csv(config.data) if config.data.exists() else []
        existing_symbols = {b.symbol for b in existing}
        history_symbols = set(preliminary["selected"]) | existing_symbols
        provider_class = (
            TencentDailyProvider if config.data_provider == "tencent" else EastmoneyDailyProvider
        )
        dates_by_symbol = {}
        for bar in existing:
            dates_by_symbol[bar.symbol] = max(dates_by_symbol.get(bar.symbol, bar.date), bar.date)

        def fetch_history(symbol):
            start = max(
                config.market.history_start,
                dt.date.fromisoformat(historical_listings[symbol]["listed_on"]),
            )
            if symbol in dates_by_symbol:
                start = max(start, dates_by_symbol[symbol] + dt.timedelta(days=1))
            if start > asof:
                return []
            provider = provider_class(session=CachedSession(downloader))
            # Calendar-year windows avoid vendor failures on short empty ranges
            # (suspended securities). Reuse the archived raw response and append
            # only genuinely new dates; no cross-vendor price substitution.
            request_start = (
                start.replace(month=1, day=1) if config.data_provider == "tencent" else start
            )
            return [
                bar
                for bar in provider.fetch(stock_instrument(symbol), request_start, asof)
                if bar.date >= start
            ]

        appended, failures = [], {}
        completed = 0
        with ThreadPoolExecutor(max_workers=config.market.workers) as pool:
            jobs = {pool.submit(fetch_history, s): s for s in sorted(history_symbols)}
            for job in as_completed(jobs):
                symbol = jobs[job]
                try:
                    appended.extend(job.result())
                except Exception as exc:
                    failures[symbol] = str(exc)
                completed += 1
                if completed % 20 == 0 or completed == len(jobs):
                    notify(
                        {
                            "stage": "history",
                            "completed": completed,
                            "total": len(jobs),
                            "failed": len(failures),
                        }
                    )
        manifest.update(
            {
                "source_profile_rows": len(profiles),
                "registry_count": len(listings),
                "out_of_scope": out_of_scope,
                "listed_by_exchange": dict(Counter(r["market"] for r in listings)),
                "priced_count": len(prices),
                "priced_by_exchange": dict(Counter(s.split(".")[-1] for s in prices)),
                "financial_count": len(facts),
                "financial_unavailable": unavailable,
                "prequalified_count": len(preliminary["selected"]),
                "history_requested": len(history_symbols),
                "history_errors": failures,
            }
        )
        if failures:
            manifest["status"] = "incomplete"
            store.checkpoint(run_id, "incomplete", manifest)
            raise ValueError(
                f"{len(failures)} history sources failed; all other tasks completed and cached; resume the same scan"
            )
        bars = sorted([*existing, *appended], key=lambda b: (b.date, b.symbol))
        if len({(b.date, b.symbol) for b in bars}) != len(bars):
            raise ValueError("duplicate market bars after resume")
        bars, action_provenance = fetch_actions(downloader, bars, config.market.history_start, asof)
        manifest["corporate_actions"] = action_provenance
        amounts, latest_dates, latest_volumes = {}, {}, {}
        for bar in bars:
            if bar.date <= asof:
                amounts.setdefault(bar.symbol, {})[bar.date] = bar.close * bar.volume
                latest_dates[bar.symbol] = bar.date
                latest_volumes[bar.symbol] = bar.volume
        calendar = sorted({bar.date for bar in bars if bar.date <= asof})[
            -config.value.liquidity_window :
        ]
        amounts = {
            symbol: [values.get(date, 0) for date in calendar] for symbol, values in amounts.items()
        }
        tradable = {
            s for s in prices if latest_dates.get(s) == asof and latest_volumes.get(s, 0) > 0
        }
        final = screen_value(config.value, facts, known_on, prices, amounts, tradable)
        all_reasons = {}
        for row in listings:
            symbol = row["symbol"]
            if symbol not in prices:
                all_reasons[symbol] = "no_current_market_price"
            elif symbol in unavailable:
                all_reasons[symbol] = unavailable[symbol]
            elif symbol in preliminary["rejected"]:
                all_reasons[symbol] = preliminary["rejected"][symbol]
            elif symbol in final["selected"]:
                all_reasons[symbol] = "selected"
            else:
                all_reasons[symbol] = final["rejected"].get(symbol, "rank_or_industry_rule")
        # Publication occurs only after all requests finish. Future catalog
        # additions are dated observations, not changes to past experiments.
        completed_on = dt.datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if completed_on != known_on:
            raise ValueError(
                "scan crossed midnight; resume with a new observation date before publication"
            )
        ValueStore(config.value.fundamentals).append(
            facts,
            evidence={
                "run_id": run_id,
                "profile_count": len(profiles),
                "annual_rows": len(annuals),
                "price_rows": len(price_rows),
            },
        )
        write_csv(config.data, bars)
        store.publish(known_on, listings)
        store.publish_history(known_on, history_symbols)
        manifest.update(
            {
                "status": "complete",
                "selected": final["selected"],
                "selected_count": len(final["selected"]),
                "ranking": final["ranking"],
                "screening_status": all_reasons,
                "status_counts": dict(Counter(all_reasons.values())),
                "data_rows": len(bars),
                "fresh_rows": len(appended),
                "no_quantity_truncation": True,
                "historical_financial_backtest_ready": False,
                "history_coverage_warning": "Missing/short histories and stale bars are explicitly excluded by liquidity/tradability rules, not by a candidate count limit.",
            }
        )
        store.checkpoint(run_id, "complete", manifest)
        ValueStore(config.value.fundamentals).save_pool(final)
        write_json(config.data.parent / "full-market-scan.json", manifest)
        return manifest
    except Exception as exc:
        manifest.update({"status": "incomplete", "error": str(exc)})
        store.checkpoint(run_id, "incomplete", manifest)
        raise
