"""值对象/模型与日期窗口的单元测试。"""

from __future__ import annotations

import datetime as dt

import pytest

from emtl._models import (
    ApiResponse,
    Direction,
    QuoteMarket,
    TradeMarket,
    date_range,
    guess_market,
    guess_quote_market,
)


def test_direction_enum_values() -> None:
    assert Direction.BUY.value == "B"
    assert Direction.SELL.value == "S"
    assert Direction.BUY == "B"  # StrEnum 可直接与字面量比较


def test_market_enum_values() -> None:
    assert TradeMarket.SH.value == "HA"
    assert TradeMarket.SZ.value == "SA"


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("510300", "SH"),
        ("518880", "SH"),
        ("600000", "SH"),
        ("900901", "SH"),
        ("159915", "SZ"),
        ("000001", "SZ"),
    ],
)
def test_stock_and_etf_market_inference(code: str, market: str) -> None:
    assert guess_market(code) == TradeMarket[market]
    assert guess_quote_market(code) == QuoteMarket[market]


def test_api_response_ok() -> None:
    assert ApiResponse({"Status": 0}).ok
    assert not ApiResponse({"Status": -1}).ok
    assert not ApiResponse({"Status": None}).ok
    assert not ApiResponse([1, 2]).ok
    assert ApiResponse({"Status": 0, "Data": {"x": 1}}).data == {"x": 1}


def test_date_range_window() -> None:
    st, et = date_range(30, end=dt.date(2026, 9, 7))
    assert et == "2026-09-07"
    assert st == "2026-08-08"  # 30 天窗口
