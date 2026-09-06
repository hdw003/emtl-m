"""RSA 登录加密：只验输出形状与随机性，不解密（无私钥，也不应接触真实密码）。"""

from __future__ import annotations

from emtl._crypto import encrypt_password

# 1024-bit RSA + PKCS1v15 → 128 字节密文 → Base64 定长 172 字符
_B64_CIPHERTEXT_LEN = 172


def test_encrypt_len_shape() -> None:
    out = encrypt_password("secret123")
    assert len(out) == _B64_CIPHERTEXT_LEN
    assert out.isascii()
    # base64 字符集
    assert all(c.isalnum() or c in "+/=" for c in out)


def test_encrypt_is_randomized() -> None:
    # PKCS#1 v1.5 的 PS 填充字节是随机非零的，故同一明文两次加密密文应不同。
    a = encrypt_password("secret123")
    b = encrypt_password("secret123")
    assert a != b
    assert len(a) == len(b) == _B64_CIPHERTEXT_LEN


def test_encrypt_differs_for_different_input() -> None:
    assert encrypt_password("passwordA") != encrypt_password("passwordB")
