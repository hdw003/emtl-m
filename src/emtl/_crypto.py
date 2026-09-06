"""登录密码的 RSA 加密。

公钥与填充方式来自协议逆向（同 riiy/emtl）：PKCS#1 v1.5，Base64 输出。
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

# 东财网页登录固定使用的 RSA 公钥（逆向得到，勿改）。
_TRADE_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDHdsyxT66pDG4p73yope7jxA92
c0AT4qIJ/xtbBcHkFPK77upnsfDTJiVEuQDH+MiMeb+XhCLNKZGp0yaUU6GlxZdp
+nLW8b7Kmijr3iepaDhcbVTsYBWchaWUXauj9Lrhz58/6AE/NF0aMolxIGpsi+ST
2hSHPu3GSXMdhPCkWQIDAQAB
-----END PUBLIC KEY-----
"""


def encrypt_password(plaintext: str, *, public_key_pem: str = _TRADE_PUBLIC_KEY_PEM) -> str:
    """把明文密码加密为登录表单所需的 Base64 字符串。"""
    public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    ciphertext = public_key.encrypt(plaintext.encode("utf-8"), padding.PKCS1v15())  # type: ignore[attr-defined]
    return base64.b64encode(ciphertext).decode("ascii")
