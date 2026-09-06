"""验证码识别：可注入的求解器 + 默认 ddddocr 实现（延迟导入）。

若不想引入 ddddocr 这一较重依赖，可在构造 EMTClient 时传入自定义 solver，
或在 login() 时用 `manual_code` 直接提供人工识别的验证码。
"""

from __future__ import annotations

from typing import Protocol


class CaptchaSolver(Protocol):
    """把验证码图片字节解析成文本。"""

    def solve(self, image: bytes) -> str: ...


class DdddOcrSolver:
    """基于 ddddocr 的离线验证码识别。首次使用时才导入（ddddocr 依赖较重）。"""

    def __init__(self) -> None:
        try:
            from ddddocr import DdddOcr  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于安装环境
            raise ImportError(
                "ddddocr 未安装：pip install 'emtl-m[captcha]'，或传入自定义 captcha_solver"
            ) from exc
        self._ocr = DdddOcr(show_ad=False)

    def solve(self, image: bytes) -> str:
        result = self._ocr.classification(image)
        return result or ""
