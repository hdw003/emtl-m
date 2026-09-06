"""EMTClient：东方财富证券(18.cn)网页交易客户端。

协议逆向自 riiy/emtl（MIT）。本实现把原来的模块级全局状态收敛为实例状态：
每个 EMTClient 拥有独立的 Session / 验证码求解器 / validatekey，可安全地同时
操作多个账号。

网络侧保持协议原样：
- 验证码：GET /Login/YZM?randNum=<随机数> 取图 → OCR
- 登录：POST /Login/Authentication?validatekey= 携带 RSA(Base64) 加密的密码
- 凭证：GET /Trade/Buy 抠隐藏 input#em_validatekey
- 之后所有查询/下单 URL 拼接 validatekey
"""

from __future__ import annotations

import logging
import os
import re
from random import SystemRandom
from typing import Any

import requests

from ._captcha import CaptchaSolver
from ._config import (
    BASE_HEADERS,
    DEFAULT_QUERY_BODY,
    LOGIN_FIXED_FORM,
    LOGIN_REFERER,
    LOGIN_PAGE,
    QUOTE_URL,
    URLS,
    Action,
)
from ._crypto import encrypt_password
from ._models import ApiResponse, Direction, TradeMarket
from .errors import LoginFailedError, LoginRequiredError, ProtocolError

logger = logging.getLogger(__name__)

_VALIDATEKEY_RE = re.compile(r'id="em_validatekey" type="hidden" value="(.*?)"')


def _first_float(data: dict[str, Any], key: str) -> float:
    """从后端字典取值并转 float；缺失/空值 → NaN（沿用原库语义但不抛 KeyError）。"""
    raw = data.get(key)
    if raw is None:
        return float("nan")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    try:
        return float(text) if text else float("nan")
    except ValueError:
        return float("nan")


class EMTClient:
    """一个账号一个实例。用户名/密码缺省时从环境变量 EM_USERNAME / EM_PASSWORD 读取。"""

    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        duration: int = 30,
        solver: CaptchaSolver | None = None,
        timeout: float = 20.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.username = (username or os.getenv("EM_USERNAME") or "").strip()
        self._password = password if password is not None else (os.getenv("EM_PASSWORD") or "")
        self.duration = duration
        self.timeout = timeout
        self._solver = solver
        self._rng = SystemRandom()

        self.validate_key: str | None = None
        self._session = requests.Session()
        headers = dict(BASE_HEADERS)
        headers.update(extra_headers or {})
        self._session.headers.update(headers)

    # ------------------------------------------------------------------ 传输
    def _get(self, url: str, *, headers: dict[str, str] | None = None) -> requests.Response:
        resp = self._session.get(url, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise ProtocolError(
                f"GET {url} 失败: HTTP {resp.status_code}", status_code=resp.status_code, body=resp.text[:500]
            )
        return resp

    def _post(
        self, url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None
    ) -> requests.Response:
        resp = self._session.post(url, data=data, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise ProtocolError(
                f"POST {url} 失败: HTTP {resp.status_code}", status_code=resp.status_code, body=resp.text[:500]
            )
        return resp

    # ------------------------------------------------------------------ 登录
    def _captcha_image(self, random_num: float) -> bytes:
        url = URLS[Action.YZM] + str(random_num)
        return self._get(url, headers=_ajax_headers()).content

    def _fetch_validate_key(self) -> str | None:
        resp = self._get(LOGIN_PAGE)
        match = _VALIDATEKEY_RE.search(resp.text)
        return match.group(1).strip() if match else None

    def login(self, *, manual_code: str | None = None) -> str:
        """完成登录并缓存 validatekey，返回之。

        :param manual_code: 验证码图片中的人工识别结果；缺省用 captcha_solver。
        """
        if not self.username or not self._password:
            raise LoginFailedError("缺少用户名/密码：传入参数或设置 EM_USERNAME / EM_PASSWORD")

        random_num = self._rng.random()
        image = self._captcha_image(random_num)
        if manual_code is not None:
            code = manual_code
        else:
            if self._solver is None:
                from ._captcha import DdddOcrSolver  # noqa: PLC0415

                self._solver = DdddOcrSolver()
            code = self._solver.solve(image).strip()

        resp = self._post(
            URLS[Action.LOGIN],
            data=self._build_login_form(random_num, code),
            headers=_ajax_headers(referer=LOGIN_REFERER),
        )

        status = _login_status(resp)
        if status is not None and status != 0:
            raise LoginFailedError("登录失败：后端返回非零状态", status=status, payload=_safe_json(resp))

        validate_key = self._fetch_validate_key()
        if not validate_key:
            raise LoginFailedError("登录后未能在交易页找到 em_validatekey", payload=_safe_json(resp))
        self.validate_key = validate_key
        logger.info("登录成功，账号=%s", self.username)
        return validate_key

    def _build_login_form(self, random_num: float, code: str) -> dict[str, str]:
        form = dict(LOGIN_FIXED_FORM)
        form.update(
            {
                "userId": self.username,
                "password": encrypt_password(self._password),
                "randNumber": str(random_num),
                "identifyCode": code,
                "duration": str(self.duration),
            }
        )
        return form

    def _require_validate_key(self) -> None:
        if not self.validate_key:
            self.validate_key = self.login()
        if not self.validate_key:
            raise LoginRequiredError("尚未登录：先调用 login()")

    # ------------------------------------------------------------------ 业务
    def last_price(self, symbol_code: str, market: str) -> float:
        """实时行情快照：返回最新价，缺失为 NaN。"""
        r = self._session.get(
            QUOTE_URL,
            params={"id": symbol_code.strip(), "market": market},
            headers=BASE_HEADERS,
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise ProtocolError(f"行情请求失败: HTTP {r.status_code}", status_code=r.status_code)
        payload = r.json()
        if not isinstance(payload, dict) or payload.get("status") != 0:
            return float("nan")
        quote = payload.get("realtimequote")
        if not isinstance(quote, dict):
            return float("nan")
        return _first_float(quote, "currentPrice")

    def query_asset_and_position(self) -> ApiResponse:
        return self._post_and_wrap(Action.QUERY_ASSET_AND_POS)

    def query_orders(self) -> ApiResponse:
        return self._post_and_wrap(Action.QUERY_ORDERS)

    def query_trades(self) -> ApiResponse:
        return self._post_and_wrap(Action.QUERY_TRADES)

    def query_history_orders(self, size: int, start: str, end: str) -> ApiResponse:
        return self._post_and_wrap(Action.QUERY_HIS_ORDERS, _history_body(size, start, end))

    def query_history_trades(self, size: int, start: str, end: str) -> ApiResponse:
        return self._post_and_wrap(Action.QUERY_HIS_TRADES, _history_body(size, start, end))

    def query_funds_flow(self, size: int, start: str, end: str) -> ApiResponse:
        return self._post_and_wrap(Action.QUERY_FUNDS_FLOW, _history_body(size, start, end))

    def create_order(
        self,
        symbol_code: str,
        direction: Direction | str,
        market: TradeMarket | str,
        price: float,
        amount: int,
    ) -> ApiResponse:
        body: dict[str, Any] = {
            "stockCode": symbol_code,
            "tradeType": Direction(direction).value if isinstance(direction, str) else direction.value,
            "zqmc": "",
            "market": TradeMarket(market).value if isinstance(market, str) else market.value,
            "price": price,
            "amount": amount,
        }
        return self._post_and_wrap(Action.CREATE_ORDER, body)

    def cancel_order(self, order_str: str) -> str:
        """撤单。order_str 由成交日期+成交编号组成，如 `20240520_130662`。返回后端文本。"""
        self._require_validate_key()
        resp = self._post(
            URLS[Action.CANCEL_ORDER] + self.validate_key,
            data={"revokes": order_str.strip()},
            headers=_ajax_headers(),
        )
        return resp.text.strip()

    # ------------------------------------------------------------------ 内部
    def _post_and_wrap(
        self, action: Action, body: dict[str, Any] | None = None, *, default_body: bool = True
    ) -> ApiResponse:
        self._require_validate_key()
        assert self.validate_key is not None  # _require_validate_key 已保证
        data = body if body is not None else (DEFAULT_QUERY_BODY if default_body else {})
        resp = self._post(URLS[action] + self.validate_key, data=data, headers=_ajax_headers())
        payload = _safe_json(resp)
        return ApiResponse(payload)


def _ajax_headers(*, referer: str | None = None) -> dict[str, str]:
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if referer:
        headers["Referer"] = referer
    return headers


def _history_body(size: int, start: str, end: str) -> dict[str, Any]:
    return {"qqhs": size, "dwc": "", "st": start, "et": end}


def _login_status(resp: requests.Response) -> int | None:
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    status = payload.get("Status")
    return int(status) if status is not None else None


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"_raw": resp.text[:1000]}
