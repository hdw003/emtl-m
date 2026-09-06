"""异常层级：所有 emtl 错误都是 EMTError 的子类。"""

from __future__ import annotations

from typing import Any


class EMTError(Exception):
    """本库所有可预期错误的基类。"""


class LoginRequiredError(EMTError):
    """尚未登录/会话过期，但请求需要 validatekey。"""


class LoginFailedError(EMTError):
    """登录失败（含验证码错误、密码错误等，服务端返回的非零状态）。"""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class AuthFailedError(EMTError):
    """已登录但携带的 validatekey 失效（服务端返回未登录/会话过期）。"""


class ProtocolError(EMTError):
    """响应不符合预期：HTTP 非 200、HTML 结构缺失、JSON 无法解析等。"""

    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class BusinessError(EMTError):
    """业务被拒：后端返回非零 Status（如非交易时段、余额不足、风控拦截）。"""

    def __init__(
        self, message: str, *, status: int | None = None, detail: Any | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
