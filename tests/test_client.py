"""EMTClient 登录/请求构建的离线测试：用假传输替换真实网络。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from emtl._client import EMTClient
from emtl._models import Direction, TradeMarket
from emtl.errors import LoginFailedError

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
