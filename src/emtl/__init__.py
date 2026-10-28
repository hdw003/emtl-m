"""emtl — 东方财富证券网页交易接口的现代 Python 客户端。

协议逆向自 MIT 开源项目 riiy/emtl（主机 jywg.18.cn）。
本包保持协议细节不变，仅重写为：类型化、无全局可变状态、可注入、异常分层、可选验证码求解器。
"""

from ._client import EMTClient
from ._models import Direction, QuoteMarket, TradeMarket
from .errors import (
    AuthFailedError,
    BusinessError,
    EMTError,
    LoginFailedError,
    LoginRequiredError,
    ProtocolError,
)

__version__ = "0.5.0"

__all__ = [
    "AuthFailedError",
    "BusinessError",
    "Direction",
    "EMTClient",
    "EMTError",
    "LoginFailedError",
    "LoginRequiredError",
    "ProtocolError",
    "QuoteMarket",
    "TradeMarket",
    "__version__",
]
