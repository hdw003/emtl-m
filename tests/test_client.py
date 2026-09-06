"""EMTClient 登录/请求构建的离线测试：用假传输替换真实网络。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from emtl._client import EMTClient
from emtl._models import Direction, QuoteMarket, TradeMarket
from emtl.errors import BusinessError, LoginFailedError

HTML_WITH_KEY = (
    '<input id="em_validatekey" type="hidden" value="b91d8012-b70b-4265-bb5d-f79442531017" />'
)


def fake_response(*, text: str = "", content: bytes = b"", json_data: Any = None) -> SimpleNamespace:
    def _json() -> Any:
        if json_data is None:
            raise ValueError(text)
        return json_data

    return SimpleNamespace(status_code=200, text=text, content=content, json=_json)


class FakeClient(EMTClient):
    """抓取所有请求的假客户端。"""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _get(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        if "Trade/Buy" in url:
            return fake_response(text=HTML_WITH_KEY)
        return fake_response(content=b"\x89PNG-fake-captcha")

    def _post(self, url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
        self.posts.append((url, dict(data)))
        if "Authentication" in url:
            return fake_response(json_data={"Status": 0, "Data": "logged-in"})
        return fake_response(json_data={"Status": 0, "Data": []})


def _client() -> FakeClient:
    return FakeClient(username="demo_user", password="demo_pass", solver=None)


class FlakyCaptcha:
    def solve(self, image: bytes) -> str:
        return "1234"


def test_login_sets_validate_key() -> None:
    c = _client()
    vk = c.login(manual_code="9999")
    assert vk == "b91d8012-b70b-4265-bb5d-f79442531017"
    assert c.validate_key == vk
    url, data = c.posts[0]
    assert "/Login/Authentication?validatekey=" in url
    assert data["userId"] == "demo_user"
    assert data["identifyCode"] == "9999"
    assert data["type"] == "Z"
    assert data["password"]  # 已 RSA 加密
    assert data["password"] != "demo_pass"


def test_login_requires_credentials() -> None:
    with pytest.raises(LoginFailedError):
        EMTClient(username="", password="").login(manual_code="1")


def test_login_raises_on_nonzero_status() -> None:
    class Bad(FakeClient):
        def _post(self, url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
            if "Authentication" in url:
                return fake_response(json_data={"Status": -1, "Data": "bad password"})
            return super()._post(url, data=data, headers=headers)

    with pytest.raises(LoginFailedError):
        Bad(username="u", password="p").login(manual_code="1")


def test_login_retries_when_solver_captcha_rejected() -> None:
    class Flaky(FakeClient):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            self.n = 0

        def _post(self, url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
            if "Authentication" in url:
                self.n += 1
                if self.n < 3:
                    return fake_response(json_data={"Status": -1, "Data": "验证码错误"})
                return fake_response(json_data={"Status": 0, "Data": "ok"})
            return super()._post(url, data=data, headers=headers)

    c = Flaky(username="u", password="p", solver=FlakyCaptcha())
    vk = c.login(max_attempts=3)
    assert vk == "b91d8012-b70b-4265-bb5d-f79442531017"
    assert c.n == 3  # 失败 2 次 + 成功 1 次


def test_query_wraps_and_appends_key() -> None:
    c = _client()
    c.login(manual_code="1")
    resp = c.query_orders()
    assert resp.ok
    url, _ = c.posts[-1]
    assert "?validatekey=b91d8012-b70b-4265-bb5d-f79442531017" in url


def test_create_order_form_maps_direction_and_market() -> None:
    c = _client()
    c.login(manual_code="1")
    c.create_order("600000", Direction.BUY, TradeMarket.SH, 10.21, 100)
    url, data = c.posts[-1]
    assert "SubmitTradeV2" in url
    assert data["stockCode"] == "600000"
    assert data["tradeType"] == "B"
    assert data["market"] == "HA"
    assert data["price"] == 10.21
    assert data["amount"] == 100


def _rejecting_client(message: str = "非交易时间") -> FakeClient:
    class Reject(FakeClient):
        def _post(self, url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
            if "SubmitTradeV2" in url:
                return fake_response(json_data={"Status": -1, "Message": message})
            return super()._post(url, data=data, headers=headers)

    c: FakeClient = Reject(username="u", password="p")
    c.login(manual_code="1")
    return c


def test_create_order_strict_raises_business_error() -> None:
    c = _rejecting_client()
    with pytest.raises(BusinessError) as exc_info:
        c.create_order("600000", Direction.BUY, TradeMarket.SH, 10.0, 100)
    assert exc_info.value.status == -1
    assert "非交易时间" in str(exc_info.value)


def test_create_order_strict_false_returns_raw() -> None:
    c = _rejecting_client()
    resp = c.create_order("600000", Direction.BUY, TradeMarket.SH, 10.0, 100, strict=False)
    assert not resp.ok
    assert resp.message == "非交易时间"


def test_export_chain_builds_url() -> None:
    chain_calls: list[tuple[str, dict[str, Any]]] = []
    original_post = FakeClient._post

    class ExportClient(FakeClient):
        def _post(self, url: str, *, data: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
            if "GetEmTradeFileUrl" in url:
                chain_calls.append(("url", dict(data)))
                return fake_response(json_data={"Status": 0, "Data": "https://dl.example/export.zip"})
            if "queryDataExport" in url:
                chain_calls.append(("query", dict(data)))
                return fake_response(json_data={"Status": 0, "Data": [{"fileUrl": "inner_20260907.dat"}]})
            if "applyDataExport" in url:
                chain_calls.append(("apply", dict(data)))
                return fake_response(json_data={"Status": 0, "Data": [{"applyCode": "APC001"}]})
            return original_post(self, url, data=data, headers=headers)

    c = ExportClient(username="u", password="p")
    c.login(manual_code="1")
    url = c.export_history_file("1")
    assert url == "https://dl.example/export.zip"
    assert [k for k, _ in chain_calls] == ["apply", "query", "url"]


def test_credit_stock_info_maps_endpoint() -> None:
    c = _client()
    c.login(manual_code="1")
    c.credit_stock_info("600519", QuoteMarket.SH.value)
    url, data = c.posts[-1]
    assert "MarginSearch/queryCreditStkInfoWEB" in url
    assert data["zqdm"] == "600519"
    assert data["market"] == "SH"
