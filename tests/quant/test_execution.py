"""Live submissions are opt-in and never retried after an ambiguous outcome."""

import datetime as dt
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from emtl._models import ApiResponse, TradeMarket
from emtl.quant import AccountSnapshot, EMTBroker, Instrument, OrderIntent, plan_rebalance


def test_disabled_broker_does_not_submit(tmp_path: Path) -> None:
    broker = EMTBroker(SimpleNamespace(), tmp_path / "orders.db")
    with pytest.raises(RuntimeError, match="disabled"):
        broker.submit(OrderIntent("510300.SH", "buy", 100, 10), "daily-1")


def test_success_and_duplicate_are_journaled(tmp_path: Path) -> None:
    calls = []

    def create_order(*args):
        calls.append(args)
        return ApiResponse({"Status": 0, "Data": []})

    client = SimpleNamespace(validate_key="fake", create_order=create_order)
    broker = EMTBroker(client, tmp_path / "orders.db", enabled=True)
    intent = OrderIntent("510300.SH", "buy", 100, 10)
    assert broker.submit(intent, "2024-01-02/510300.SH/buy").ok
    assert calls[0][2] == TradeMarket.SH
    with pytest.raises(RuntimeError, match="already recorded"):
        broker.submit(intent, "2024-01-02/510300.SH/buy")
    assert len(calls) == 1


def test_timeout_blocks_automatic_resubmission(tmp_path: Path) -> None:
    def timeout(*args):
        raise TimeoutError("ambiguous response")

    database = tmp_path / "orders.db"
    broker = EMTBroker(
        SimpleNamespace(validate_key="fake", create_order=timeout), database, enabled=True
    )
    intent = OrderIntent("159915.SZ", "buy", 100, 10)
    with pytest.raises(TimeoutError):
        broker.submit(intent, "stable-key")
    with pytest.raises(RuntimeError, match="unknown"):
        broker.submit(intent, "stable-key")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT state FROM submissions").fetchone()[0] == "unknown"


def test_plan_respects_t_plus_one_and_does_not_spend_unfilled_sells() -> None:
    now = dt.datetime(2024, 1, 2, 2, tzinfo=dt.UTC)
    account = AccountSnapshot(0, {"510300.SH": 1000}, {"510300.SH": 500}, now)
    instruments = [Instrument("510300.SH"), Instrument("159915.SZ")]
    orders = plan_rebalance(
        {"159915.SZ": 1},
        account,
        {"510300.SH": 10, "159915.SZ": 10},
        instruments,
        now=now,
        quote_time=now,
    )
    assert orders == [OrderIntent("510300.SH", "sell", 500, 10)]
    with pytest.raises(ValueError, match="stale"):
        plan_rebalance(
            {},
            account,
            {"510300.SH": 10},
            instruments,
            now=now + dt.timedelta(minutes=2),
            quote_time=now,
        )
