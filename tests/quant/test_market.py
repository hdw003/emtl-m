"""Uncapped discovery, complete pagination, immutable catalogs and activity."""

import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.quant.test_value import ASOF, fact
from tests.quant.test_value import config as value_config

from emtl.quant import market_scan
from emtl.quant.activity import activity
from emtl.quant.config import QuantConfig
from emtl.quant.data import load_csv, write_csv
from emtl.quant.engine import BacktestResult
from emtl.quant.execution import EMTBroker
from emtl.quant.market import (
    CatalogHistory,
    MarketDownloader,
    MarketSpec,
    MarketStore,
    normalize_listings,
    stock_instrument,
)
from emtl.quant.market_actions import annotate_actions
from emtl.quant.models import Bar, Costs, EquityPoint, Fill, OrderIntent, Portfolio
from emtl.quant.paper import run_paper
from emtl.quant.risk import RiskPolicy
from emtl.quant.strategies import StrategySpec
from emtl.quant.value import ValueSpec, ValueStore, screen_value


def test_value_no_quantity_or_industry_ceiling(tmp_path):
    facts = [fact(f"{i:06}.SZ") for i in range(1000)]
    symbols = {f.symbol for f in facts}
    spec = ValueSpec(tmp_path / "facts.db", liquidity_window=1, min_daily_amount=0)
    result = screen_value(
        spec, facts, ASOF, dict.fromkeys(symbols, 10), {s: [10] for s in symbols}, symbols
    )
    assert len(result["selected"]) == 1000
    assert len({r["industry"] for r in result["ranking"]}) == 1
    limited = screen_value(
        replace(spec, top_n=8),
        facts,
        ASOF,
        dict.fromkeys(symbols, 10),
        {},
        symbols,
        check_liquidity=False,
    )
    assert len(limited["selected"]) == 8


def test_all_pages_cached_and_failed_page_resumes(tmp_path):
    calls, fail = [], {3}

    def get(url, *, params, timeout):
        page = params["pageNumber"]
        calls.append(page)
        if page in fail:
            raise ValueError("temporary failure")
        data = [{"id": i} for i in range((page - 1) * 100, min(page * 100, 701))]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"success": True, "result": {"count": 701, "pages": 8, "data": data}},
        )

    downloader = MarketDownloader(
        MarketSpec(tmp_path / "market.db", page_size=100),
        "today",
        session_factory=lambda: SimpleNamespace(get=get),
    )
    with pytest.raises(ValueError, match="pages failed"):
        downloader.table("all", "ALL", "")
    assert set(calls) == set(range(1, 9))
    fail.clear()
    calls.clear()
    assert len(downloader.table("all", "ALL", "")) == 701
    assert calls == [3]


def test_duplicate_or_truncated_pages_rejected(tmp_path):
    downloader = MarketDownloader(MarketSpec(tmp_path / "market.db", page_size=2), "today")
    downloader.get_json = lambda url, params: {
        "result": {"count": 4, "pages": 2, "data": [{"id": 1}, {"id": 2}]}
    }
    with pytest.raises(ValueError, match="duplicate"):
        downloader.table("all", "ALL", "")
    downloader.get_json = lambda url, params: {
        "result": {"count": 4, "pages": 2, "data": [{"id": params["pageNumber"]}]}
    }
    with pytest.raises(ValueError, match="truncated"):
        downloader.table("all", "ALL", "")


def test_listing_scope_and_board_rules():
    rows = [
        {"SECUCODE": s, "SECURITY_NAME_ABBR": s, "LISTING_DATE": "2020-01-01", "EM2016": "工业"}
        for s in ["600000.SH", "300001.SZ", "920000.BJ", "688001.SH"]
    ]
    rows += [
        {"SECUCODE": "A10003.SZ", "LISTING_DATE": None},
        {"SECUCODE": "001999.SZ", "LISTING_DATE": None},
    ]
    listings, excluded = normalize_listings(rows)
    assert len(listings) == 4
    assert {r["reason"] for r in excluded} == {"unlisted_application", "not_listed_yet"}
    assert stock_instrument("920000.BJ").price_limit == 0.30
    assert stock_instrument("300001.SZ").price_limit == 0.20
    assert stock_instrument("688001.SH").lot_size == 200
    with pytest.raises(ValueError, match="missing exchanges"):
        normalize_listings(rows[:1])


def test_dated_catalog_is_append_only(tmp_path):
    store = MarketStore(tmp_path / "market.db")
    today = dt.date(2024, 1, 2)
    tomorrow = today + dt.timedelta(days=1)
    listing = {"symbol": "600000.SH"}
    store.publish(today, [listing])
    store.publish(today, [listing])
    with pytest.raises(ValueError, match="conflict"):
        store.publish(today, [{"symbol": "000001.SZ"}])
    store.publish(tomorrow, [listing, {"symbol": "000001.SZ"}])
    history = CatalogHistory(store.catalogs())
    assert history.members(today - dt.timedelta(days=1)) == set()
    assert history.members(today) == {"600000.SH"}
    assert history.members(tomorrow) == {"600000.SH", "000001.SZ"}


def test_activity_resizing_is_not_stock_replacement():
    dates = [ASOF + dt.timedelta(days=i) for i in range(5)]
    result = BacktestResult(100, Portfolio(100))
    result.curve = [EquityPoint(d, 100, 0, 0) for d in dates]
    result.holdings = [
        (dates[0], frozenset()),
        (dates[1], frozenset({"A"})),
        (dates[2], frozenset({"A"})),
        (dates[3], frozenset({"B"})),
        (dates[4], frozenset({"B"})),
    ]
    result.fills = [
        Fill(dates[1], "A", "buy", 100, 1, 0),
        Fill(dates[2], "A", "buy", 100, 1, 0),
        Fill(dates[3], "A", "sell", 200, 1, 0),
        Fill(dates[3], "B", "buy", 100, 1, 0),
    ]
    stats = activity(result, "weekly")
    assert stats["actual_trading_days"] == 3
    assert stats["replacement_days"] == 1
    assert stats["same_day_replacement_pairs"] == 1
    assert stats["stock_entry_episodes"] == 2
    assert stats["stock_full_exit_episodes"] == 1
    assert stats["mean_completed_holding_sessions"] == 2
    assert stats["open_holding_episodes"] == 1


@pytest.mark.parametrize("fail", [False, True])
def test_scan_fetches_every_qualifier_without_200_ceiling(tmp_path, monkeypatch, fail):

    today = market_scan.latest_completed_date()
    symbols = [f"{i:06}.SZ" for i in range(251)] + ["600000.SH", "920000.BJ"]
    profiles = [
        {"SECUCODE": s, "SECURITY_NAME_ABBR": s, "LISTING_DATE": "2000-01-01", "EM2016": "工业"}
        for s in symbols
    ]
    annual = [
        {
            "SECUCODE": s,
            "REPORT_DATE": f"{today.year - 2}-12-31",
            "NOTICE_DATE": f"{today.year - 1}-04-01",
            "UPDATE_DATE": f"{today.year - 1}-04-01",
            "EPSJB": 2,
            "BPS": 10,
            "ROEJQ": 15,
            "MGJYXJJE": 3,
            "ZCFZL": 40,
        }
        for s in symbols
    ]

    def table(self, report, *args, **kwargs):
        if report == "RPT_F10_BASIC_ORGINFO":
            return profiles
        if report == "RPT_F10_FINANCE_MAINFINADATA":
            return annual
        return [{"SECUCODE": s, "TRADE_DATE": str(today), "CLOSE_PRICE": 10} for s in symbols]

    fetched = []

    def fetch(self, instrument, start, end):
        fetched.append(instrument.symbol)
        if fail and instrument.symbol == symbols[0]:
            raise ValueError("source unavailable")
        return [
            Bar(today - dt.timedelta(days=i), instrument.symbol, 10, 10, 10, 10, 3_000_000)
            for i in reversed(range(25))
        ]

    monkeypatch.setattr(MarketDownloader, "table", table)
    monkeypatch.setattr(market_scan.TencentDailyProvider, "fetch", fetch)
    monkeypatch.setattr(
        market_scan, "fetch_actions", lambda downloader, bars, start, end: (bars, {})
    )
    spec = MarketSpec(tmp_path / "market.db")
    config = QuantConfig(
        tmp_path / "bars.csv",
        (),
        StrategySpec(name="dual_ma"),
        Costs(),
        RiskPolicy(),
        value=ValueSpec(tmp_path / "facts.db", max_financial_age_days=2000),
        market=spec,
        data_provider="tencent",
    )
    prior = {
        "symbol": "999999.SZ",
        "listed_on": "2000-01-01",
        "name": "removed",
        "industry": "工业",
        "market": "SZ",
    }
    store = MarketStore(spec.store)
    store.publish(today - dt.timedelta(days=1), [prior])
    old_catalogs = store.catalogs()
    old_bars = [Bar(today - dt.timedelta(days=40), "999999.SZ", 10, 10, 10, 10, 10000)]
    write_csv(config.data, old_bars)
    if fail:
        with pytest.raises(ValueError, match="sources failed"):
            market_scan.scan_market(config, asof=today)
        assert len(fetched) == 254
        assert MarketStore(spec.store).catalogs() == old_catalogs
        assert load_csv(config.data) == old_bars
        return
    result = market_scan.scan_market(config, asof=today)
    assert len(fetched) == 254
    assert "999999.SZ" in fetched  # Removed catalog names still get bars for exits.
    assert result["selected_count"] == 253
    assert set(result["screening_status"]) == set(symbols)
    assert result["no_quantity_truncation"] is True
    assert len(MarketStore(spec.store).history_availability()) == 254


def test_dynamic_history_bootstrap_does_not_rewrite_paper(tmp_path):

    original = value_config(tmp_path, facts=[fact()])
    market = MarketSpec(tmp_path / "market.db", fractional_action="error")
    store = MarketStore(market.store)
    store.publish(ASOF, [{"symbol": "600000.SH"}])
    store.publish_history(ASOF, {"600000.SH"})
    base = replace(original, market=market, instruments=store.instruments())
    allbars = load_csv(base.data)
    write_csv(base.data, [bar for bar in allbars if bar.symbol == "600000.SH"])
    database = tmp_path / "paper.db"
    first, _ = run_paper(base, database, asof=ASOF + dt.timedelta(days=2))
    later = dt.date(2024, 2, 1)
    store.publish(later, [{"symbol": "600000.SH"}, {"symbol": "000001.SZ"}])
    store.publish_history(later, {"000001.SZ"})
    ValueStore(base.value.fundamentals).append([fact("000001.SZ", known_on=later, annual_eps=3)])
    write_csv(base.data, allbars)
    extended = replace(base, instruments=store.instruments())
    assert extended.fingerprint() == base.fingerprint()
    second, added = run_paper(extended, database, asof=dt.date(2024, 2, 2))
    assert added == 2
    assert [
        point for point in second.curve if point.date <= ASOF + dt.timedelta(days=2)
    ] == first.curve


def test_distribution_units_duplicates_and_delayed_bar():
    rows = [
        {
            "ASSIGN_PROGRESS": "实施分配",
            "SECUCODE": "600000.SH",
            "EX_DIVIDEND_DATE": "2024-01-03",
            "REPORT_DATE": "2023-12-31",
            "PRETAX_BONUS_RMB": 10,
            "BONUS_IT_RATIO": 2,
            "BONUS_RATIO": 1,
            "IT_RATIO": 1,
        }
    ]
    bars = [Bar(dt.date(2024, 1, day), "600000.SH", 10, 10, 10, 10, 10000) for day in (2, 4)]
    enriched, evidence = annotate_actions(bars, rows)
    assert enriched[-1].cash_dividend == 1
    assert enriched[-1].split_ratio == 1.2
    assert evidence["delayed_until_next_observed_bar"][0]["applied_on"] == "2024-01-04"
    with pytest.raises(ValueError, match="duplicate"):
        annotate_actions(bars, rows * 2)
    combined, _ = annotate_actions(bars, [*rows, {**rows[0], "REPORT_DATE": "2022-12-31"}])
    assert combined[-1].cash_dividend == 2
    assert combined[-1].split_ratio == 1.4
    with pytest.raises(ValueError, match="invalid distribution"):
        annotate_actions(bars, [{**rows[0], "BONUS_IT_RATIO": 10}])


def test_bj_execution_fails_before_journal_or_network(tmp_path):
    client = SimpleNamespace(validate_key="unused")
    journal = tmp_path / "orders.db"
    broker = EMTBroker(client, journal, enabled=True)
    with pytest.raises(ValueError, match="BJ live routing"):
        broker.submit(OrderIntent("920000.BJ", "buy", 100, 10), "bj-test")
    assert not journal.exists()
