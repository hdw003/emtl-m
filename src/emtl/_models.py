"""类型与值对象：请求方向、市场、统一响应包裹。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from .errors import BusinessError


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


class QuoteMarket(StrEnum):
    """行情接口使用的市场码（与下单的 TradeMarket 是两套编码：此处为 SH/SZ）。"""

    SH = "SH"  # 上海
    SZ = "SZ"  # 深圳


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

    @property
    def message(self) -> str | None:
        """后端业务失败时的 Message 字段（若存在）。"""
        if isinstance(self.json, dict):
            msg = self.json.get("Message")
            if isinstance(msg, str) and msg:
                return msg
        return None

    def ensure_ok(self) -> ApiResponse:
        """业务被拒时抛 BusinessError（带回 Message/Data 便于定位），否则返回自身。"""
        if not self.ok:
            detail = self.data if isinstance(self.json, dict) else None
            raise BusinessError(
                self.message or f"业务被拒 (Status={self.status})",
                status=self.status,
                detail=detail,
            )
        return self


def date_range(days: int = 30, *, end: dt.date | None = None) -> tuple[str, str]:
    """返回 (起始日期, 结束日期) 的 `%Y-%m-%d` 字符串，供历史查询。"""
    end = end or dt.date.today()
    start = end - dt.timedelta(days=days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def guess_market(code: str) -> TradeMarket:
    """纯决策：6 开头按沪市(HA)，否则默认深市(SA)。可被显式 --market 覆盖。"""
    return TradeMarket.SH if code.strip().startswith("6") else TradeMarket.SZ


def guess_quote_market(code: str) -> QuoteMarket:
    """纯决策：6 开头为沪(SH)，否则为深(SZ)。"""
    return QuoteMarket.SH if code.strip().startswith("6") else QuoteMarket.SZ


OrderSide = Literal["B", "S"]
MarketCode = Literal["HA", "SA"]
