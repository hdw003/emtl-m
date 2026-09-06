"""类型与值对象：请求方向、市场、统一响应包裹。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal


class Direction(StrEnum):
    """交易方向。值即后端 `tradeType` 字段取值。"""

    BUY = "B"
    SELL = "S"


class TradeMarket(StrEnum):
    """市场。值即后端 `market` 字段取值（东财网页交易的既有约定）。"""

    SH = "HA"  # 上海
    SZ = "SA"  # 深圳


class Side(StrEnum):
    """登录用户类型（沿用东财 login 表单的 type 字段）。"""

    STOCK = "Z"


DateRange = tuple[dt.date, dt.date]


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """对返回 JSON 的轻量包裹：`Status==0` 视为业务成功。

    不臆造字段映射——原始 data 结构随后端版本变化，统一从 `.data` / `.json`
    取用；`.ok` 只按最外层 `Status` 判定。
    """

    json: dict[str, Any] | list[Any]

    @property
    def ok(self) -> bool:
        if isinstance(self.json, dict):
            return self.json.get("Status") == 0
        return False

    @property
    def status(self) -> int | None:
        if isinstance(self.json, dict):
            s = self.json.get("Status")
            return int(s) if s is not None else None
        return None

    @property
    def data(self) -> Any:
        if isinstance(self.json, dict):
            return self.json.get("Data")
        return self.json


def date_range(days: int = 30, *, end: dt.date | None = None) -> tuple[str, str]:
    """返回 (起始日期, 结束日期) 的 `%Y-%m-%d` 字符串，供历史查询。"""
    end = end or dt.date.today()
    start = end - dt.timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def guess_market(code: str) -> TradeMarket:
    """纯决策：6 开头按沪市(HA)，否则默认深市(SA)。可被显式 --market 覆盖。"""
    return TradeMarket.SH if code.strip().startswith("6") else TradeMarket.SZ


OrderSide = Literal["B", "S"]
MarketCode = Literal["HA", "SA"]
