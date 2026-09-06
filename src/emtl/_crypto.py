"""登录密码的 RSA 加密。

公钥与填充方式来自协议逆向（同 riiy/emtl）：PKCS#1 v1.5，Base64 输出。
用纯 Python 的 `rsa` 实现，避免 cryptography 的本地编译依赖。
"""

from __future__ import annotations

import base64

import rsa

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
    public_key = rsa.PublicKey.load_pkcs1_openssl_pem(public_key_pem.encode("ascii"))
    ciphertext = rsa.encrypt(plaintext.encode("utf-8"), public_key)  # PKCS1v15（默认填充）
    return base64.b64encode(ciphertext).decode("ascii")
