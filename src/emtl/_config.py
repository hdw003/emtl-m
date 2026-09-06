"""协议常量：端点、头、以及按用途分组的端点表。协议细节与 riiy/emtl 一致。"""

from __future__ import annotations

from enum import StrEnum


class Action(StrEnum):
    """可用的后端动作标签。"""

    YZM = "yzm"
    LOGIN = "login"
    QUERY_ASSET_AND_POS = "query_asset_and_pos"
    QUERY_ORDERS = "query_orders"
    QUERY_TRADES = "query_trades"
    QUERY_HIS_ORDERS = "query_his_orders"
    QUERY_HIS_TRADES = "query_his_trades"
    QUERY_FUNDS_FLOW = "query_funds_flow"
    CREATE_ORDER = "create_order"
    CANCEL_ORDER = "cancel_order"


# 每个 URL 都以查询串开头（?validatekey= / ?randNum=），调用方负责拼接剩余部分。
URLS: dict[Action, str] = {
    Action.YZM: "https://jywg.18.cn/Login/YZM?randNum=",
    Action.LOGIN: "https://jywg.18.cn/Login/Authentication?validatekey=",
    Action.QUERY_ASSET_AND_POS: "https://jywg.18.cn/Com/queryAssetAndPositionV1?validatekey=",
    Action.QUERY_ORDERS: "https://jywg.18.cn/Search/GetOrdersData?validatekey=",
    Action.QUERY_TRADES: "https://jywg.18.cn/Search/GetDealData?validatekey=",
    Action.QUERY_HIS_ORDERS: "https://jywg.18.cn/Search/GetHisOrdersData?validatekey=",
    Action.QUERY_HIS_TRADES: "https://jywg.18.cn/Search/GetHisDealData?validatekey=",
    Action.QUERY_FUNDS_FLOW: "https://jywg.18.cn/Search/GetFundsFlow?validatekey=",
    Action.CREATE_ORDER: "https://jywg.18.cn/Trade/SubmitTradeV2?validatekey=",
    Action.CANCEL_ORDER: "https://jywg.18.cn/Trade/RevokeOrders?validatekey=",
}

# 实时行情快照（独立于交易站）。
QUOTE_URL = "https://emhsmarketwg.eastmoneysec.com/api/SHSZQuoteSnapshot"

# 登录成功后从此页抠 em_validatekey。
LOGIN_PAGE = "https://jywg.18.cn/Trade/Buy"

BASE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    ),
    "Origin": "https://jywg.18.cn",
    "Host": "jywg.18.cn",
}

# 登录 POST 专属头。
LOGIN_REFERER = "https://jywg.18.cn/Login?el=1&clear=&returl=%2fTrade%2fBuy"

# 登录表单固定字段（type=Z：股票账户）。
LOGIN_FIXED_FORM: dict[str, str] = {
    "authCode": "",
    "type": "Z",
    "secInfo": "",
}

# 通用查询请求的默认体（不带参数的历史查询用）。
DEFAULT_QUERY_BODY: dict[str, object] = {"qqhs": 100, "dwc": ""}
