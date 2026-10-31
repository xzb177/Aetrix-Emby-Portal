"""115 app 系接口（proapi）的 RSA 加解密——纯标准库实现。

115 的电脑端/浏览器扩展通道（``POST /app/chrome/downurl``）不接受明文
参数：请求体是 ``{"data": <RSA 加密并 base64 的 JSON>}``，响应里的
``data`` 字段同样是加密串，要用同一套算法解开。这套算法是 115 app
协议的一部分（开源客户端 p115client / p115cipher 有公开实现），本
模块按协议常量独立实现，只依赖 Python 标准库的整数运算：

- 明文先与 4 字节固定 key 异或、逆序，再与由 16 字节随机前缀派生的
  12 字节 key 异或，前置随机前缀后整体做 RSA 公钥运算（PKCS#1 v1.5
  形态的填充，但填充字节是 0x02 重复而非随机数，与协议一致）；
- 解密是逆过程：RSA 公钥运算解出分块，前 16 字节是随机前缀，
  据此派生 key 后逆序异或还原。

随机前缀固定全零与开源实现默认一致（服务端只用它派生 key，
不校验随机性）。
"""
from __future__ import annotations

from base64 import b64decode, b64encode

# 协议常量（115 app 协议公开常量，与 p115cipher 中的 G_kts / 公钥对一致）
_G_KTS = bytes((
    0xf0, 0xe5, 0x69, 0xae, 0xbf, 0xdc, 0xbf, 0x8a, 0x1a, 0x45, 0xe8, 0xbe, 0x7d, 0xa6, 0x73, 0xb8,
    0xde, 0x8f, 0xe7, 0xc4, 0x45, 0xda, 0x86, 0xc4, 0x9b, 0x64, 0x8b, 0x14, 0x6a, 0xb4, 0xf1, 0xaa,
    0x38, 0x01, 0x35, 0x9e, 0x26, 0x69, 0x2c, 0x86, 0x00, 0x6b, 0x4f, 0xa5, 0x36, 0x34, 0x62, 0xa6,
    0x2a, 0x96, 0x68, 0x18, 0xf2, 0x4a, 0xfd, 0xbd, 0x6b, 0x97, 0x8f, 0x4d, 0x8f, 0x89, 0x13, 0xb7,
    0x6c, 0x8e, 0x93, 0xed, 0x0e, 0x0d, 0x48, 0x3e, 0xd7, 0x2f, 0x88, 0xd8, 0xfe, 0xfe, 0x7e, 0x86,
    0x50, 0x95, 0x4f, 0xd1, 0xeb, 0x83, 0x26, 0x34, 0xdb, 0x66, 0x7b, 0x9c, 0x7e, 0x9d, 0x7a, 0x81,
    0x32, 0xea, 0xb6, 0x33, 0xde, 0x3a, 0xa9, 0x59, 0x34, 0x66, 0x3b, 0xaa, 0xba, 0x81, 0x60, 0x48,
    0xb9, 0xd5, 0x81, 0x9c, 0xf8, 0x6c, 0x84, 0x77, 0xff, 0x54, 0x78, 0x26, 0x5f, 0xbe, 0xe8, 0x1e,
    0x36, 0x9f, 0x34, 0x80, 0x5c, 0x45, 0x2c, 0x9b, 0x76, 0xd5, 0x1b, 0x8f, 0xcc, 0xc3, 0xb8, 0xf5,
))
_RSA_N = 0x8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b8602136477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad6338c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f10a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683
_RSA_E = 0x10001
_G_KEY_L = bytes((0x78, 0x06, 0xad, 0x4c, 0x33, 0x86, 0x5d, 0x18, 0x4c, 0x01, 0x3f, 0x46))
_RSA_KEY = bytes((0x8d, 0xa5, 0xa5, 0x8d))
_RAND_PREFIX = b"\x00" * 16
_BLOCK = 128


def _xor(src: bytes, key: bytes) -> bytes:
    """协议异或：先处理 len&3 的头部，再按 key 长度分块逐块从 key 头异或。"""
    out = bytearray()
    head = len(src) & 3
    if head:
        out += bytes(a ^ b for a, b in zip(src[:head], key[:head]))
    pos = head
    while pos < len(src):
        chunk = src[pos:pos + len(key)]
        out += bytes(a ^ b for a, b in zip(chunk, key[:len(chunk)]))
        pos += len(chunk)
    return bytes(out)


def _gen_key(rand: bytes, sk_len: int) -> bytes:
    key = bytearray(sk_len)
    length = sk_len * (sk_len - 1)
    index = 0
    for i in range(sk_len):
        x = (rand[i] + _G_KTS[index]) & 0xFF
        key[i] = _G_KTS[length] ^ x
        length -= sk_len
        index += sk_len
    return bytes(key)


def _pad(msg: bytes, size: int = _BLOCK) -> int:
    data = bytearray(b"\x00")
    data += b"\x02" * (size - 2 - len(msg))
    data += b"\x00"
    data += msg
    return int.from_bytes(data, "big")


def _rsa_public_encrypt(data: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(data):
        chunk = data[pos:pos + _BLOCK - 11]
        out += pow(_pad(chunk), _RSA_E, _RSA_N).to_bytes(_BLOCK, "big")
        pos += len(chunk)
    return bytes(out)


def _rsa_public_decrypt(data: bytes) -> bytes:
    out = bytearray()
    for pos in range(0, len(data), _BLOCK):
        chunk = data[pos:pos + _BLOCK]
        p = pow(int.from_bytes(chunk, "big"), _RSA_E, _RSA_N)
        b = p.to_bytes((p.bit_length() + 7) // 8, "big")
        out += b[b.index(0) + 1:]
    return bytes(out)


def rsa_encrypt(data: bytes) -> str:
    """把明文字节按 115 app 协议加密，返回 base64 字符串（请求用）。"""
    tmp = _xor(data, _RSA_KEY)[::-1]
    xor_data = _RAND_PREFIX + _xor(tmp, _G_KEY_L)
    return b64encode(_rsa_public_encrypt(xor_data)).decode("ascii")


def rsa_decrypt(cipher_b64: str) -> bytes:
    """解开 115 app 协议加密串（响应 data 字段用）。"""
    data = _rsa_public_decrypt(b64decode(cipher_b64))
    rand = data[:16]
    key_l = _gen_key(rand, 12)
    tmp = _xor(data[16:], key_l)[::-1]
    return _xor(tmp, _RSA_KEY)
