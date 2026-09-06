"""emtl — 东方财富证券网页交易接口的现代 Python 客户端。

协议逆向自 MIT 开源项目 riiy/emtl（主机 jywg.18.cn）。
本包保持协议细节不变，仅重写为：类型化、无全局可变状态、可注入、异常分层、可选验证码求解器。
"""

from ._client import EMTClient
from ._models import Direction, TradeMarket
from .errors import (
    AuthFailedError,
    EMTError,
    LoginFailedError,
    LoginRequiredError,
    ProtocolError,
)

__version__ = "0.3.0"

__all__ = [
    "EMTClient",
    "Direction",
    "TradeMarket",
    "EMTError",
    "LoginFailedError",
    "LoginRequiredError",
    "AuthFailedError",
    "ProtocolError",
    "__version__",
]
