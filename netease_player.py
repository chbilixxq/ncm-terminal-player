#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
netease_player.py —— 网易云音乐 · 终端播放器
=============================================

一个**纯 Python 标准库**实现的网易云音乐命令行播放器：

    python3 netease_player.py

特性
----
* 无 GUI，纯交互式终端（支持宽屏双栏 / 窄屏单栏 / 极小屏降级 / 管道脚本模式）
* 音乐播放（自动选择 mpv / ffplay / mpg123 播放内核，无声环境可用模拟内核）
* 搜索（歌曲 / 歌手 / 专辑 / 歌单 / 歌词）、歌单、排行榜、每日推荐、私人 FM
* 登录（扫码登录 + 账号密码登录），登录后可使用我的歌单 / 每日推荐 / 红心歌曲
* 逐行同步歌词（含翻译、罗马音），进度条、音量、播放模式、队列管理
* 歌曲下载：当前歌曲 / 歌单批量下载，自动解密 NCM 并输出可播放的 mp3/flac
* 单文件启动，无需安装任何第三方依赖

快捷开始
--------
    python3 netease_player.py            # 启动交互界面
    python3 netease_player.py --help     # 查看全部参数
    python3 netease_player.py --plain    # 纯文本模式（管道 / 脚本友好）

进入界面后输入 help 查看全部命令。
"""

from __future__ import annotations

import base64
import binascii
import getpass
import hashlib
import http.cookiejar
import json
import os
import queue
import random
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# 基本常量
# --------------------------------------------------------------------------

APP_NAME = "网易云音乐终端播放器"
APP_VERSION = "1.1.0"

API_HOST = "https://music.163.com"

# 通用请求头：模拟网页端，减少被风控的概率
BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://music.163.com",
    "Connection": "keep-alive",
}

# weapi 加密参数（公开的固定值）
WEAPI_IV = b"0102030405060708"
WEAPI_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
WEAPI_RSA_MODULUS = (
    "e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b"
    "3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda"
    "92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cf"
    "e4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
WEAPI_RSA_EXPONENT = 0x10001

# 可选音质阶梯（从高到低，播放时逐级回退）
QUALITY_LADDER = [
    ("lossless", 999000),  # 无损 FLAC
    ("hires", 320000),
    ("exhigh", 320000),
    ("higher", 192000),
    ("standard", 128000),
    ("lower", 96000),
]

SEARCH_TYPES = {
    "song": 1,
    "artist": 100,
    "album": 10,
    "playlist": 1000,
    "lyric": 1006,
    "user": 1002,
    "mv": 1004,
}

PLAY_MODES = ("list", "one", "seq", "shuffle")
PLAY_MODE_NAMES = {
    "list": "列表循环",
    "one": "单曲循环",
    "seq": "顺序播放",
    "shuffle": "随机播放",
}

ESC = "\x1b["


# --------------------------------------------------------------------------
# 终端宽度 / 文本排版工具（正确处理中日韩全角字符）
# --------------------------------------------------------------------------

def char_width(ch: str) -> int:
    """单个字符的显示宽度（全角=2，零宽=0，其余=1）。

    注意：盒绘符号（│─╭）、进度条（█░）、状态图标（▶）这类
    East_Asian_Width=A 的符号在终端里占 1 列，不能按 2 处理。
    """
    if ch == "\t":
        return 4
    o = ord(ch)
    if o < 32:
        return 0
    # 变体选择符 / 零宽连接符 / 格式字符都是零宽
    if ch in ("\ufe0f", "\u200d", "\u200b", "\u2060", "\ufeff"):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def disp_width(text: str) -> int:
    """字符串的显示宽度。"""
    return sum(char_width(c) for c in text)


def slice_disp(text: str, max_w: int, ellipsis: str = "…") -> str:
    """按显示宽度截断字符串。"""
    if max_w <= 0:
        return ""
    if disp_width(text) <= max_w:
        return text
    elli_w = disp_width(ellipsis)
    if max_w <= elli_w:
        return ellipsis[:max_w]
    out = []
    w = 0
    for ch in text:
        cw = char_width(ch)
        if w + cw > max_w - elli_w:
            break
        out.append(ch)
        w += cw
    return "".join(out) + ellipsis


def pad_disp(text: str, width: int, align: str = "left", fill: str = " ") -> str:
    """按显示宽度补齐 / 截断字符串。"""
    text = slice_disp(text, width) if disp_width(text) > width else text
    gap = width - disp_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return fill * gap + text
    if align == "center":
        left = gap // 2
        return fill * left + text + fill * (gap - left)
    return text + fill * gap


def center_disp(text: str, width: int) -> str:
    return pad_disp(text, width, "center")


def fmt_time(seconds: float) -> str:
    """秒 -> mm:ss（超过 1 小时显示 h:mm:ss）。"""
    if seconds is None or seconds < 0 or seconds != seconds:
        return "--:--"
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def human_size(num: Optional[float]) -> str:
    if not num:
        return "-"
    num = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"


_CONTROL_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ufeff\u200b-\u200f\u2028\u2029]")


def clean_text(text: Any) -> str:
    """去掉零宽 / 控制字符（歌名里偶发的 BOM、软换行等），只保留可读文本。"""
    return _CONTROL_RE.sub("", str(text or "")).replace("\t", " ").strip()


# --------------------------------------------------------------------------
# 主题 / ANSI 颜色
# --------------------------------------------------------------------------

class Theme:
    """终端配色，支持 NO_COLOR 与单色模式。"""

    _CODES = {
        "reset": "0", "bold": "1", "dim": "2", "underline": "4",
        "red": "31", "green": "32", "yellow": "33", "blue": "34",
        "magenta": "35", "cyan": "36", "white": "37",
        "bred": "91", "bgreen": "92", "byellow": "93", "bblue": "94",
        "bmagenta": "95", "bcyan": "96", "bwhite": "97",
        "gray": "90",
    }

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._cache: Dict[str, str] = {}

    def enable(self, value: bool = True) -> None:
        self.enabled = value
        self._cache.clear()

    def __getattr__(self, name: str) -> str:
        if name.startswith("_"):
            raise AttributeError(name)
        if not self.enabled or name not in self._CODES:
            return ""
        if name not in self._cache:
            self._cache[name] = f"{ESC}{self._CODES[name]}m"
        return self._cache[name]

    def paint(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        prefix = "".join(getattr(self, s) for s in styles if s)
        if not prefix:
            return text
        return f"{prefix}{text}{self.reset}"


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ensure_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_json(text: Any, default: Any = None) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


def config_home() -> str:
    """配置目录：优先 $NCM_CONFIG_DIR，其次 ~/.config/ncm-player，不可写时退化。"""
    candidates = []
    custom = os.environ.get("NCM_CONFIG_DIR")
    if custom:
        candidates.append(custom)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "ncm-player"))
    home = os.path.expanduser("~")
    if home and home != "~":
        candidates.append(os.path.join(home, ".config", "ncm-player"))
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, ".ncm-player"))
    candidates.append(os.path.join(os.environ.get("TMPDIR", "/tmp"), "ncm-player"))
    for path in candidates:
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".probe")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
            return path
        except OSError:
            continue
    return candidates[-1]


# --------------------------------------------------------------------------
# 加密：纯标准库实现 AES-128-CBC + weapi 混淆（用于登录等接口）
# --------------------------------------------------------------------------

def _gf_mul(a: int, b: int) -> int:
    """GF(2^8) 乘法（模 0x11b）。"""
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        a = (a << 1) ^ (0x11B if a & 0x80 else 0)
        a &= 0xFF
        b >>= 1
    return res


def _gf_pow(a: int, e: int) -> int:
    res = 1
    while e:
        if e & 1:
            res = _gf_mul(res, a)
        a = _gf_mul(a, a)
        e >>= 1
    return res


def _build_sbox() -> bytes:
    """由有限域逆元 + 仿射变换构造 AES S 盒（避免硬编码 256 字节表）。"""
    sbox = bytearray(256)
    for i in range(256):
        inv = 0 if i == 0 else _gf_pow(i, 254)
        s = inv
        for k in (1, 2, 3, 4):
            s ^= ((inv << k) | (inv >> (8 - k))) & 0xFF
        sbox[i] = s ^ 0x63
    return bytes(sbox)


_AES_SBOX = _build_sbox()
# 逆 S 盒：解密 .ncm 需要 AES-128-ECB 解密
_AES_INV_SBOX = bytes(_AES_SBOX.index(i) for i in range(256))

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C]


def _aes128_expand_key(key: bytes) -> List[List[int]]:
    """AES-128 密钥扩展，返回 11 个轮密钥（每个 4 个 32 位字）。"""
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    words: List[List[int]] = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                      # RotWord
            temp = [_AES_SBOX[b] for b in temp]             # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    round_keys = []
    for r in range(11):
        rk: List[int] = []
        for w in range(4):
            rk.extend(words[r * 4 + w])
        round_keys.append(rk)
    return round_keys


def _aes128_encrypt_block(block: bytes, round_keys: List[List[int]]) -> bytes:
    """加密一个 16 字节分组。"""
    state = list(block)
    rk = round_keys[0]
    for i in range(16):
        state[i] ^= rk[i]

    for rnd in range(1, 10):
        # SubBytes
        for i in range(16):
            state[i] = _AES_SBOX[state[i]]
        # ShiftRows: 第 r 行左移 r 位（列主序布局）
        state = [
            state[0], state[5], state[10], state[15],
            state[4], state[9], state[14], state[3],
            state[8], state[13], state[2], state[7],
            state[12], state[1], state[6], state[11],
        ]
        # MixColumns
        for c in range(4):
            a0, a1, a2, a3 = state[c * 4:c * 4 + 4]
            state[c * 4 + 0] = _gf_mul(a0, 2) ^ _gf_mul(a1, 3) ^ a2 ^ a3
            state[c * 4 + 1] = a0 ^ _gf_mul(a1, 2) ^ _gf_mul(a2, 3) ^ a3
            state[c * 4 + 2] = a0 ^ a1 ^ _gf_mul(a2, 2) ^ _gf_mul(a3, 3)
            state[c * 4 + 3] = _gf_mul(a0, 3) ^ a1 ^ a2 ^ _gf_mul(a3, 2)
        # AddRoundKey
        rk = round_keys[rnd]
        for i in range(16):
            state[i] ^= rk[i]

    # 最后一轮：无 MixColumns
    for i in range(16):
        state[i] = _AES_SBOX[state[i]]
    state = [
        state[0], state[5], state[10], state[15],
        state[4], state[9], state[14], state[3],
        state[8], state[13], state[2], state[7],
        state[12], state[1], state[6], state[11],
    ]
    rk = round_keys[10]
    for i in range(16):
        state[i] ^= rk[i]
    return bytes(state)


def _aes128_decrypt_block(block: bytes, round_keys: List[List[int]]) -> bytes:
    """解密一个 16 字节分组（AES-128 逆变换）。"""
    state = list(block)
    rk = round_keys[10]
    for i in range(16):
        state[i] ^= rk[i]

    for rnd in range(9, 0, -1):
        # InvShiftRows：第 r 行右移 r 位
        state = [
            state[0], state[13], state[10], state[7],
            state[4], state[1], state[14], state[11],
            state[8], state[5], state[2], state[15],
            state[12], state[9], state[6], state[3],
        ]
        # InvSubBytes
        for i in range(16):
            state[i] = _AES_INV_SBOX[state[i]]
        # AddRoundKey
        rk = round_keys[rnd]
        for i in range(16):
            state[i] ^= rk[i]
        # InvMixColumns
        for c in range(4):
            a0, a1, a2, a3 = state[c * 4:c * 4 + 4]
            state[c * 4 + 0] = (_gf_mul(a0, 14) ^ _gf_mul(a1, 11)
                                ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9))
            state[c * 4 + 1] = (_gf_mul(a0, 9) ^ _gf_mul(a1, 14)
                                ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13))
            state[c * 4 + 2] = (_gf_mul(a0, 13) ^ _gf_mul(a1, 9)
                                ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11))
            state[c * 4 + 3] = (_gf_mul(a0, 11) ^ _gf_mul(a1, 13)
                                ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14))

    # 最后一轮：InvShiftRows + InvSubBytes + AddRoundKey
    state = [
        state[0], state[13], state[10], state[7],
        state[4], state[1], state[14], state[11],
        state[8], state[5], state[2], state[15],
        state[12], state[9], state[6], state[3],
    ]
    for i in range(16):
        state[i] = _AES_INV_SBOX[state[i]]
    rk = round_keys[0]
    for i in range(16):
        state[i] ^= rk[i]
    return bytes(state)


def aes_ecb_encrypt(data: bytes, key: bytes, pad: bool = True) -> bytes:
    """AES-128-ECB 加密（默认 PKCS#7 填充，用于自检构造测试样本）。"""
    if pad:
        pad_len = 16 - (len(data) % 16)
        data = data + bytes([pad_len]) * pad_len
    elif len(data) % 16:
        raise ValueError("AES-ECB 数据长度必须是 16 的倍数")
    round_keys = _aes128_expand_key(key)
    return b"".join(
        _aes128_encrypt_block(data[off:off + 16], round_keys)
        for off in range(0, len(data), 16)
    )


def aes_ecb_decrypt(data: bytes, key: bytes, unpad: bool = True) -> bytes:
    """AES-128-ECB 解密（默认去除 PKCS#7 填充）。

    .ncm 的密钥块本身没有填充（长度恰为 16 的倍数），调用时传 unpad=False；
    元数据块通常有 PKCS#7 填充，但解析时用 JSON 截断兜底。
    """
    if len(data) % 16:
        raise ValueError("AES-ECB 数据长度必须是 16 的倍数")
    round_keys = _aes128_expand_key(key)
    out = b"".join(
        _aes128_decrypt_block(data[off:off + 16], round_keys)
        for off in range(0, len(data), 16)
    )
    if unpad and out:
        pad_len = out[-1]
        if 1 <= pad_len <= 16 and out.endswith(bytes([pad_len]) * pad_len):
            out = out[:-pad_len]
    return out


def aes_cbc_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-128-CBC 加密（PKCS#7 填充）。"""
    pad_len = 16 - (len(data) % 16)
    data = data + bytes([pad_len]) * pad_len
    round_keys = _aes128_expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[off:off + 16], prev))
        enc = _aes128_encrypt_block(block, round_keys)
        out.extend(enc)
        prev = enc
    return bytes(out)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _rsa_encrypt(text: str, modulus_hex: str, exponent: int) -> str:
    """网易 weapi 使用的经典 RSA（PKCS#1 v1.5 风格的裸模幂 + 左补齐）。"""
    raw = text[::-1].encode("utf-8")          # 反转后转大整数
    value = int.from_bytes(raw, "big")
    n = int(modulus_hex, 16)
    out = pow(value, exponent, n)
    return format(out, "x").zfill(256)


def weapi_encrypt(payload: Dict[str, Any]) -> Tuple[str, str]:
    """weapi 加密，返回 (params, encSecKey)。"""
    secret = "".join(random.sample("abcdefghijklmnopqrstuvwxyz0123456789", 16))
    first = aes_cbc_encrypt(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        WEAPI_PRESET_KEY,
        WEAPI_IV,
    )
    # 第一层密文先做 base64，再作为第二层明文（服务端约定）
    second = aes_cbc_encrypt(base64.b64encode(first), secret.encode("ascii"), WEAPI_IV)
    return base64.b64encode(second).decode(), _rsa_encrypt(
        secret, WEAPI_RSA_MODULUS, WEAPI_RSA_EXPONENT
    )


def encode_anon_username(device_id: str) -> str:
    """匿名注册接口的 username 字段（cloudmusic.dll 的设备号编码）。"""
    xor_key = "3go8&$8*3*3h0k(2)2"
    xored = "".join(
        chr(ord(c) ^ ord(xor_key[i % len(xor_key)]))
        for i, c in enumerate(device_id)
    )
    digest = hashlib.md5(xored.encode("utf-8")).digest()
    inner = base64.b64encode(digest).decode()
    return base64.b64encode(f"{device_id} {inner}".encode()).decode()


def selftest_crypto() -> bool:
    """内置自检：S 盒 / AES 向量 / weapi 结构。"""
    assert _AES_SBOX[0] == 0x63 and _AES_SBOX[1] == 0x7c and _AES_SBOX[255] == 0x16
    # NIST AES-128 测试向量
    key = bytes(range(16))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    rk = _aes128_expand_key(key)
    cipher = _aes128_encrypt_block(plain, rk).hex()
    assert cipher == "69c4e0d86a7b0430d8cdb78070b4c55a", cipher
    enc = aes_cbc_encrypt(b"netease", key, bytes(16))
    assert len(enc) == 16
    params, sec = weapi_encrypt({"type": 1})
    assert len(params) > 20 and len(sec) == 256
    # 解密向量：与上面的 NIST 加密结果互为逆运算
    dec = _aes128_decrypt_block(bytes.fromhex(cipher), rk)
    assert dec == plain, dec.hex()
    return True


# --------------------------------------------------------------------------
# 下载：NCM 解密 / 音频格式识别 / 文件名与目录处理
# --------------------------------------------------------------------------

AUDIO_EXTENSIONS = ("mp3", "flac", "m4a", "aac", "ogg", "wav")
_AUDIO_FORMAT_ALIASES = {
    "mp3": "mp3", "mpeg": "mp3", "mpeg3": "mp3",
    "flac": "flac",
    "m4a": "m4a", "mp4": "m4a", "alac": "m4a",
    "aac": "aac",
    "ogg": "ogg", "opus": "ogg",
    "wav": "wav", "wave": "wav",
}

# 下载音质偏好直接沿用 QUALITY_LADDER（服务端按账号权限实际下发）
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024       # 单曲上限 300MB（足够无损）
NCM_MAGIC = b"CTENFDAM"
NCM_KEY_PREFIX = b"neteasecloudmusic"
NCM_META_PREFIX = b"163 key(Don't modify):"
NCM_CORE_KEY = bytes.fromhex("687A4852416D736F356B496E62617857")
NCM_META_KEY = bytes.fromhex("2331346C6A6B5F215C5D2630553C2728")


class DownloadError(Exception):
    """下载 / 解密过程中可展示给用户的错误。"""


def default_download_dir() -> str:
    """默认下载目录：程序所在目录下的 download/。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "download")


def configured_download_dir(cfg: Any) -> str:
    """读取配置里的下载目录；空值表示使用默认目录。"""
    raw = str(cfg.get("download_dir", "") or "").strip()
    if not raw:
        return default_download_dir()
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    return path


def ensure_writable_dir(path: str) -> str:
    """创建目录并确认可写；失败时抛 OSError。"""
    os.makedirs(path, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path, prefix=".ncm-write-",
                                     delete=True) as fh:
        fh.write(b"ok")
    return path


def sanitize_filename(name: Any, max_bytes: int = 180) -> str:
    """把歌名/歌手变成安全的文件名（去掉路径分隔符与控制字符）。"""
    text = str(name or "").strip()
    text = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip().strip(" .")
    if not text:
        text = "未命名"
    if len(text.encode("utf-8")) > max_bytes:
        while text and len(text.encode("utf-8")) > max_bytes:
            text = text[:-1]
        text = text.rstrip(" .") or "未命名"
    return text


def normalize_audio_format(value: Any) -> str:
    """把服务端 / NCM 元数据里的格式名规范成扩展名。"""
    token = str(value or "").strip().lower().lstrip(".")
    return _AUDIO_FORMAT_ALIASES.get(token, "")


def audio_format_from_head(head: bytes) -> str:
    """根据文件头判断音频格式（mp3/flac/m4a/aac/ogg/wav）。"""
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    if head.startswith(b"OggS"):
        return "ogg"
    if len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # ADTS AAC 与 MP3 的同步字相同，用 layer 位区分
        return "aac" if (head[1] & 0x06) == 0 else "mp3"
    return ""


def download_format_hint(info: Dict[str, Any]) -> str:
    """从下载接口返回的 type / URL 后缀推断格式。"""
    fmt = normalize_audio_format(info.get("type"))
    if fmt:
        return fmt
    path = urllib.parse.urlparse(str(info.get("url") or "")).path
    return normalize_audio_format(os.path.splitext(path)[1])


def quality_ladder(preferred: str = "exhigh") -> List[Tuple[str, int]]:
    """返回从当前音质偏好开始的回退阶梯（去重码率）。"""
    names = [name for name, _br in QUALITY_LADDER]
    key = str(preferred or "exhigh").lower()
    start = names.index(key) if key in names else names.index("exhigh")
    out: List[Tuple[str, int]] = []
    seen = set()
    for name, br in QUALITY_LADDER[start:]:
        if br in seen:
            continue
        seen.add(br)
        out.append((name, br))
    return out


def _read_exact(fh: Any, size: int, what: str) -> bytes:
    data = fh.read(size)
    if len(data) != size:
        raise DownloadError(f"NCM 文件不完整（{what}）")
    return data


def _read_u32(fh: Any, what: str) -> int:
    raw = _read_exact(fh, 4, what)
    return struct.unpack("<I", raw)[0]


def _ncm_keybox(key_data: bytes) -> bytes:
    """NCM 的 RC4 变体密钥盒。"""
    if not key_data:
        raise DownloadError("NCM 密钥为空")
    box = bytearray(range(256))
    c = 0
    last_byte = 0
    offset = 0
    for i in range(256):
        swap = box[i]
        c = (swap + last_byte + key_data[offset]) & 0xFF
        offset += 1
        if offset >= len(key_data):
            offset = 0
        box[i] = box[c]
        box[c] = swap
        last_byte = c
    return bytes(box)


def _ncm_keystream(keybox: bytes) -> bytes:
    """NCM 的 256 字节密钥流（每 256 字节循环一次）。"""
    out = bytearray(256)
    for n in range(1, 257):
        j = n & 0xFF
        out[n - 1] = keybox[(keybox[j] + keybox[(keybox[j] + j) & 0xFF]) & 0xFF]
    return bytes(out)


def _ncm_xor(data: bytes, keybox: bytes, offset: int = 0,
              stream: Optional[bytes] = None) -> bytes:
    """用 NCM 密钥流异或数据；大块用大整数异或，比逐字节循环快得多。"""
    if not data:
        return b""
    stream = stream or _ncm_keystream(keybox)
    if offset:
        shift = offset % 256
        stream = stream[shift:] + stream[:shift]
    reps = (len(data) + 255) // 256
    mask = (stream * reps)[:len(data)]
    return (int.from_bytes(data, "big")
            ^ int.from_bytes(mask, "big")).to_bytes(len(data), "big")


def ncm_read_header(fh: Any) -> Tuple[Dict[str, Any], bytes]:
    """读取并解密 NCM 头部，返回 (元数据, RC4 密钥)。"""
    magic = fh.read(8)
    if magic != NCM_MAGIC:
        raise DownloadError("不是 NCM 文件")
    _read_exact(fh, 2, "文件头")
    key_len = _read_u32(fh, "密钥长度")
    if not (0 < key_len <= 1 << 20):
        raise DownloadError("NCM 密钥长度异常")
    enc_key = _read_exact(fh, key_len, "密钥数据")
    try:
        key_plain = aes_ecb_decrypt(
            bytes(b ^ 0x64 for b in enc_key), NCM_CORE_KEY, unpad=False)
    except ValueError as exc:
        raise DownloadError(f"NCM 密钥解密失败：{exc}") from exc
    if not key_plain.startswith(NCM_KEY_PREFIX):
        raise DownloadError("NCM 密钥校验失败（文件可能不是 NCM 或已损坏）")
    key_data = key_plain[len(NCM_KEY_PREFIX):]
    if not key_data:
        raise DownloadError("NCM 密钥为空")

    meta_len = _read_u32(fh, "元数据长度")
    meta: Dict[str, Any] = {}
    if meta_len:
        if meta_len > 16 * 1024 * 1024:
            raise DownloadError("NCM 元数据长度异常")
        enc_meta = _read_exact(fh, meta_len, "元数据")
        try:
            raw = bytes(b ^ 0x63 for b in enc_meta)
            if raw.startswith(NCM_META_PREFIX):
                raw = raw[len(NCM_META_PREFIX):]
            plain = aes_ecb_decrypt(base64.b64decode(raw), NCM_META_KEY,
                                    unpad=False)
            if plain.startswith(b"music:"):
                plain = plain[len(b"music:"):]
            text = plain.decode("utf-8", "ignore").rstrip("\x00")
            end = text.rfind("}")
            if end >= 0:
                text = text[:end + 1]
            if text:
                parsed = safe_json(text, {})
                if isinstance(parsed, dict):
                    meta = parsed
        except Exception:            # 元数据损坏不影响音频解密
            meta = {}

    _read_exact(fh, 4, "CRC")
    _read_exact(fh, 5, "间隔区")
    image_len = _read_u32(fh, "封面长度")
    if image_len:
        if image_len > 64 * 1024 * 1024:
            raise DownloadError("NCM 封面数据异常")
        _read_exact(fh, image_len, "封面数据")
    return meta, key_data


def ncm_extract(src_path: str, dst_path: str) -> Dict[str, Any]:
    """把 NCM 文件解密成可直接播放的音频文件，返回其中的元数据。"""
    with open(src_path, "rb") as fin:
        meta, key_data = ncm_read_header(fin)
        keybox = _ncm_keybox(key_data)
        stream = _ncm_keystream(keybox)
        total = 0
        with open(dst_path, "wb") as fout:
            while True:
                chunk = fin.read(0x10000)      # 0x10000 是 256 的倍数
                if not chunk:
                    break
                fout.write(_ncm_xor(chunk, keybox, 0, stream))
                total += len(chunk)
    if total < 1024:
        raise DownloadError("NCM 解密后的音频内容过短")
    return meta


def selftest_ncm() -> bool:
    """离线验证 NCM 解密（构造一个符合格式的加密样本）。"""
    # 1) AES-128 解密向量
    key = bytes(range(16))
    cipher = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert _aes128_decrypt_block(cipher, _aes128_expand_key(key)) == plain

    # 2) 构造 NCM：密钥块 + 元数据块 + 加密音频
    audio = b"fLaC" + bytes((i * 7) & 0xFF for i in range(4096))
    meta = {"format": "flac", "musicName": "测试歌曲", "artist": [["测试歌手", 1]]}
    key_plain = NCM_KEY_PREFIX + bytes((i * 37 + 11) & 0xFF for i in range(111))
    enc_key = bytes(b ^ 0x64 for b in aes_ecb_encrypt(
        key_plain, NCM_CORE_KEY, pad=False))
    meta_plain = b"music:" + json.dumps(meta, ensure_ascii=False).encode("utf-8")
    meta_blob = NCM_META_PREFIX + base64.b64encode(
        aes_ecb_encrypt(meta_plain, NCM_META_KEY, pad=True))
    enc_meta = bytes(b ^ 0x63 for b in meta_blob)
    enc_audio = _ncm_xor(audio, _ncm_keybox(key_plain[len(NCM_KEY_PREFIX):]))
    blob = bytearray()
    blob += NCM_MAGIC + b"\x00\x00"
    blob += struct.pack("<I", len(enc_key)) + enc_key
    blob += struct.pack("<I", len(enc_meta)) + enc_meta
    blob += b"\x00" * 4 + b"\x00" * 5
    blob += struct.pack("<I", 0)
    blob += enc_audio

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "sample.ncm")
        dst = os.path.join(tmp, "sample.flac")
        with open(src, "wb") as fh:
            fh.write(blob)
        got_meta = ncm_extract(src, dst)
        with open(dst, "rb") as fh:
            decrypted = fh.read()
        assert decrypted == audio, "NCM 解密结果与原音频不一致"
        assert got_meta.get("format") == "flac", got_meta
        assert got_meta.get("musicName") == "测试歌曲", got_meta
        assert audio_format_from_head(decrypted[:16]) == "flac"
    assert sanitize_filename('a/b:c*?"<>|') == "a_b_c_"
    assert sanitize_filename("  ") == "未命名"
    return True


# --------------------------------------------------------------------------
# HTTP 客户端：Cookie 管理 + api / weapi 双通道
# --------------------------------------------------------------------------

class ApiError(Exception):
    """接口调用错误，message 面向用户展示。"""

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code


_CODE_HINTS = {
    301: "需要登录后才能使用该功能",
    404: "接口不存在",
    405: "接口禁止访问",
    406: "服务器拒绝了请求",
    408: "请求超时",
    500: "服务器开小差了",
    502: "账号或密码错误",
    802: "登录已确认（等待服务器下发凭证）",
    803: "登录成功",
    -460: "操作过于频繁，被风控拦下了，请稍后再试",
    -462: "触发风控，请稍后再试",
    -100: "需要登录",
    -101: "账号未登录",
    -110: "需要开通 VIP 才能收听",
}


def friendly_code_message(code: Optional[int], raw: str = "") -> str:
    if code in _CODE_HINTS:
        base = _CODE_HINTS[code]
        if raw and raw != base:
            return f"{base}（{raw}）"
        return base
    return raw or f"接口返回异常 code={code}"


class NetEaseClient:
    """网易云音乐接口客户端（单例式，内部维护 Cookie）。"""

    def __init__(self, cfg_dir: str):
        self.cfg_dir = cfg_dir
        self.cookie_path = os.path.join(cfg_dir, "cookies.txt")
        self.jar = http.cookiejar.MozillaCookieJar(self.cookie_path)
        self._load_cookies()
        self.device: Dict[str, str] = self._load_device()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.anon_registered = False
        self._lock = threading.Lock()

    # -- 持久化 --------------------------------------------------------
    def _load_cookies(self) -> None:
        try:
            if os.path.exists(self.cookie_path):
                self.jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            try:
                os.remove(self.cookie_path)
            except OSError:
                pass

    def save_cookies(self) -> None:
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass

    def _load_device(self) -> Dict[str, str]:
        path = os.path.join(self.cfg_dir, "device.json")
        device: Dict[str, str] = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    device = json.load(fh) or {}
            except Exception:
                device = {}
        if not device.get("deviceId"):
            device = {
                "deviceId": binascii.hexlify(os.urandom(26)).decode().upper(),
                "_ntes_nuid": binascii.hexlify(os.urandom(16)).decode(),
                "WNMCID": "%s.%d.01.0" % (
                    "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=6)),
                    now_ms(),
                ),
                "os": "pc",
                "appver": "3.0.18.203152",
                "osver": "Microsoft-Windows-10-build-22631",
                "channel": "netease",
            }
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(device, fh, ensure_ascii=False, indent=2)
            except OSError:
                pass
        return device

    # -- 状态查询 ------------------------------------------------------
    @property
    def cookies_dict(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for c in self.jar:
            out[c.name] = c.value
        return out

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies_dict.get("MUSIC_U"))

    @property
    def csrf(self) -> str:
        return self.cookies_dict.get("__csrf", "")

    def _extra_cookies(self) -> Dict[str, str]:
        """随请求附带的客户端设备信息 Cookie。"""
        base = {
            "__remember_me": "true",
            "_ntes_nuid": self.device.get("_ntes_nuid", ""),
            "WNMCID": self.device.get("WNMCID", ""),
            "WEVNSM": "1.0.0",
            "os": self.device.get("os", "pc"),
            "osver": self.device.get("osver", ""),
            "deviceId": self.device.get("deviceId", ""),
            "channel": self.device.get("channel", "netease"),
            "appver": self.device.get("appver", ""),
        }
        if not self.logged_in:
            base.setdefault("ntes_kaola_ad", "1")
        return base

    def _cookie_header(self, url: str) -> str:
        """把 cookiejar 中与目标相关的 Cookie 与设备 Cookie 合并。"""
        merged: Dict[str, str] = self._extra_cookies()
        host = urllib.parse.urlparse(url).hostname or ""
        for c in self.jar:
            domain = (c.domain or "").lstrip(".")
            if not domain or host == domain or host.endswith("." + domain):
                merged[c.name] = c.value
        return "; ".join(f"{k}={v}" for k, v in merged.items() if v != "")

    # -- 底层请求 ------------------------------------------------------
    def request(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        weapi: bool = False,
        method: Optional[str] = None,
        timeout: float = 15.0,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """POST 一个接口。weapi=True 时走加密通道。"""
        data = dict(data or {})
        if weapi:
            data.setdefault("csrf_token", self.csrf)
            params, enc_sec = weapi_encrypt(data)
            url = f"{API_HOST}/weapi/{path[5:] if path.startswith('/api/') else path.lstrip('/')}"
            body = urllib.parse.urlencode({"params": params, "encSecKey": enc_sec})
        else:
            url = f"{API_HOST}{path}"
            body = urllib.parse.urlencode(data)

        headers = dict(BASE_HEADERS)
        headers["Cookie"] = self._cookie_header(url)
        headers["Content-Length"] = str(len(body.encode()))

        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=body.encode("utf-8"), headers=headers,
                method=method or "POST",
            )
            try:
                with self.opener.open(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                result = safe_json(raw, None)
                if result is None:
                    raise ApiError("服务器返回了无法解析的内容", None)
                return result
            except ApiError as exc:
                raise exc
            except urllib.error.HTTPError as exc:
                last_err = exc
                try:
                    payload = exc.read().decode("utf-8", "replace")
                except Exception:
                    payload = ""
                info = safe_json(payload, {}) or {}
                code = info.get("code")
                if exc.code in (405, 406) or code in (405, 406):
                    raise ApiError(friendly_code_message(code or exc.code), code)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_err = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
        raise ApiError(f"网络请求失败：{last_err}", None)

    def get(self, url: str, timeout: float = 15.0) -> bytes:
        """普通 GET（用于下载音频流等）。"""
        headers = dict(BASE_HEADERS)
        headers.pop("Content-Type", None)
        headers["Cookie"] = self._cookie_header(url)
        req = urllib.request.Request(url, headers=headers, method="GET")
        with self.opener.open(req, timeout=timeout) as resp:
            return resp.read()

    def open_url(self, url: str, timeout: float = 15.0):
        headers = dict(BASE_HEADERS)
        headers.pop("Content-Type", None)
        headers["Cookie"] = self._cookie_header(url)
        req = urllib.request.Request(url, headers=headers, method="GET")
        return self.opener.open(req, timeout=timeout)

    # -- 抽象检查 ------------------------------------------------------
    @staticmethod
    def check(result: Dict[str, Any], tip: str = "请求失败") -> Dict[str, Any]:
        code = result.get("code")
        if code is not None and code != 200:
            raise ApiError(friendly_code_message(code, str(result.get("message") or result.get("msg") or "")) or tip, code)
        return result

    # -- 匿名登录 ------------------------------------------------------
    def anonymous_register(self) -> bool:
        """匿名注册，获取游客凭证（失败不影响主流程）。"""
        if self.logged_in or self.anon_registered:
            return False
        try:
            device_id = self.device.get("deviceId", "")
            res = self.request(
                "/api/register/anonimous",
                {"username": encode_anon_username(device_id)},
                weapi=True,
                timeout=8,
                retries=0,
            )
            if res.get("code") == 200:
                self.anon_registered = True
                self.save_cookies()
                return True
        except Exception:
            pass
        return False


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class Song:
    """一首歌的统一表示（兼容新旧两套字段命名）。"""
    id: int
    name: str
    artists: List[str] = field(default_factory=list)
    artist_ids: List[int] = field(default_factory=list)
    album: str = ""
    album_id: int = 0
    duration: int = 0            # 毫秒
    fee: int = 0                 # 0 免费 1 VIP 4 专辑 8 低品免费 付费
    mv_id: int = 0
    alias: List[str] = field(default_factory=list)
    privilege: Optional[int] = None   # 0=可播放
    no: int = 0
    reason: str = ""             # 不可播放原因

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "Song":
        artists_raw = raw.get("artists") or raw.get("ar") or []
        album_raw = raw.get("album") or raw.get("al") or {}
        privilege = raw.get("privilege") or {}
        return cls(
            id=int(raw.get("id") or 0),
            name=clean_text(raw.get("name")),
            artists=[clean_text(a.get("name")) for a in artists_raw],
            artist_ids=[int(a.get("id") or 0) for a in artists_raw],
            album=clean_text(album_raw.get("name")),
            album_id=int(album_raw.get("id") or 0),
            duration=int(raw.get("duration") or raw.get("dt") or 0),
            fee=int(raw.get("fee") or 0),
            mv_id=int(raw.get("mvid") or raw.get("mv") or 0),
            alias=[clean_text(a) for a in (raw.get("alias") or raw.get("alia") or [])],
            privilege=privilege.get("code") if privilege else None,
            no=int(raw.get("no") or 0),
            reason=str(raw.get("reason") or ""),
        )

    @property
    def artist(self) -> str:
        return " / ".join(a for a in self.artists if a) or "未知歌手"

    @property
    def album_name(self) -> str:
        return self.album or "未知专辑"

    @property
    def duration_s(self) -> float:
        return self.duration / 1000.0

    @property
    def playable_flag(self) -> str:
        if self.privilege is not None and self.privilege != 0:
            return "×"
        if self.fee == 1:
            return "VIP"
        if self.fee == 4:
            return "EP"
        if self.fee == 8:
            return "低品"
        return ""

    def display(self) -> str:
        base = f"{self.name} - {self.artist}"
        flag = self.playable_flag
        if flag:
            base += f" [{flag}]"
        return base

    def row(self, index: int, width: int) -> str:
        """列表中的一行：序号 / 歌名 / 时长。"""
        num = f"{index:>3}."
        dur = fmt_time(self.duration_s)
        left = slice_disp(f"{self.name}", max(4, width - 3 - len(dur) - 2 - 8))
        right = f"{self.artist}"
        gap = width - disp_width(num) - disp_width(left) - disp_width(dur) - 4
        line = f"{num} {left}"
        if gap > 1 and right:
            line += " " * (gap - disp_width(right)) + slice_disp(right, max(0, gap - 1))
        else:
            line += " " + slice_disp(right, max(0, gap - 1)) if gap > 1 else ""
        line += f" {dur}"
        return slice_disp(line, width)


@dataclass
class Playlist:
    id: int
    name: str
    creator: str = ""
    track_count: int = 0
    play_count: int = 0
    update_frequency: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    kind: str = "playlist"

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "Playlist":
        creator = raw.get("creator") or {}
        return cls(
            id=int(raw.get("id") or 0),
            name=clean_text(raw.get("name")),
            creator=clean_text(creator.get("nickname")),
            track_count=int(raw.get("trackCount") or raw.get("trackNumberUpdateTime") and 0 or 0)
            or int(raw.get("trackCount") or 0),
            play_count=int(raw.get("playCount") or 0),
            update_frequency=str(raw.get("updateFrequency") or ""),
            description=str(raw.get("description") or ""),
            tags=[str(t) for t in (raw.get("tags") or [])],
        )

    def row(self, index: int, width: int) -> str:
        num = f"{index:>3}."
        meta = self.update_frequency or (
            f"{self.track_count} 首" if self.track_count else ""
        )
        right = meta
        left_max = width - disp_width(num) - disp_width(right) - 3
        left = slice_disp(self.name, max(4, left_max))
        line = f"{num} {left}"
        pad = width - disp_width(line) - disp_width(right)
        if pad > 0:
            line += " " * pad + right
        else:
            line = slice_disp(line + " " + right, width)
        return line


@dataclass
class Artist:
    id: int
    name: str
    music_size: int = 0
    album_size: int = 0
    brief: str = ""
    fans_count: int = 0
    kind: str = "artist"

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "Artist":
        return cls(
            id=int(raw.get("id") or 0),
            name=clean_text(raw.get("name")),
            music_size=int(raw.get("musicSize") or 0),
            album_size=int(raw.get("albumSize") or 0),
            brief=str(raw.get("briefDesc") or raw.get("briefDesc") or ""),
            fans_count=int(raw.get("fansCount") or 0),
        )

    def row(self, index: int, width: int) -> str:
        num = f"{index:>3}."
        right = f"{self.music_size} 首 · {self.album_size} 专"
        left = slice_disp(self.name, max(4, width - disp_width(num) - disp_width(right) - 3))
        line = f"{num} {left}"
        pad = width - disp_width(line) - disp_width(right)
        if pad > 0:
            line += " " * pad + right
        return line


@dataclass
class Album:
    id: int
    name: str
    artist: str = ""
    artist_id: int = 0
    size: int = 0
    publish: str = ""
    kind: str = "album"

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "Album":
        artist = raw.get("artist") or {}
        if isinstance(raw.get("artists"), list) and raw["artists"]:
            artist = raw["artists"][0]
        publish = raw.get("publishTime") or 0
        try:
            publish_s = time.strftime("%Y-%m-%d", time.localtime(publish / 1000)) if publish else ""
        except Exception:
            publish_s = ""
        return cls(
            id=int(raw.get("id") or 0),
            name=clean_text(raw.get("name")),
            artist=clean_text(artist.get("name")),
            artist_id=int(artist.get("id") or 0),
            size=int(raw.get("size") or raw.get("albumSize") or 0),
            publish=publish_s,
        )

    def row(self, index: int, width: int) -> str:
        num = f"{index:>3}."
        right = f"{self.publish or ''} {self.size} 首".strip()
        left = slice_disp(self.name, max(4, width - disp_width(num) - disp_width(right) - 3))
        line = f"{num} {left}"
        pad = width - disp_width(line) - disp_width(right)
        if pad > 0:
            line += " " * pad + right
        return line


@dataclass
class ResultSet:
    """一次查询的结果（支持翻页）。"""
    kind: str                      # song / artist / album / playlist / lyric / tracks
    items: List[Any]
    total: int = 0
    page: int = 1
    page_size: int = 30
    query: str = ""
    offset: int = 0

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    def kind_cn(self) -> str:
        return {
            "song": "单曲", "artist": "歌手", "album": "专辑",
            "playlist": "歌单", "lyric": "歌词", "tracks": "歌单曲目",
        }.get(self.kind, self.kind)


@dataclass
class LyricLine:
    time: float                    # 秒
    main: str = ""
    trans: str = ""
    rom: str = ""


class LyricSheet:
    """LRC 歌词表（含翻译与罗马音合并）。"""

    def __init__(self) -> None:
        self.lines: List[LyricLine] = []
        self.offset: float = 0.0     # 秒
        self.meta: Dict[str, str] = {}
        self.has_translation = False
        self.has_roman = False

    # -- 解析 ----------------------------------------------------------
    _TAG_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
    _TAG_META = re.compile(r"\[([a-zA-Z#]+):([^\]]*)\]")

    @classmethod
    def parse_lrc(cls, text: str) -> Tuple[List[LyricLine], Dict[str, str], float]:
        lines: List[LyricLine] = []
        meta: Dict[str, str] = {}
        offset = 0.0
        if not text:
            return lines, meta, offset
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            m_meta = cls._TAG_META.match(raw)
            times = cls._TAG_TIME.findall(raw)
            if not times:
                if m_meta:
                    key = m_meta.group(1).lower()
                    meta[key] = m_meta.group(2).strip()
                    if key == "offset":
                        try:
                            offset = float(meta[key]) / 1000.0
                        except ValueError:
                            pass
                continue
            content = cls._TAG_TIME.sub("", raw).strip()
            for mm, ss, frac in times:
                secs = int(mm) * 60 + int(ss)
                if frac:
                    secs += float("0." + frac)
                lines.append(LyricLine(time=secs, main=content))
        lines.sort(key=lambda ln: ln.time)
        return lines, meta, offset

    @classmethod
    def build(cls, lrc: str = "", tlyrc: str = "", romalrc: str = "") -> "LyricSheet":
        sheet = cls()
        main, meta, offset = cls.parse_lrc(lrc)
        sheet.lines = main
        sheet.meta = meta
        sheet.offset = offset
        if tlyrc:
            trans, _, _ = cls.parse_lrc(tlyrc)
            index = {round(ln.time, 2): ln for ln in main}
            for ln in trans:
                target = index.get(round(ln.time, 2))
                if target is not None and ln.main:
                    target.trans = ln.main
                    sheet.has_translation = True
            if not sheet.has_translation and trans:
                # 时间戳不完全一致时退化为按顺序对齐
                for i, ln in enumerate(trans[:len(main)]):
                    if ln.main and not main[i].trans:
                        main[i].trans = ln.main
                        sheet.has_translation = True
        if romalrc:
            roms, _, _ = cls.parse_lrc(romalrc)
            index = {round(ln.time, 2): ln for ln in main}
            for ln in roms:
                target = index.get(round(ln.time, 2))
                if target is not None and ln.main:
                    target.rom = ln.main
                    sheet.has_roman = True
        return sheet

    # -- 查询 ----------------------------------------------------------
    def empty(self) -> bool:
        return not any(ln.main for ln in self.lines)

    def index_at(self, t: float) -> int:
        """当前时间所处的歌词行下标（-1 表示尚未开始）。"""
        pos = t + self.offset
        idx = -1
        for i, ln in enumerate(self.lines):
            if ln.time <= pos + 0.001:
                idx = i
            else:
                break
        return idx

    def text_at(self, t: float) -> str:
        idx = self.index_at(t)
        if idx < 0:
            return ""
        return self.lines[idx].main


# --------------------------------------------------------------------------
# 接口封装
# --------------------------------------------------------------------------

class NcmApi(NetEaseClient):
    """面向业务的网易云音乐接口集合。"""

    # -- 搜索 ----------------------------------------------------------
    def search(self, keyword: str, kind: str = "song", limit: int = 30,
               offset: int = 0) -> ResultSet:
        stype = SEARCH_TYPES.get(kind, 1)
        if kind == "song":
            res = self.request(
                "/api/cloudsearch/pc",
                {"s": keyword, "type": 1, "limit": limit, "offset": offset, "total": "true"},
            )
        else:
            res = self.request(
                "/api/search/get",
                {"s": keyword, "type": stype, "limit": limit, "offset": offset},
            )
        self.check(res, "搜索失败")
        result = res.get("result") or {}
        items: List[Any] = []
        total = int(result.get("songCount") or result.get("artistCount")
                    or result.get("albumCount") or result.get("playlistCount")
                    or result.get("resultCount") or 0)
        if kind == "song":
            items = [Song.from_api(s) for s in (result.get("songs") or [])]
        elif kind == "artist":
            items = [Artist.from_api(a) for a in (result.get("artists") or [])]
        elif kind == "album":
            items = [Album.from_api(a) for a in (result.get("albums") or [])]
        elif kind == "playlist":
            items = [Playlist.from_api(p) for p in (result.get("playlists") or [])]
        elif kind == "lyric":
            raw_items = (result.get("songs") or [])
            items = [Song.from_api(s) for s in raw_items]
        return ResultSet(kind=kind, items=items, total=total,
                         offset=offset, page=offset // max(1, limit) + 1,
                         page_size=limit, query=keyword)

    def search_suggest(self, keyword: str) -> List[str]:
        try:
            res = self.request("/api/search/suggest/web", {"s": keyword}, weapi=True)
            result = res.get("result") or {}
            out = [str(x.get("keyword") or "") for x in (result.get("allMatch") or [])]
            return [x for x in out if x]
        except Exception:
            return []

    # -- 歌曲 ----------------------------------------------------------
    def song_detail(self, ids: Sequence[int]) -> List[Song]:
        songs: List[Song] = []
        ids = [int(i) for i in ids if i]
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            payload = "[" + ",".join(json.dumps({"id": x}) for x in chunk) + "]"
            res = self.request("/api/v3/song/detail", {"c": payload}, weapi=True)
            self.check(res, "获取歌曲详情失败")
            songs.extend(Song.from_api(s) for s in (res.get("songs") or []))
        order = {sid: i for i, sid in enumerate(ids)}
        songs.sort(key=lambda s: order.get(s.id, 1 << 30))
        return songs

    def song_urls(self, ids: Sequence[int], br: int = 320000) -> Dict[int, Dict[str, Any]]:
        """批量获取播放地址，返回 {id: info}。"""
        if not ids:
            return {}
        payload = json.dumps([str(i) for i in ids])
        res = self.request(
            "/api/song/enhance/player/url",
            {"ids": payload, "br": int(br)},
        )
        self.check(res, "获取播放链接失败")
        out: Dict[int, Dict[str, Any]] = {}
        for item in (res.get("data") or []):
            if isinstance(item, dict) and item.get("id"):
                out[int(item["id"])] = item
        return out

    def resolve_song_url(self, song: Song) -> Dict[str, Any]:
        """带音质回退的播放地址解析（服务端按账号权限下发实际可用音质）。"""
        for _name, br in QUALITY_LADDER:
            info = self.song_urls([song.id], br=br).get(song.id)
            if info and info.get("url"):
                return info
        return {}

    def song_download_url(self, song_id: int, br: int = 320000) -> Dict[str, Any]:
        """官方下载接口；部分歌曲/账号不可用，失败返回空字典。"""
        try:
            res = self.request(
                "/api/song/enhance/download/url",
                {"id": int(song_id), "br": int(br)},
            )
            if res.get("code") not in (200, None):
                return {}
            data = res.get("data")
            return data if isinstance(data, dict) else {}
        except ApiError:
            return {}

    def resolve_download_info(self, song: Song, quality: str = "exhigh") -> Dict[str, Any]:
        """解析下载地址：优先播放地址（稳定），再试官方下载接口，按音质回退。"""
        fallback: Dict[str, Any] = {}
        for _name, br in quality_ladder(quality):
            try:
                info = self.song_urls([song.id], br=br).get(song.id) or {}
            except ApiError:
                info = {}
            if info.get("url"):
                return info
            if info and not fallback:
                fallback = dict(info)
        # 某些账号下播放接口不给地址，但官方下载接口可用
        for _name, br in quality_ladder(quality):
            info = self.song_download_url(song.id, br)
            if info.get("url"):
                return info
            if info and not fallback:
                fallback = dict(info)
        return fallback

    def check_music(self, song_id: int, br: int = 999000) -> bool:
        info = self.song_urls([song_id], br=br).get(song_id)
        return bool(info and info.get("url"))

    # -- 歌词 ----------------------------------------------------------
    def lyric(self, song_id: int) -> LyricSheet:
        res = self.request(
            "/api/song/lyric",
            {"id": song_id, "tv": -1, "lv": -1, "rv": -1, "kv": -1, "_nmclfl": 1},
        )
        if res.get("code") not in (200, None):
            return LyricSheet()
        lrc = ((res.get("lrc") or {}).get("lyric")) or ""
        tlyrc = ((res.get("tlyric") or {}).get("lyric")) or ""
        romalrc = ((res.get("romalrc") or {}).get("lyric")) or ""
        return LyricSheet.build(lrc, tlyrc, romalrc)

    # -- 歌单 ----------------------------------------------------------
    def playlist_meta(self, playlist_id: int) -> Dict[str, Any]:
        res = self.request("/api/v6/playlist/detail",
                           {"id": playlist_id, "n": 100000, "s": 8})
        self.check(res, "获取歌单详情失败")
        pl = res.get("playlist") or {}
        return pl

    def playlist_tracks(self, playlist_id: int, limit: int = 0,
                        offset: int = 0) -> Tuple[Dict[str, Any], List[Song]]:
        pl = self.playlist_meta(playlist_id)
        track_ids = [int(t.get("id") or 0) for t in (pl.get("trackIds") or [])]
        track_ids = [x for x in track_ids if x]
        if limit:
            track_ids = track_ids[offset:offset + limit]
        else:
            track_ids = track_ids[offset:]
        songs = self.song_detail(track_ids) if track_ids else []
        return pl, songs

    def toplists(self) -> List[Playlist]:
        res = self.request("/api/toplist", {}, weapi=True)
        self.check(res, "获取排行榜失败")
        out = []
        for raw in (res.get("list") or []):
            pl = Playlist.from_api(raw)
            pl.update_frequency = str(raw.get("updateFrequency") or "")
            pl.track_count = int(raw.get("trackCount") or 0)
            out.append(pl)
        return out

    def hot_categories(self) -> List[str]:
        res = self.request("/api/playlist/hot", {}, method="GET")
        hots = ((res.get("result") or {}).get("hots")) or []
        return [str(h.get("name") or "") for h in hots if h.get("name")]

    def playlists_by_cat(self, cat: str = "全部", limit: int = 30,
                         offset: int = 0) -> ResultSet:
        res = self.request("/api/playlist/list", {
            "cat": cat, "limit": limit, "offset": offset, "total": "true",
        }, method="GET")
        self.check(res, "获取歌单失败")
        items = [Playlist.from_api(p) for p in (res.get("playlists") or [])]
        return ResultSet(kind="playlist", items=items,
                         total=int(res.get("total") or 0), offset=offset,
                         page=offset // max(1, limit) + 1, page_size=limit,
                         query=cat)

    def personalized_playlists(self, limit: int = 12) -> List[Playlist]:
        res = self.request("/api/personalized/playlist",
                           {"limit": limit, "total": "true", "n": limit}, weapi=True)
        self.check(res, "获取推荐歌单失败")
        return [Playlist.from_api(p) for p in (res.get("result") or [])]

    # -- 歌手 / 专辑 ---------------------------------------------------
    def artist(self, artist_id: int) -> Tuple[Dict[str, Any], List[Song]]:
        res = self.request(f"/api/artist/{artist_id}", {}, method="GET")
        self.check(res, "获取歌手信息失败")
        info = res.get("artist") or {}
        songs = [Song.from_api(s) for s in (res.get("hotSongs") or [])]
        return info, songs

    def artist_top_songs(self, artist_id: int, limit: int = 50) -> List[Song]:
        res = self.request("/api/artist/top/song", {"id": artist_id, "limit": limit},
                           method="GET")
        self.check(res, "获取歌手热门歌曲失败")
        return [Song.from_api(s) for s in (res.get("songs") or [])]

    def album(self, album_id: int) -> Tuple[Dict[str, Any], List[Song]]:
        res = self.request(f"/api/v1/album/{album_id}", {}, weapi=True)
        self.check(res, "获取专辑信息失败")
        info = res.get("album") or {}
        songs = [Song.from_api(s) for s in (res.get("songs") or [])]
        return info, songs

    # -- 账号 ----------------------------------------------------------
    def account(self) -> Dict[str, Any]:
        res = self.request("/api/nuser/account/get", {}, weapi=True)
        self.check(res, "获取账号信息失败")
        return res

    def login_status(self) -> Dict[str, Any]:
        res = self.request("/api/w/nuser/account/get", {}, weapi=True)
        self.check(res, "获取登录状态失败")
        return res.get("data") or res

    def user_playlists(self, uid: int, limit: int = 100,
                       offset: int = 0) -> List[Playlist]:
        res = self.request("/api/user/playlist", {
            "uid": uid, "limit": limit, "offset": offset, "includeVideo": "true",
        }, weapi=True)
        self.check(res, "获取用户歌单失败")
        return [Playlist.from_api(p) for p in (res.get("playlist") or [])]

    def likelist(self, uid: int) -> List[int]:
        res = self.request("/api/song/like/get", {"uid": uid}, weapi=True)
        if res.get("code") != 200:
            return []
        return [int(x) for x in (res.get("ids") or [])]

    # -- 每日推荐 / 私人 FM -------------------------------------------
    def daily_songs(self) -> List[Song]:
        res = self.request("/api/v3/discovery/recommend/songs", {}, weapi=True)
        self.check(res, "获取每日推荐失败")
        data = res.get("data") or {}
        return [Song.from_api(s) for s in (data.get("dailySongs") or [])]

    def daily_playlists(self) -> List[Playlist]:
        res = self.request("/api/v1/discovery/recommend/resource", {}, weapi=True)
        self.check(res, "获取每日推荐歌单失败")
        return [Playlist.from_api(p) for p in (res.get("recommend") or [])]

    def recommend_playlists(self) -> List[Playlist]:
        """未登录时的推荐歌单。"""
        res = self.request("/api/personalized/playlist",
                           {"limit": 12, "total": "true", "n": 12}, weapi=True)
        self.check(res, "获取推荐歌单失败")
        return [Playlist.from_api(p) for p in (res.get("result") or [])]

    def personal_fm(self) -> List[Song]:
        res = self.request("/api/v1/radio/get", {}, weapi=True)
        self.check(res, "获取私人 FM 失败")
        return [Song.from_api(s) for s in (res.get("data") or [])]

    def fm_trash(self, song_id: int) -> None:
        res = self.request("/api/radio/trash/add",
                           {"songId": song_id, "alg": "RT", "time": 25}, weapi=True)
        if res.get("code") != 200:
            raise ApiError(friendly_code_message(res.get("code"), str(res.get("msg") or "")),
                           res.get("code"))

    # -- 红心 / 评论 ---------------------------------------------------
    def like(self, song_id: int, like: bool = True) -> None:
        res = self.request("/api/radio/like", {
            "alg": "itembased", "trackId": song_id, "like": "true" if like else "false",
            "time": "3",
        }, weapi=True)
        if res.get("code") != 200:
            raise ApiError(friendly_code_message(res.get("code"), str(res.get("msg") or "")),
                           res.get("code"))

    def post_comment(self, song_id: int, content: str,
                     reply_to: Optional[int] = None) -> Dict[str, Any]:
        """发表（或回复）歌曲评论：weapi /api/resource/comments/add|reply。"""
        path = ("/api/resource/comments/reply" if reply_to
                else "/api/resource/comments/add")
        data: Dict[str, Any] = {
            "threadId": f"R_SO_4_{song_id}",
            "content": content,
        }
        if reply_to:
            data["commentId"] = reply_to
        return self.request(path, data, weapi=True, retries=0)

    def comments(self, song_id: int, limit: int = 15, offset: int = 0) -> Dict[str, Any]:
        # 评论接口较容易被限流：只重试 1 次，失败快速返回让用户用 l/r 重试
        res = self.request(f"/api/v1/resource/comments/R_SO_4_{song_id}", {
            "rid": song_id, "limit": limit, "offset": offset, "beforeTime": 0,
        }, weapi=True, retries=1)
        self.check(res, "获取评论失败")
        return res

    # -- 登录 / 登出 ---------------------------------------------------
    def qr_key(self) -> str:
        res = self.request("/api/login/qrcode/unikey", {"type": 3}, weapi=True)
        self.check(res, "获取扫码登录钥匙失败")
        return str(res.get("unikey") or "")

    def qr_check(self, key: str) -> Dict[str, Any]:
        res = self.request("/api/login/qrcode/client/login",
                           {"key": key, "type": 3}, weapi=True, retries=0)
        return res

    @staticmethod
    def qr_url(key: str) -> str:
        return f"{API_HOST}/login?codekey={key}"

    def login_cellphone(self, phone: str, password: str,
                        captcha: str = "",
                        countrycode: str = "86") -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "type": "1",
            "https": "true",
            "phone": phone,
            "countrycode": countrycode,
            "rememberLogin": "true",
        }
        if captcha:
            data["captcha"] = captcha
        else:
            data["password"] = md5_hex(password)
        res = self.request("/api/w/login/cellphone", data, weapi=True, retries=0)
        if res.get("code") == 200:
            self.save_cookies()
        return res

    def send_captcha(self, phone: str, ctcode: str = "86") -> Dict[str, Any]:
        """发送短信验证码（weapi /api/sms/captcha/sent）。"""
        return self.request("/api/sms/captcha/sent", {
            "ctcode": ctcode, "cellphone": phone,
        }, weapi=True, retries=0)

    def login_email(self, email: str, password: str) -> Dict[str, Any]:
        data = {
            "type": "0",
            "https": "true",
            "username": email,
            "password": md5_hex(password),
            "rememberLogin": "true",
        }
        res = self.request("/api/w/login", data, weapi=True, retries=0)
        if res.get("code") == 200:
            self.save_cookies()
        return res

    def logout(self) -> None:
        try:
            self.request("/api/logout", {}, weapi=True, retries=0)
        except Exception:
            pass
        for cookie in list(self.jar):
            self.jar.clear(cookie.domain, cookie.path, cookie.name)
        self.save_cookies()


# --------------------------------------------------------------------------
# 播放后端（mpv / ffplay / mpg123 / 模拟），统一抽象
# --------------------------------------------------------------------------

class BackendError(Exception):
    """后端启动或运行失败。"""


def _find_binary(names: Sequence[str]) -> Optional[str]:
    """在 PATH 与若干常见位置查找可执行文件（支持 tools/ffbin 便携目录）。"""
    extra_dirs = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "ffbin"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"),
        os.path.join(os.getcwd(), "tools", "ffbin"),
    ]
    for name in names:
        found = shutil.which(name)
        if found:
            return found
        for d in extra_dirs:
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return None


def _silent_wav(path: str) -> bool:
    """生成 0.08 秒静音 WAV，用于探测播放器是否真的可用。"""
    try:
        rate = 8000
        frames = rate * 8 // 100
        data = b"\x00\x00" * frames
        with open(path, "wb") as fh:
            fh.write(b"RIFF")
            fh.write(struct.pack("<I", 36 + len(data)))
            fh.write(b"WAVEfmt ")
            fh.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
            fh.write(b"data")
            fh.write(struct.pack("<I", len(data)))
            fh.write(data)
        return True
    except OSError:
        return False


class PlaybackBackend:
    """播放内核基类：只负责“把一个 URL 播出来”，队列逻辑归 Player 管。"""

    name = "base"
    label = "基础"
    caps: set = set()          # pause / seek / volume / exact

    def __init__(self, volume: int = 80, cfg_dir: Optional[str] = None):
        self.volume = int(clamp(volume, 0, 100))
        self.cfg_dir = cfg_dir or config_home()
        self.duration = 0.0
        self.tool_name = self.name      # 实际使用的可执行文件名（可能与 name 不同）
        self._on_end: Optional[Callable[[str], None]] = None
        self._on_error: Optional[Callable[[str], None]] = None
        self._lock = threading.RLock()
        self._running = False
        self._start_ts = 0.0
        self._base_offset = 0.0
        self._paused_total = 0.0
        self._pause_ts = 0.0
        self._paused = False
        self._ended_flag = False
        self._gen = 0            # 播放“代际”：防止旧进程监听线程误杀新音轨

    def _bump_gen(self) -> int:
        with self._lock:
            self._gen += 1
            return self._gen

    def _gen_alive(self, gen: int) -> bool:
        with self._lock:
            return gen == self._gen

    # -- 能力探测 ------------------------------------------------------
    @classmethod
    def available(cls) -> bool:
        return True

    @classmethod
    def probe(cls) -> Tuple[bool, str]:
        """轻量探测后端能否正常工作，返回 (可用, 原因)。"""
        return True, ""

    # -- 播放控制 ------------------------------------------------------
    def start(self, url: str, offset: float = 0.0, duration: float = 0.0,
              on_end: Optional[Callable[[str], None]] = None,
              on_error: Optional[Callable[[str], None]] = None) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def pause(self) -> bool:
        return False

    def resume(self) -> bool:
        return False

    def seek(self, position: float) -> bool:
        return False

    def set_volume(self, volume: int) -> bool:
        self.volume = int(clamp(volume, 0, 100))
        return False

    def position(self) -> float:
        """当前播放位置（秒）。"""
        return 0.0

    def is_playing(self) -> bool:
        return self._running

    # -- 内部 ----------------------------------------------------------
    def _mark_end(self, reason: str = "eof") -> None:
        with self._lock:
            if self._ended_flag or not self._running:
                return
            self._ended_flag = True
            self._running = False
        cb = self._on_end
        if cb:
            cb(reason)

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._ended_flag:
                return
            self._ended_flag = True
            self._running = False
        cb = self._on_error
        if cb:
            cb(message)

    def _wall_position(self) -> float:
        """基于墙钟的位置估算（用于非精确定位的后端）。"""
        with self._lock:
            if not self._running:
                return max(0.0, min(self._base_offset, self.duration or self._base_offset))
            now = time.monotonic()
            paused = self._paused_total + (
                (now - self._pause_ts) if self._paused else 0.0
            )
            pos = self._base_offset + (now - self._start_ts) - paused
        if self.duration > 0:
            pos = min(pos, self.duration)
        return max(0.0, pos)


# -- mpv：功能最完整的后端（推荐） ----------------------------------------

class MpvBackend(PlaybackBackend):
    name = "mpv"
    label = "mpv"
    caps = {"pause", "seek", "volume", "exact"}
    _probe_ao: Optional[str] = None      # None=未探测 / "auto"=系统默认 / "null"=静音驱动

    def __init__(self, volume: int = 80, cfg_dir: Optional[str] = None):
        super().__init__(volume, cfg_dir)
        self.binary = _find_binary(["mpv"])
        self._ao = type(self)._probe_ao
        self.proc: Optional[subprocess.Popen] = None
        self.sock_path = ""
        self.sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._req_id = 0
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._props: Dict[str, Any] = {}

    @classmethod
    def available(cls) -> bool:
        return bool(_find_binary(["mpv"]))

    @classmethod
    def probe(cls) -> Tuple[bool, str]:
        """先试系统默认音频输出；不行再退回 ao=null（无声环境仍可驱动）。"""
        binary = _find_binary(["mpv"])
        if not binary:
            cls._probe_ao = None
            return False, "未找到 mpv"
        tmp = os.path.join(ensure_dir(os.path.join(tempfile.gettempdir(), "ncm-probe")),
                           f"p{os.getpid()}.wav")
        if not _silent_wav(tmp):
            cls._probe_ao = None
            return False, "无法写入临时文件"
        last_err = ""
        for ao in (None, "null"):
            cmd = [binary, "--no-video", "--really-quiet"]
            if ao:
                cmd.append(f"--ao={ao}")
            cmd += ["--frames=1", tmp]
            try:
                proc = subprocess.run(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=20,
                )
            except subprocess.TimeoutExpired:
                cls._probe_ao = "auto" if ao is None else "null"
                return True, ""
            except Exception as exc:        # noqa: BLE001
                last_err = str(exc)
                continue
            if proc.returncode == 0:
                cls._probe_ao = "auto" if ao is None else "null"
                return True, ("" if ao is None
                              else "已找到 mpv（无音频设备，仅静音驱动）")
            last_err = proc.stderr.decode("utf-8", "replace")[-160:]
        cls._probe_ao = None
        return False, last_err or "mpv 无法播放"

    # -- IPC -----------------------------------------------------------
    def _connect(self, timeout: float = 6.0) -> None:
        end = time.monotonic() + timeout
        last: Optional[Exception] = None
        while time.monotonic() < end:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(self.sock_path)
                s.settimeout(None)
                self.sock = s
                return
            except OSError as exc:
                last = exc
                time.sleep(0.15)
        raise BackendError(f"无法连接 mpv IPC：{last}")

    def _send(self, command: List[Any], wait_id: bool = True,
              timeout: float = 4.0) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self.sock:
                return None
            self._req_id += 1
            rid = self._req_id
            payload = json.dumps({"command": command, "request_id": rid}) + "\n"
            try:
                self.sock.sendall(payload.encode())
            except OSError:
                return None
        if not wait_id:
            return None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if rid in self._responses:
                    return self._responses.pop(rid)
            time.sleep(0.02)
        return None

    def _reader_loop(self, gen: int) -> None:
        buf = b""
        sock = self.sock
        if sock is None:
            return
        while self._gen_alive(gen):
            try:
                chunk = sock.recv(8192)
            except (OSError, ConnectionError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                msg = safe_json(line.decode("utf-8", "replace"), None)
                if not isinstance(msg, dict):
                    continue
                if "request_id" in msg and msg["request_id"] is not None:
                    with self._lock:
                        self._responses[int(msg["request_id"])] = msg
                    continue
                if msg.get("event") == "property-change":
                    name = msg.get("name")
                    self._props[name] = msg.get("data")
                    if name == "eof-reached" and msg.get("data"):
                        self._mark_end("eof")

    # -- 控制 ----------------------------------------------------------
    def start(self, url, offset=0.0, duration=0.0, on_end=None, on_error=None):
        self._bump_gen()                 # 先失效旧监听线程，避免误判为“播放结束”
        self.stop()
        self._on_end, self._on_error = on_end, on_error
        self.duration = float(duration or 0.0)
        self._ended_flag = False
        self._props = {}
        sock_dir = ensure_dir(os.path.join(tempfile.gettempdir(), "ncm-mpv"))
        self.sock_path = os.path.join(
            sock_dir, f"mpv-{os.getpid()}-{random.randint(1000, 9999)}.sock")
        try:
            os.remove(self.sock_path)
        except OSError:
            pass
        if self._ao is None:                 # 用户显式指定内核时也要探测一次
            ok, _ = type(self).probe()
            self._ao = type(self)._probe_ao if ok else "auto"
        cmd = [
            self.binary, "--no-video", "--really-quiet",
            f"--input-ipc-server={self.sock_path}",
            f"--volume={self.volume}",
            "--no-input-default-bindings", "--input-terminal=no",
            "--msg-level=all=warn",
        ]
        if self._ao == "null":
            cmd.append("--ao=null")
        if offset and offset > 0.5:
            cmd.append(f"--start={offset:.3f}")
        if self.duration > 0:
            cmd.append(f"--length={self.duration + 1:.3f}")
        cmd.append(url)
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise BackendError(f"启动 mpv 失败：{exc}")
        self._connect()
        gen = self._gen
        with self._lock:
            self._running = True
            self._start_ts = time.monotonic()
            self._base_offset = float(offset or 0.0)
            self._paused_total = 0.0
            self._paused = False
            self._pause_ts = 0.0
        for prop in ("time-pos", "duration", "pause", "eof-reached", "volume"):
            self._send(["observe_property", random.randint(1, 1 << 30), prop], wait_id=False)
        self._reader = threading.Thread(target=self._reader_loop, args=(gen,), daemon=True)
        self._reader.start()
        threading.Thread(target=self._wait_exit, args=(gen,), daemon=True).start()

    def _wait_exit(self, gen: int) -> None:
        proc = self.proc
        if not proc:
            return
        code = proc.wait()
        time.sleep(0.15)
        if not self._gen_alive(gen):
            return                     # 已被新一代播放取代，忽略本次退出
        with self._lock:
            ended = self._ended_flag
        if not ended:
            if code == 0:
                self._mark_end("eof")
            else:
                self._fail(f"mpv 异常退出（code={code}）")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        with self._lock:
            self._running = False
            self._ended_flag = True
        try:
            if self.sock:
                self._send(["quit"], wait_id=False, timeout=0.3)
        except Exception:
            pass
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.sock_path:
            try:
                os.remove(self.sock_path)
            except OSError:
                pass

    def pause(self) -> bool:
        ok = self._send(["set_property", "pause", True]) is not None
        if ok:
            with self._lock:
                if not self._paused:
                    self._paused = True
                    self._pause_ts = time.monotonic()
        return ok

    def resume(self) -> bool:
        ok = self._send(["set_property", "pause", False]) is not None
        if ok:
            with self._lock:
                if self._paused:
                    self._paused_total += time.monotonic() - self._pause_ts
                    self._paused = False
        return ok

    def seek(self, position: float) -> bool:
        position = max(0.0, position)
        ok = self._send(["seek", position, "absolute"]) is not None
        if ok:
            with self._lock:
                self._base_offset = position
                self._start_ts = time.monotonic()
                self._paused_total = 0.0
                if self._paused:
                    self._pause_ts = time.monotonic()
        return ok

    def set_volume(self, volume: int) -> bool:
        self.volume = int(clamp(volume, 0, 100))
        return self._send(["set_property", "volume", float(self.volume)]) is not None

    def position(self) -> float:
        resp = self._send(["get_property", "time-pos"], timeout=1.0)
        if resp and resp.get("data") is not None:
            try:
                return max(0.0, float(resp["data"]))
            except (TypeError, ValueError):
                pass
        cached = self._props.get("time-pos")
        if isinstance(cached, (int, float)):
            return float(cached)
        return self._wall_position()


# -- ffplay：ffmpeg 自带播放器，功能精简但普及率高 --------------------------

class FfplayBackend(PlaybackBackend):
    """ffplay 后端；系统里没有 ffplay 时自动改用 ffmpeg（同参数体系）。"""

    name = "ffplay"
    label = "ffplay"
    caps = {"pause", "seek", "volume"}
    _ffmpeg_out: Optional[Tuple[str, str]] = None    # (格式, 目标) 探测缓存

    _prefer_tool: Optional[str] = None     # 探测后锁定实际可用的工具

    @classmethod
    def _resolve(cls) -> Tuple[Optional[str], str]:
        if cls._prefer_tool == "ffmpeg":
            ffmpeg = _find_binary(["ffmpeg"])
            if ffmpeg:
                return ffmpeg, "ffmpeg"
        ffplay = _find_binary(["ffplay"])
        if ffplay:
            return ffplay, "ffplay"
        ffmpeg = _find_binary(["ffmpeg"])
        if ffmpeg:
            return ffmpeg, "ffmpeg"
        return None, ""

    def __init__(self, volume: int = 80, cfg_dir: Optional[str] = None):
        super().__init__(volume, cfg_dir)
        self.binary, self.tool = self._resolve()
        self.tool_name = self.tool or "ffplay"
        self.proc: Optional[subprocess.Popen] = None
        self._current_url: Optional[str] = None
        self._stderr_tail: List[str] = []

    @classmethod
    def available(cls) -> bool:
        return bool(cls._resolve()[0])

    @classmethod
    def probe(cls) -> Tuple[bool, str]:
        binary, tool = cls._resolve()
        if not binary:
            return False, "未找到 ffplay / ffmpeg"
        tmp = os.path.join(ensure_dir(os.path.join(tempfile.gettempdir(), "ncm-probe")),
                           f"p{os.getpid()}.wav")
        if not _silent_wav(tmp):
            return False, "无法写入临时文件"
        try:
            if tool == "ffplay":
                proc = subprocess.run(
                    [binary, "-nodisp", "-autoexit", "-loglevel", "error",
                     "-t", "0.05", tmp],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=25,
                )
                err = proc.stderr.decode("utf-8", "replace").strip()
                # ffplay 在无声卡环境也会以 0 退出，需同时检查 stderr
                if proc.returncode == 0 and "audio open failed" not in err \
                        and "Failed to open" not in err:
                    cls._prefer_tool = None
                    return True, ""
                # ffplay 不可用（多为无声卡环境）→ 继续尝试 ffmpeg 通道
                cls._prefer_tool = "ffmpeg"
                binary, tool = cls._resolve()
                if not binary or tool != "ffmpeg":
                    return False, err[-160:] or "ffplay 无法播放"
            # ffmpeg：按 alsa → pulse → null 顺序找可用输出
            for fmt, target in (("alsa", "default"), ("pulse", "default"),
                                ("null", "-")):
                proc = subprocess.run(
                    [binary, "-v", "error", "-nostdin", "-i", tmp, "-t", "0.05",
                     "-f", fmt, "-y", target],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=25,
                )
                if proc.returncode == 0:
                    cls._ffmpeg_out = (fmt, target)
                    return True, ("" if fmt != "null"
                                  else "已找到 ffmpeg（无音频设备，仅静音驱动）")
            err = subprocess.run(  # noqa: S603
                [binary, "-version"], stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=10)
            return False, "ffmpeg 无法输出音频"
        except subprocess.TimeoutExpired:
            return True, ""     # 卡住说明在放，只是没退出
        except Exception as exc:
            return False, str(exc)

    def start(self, url, offset=0.0, duration=0.0, on_end=None, on_error=None):
        self._bump_gen()                 # 先失效旧监听线程，避免误判为“播放结束”
        self.stop()
        self._on_end, self._on_error = on_end, on_error
        self.duration = float(duration or 0.0)
        self._ended_flag = False
        self._stderr_tail = []
        self._current_url = url
        gain = round(self.volume / 100.0 * 4.0, 2)   # ffplay 音量为 0~4 倍
        if not self.binary:
            self.binary, self.tool = self._resolve()
        if not self.binary:
            raise BackendError("未找到 ffplay / ffmpeg")
        if self.tool == "ffmpeg":
            out = type(self)._ffmpeg_out or ("null", "-")
            cmd = [self.binary, "-v", "error", "-nostdin", "-re"]
            if offset and offset > 0.3:
                cmd += ["-ss", f"{offset:.3f}"]
            cmd += ["-i", url, "-vn", "-sn", "-af", f"volume={gain}",
                    "-f", out[0], "-y", out[1]]
        else:
            # 注意：ffplay 不接受 -re（会把它当成带值选项）；音量用 -volume，
            # 音频输出本身按实时速率节流，无需 -re
            cmd = [self.binary, "-nodisp", "-autoexit", "-loglevel", "warning",
                   "-vn", "-sn", "-volume", str(self.volume)]
            if offset and offset > 0.3:
                cmd += ["-ss", f"{offset:.3f}"]
            cmd.append(url)
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise BackendError(f"启动 {self.tool} 失败：{exc}")
        gen = self._gen
        with self._lock:
            self._running = True
            self._start_ts = time.monotonic()
            self._base_offset = float(offset or 0.0)
            self._paused_total = 0.0
            self._paused = False
            self._pause_ts = 0.0
        threading.Thread(target=self._watch, args=(gen,), daemon=True).start()

    def _watch(self, gen: int) -> None:
        proc = self.proc
        if not proc:
            return
        born = time.monotonic()

        def pump() -> None:
            try:
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        self._stderr_tail.append(line)
                        if len(self._stderr_tail) > 12:
                            self._stderr_tail.pop(0)
            except (ValueError, OSError):
                pass

        t = threading.Thread(target=pump, daemon=True)
        t.start()
        code = proc.wait()
        t.join(timeout=1.0)
        if not self._gen_alive(gen):
            return                     # 旧进程：已被新一代播放取代
        alive = time.monotonic() - born
        with self._lock:
            ended = self._ended_flag
        if ended:
            return
        detail = self._stderr_tail[-1] if self._stderr_tail else ""
        audio_broken = any("audio open failed" in ln or "Failed to open" in ln
                           for ln in self._stderr_tail)
        if audio_broken or (alive < 1.5 and code != 0):
            self._fail(f"{self.tool} 无法播放（{detail or f'退出码 {code}'}）")
        else:
            self._mark_end("eof")

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        with self._lock:
            was_running = self._running
            self._running = False
            self._ended_flag = True
        if proc:
            try:
                if was_running and proc.poll() is None:
                    try:
                        os.kill(proc.pid, signal.SIGCONT)
                    except OSError:
                        pass
                    proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass

    def pause(self) -> bool:
        proc = self.proc
        if not proc or proc.poll() is not None:
            return False
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except OSError:
            return False
        with self._lock:
            if not self._paused:
                self._paused = True
                self._pause_ts = time.monotonic()
        return True

    def resume(self) -> bool:
        proc = self.proc
        if not proc or proc.poll() is not None:
            return False
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except OSError:
            return False
        with self._lock:
            if self._paused:
                self._paused_total += time.monotonic() - self._pause_ts
                self._paused = False
        return True

    def seek(self, position: float) -> bool:
        position = max(0.0, position)
        if not self._current_url:
            return False
        self._restart(max(position, 0.0))
        return True

    def set_volume(self, volume: int) -> bool:
        self.volume = int(clamp(volume, 0, 100))
        if self._current_url and self.is_playing() and not self._paused:
            self._restart(self.position())
        return True

    def _restart(self, offset: float) -> None:
        url = self._current_url
        on_end, on_error = self._on_end, self._on_error
        duration = self.duration
        self.start(url, offset=offset, duration=duration,
                   on_end=on_end, on_error=on_error)

    def position(self) -> float:
        return self._wall_position()


# -- mpg123：极轻量后备 ----------------------------------------------------

class Mpg123Backend(FfplayBackend):
    name = "mpg123"
    label = "mpg123"
    caps = {"pause", "seek"}

    def __init__(self, volume: int = 80, cfg_dir: Optional[str] = None):
        super().__init__(volume, cfg_dir)
        self.binary = _find_binary(["mpg123"])

    @classmethod
    def available(cls) -> bool:
        return bool(_find_binary(["mpg123"]))

    @classmethod
    def probe(cls) -> Tuple[bool, str]:
        return (True, "") if _find_binary(["mpg123"]) else (False, "未找到 mpg123")

    def start(self, url, offset=0.0, duration=0.0, on_end=None, on_error=None):
        self._bump_gen()                 # 先失效旧监听线程
        self.stop()
        self._on_end, self._on_error = on_end, on_error
        self.duration = float(duration or 0.0)
        self._ended_flag = False
        self._current_url = url
        cmd = [self.binary, "--quiet", "-r", "44100"]
        if offset and offset > 0.3:
            cmd += ["--start", f"{offset:.2f}"]
        cmd.append(url)
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise BackendError(f"启动 mpg123 失败：{exc}")
        gen = self._gen
        with self._lock:
            self._running = True
            self._start_ts = time.monotonic()
            self._base_offset = float(offset or 0.0)
            self._paused_total = 0.0
            self._paused = False
            self._pause_ts = 0.0
        threading.Thread(target=self._watch, args=(gen,), daemon=True).start()


# -- 模拟后端：无声环境 / 自动化测试 --------------------------------------

class SimBackend(PlaybackBackend):
    name = "sim"
    label = "模拟播放"
    caps = {"pause", "seek", "volume", "exact"}

    def __init__(self, volume: int = 80, cfg_dir: Optional[str] = None):
        super().__init__(volume, cfg_dir)
        self._pos = 0.0
        self._tick_stop = threading.Event()

    @classmethod
    def probe(cls) -> Tuple[bool, str]:
        return True, ""

    def start(self, url, offset=0.0, duration=0.0, on_end=None, on_error=None):
        self._bump_gen()                 # 先失效旧的 tick 线程
        self.stop()
        self._on_end, self._on_error = on_end, on_error
        self.duration = float(duration or 0.0)
        self._ended_flag = False
        self._pos = float(offset or 0.0)
        with self._lock:
            self._running = True
            self._start_ts = time.monotonic()
            self._base_offset = self._pos
            self._paused_total = 0.0
            self._paused = False
            self._pause_ts = 0.0
            self._pos_cache = self._pos
        gen = self._gen
        self._tick_stop.clear()
        threading.Thread(target=self._tick, args=(gen,), daemon=True).start()

    def _tick(self, gen: int) -> None:
        while not self._tick_stop.is_set() and self._gen_alive(gen):
            time.sleep(0.1)
            with self._lock:
                if not self._running or self._paused:
                    continue
            pos = self._wall_position()
            self._pos = pos
            if self.duration and pos >= self.duration - 0.05:
                self._pos = self.duration
                self._mark_end("eof")
                return

    def stop(self) -> None:
        self._tick_stop.set()
        with self._lock:
            self._running = False
            self._ended_flag = True

    def pause(self) -> bool:
        with self._lock:
            if self._paused:
                return True
            self._pos = self._wall_position()
            self._paused = True
            self._pause_ts = time.monotonic()
            self._running = False
        return True

    def resume(self) -> bool:
        with self._lock:
            if not self._paused:
                return True
            self._paused = False
            self._base_offset = self._pos
            self._start_ts = time.monotonic()
            self._paused_total = 0.0
            self._running = True
        return True

    def seek(self, position: float) -> bool:
        position = max(0.0, position)
        with self._lock:
            self._pos = position
            self._base_offset = position
            self._start_ts = time.monotonic()
            self._paused_total = 0.0
        return True

    def set_volume(self, volume: int) -> bool:
        self.volume = int(clamp(volume, 0, 100))
        return True

    def position(self) -> float:
        with self._lock:
            if self._paused or not self._running:
                return min(self._pos, self.duration or self._pos)
        return min(self._wall_position(), self.duration or 10 ** 9)


BACKEND_CLASSES: List[Any] = [MpvBackend, FfplayBackend, Mpg123Backend, SimBackend]


def available_backends() -> List[Any]:
    return [cls for cls in BACKEND_CLASSES if cls.available()]


def select_backend(preference: str = "auto") -> Optional[Any]:
    """按偏好与可用性挑选后端类。"""
    pool = available_backends()
    if preference and preference != "auto":
        for cls in pool:
            if cls.name == preference:
                return cls
        return None
    for cls in pool:
        if cls.name == "sim":
            continue
        ok, _ = cls.probe()
        if ok:
            return cls
    return SimBackend


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

class Config:
    """持久化配置（JSON）。"""

    DEFAULTS: Dict[str, Any] = {
        "backend": "auto",        # auto / mpv / ffplay / mpg123 / sim
        "quality": "exhigh",      # 音质偏好（服务端按权限实际下发）
        "volume": 80,
        "mode": "list",           # list / one / seq / shuffle
        "theme": "color",         # color / mono
        "translation": True,      # 显示歌词翻译
        "roman": False,           # 显示罗马音
        "page_size": 30,
        "show_queue": True,
        "download_dir": "",       # 空 = 程序目录下的 download/
    }

    def __init__(self, cfg_dir: str):
        self.path = os.path.join(cfg_dir, "config.json")
        self.data: Dict[str, Any] = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as fh:
                    stored = json.load(fh) or {}
                for key in self.DEFAULTS:
                    if key in stored:
                        self.data[key] = stored[key]
        except Exception:
            pass

    def save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, self.DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.data[key] = value
        if save:
            self.save()


# --------------------------------------------------------------------------
# 播放引擎：队列 / 模式 / 歌词 / 进度
# --------------------------------------------------------------------------

class PlayerEngine:
    """把“歌曲队列 + 播放内核 + 歌词”粘在一起。"""

    def __init__(self, api: NcmApi, cfg: Config, cfg_dir: str):
        self.api = api
        self.cfg = cfg
        self.cfg_dir = cfg_dir
        self.events: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()

        self.queue: List[Song] = []
        self.index: int = -1
        self.mode: str = str(cfg.get("mode", "list"))
        self.queue_source: str = ""
        self.history: List[int] = []          # 已播放的下标（随机模式用）

        self.state: str = "idle"              # idle / loading / playing / paused
        self.current: Optional[Song] = None
        self.lyric = LyricSheet()
        self.lyric_loading = False

        self.volume: int = int(clamp(int(cfg.get("volume", 80)), 0, 100))
        self.duration: float = 0.0
        self.started_at: float = 0.0          # 本首歌开始时间（用于 prev 判断）

        self._url_cache: Dict[int, Dict[str, Any]] = {}   # id -> {url, expire, br, type}
        self._file_fallback: bool = False   # 当前曲目是否已降级为本地缓存播放
        self._fail_streak: int = 0
        self._op_lock = threading.RLock()
        self._stopping = False

        self._backend_pool: List[Any] = available_backends()
        self._backend_idx: int = 0
        self.backend: Optional[PlaybackBackend] = None
        self.backend_name: str = "-"
        self._init_backend()

    # -- 后端管理 ------------------------------------------------------
    def _init_backend(self) -> None:
        pref = str(self.cfg.get("backend", "auto"))
        chosen = select_backend(pref)
        if chosen is None:
            chosen = SimBackend
        if pref != "auto" and chosen.name != pref:
            # 显式指定但不可用
            chosen = SimBackend
        if pref != "auto" and chosen.name != "sim":
            ok, reason = chosen.probe()      # 探测：确认可用并锁定能力降级
            if not ok:
                self.emit("warn",
                          f"{chosen.label} 不可用（{reason or '探测失败'}），"
                          f"自动改用其它内核")
                chosen = select_backend("auto") or SimBackend
        self._backend_pool = [c for c in available_backends() if c.name != "sim"]
        self._backend_idx = 0
        if chosen.name != "sim":
            for i, c in enumerate(self._backend_pool):
                if c.name == chosen.name:
                    self._backend_idx = i
                    break
            if not any(c.name == chosen.name for c in self._backend_pool):
                self._backend_pool.insert(0, chosen)
        self._create_backend(chosen)

    def _create_backend(self, cls: Any) -> None:
        self.backend = cls(volume=self.volume, cfg_dir=self.cfg_dir)
        self.backend_name = getattr(self.backend, "tool_name", cls.name)
        note = f" → {self.backend_name}" if self.backend_name != cls.name else ""
        self.emit("info", f"播放内核：{cls.label}{note}")

    def backend_info(self) -> str:
        cls = type(self.backend) if self.backend else None
        caps = sorted(cls.caps) if cls else []
        return f"{self.backend_name}（能力：{'/'.join(caps) or '-'}）"

    def rotate_backend(self) -> bool:
        """换一个可用后端（当前后端出问题时自动降级）。返回是否还有新后端。"""
        pool = [c for c in available_backends() if c.name != "sim"]
        if not pool:
            return False
        current_name = self.backend_name
        for _ in range(len(pool) + 1):
            self._backend_idx = (self._backend_idx + 1) % len(self._backend_pool or pool)
            nxt = (self._backend_pool or pool)[self._backend_idx]
            if nxt.name == current_name and len(pool) > 1:
                continue
            ok, reason = nxt.probe()
            if ok:
                self._create_backend(nxt)
                return True
        return False

    # -- 事件 ----------------------------------------------------------
    def emit(self, level: str, text: str) -> None:
        self.events.put(("log", level, text))

    def _post(self, kind: str, *payload: Any) -> None:
        self.events.put((kind,) + payload)

    def drain(self) -> List[Tuple[Any, ...]]:
        out: List[Tuple[Any, ...]] = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # -- 队列管理 ------------------------------------------------------
    def set_queue(self, songs: List[Song], start: int = 0, source: str = "",
                  autoplay: bool = True) -> None:
        songs = [s for s in songs if s]
        with self._op_lock:
            self.queue = list(songs)
            self.index = -1
            self.history = []
            self.queue_source = source
        if not songs:
            self.emit("warn", "队列为空")
            return
        if autoplay:
            self.play_index(max(0, min(start, len(songs) - 1)))
        else:
            self.index = max(0, min(start, len(songs) - 1))
            self.current = self.queue[self.index]

    def append_queue(self, songs: List[Song], source: str = "") -> None:
        with self._op_lock:
            self.queue.extend(songs)
            if source:
                self.queue_source = source

    @property
    def queue_len(self) -> int:
        return len(self.queue)

    def upcoming(self, count: int = 5) -> List[Tuple[int, Song]]:
        out = []
        for i in range(self.index + 1, min(len(self.queue), self.index + 1 + count)):
            out.append((i, self.queue[i]))
        return out

    # -- 播放 ----------------------------------------------------------
    def resolve_url(self, song: Song) -> Optional[Dict[str, Any]]:
        """解析播放地址（带缓存与音质回退）。"""
        cached = self._url_cache.get(song.id)
        if (cached and "trial" in cached
                and cached.get("expire", 0) > time.time()):
            return dict(cached)
        info = self.api.resolve_song_url(song)
        if not info or not info.get("url"):
            # 最后一次尝试：请求最低音质
            info = self.api.song_urls([song.id], br=96000).get(song.id) or {}
            if info and not info.get("url"):
                return None
        if not info.get("url"):
            return None
        expire = time.time() + max(60, int(info.get("expi") or 1200) - 60)
        trial = 0.0
        trial_info = info.get("freeTrialInfo") or {}
        if isinstance(trial_info, dict) and trial_info.get("end"):
            trial = float(trial_info.get("end") or 0)
        elif info.get("time"):
            trial = float(info.get("time")) / 1000.0
        entry = {
            "url": info["url"],
            "expire": expire,
            "br": int(info.get("br") or 0),
            "type": str(info.get("type") or "").lower(),
            "trial": trial,
        }
        self._url_cache[song.id] = entry
        return dict(entry)

    # -- 本地缓存播放（直连失败 / 需要时自动下载） ---------------------
    @property
    def cache_dir(self) -> str:
        return ensure_dir(os.path.join(self.cfg_dir, "cache"))

    @staticmethod
    def _ext_for(url: str, kind: str = "") -> str:
        if kind in ("mp3", "flac", "aac", "m4a", "ogg", "wav"):
            return "." + kind
        path = urllib.parse.urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        return ext if ext in (".mp3", ".flac", ".aac", ".m4a", ".ogg", ".wav") else ".mp3"

    def download_song(self, song: Song) -> Optional[str]:
        """把当前曲目下载到本地缓存，返回本地路径（失败返回 None）。"""
        info = self.resolve_url(song)
        if not info:
            return None
        target = os.path.join(self.cache_dir,
                              f"{song.id}{self._ext_for(info['url'], info.get('type', ''))}")
        if os.path.exists(target) and os.path.getsize(target) > 4096:
            return target
        tmp = target + ".part"
        limit = 80 * 1024 * 1024          # 单曲上限 80MB
        try:
            with self.api.open_url(info["url"], timeout=30) as resp, open(tmp, "wb") as fh:
                total = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise IOError("文件超过缓存上限")
                    fh.write(chunk)
            if total < 1024:
                raise IOError("下载内容过短")
            os.replace(tmp, target)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        self._prune_cache()
        return target

    def _prune_cache(self, keep: int = 30) -> None:
        try:
            files = [os.path.join(self.cache_dir, f)
                     for f in os.listdir(self.cache_dir)
                     if f.endswith((".mp3", ".flac", ".aac", ".m4a", ".ogg", ".wav"))]
            files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            for stale in files[keep:]:
                try:
                    os.remove(stale)
                except OSError:
                    pass
        except OSError:
            pass

    def play_index(self, index: int) -> bool:
        with self._op_lock:
            if not self.queue:
                return False
            index = index % len(self.queue)
            song = self.queue[index]
            self.index = index
            if index in self.history[-50:]:
                pass
            else:
                self.history.append(index)
            return self._start_locked(song, 0.0)

    def _start_locked(self, song: Song, offset: float,
                      use_file: bool = False) -> bool:
        self._stopping = False
        self.state = "loading"
        self.current = song
        self.duration = song.duration_s
        self._file_fallback = bool(use_file)
        self._post("track", song)          # UI 立即显示曲目信息
        info = self.resolve_url(song)
        # VIP 歌曲可能只下发“试听片段”，进度/跳转要按真实可听长度算
        trial = float((info or {}).get("trial") or 0.0)
        if 0 < trial < self.duration:
            self.duration = trial
            self.emit("info", f"该歌曲为 VIP 试听（{int(trial)} 秒）："
                              f"登录并开通会员可播放完整版")
        if not info:
            self._fail_streak += 1
            self.emit("error", f"无法播放：{song.display()}（版权或 VIP 限制）")
            if self._fail_streak >= min(3, max(1, len(self.queue))):
                self.emit("error", "连续多首无法播放，已停止")
                self._stop_backend()
                self.state = "idle"
                return False
            return self._auto_next(from_failure=True)
        if self.backend is None:
            self._init_backend()
        source = info["url"]
        if use_file:
            local = self.download_song(song)
            if local:
                source = local
                self.emit("info", f"使用本地缓存播放：{os.path.basename(local)}")
            else:
                self._file_fallback = False
        callbacks = dict(
            on_end=lambda reason: self._post("backend_end", reason),
            on_error=lambda msg: self._post("backend_error", msg),
        )
        try:
            self.backend.start(source, offset=offset, duration=self.duration,
                               **callbacks)
        except BackendError as exc:
            self.emit("error", str(exc))
            if self.rotate_backend() and self.backend is not None:
                try:
                    self.backend.start(source, offset=offset,
                                       duration=self.duration, **callbacks)
                except BackendError as exc2:
                    self.emit("error", str(exc2))
                    self.state = "idle"
                    return False
            else:
                self.state = "idle"
                return False
        self.state = "playing"
        self.started_at = time.time()
        self._fail_streak = 0
        self._load_lyric_async(song)
        self._prefetch_next()
        return True

    def play_song(self, song: Song, source: str = "") -> bool:
        with self._op_lock:
            if source:
                self.queue_source = source
            if song not in self.queue:
                self.queue.insert(self.index + 1 if self.index >= 0 else 0, song)
                self.index = self.queue.index(song)
            else:
                self.index = self.queue.index(song)
            return self._start_locked(song, 0.0)

    def _stop_backend(self) -> None:
        if self.backend:
            try:
                self.backend.stop()
            except Exception:
                pass

    def stop(self) -> None:
        with self._op_lock:
            self._stopping = True
            self._stop_backend()
            self.state = "idle"
            self.current = None
            self.duration = 0.0
        self.emit("info", "已停止播放")

    # -- 结束 / 切歌 ---------------------------------------------------
    def handle_backend_end(self, reason: str = "eof") -> None:
        if self._stopping:
            return
        self._auto_next(reason=reason)

    def handle_backend_error(self, message: str) -> None:
        if self._stopping:
            return
        self.emit("error", message)
        # 第一层降级：直连播放失败时，改用本地缓存文件重试同一首
        if (self.current is not None and not self._file_fallback
                and self.state not in ("idle",)):
            song = self.current
            self.emit("warn", "直连播放失败，改为下载到本地后播放")
            with self._op_lock:
                ok = self._start_locked(song, 0.0, use_file=True)
            if ok:
                return
        self._fail_streak += 1
        if self._fail_streak >= 3 and self.rotate_backend():
            self.emit("warn", "已自动切换播放内核并重试")
        if self._fail_streak >= min(4, max(1, len(self.queue))):
            self.emit("error", "连续播放失败，已停止")
            self.state = "idle"
            return
        self._auto_next(from_failure=True)

    def _next_index(self) -> Optional[int]:
        n = len(self.queue)
        if n == 0:
            return None
        if self.mode == "one":
            return self.index
        if self.mode == "shuffle":
            if n == 1:
                return 0
            pool = [i for i in range(n) if i != self.index]
            recent = set(self.history[-max(1, n // 2):])
            preferred = [i for i in pool if i not in recent]
            choices = preferred or pool
            return random.choice(choices)
        if self.mode == "seq":
            if self.index + 1 >= n:
                return None
            return self.index + 1
        return (self.index + 1) % n

    def _auto_next(self, from_failure: bool = False, reason: str = "eof") -> None:
        with self._op_lock:
            if self._stopping:
                return
            # 私人 FM 队列见底时自动补充
            if (self.queue_source == "fm"
                    and self.index + 1 >= len(self.queue)
                    and not from_failure):
                threading.Thread(target=self._refill_fm, daemon=True).start()
            nxt = self._next_index()
            if nxt is None:
                if from_failure:
                    self.emit("error", "没有更多可播放的歌曲了")
                else:
                    self.emit("info", "播放完毕")
                self._stop_backend()
                self.state = "idle"
                return
            if not from_failure:
                self.history.append(self.index)
            self.play_index(nxt)

    def _refill_fm(self) -> None:
        try:
            songs = self.api.personal_fm()
        except ApiError as exc:
            self.emit("error", f"私人 FM 加载失败：{exc.message}")
            return
        except Exception as exc:      # 网络异常
            self.emit("error", f"私人 FM 加载失败：{exc}")
            return
        if songs:
            start = self.queue_len
            self.append_queue(songs, source="fm")
            self.emit("info", f"私人 FM 补充了 {len(songs)} 首")
            with self._op_lock:
                if self.state in ("idle",) or self.index < 0:
                    self.play_index(start)

    def next(self) -> bool:
        with self._op_lock:
            if not self.queue:
                return False
            if self.mode == "one":
                return self._start_locked(self.queue[self.index], 0.0)
            nxt = self._next_index()
            if nxt is None:
                self.emit("info", "已经是最后一首")
                return False
            self.history.append(self.index)
            return self.play_index(nxt)

    def prev(self) -> bool:
        with self._op_lock:
            if not self.queue:
                return False
            if self.position() > 4.0:
                return self._start_locked(self.queue[self.index], 0.0)
            idx = self.index - 1
            if idx < 0:
                idx = len(self.queue) - 1 if self.mode in ("list", "shuffle") else 0
            return self.play_index(idx)

    def retry(self) -> bool:
        with self._op_lock:
            if self.index < 0:
                return False
            self._url_cache.pop(self.queue[self.index].id, None)
            return self._start_locked(self.queue[self.index], 0.0)

    # -- 控制 ----------------------------------------------------------
    def toggle_pause(self) -> str:
        if self.backend is None or self.state not in ("playing", "paused"):
            return "当前没有正在播放的歌曲"
        if self.state == "playing":
            ok = self.backend.pause()
            if ok:
                self.state = "paused"
                return "已暂停"
            return "该播放内核不支持暂停"
        ok = self.backend.resume()
        if ok:
            self.state = "playing"
            return "继续播放"
        return "恢复失败"

    def seek_to(self, seconds: float) -> str:
        if self.backend is None or self.current is None:
            return "当前没有正在播放的歌曲"
        if "seek" not in type(self.backend).caps:
            return "该播放内核不支持跳转进度"
        seconds = clamp(seconds, 0, max(0.1, self.duration - 0.5) if self.duration else seconds)
        ok = self.backend.seek(seconds)
        if ok:
            return f"跳转到 {fmt_time(seconds)}"
        return "跳转失败"

    def seek_by(self, delta: float) -> str:
        return self.seek_to(max(0.0, self.position() + delta))

    def set_volume(self, volume: int) -> str:
        self.volume = int(clamp(volume, 0, 100))
        self.cfg.set("volume", self.volume)
        if self.backend is not None:
            if "volume" in type(self.backend).caps:
                self.backend.set_volume(self.volume)
                return f"音量 {self.volume}%"
            self.backend.volume = self.volume
            return f"音量 {self.volume}%（切换歌曲后生效）"
        return f"音量 {self.volume}%"

    def set_mode(self, mode: str) -> str:
        if mode not in PLAY_MODES:
            return f"未知模式：{mode}（可选 {'/'.join(PLAY_MODES)}）"
        self.mode = mode
        self.cfg.set("mode", mode)
        return f"播放模式：{PLAY_MODE_NAMES[mode]}"

    # -- 进度 / 歌词 ---------------------------------------------------
    def position(self) -> float:
        if self.backend is None or self.state in ("idle",):
            return 0.0
        return self.backend.position() if self.backend else 0.0

    def progress(self) -> float:
        pos = self.position()
        if self.duration <= 0:
            return 0.0
        return clamp(pos / self.duration, 0.0, 1.0)

    def lyric_index(self) -> int:
        if self.lyric is None:
            return -1
        return self.lyric.index_at(self.position())

    def _load_lyric_async(self, song: Song) -> None:
        if self.lyric_loading:
            return
        self.lyric_loading = True
        self.lyric = LyricSheet()

        def worker() -> None:
            sheet: Optional[LyricSheet] = None
            try:
                sheet = self.api.lyric(song.id)
            except Exception:
                sheet = None
            self.lyric_loading = False
            if sheet is not None and self.current is not None and self.current.id == song.id:
                self.lyric = sheet
                self._post("lyric", sheet)

        threading.Thread(target=worker, daemon=True).start()

    def _prefetch_next(self) -> None:
        """后台预取下一首的播放地址，切换时更顺滑。"""
        nxt = self._next_index()
        if nxt is None or not self.queue or nxt >= len(self.queue):
            return
        song = self.queue[nxt]
        if song.id in self._url_cache:
            return

        def worker() -> None:
            try:
                self.resolve_url(song)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # -- 便捷状态 ------------------------------------------------------
    def status_icon(self) -> str:
        return {"playing": "▶", "paused": "⏸", "loading": "◌",
                "idle": "■"}.get(self.state, "■")

    def summary_line(self) -> str:
        if not self.current:
            return "未在播放"
        pos, dur = self.position(), self.duration
        return (f"{self.status_icon()} {self.current.name} - {self.current.artist} "
                f"[{fmt_time(pos)}/{fmt_time(dur)}] {PLAY_MODE_NAMES.get(self.mode, '')}")


# --------------------------------------------------------------------------
# 带 ANSI 的文本度量 / 排版
# --------------------------------------------------------------------------

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]")


def ansi_width(text: str) -> int:
    """忽略 ANSI 转义后的显示宽度。"""
    return disp_width(_CSI_RE.sub("", text))


def ansi_cut(text: str, max_w: int) -> str:
    """按显示宽度截断（保留转义序列）。"""
    if max_w <= 0:
        return ""
    if ansi_width(text) <= max_w:
        return text
    out: List[str] = []
    w = 0
    i = 0
    n = len(text)
    ell = "…"
    ell_w = disp_width(ell)
    while i < n:
        m = _CSI_RE.match(text, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        ch = text[i]
        cw = char_width(ch)
        if w + cw > max_w - ell_w:
            break
        out.append(ch)
        w += cw
        i += 1
    out.append(ell)
    if "\x1b[" in text:
        out.append("\x1b[0m")     # 截断后补一次复位，避免颜色串到行尾
    return "".join(out)


def ansi_rpad(text: str, width: int) -> str:
    """右侧补空格到指定显示宽度。"""
    w = ansi_width(text)
    if w > width:
        return ansi_cut(text, width)
    if w < width:
        return text + " " * (width - w)
    return text


def fit_line(text: str, width: int) -> str:
    """截断或补空格，使行恰好占 width 列。"""
    return ansi_rpad(text, width)


# --------------------------------------------------------------------------
# 渲染部件
# --------------------------------------------------------------------------

def build_box(title: str, content: List[str], width: int, theme: "Theme",
              style: str = "cyan", height: int = 0,
              border: bool = True) -> List[str]:
    """圆角边框盒子；content 会被补齐/截断到 height 行。"""
    if width < 10 or not border:
        lines = []
        if title:
            lines.append(theme.paint(f"[{title}]", style, "bold"))
        for row in content:
            lines.append(fit_line(row, width))
        if height:
            while len(lines) < height:
                lines.append(" " * width)
            lines = lines[:height]
        return lines

    inner = width - 4
    if height:
        while len(content) < height - 2:
            content.append("")
        content = content[:max(0, height - 2)]

    out: List[str] = []
    if title:
        t = theme.paint(f" {title} ", style, "bold")
        t_w = ansi_width(t)
        # 总宽 = 1(╭) + t_w + dash + 1(╮) → dash = width - 2 - t_w
        dash = max(0, width - 2 - t_w)
        out.append(theme.paint("╭", style) + t + theme.paint("─" * dash + "╮", style))
    else:
        out.append(theme.paint("╭" + "─" * (width - 2) + "╮", style))
    for row in content:
        body = fit_line(row, inner)
        out.append(theme.paint("│ ", style) + body + theme.paint(" │", style))
    out.append(theme.paint("╰" + "─" * (width - 2) + "╯", style))
    return out


def progress_bar(ratio: float, width: int, theme: "Theme",
                 style: str = "green") -> str:
    """[████░░░░] 形式的进度条（宽度含方括号）。"""
    width = max(3, width)
    inner = width - 2
    ratio = clamp(ratio, 0.0, 1.0)
    filled = int(round(inner * ratio))
    filled = min(inner, max(0, filled))
    bar = theme.paint("█" * filled, style) + theme.paint("░" * (inner - filled), "gray")
    return f"[{bar}]"


def render_player_panel(engine: PlayerEngine, width: int, height: int,
                        theme: "Theme", cfg: Config) -> List[str]:
    """正在播放面板（含进度与接下来队列）。"""
    content: List[str] = []
    song = engine.current
    if not song:
        content.append(theme.paint("未在播放", "gray"))
        content.append(theme.paint("输入 help 查看命令，s <关键词> 搜索歌曲", "gray"))
    else:
        icon = engine.status_icon()
        style = {"playing": "bgreen", "paused": "byellow",
                 "loading": "bcyan"}.get(engine.state, "gray")
        title = theme.paint(f"{icon} ", style, "bold") + theme.paint(
            slice_disp(song.name, max(6, width - 8)), "bold")
        artist = theme.paint(f" - {song.artist}", "cyan")
        content.append(title + artist)
        flag = song.playable_flag
        meta_bits = []
        if flag:
            meta_bits.append(theme.paint(f"[{flag}]", "byellow"))
        if song.album:
            meta_bits.append(theme.paint(f"专辑 {slice_disp(song.album, 14)}", "gray"))
        if engine.current and getattr(engine, "_cur_br", 0):
            pass
        if meta_bits:
            content.append(" ".join(meta_bits))

        pos, dur = engine.position(), engine.duration
        pct = int(engine.progress() * 100)
        time_txt = f"{fmt_time(pos)} / {fmt_time(dur)}"
        bar_w = max(8, width - len(time_txt) - 6)
        bar = progress_bar(engine.progress(), bar_w, theme)
        content.append(f"{theme.paint(time_txt, 'yellow')} {bar} "
                       f"{theme.paint(f'{pct:>3}%', 'gray')}")

        vol = f"♪ 音量 {engine.volume}%"
        mode = f"模式 {PLAY_MODE_NAMES.get(engine.mode, engine.mode)}"
        back = f"内核 {engine.backend_name}"
        info_line = f"{theme.paint(vol, 'cyan')}  {theme.paint(mode, 'magenta')}  " \
                    f"{theme.paint(back, 'gray')}"
        content.append(info_line)

    # 接下来
    upcoming = engine.upcoming(4 if height >= 14 else 2)
    if upcoming and height >= 10:
        content.append(theme.paint("─" * max(0, width), "gray"))
        for idx, item in upcoming:
            num = theme.paint(f"{idx + 1:>3}.", "gray")
            txt = slice_disp(item.name, max(4, width - disp_width(f"{idx + 1:>3}. ")
                                            - len(fmt_time(item.duration_s)) - 3))
            line = f"{num} {txt}"
            line += " " * max(1, width - ansi_width(line) - len(fmt_time(item.duration_s)))
            line += theme.paint(fmt_time(item.duration_s), "gray")
            content.append(line)

    box = build_box("正在播放", content, width, theme, "cyan", height=height)
    return box


def render_lyric_panel(engine: PlayerEngine, width: int, height: int,
                       theme: "Theme", cfg: Config) -> List[str]:
    """歌词面板。"""
    sheet = engine.lyric
    content: List[str] = []
    inner = width - 4 if width >= 10 else width
    if sheet is None or sheet.empty():
        content.append(theme.paint("（暂无歌词）", "gray"))
        if engine.lyric_loading:
            content.append(theme.paint("歌词加载中…", "gray"))
        if engine.current:
            content.append(theme.paint("纯音乐或暂无歌词的歌曲", "gray"))
    else:
        idx = sheet.index_at(engine.position())
        show_trans = bool(cfg.get("translation", True)) and sheet.has_translation
        show_roman = bool(cfg.get("roman", False)) and sheet.has_roman
        rows: List[Tuple[str, bool]] = []      # (text, is_current)
        window = max(1, (height - 2) // (2 if show_trans else 1))
        start = idx - window // 2
        start = max(0, start)
        end = min(len(sheet.lines), start + window)
        if idx < 0:
            start, end = 0, min(len(sheet.lines), window)
        for i in range(start, end):
            ln = sheet.lines[i]
            cur = (i == idx)
            text = ln.main if ln.main else "…"
            rows.append((text, cur))
            if show_trans and ln.trans:
                rows.append((ln.trans, False))
            if show_roman and ln.rom:
                rows.append((ln.rom, False))
        rows = rows[:max(1, height - 2)]
        for text, cur in rows:
            cut = inner - (2 if width >= 10 else 0)
            body = slice_disp(text, max(4, cut))
            if cur:
                content.append(theme.paint(f"› {body}", "byellow", "bold"))
            else:
                content.append(theme.paint(f"  {body}", "gray"))
        if idx < 0 and sheet.lines:
            content = content[:max(1, height - 2)]

    title = "歌词"
    if sheet is not None and sheet.has_translation and cfg.get("translation", True):
        title = "歌词 · 双语"
    return build_box(title, content, width, theme, "magenta", height=height)


def adaptive_layout(cols: int, rows: int) -> Dict[str, Any]:
    """按终端尺寸给出布局参数（宽屏双栏 / 中屏堆叠 / 窄屏极简）。"""
    layout: Dict[str, Any] = {"cols": cols, "rows": rows, "prompt_rows": 1}
    if rows >= 30:
        live_h = 17
    elif rows >= 24:
        live_h = 14
    elif rows >= 18:
        live_h = 10
    elif rows >= 13:
        live_h = 7
    else:
        live_h = 4
    # 输出区至少保留 3 行
    avail = rows - 1
    live_h = min(live_h, max(3, avail - 3))
    out_h = avail - live_h
    layout["live_h"] = live_h
    layout["out_h"] = max(0, out_h)
    if cols >= 96:
        left = int(cols * 0.56)
        left = max(40, left)
        layout["mode"] = "wide"
        layout["left_w"] = left
        layout["right_w"] = cols - left
    elif cols >= 64:
        layout["mode"] = "stack"
        layout["left_w"] = cols
        layout["right_w"] = cols
    else:
        layout["mode"] = "mini"
        layout["left_w"] = cols
        layout["right_w"] = cols
    return layout


# --------------------------------------------------------------------------
# 应用核心：消息缓冲 / 事件 / 任务队列
# --------------------------------------------------------------------------

class Application:
    """命令与状态的中枢（与具体终端形式解耦）。"""

    MAX_MESSAGES = 600

    def __init__(self, args: Any):
        self.args = args
        self.cfg_dir = config_home()
        self.cfg = Config(self.cfg_dir)
        if getattr(args, "backend", None):
            self.cfg.set("backend", args.backend, save=True)
        if getattr(args, "mono", False):
            self.cfg.set("theme", "mono", save=True)
        color_ok = (self.cfg.get("theme", "color") == "color"
                    and not os.environ.get("NO_COLOR"))
        self.theme = Theme(enabled=color_ok)
        self.api = NcmApi(self.cfg_dir)
        self.engine = PlayerEngine(self.api, self.cfg, self.cfg_dir)

        self.messages: List[str] = []
        self.scroll: int = 0            # 0=贴底；>0 向上翻的行数
        self.results: Optional[ResultSet] = None
        self.user: Dict[str, Any] = {}
        self.running: bool = True
        self.busy: str = ""             # 状态栏上的“正在…”提示
        self.output_width = int(os.environ.get("COLUMNS") or 100)
        self.output_height = int(os.environ.get("LINES") or 30)
        # 安全输入（密码）交互：TUI 会在命令行上以 ● 回显
        self.supports_secret = False
        self.secret_active = False
        self.secret_label = ""
        self.secret_buf = ""
        self.secret_mask = True            # False=明文回显（如短信验证码）
        self.secret_cb: Optional[Callable[[str], None]] = None
        # 全屏浏览模式（PgUp / Ctrl-B 进入，j/k 翻行、q 返回）
        self.supports_browse = False       # TUI 才开启
        self.browse_pending: Optional[int] = None   # 长输出自动切入浏览
        self.browse = False
        self.browse_offset: Optional[int] = None   # 浏览模式首行下标（None=自动到底部）
        # 扫码登录：全屏二维码视图（二维码比普通输出高得多，必须独占整屏）
        self.qr_poll_id = 0              # 轮询代次：新的 login qr 取代旧轮询
        self.qr_active = False
        self.qr_lines: List[str] = []
        self.qr_url = ""
        self.qr_status = ""
        self.qr_need_rows = 0
        self.screen_rows = int(os.environ.get("LINES") or 30)
        # 评论页：全屏浏览全部评论 + 发表评论
        self.cm_active = False
        self.cm_song: Optional[Song] = None
        self.cm_comments: List[Dict[str, Any]] = []
        self.cm_total = 0
        self.cm_loaded = 0            # 已加载条数（不含热评）
        self.cm_more = False
        self.cm_loading = False
        self.cm_offset = 0            # 视口首行（按渲染后的行号）
        self.cm_page = 20
        self.cm_composing = False
        self.cm_input = ""
        self.cm_status = ""
        # 启动参数里预置的命令（-c），在 startup 时统一执行
        self.pending_commands: List[str] = list(
            getattr(args, "command", None) or [])
        self._jobs: "queue.Queue[Tuple[str, Callable[[], Any]]]" = queue.Queue()
        self._jobs_pending: int = 0
        self._jobs_lock = threading.Lock()
        self._job_thread = threading.Thread(target=self._job_worker, daemon=True)
        self._job_thread.start()
        self._main_lock = threading.RLock()
        # 当前打开的歌单（id, name），供 download pl 等命令使用
        self.pl_meta: Optional[Tuple[int, str]] = None

    @property
    def download_dir(self) -> str:
        """当前下载目录（默认程序目录 / download）。"""
        return configured_download_dir(self.cfg)

    # -- 输出缓冲 ------------------------------------------------------
    def println(self, text: str = "", *styles: str) -> None:
        for i, line in enumerate(str(text).split("\n")):
            painted = self.theme.paint(line, *styles) if styles else line
            if i == 0 and self.messages and self.messages[-1] == "" and not text:
                continue
            self.messages.append(painted)
        if len(self.messages) > self.MAX_MESSAGES:
            del self.messages[:len(self.messages) - self.MAX_MESSAGES]
        self.scroll = 0

    def info(self, text: str) -> None:
        self.println(text, "bcyan")

    def ok(self, text: str) -> None:
        self.println(text, "bgreen")

    def warn(self, text: str) -> None:
        self.println(text, "byellow")

    def error(self, text: str) -> None:
        self.println(f"✗ {text}", "bred")

    def blank(self) -> None:
        self.println("")

    def heading(self, text: str) -> None:
        self.println(text, "bold", "bmagenta")

    def table(self, rows: List[str], header: Optional[str] = None,
              footer: Optional[str] = None) -> None:
        if header:
            self.println(header, "bold", "bcyan")
        for row in rows:
            self.println(row)
        if footer:
            self.println(footer, "gray")

    # -- 后台任务 ------------------------------------------------------
    def run_job(self, name: str, fn: Callable[[], Any]) -> None:
        with self._jobs_lock:
            self._jobs_pending += 1
        self._jobs.put((name, fn))

    def has_pending_jobs(self) -> bool:
        with self._jobs_lock:
            return self._jobs_pending > 0

    def _job_worker(self) -> None:
        while True:
            name, fn = self._jobs.get()
            try:
                fn()
            except ApiError as exc:
                self.error(f"{name}：{exc.message}")
            except Exception as exc:      # 网络 / 解析异常兜底
                self.error(f"{name}：{exc}")
            finally:
                with self._jobs_lock:
                    self._jobs_pending = max(0, self._jobs_pending - 1)
                self.busy = ""

    # -- 事件泵 --------------------------------------------------------
    def pump_events(self) -> List[str]:
        """处理引擎事件，返回需要立刻刷新的原因说明。"""
        reasons: List[str] = []
        for ev in self.engine.drain():
            kind = ev[0]
            if kind == "log":
                _, level, text = ev
                if level == "error":
                    self.error(text)
                elif level == "warn":
                    self.warn(text)
                elif level == "info":
                    self.info(text)
                else:
                    self.println(text)
                reasons.append("message")
            elif kind == "track":
                song: Song = ev[1]
                self.println(
                    self.theme.paint("▶ ", "bgreen", "bold")
                    + f"{song.name} - {song.artist} "
                    + self.theme.paint(f"({fmt_time(song.duration_s)})", "gray"))
                reasons.append("track")
            elif kind == "lyric":
                sheet: LyricSheet = ev[1]
                if sheet.has_translation and self.cfg.get("translation", True):
                    reasons.append("lyric")
            elif kind == "backend_end":
                reasons.append("end")
                self.engine.handle_backend_end(ev[1])
            elif kind == "backend_error":
                reasons.append("error")
                self.engine.handle_backend_error(ev[1])
        return reasons

    # -- 命令分发 ------------------------------------------------------
    def execute(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("quit", "exit", "q", "退出", "bye"):
            self.running = False
            return
        handler = COMMANDS.get(cmd)
        if handler is None:
            alias = ALIASES.get(cmd)
            handler = COMMANDS.get(alias) if alias else None
        if handler is None:
            self.error(f"未知命令：{cmd}（输入 help 查看帮助）")
            suggestion = [name for name in COMMANDS if name.startswith(cmd[:2])]
            if suggestion:
                self.info("你是不是想输入：" + "、".join(suggestion[:5]))
            return
        before = len(self.messages)
        try:
            handler(self, args, line)
        except ApiError as exc:
            self.error(exc.message)
        except KeyboardInterrupt:
            self.warn("已取消")
        except Exception as exc:      # 命令级兜底，避免整个界面崩溃
            self.error(f"命令执行出错：{exc}")
            if os.environ.get("NCM_DEBUG"):
                import traceback
                self.println(traceback.format_exc(), "gray")
        emitted = len(self.messages) - before
        if self.supports_browse and emitted > max(8, (self.output_height or 20) - 2):
            # 输出比窗口高：自动切入全屏浏览，从本次输出的开头看起（q 返回）
            self.browse_pending = before

    # -- 启动流程 ------------------------------------------------------
    def startup(self) -> None:
        self.println(
            self.theme.paint(f" {APP_NAME} ", "bold", "bcyan")
            + self.theme.paint(f"v{APP_VERSION}", "gray")
            + "   "
            + self.theme.paint("help 查看命令 · PgUp/Ctrl-B 回看输出 · quit 退出",
                               "gray"))
        # 播放内核探测
        pref = str(self.cfg.get("backend", "auto"))
        chosen = type(self.engine.backend) if self.engine.backend else None
        if chosen and chosen.name == "sim":
            self.warn("未找到可用的音频播放程序（mpv / ffplay / mpg123），"
                      "已切换到“模拟播放”模式：进度与歌词仍可用，但不会出声")
            self.info("安装任意一个即可恢复真实出声：apt install mpv / ffmpeg / mpg123")
        # 登录状态
        if self.api.logged_in:
            self.run_job("读取账号", self._load_profile_quiet)
        self.pump_events()          # 先消化启动阶段的引擎事件（播放内核提示等）
        # 启动参数 -c 预置的命令
        while self.pending_commands:
            cmd_line = self.pending_commands.pop(0)
            self.println(self.theme.paint(f"> {cmd_line}", "bgreen", "bold"))
            self.execute(cmd_line)

    def _load_profile_quiet(self) -> None:
        try:
            data = self.api.account()
            profile = (data.get("profile") or {})
            if profile:
                self.user = profile
        except Exception:
            pass

    def shutdown(self) -> None:
        try:
            self.engine.stop()
        except Exception:
            pass
        self.api.save_cookies()
        self.cfg.save()


# --------------------------------------------------------------------------
# 按键解析（原始终端）
# --------------------------------------------------------------------------

@dataclass
class KeyEvent:
    kind: str                 # char / key
    char: str = ""
    name: str = ""


_CSI_KEYMAP = {
    ("", "A"): "up", ("", "B"): "down", ("", "C"): "right", ("", "D"): "left",
    ("", "H"): "home", ("", "F"): "end", ("1", "~"): "home", ("7", "~"): "home",
    ("4", "~"): "end", ("8", "~"): "end", ("3", "~"): "delete",
    ("5", "~"): "pgup", ("6", "~"): "pgdn", ("2", "~"): "insert",
    ("Z", ""): "shift-tab",
}
_CTRL_KEYS = {
    1: "ctrl-a", 3: "ctrl-c", 4: "ctrl-d", 5: "ctrl-e", 12: "ctrl-l",
    14: "ctrl-n", 16: "ctrl-p", 21: "ctrl-u", 23: "ctrl-w",
}


def parse_input(buffer: bytes) -> Tuple[List[KeyEvent], bytes]:
    """把原始字节流解析成按键事件，返回 (事件, 未消费的字节)。"""
    events: List[KeyEvent] = []
    i = 0
    n = len(buffer)
    while i < n:
        b = buffer[i]
        if b == 0x1B:
            if buffer[i:i + 2] == b"\x1b[":
                j = i + 2
                while j < n and (0x30 <= buffer[j] <= 0x3F or 0x20 <= buffer[j] <= 0x2F):
                    j += 1
                if j >= n:
                    return events, buffer[i:]          # 序列未收全
                params = buffer[i + 2:j].decode("ascii", "replace")
                final = chr(buffer[j])
                i = j + 1
                name = _CSI_KEYMAP.get((params, final))
                if name:
                    events.append(KeyEvent("key", name=name))
                continue
            i += 1                                    # 单独 ESC / Alt 组合：忽略
            continue
        if b in (13, 10):
            events.append(KeyEvent("key", name="enter"))
            i += 1
            continue
        if b in (127, 8):
            events.append(KeyEvent("key", name="backspace"))
            i += 1
            continue
        if b == 9:
            events.append(KeyEvent("key", name="tab"))
            i += 1
            continue
        if b in _CTRL_KEYS:
            events.append(KeyEvent("key", name=_CTRL_KEYS[b]))
            i += 1
            continue
        if b < 0x20:
            i += 1
            continue
        # UTF-8 多字节
        if b < 0x80:
            need = 1
        elif b >> 5 == 0b110:
            need = 2
        elif b >> 4 == 0b1110:
            need = 3
        elif b >> 3 == 0b11110:
            need = 4
        else:
            i += 1
            continue
        if i + need > n:
            return events, buffer[i:]                  # 多字节未收全
        raw = buffer[i:i + need]
        try:
            ch = raw.decode("utf-8")
        except UnicodeDecodeError:
            i += 1
            continue
        events.append(KeyEvent("kind_char", char=ch))
        i += need
    return events, b""


# --------------------------------------------------------------------------
# 交互式终端界面（全屏 TUI：输出区 + 实况面板 + 命令行）
# --------------------------------------------------------------------------

class TerminalUI:
    TICK = 0.28                      # 刷新周期（秒）

    def __init__(self, app: Application):
        self.app = app
        self.theme = app.theme
        self.input_text = ""
        self.cursor = 0
        self.history: List[str] = self._load_history()
        self.hist_pos: Optional[int] = None
        self.hist_stash = ""
        self.prev_frame: List[str] = []
        self._pending = b""
        self._raw = False
        self._saved_termios = None
        self._resized = True
        self._quit_hint = False
        self.key_queue: "queue.Queue[List[KeyEvent]]" = queue.Queue()
        self.cols, self.rows = self._terminal_size()
        self.app.supports_secret = True      # TUI 支持“● 掩码”安全输入
        self.app.supports_browse = True      # TUI 支持全屏回看输出

    # -- 终端管理 ------------------------------------------------------
    def _terminal_size(self) -> Tuple[int, int]:
        try:
            ts = os.get_terminal_size(0)
            return max(20, ts.columns), max(6, ts.lines)
        except OSError:
            pass
        return shutil.get_terminal_size((80, 24)).columns, \
            shutil.get_terminal_size((80, 24)).lines

    def _write(self, text: str) -> None:
        try:
            sys.stdout.write(text)
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            self.app.running = False

    def _enter(self) -> None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                self._saved_termios = termios.tcgetattr(0)
                tty.setraw(0)
                self._raw = True
            except (termios.error, OSError):
                self._raw = False
            self._write("\x1b[?1049h")        # 切换备用屏幕
            self._write("\x1b[?25l")          # 隐藏光标（渲染时临时显示）
        signal.signal(signal.SIGWINCH, self._on_winch)

    def _leave(self) -> None:
        self._write("\x1b[?25h")
        if self._raw:
            self._write("\x1b[?1049l")
            if self._saved_termios is not None:
                try:
                    termios.tcsetattr(0, termios.TCSADRAIN, self._saved_termios)
                except (termios.error, OSError):
                    pass
        else:
            self._write("\n")

    def _on_winch(self, *_args: Any) -> None:
        self._resized = True
        if os.environ.get("NCM_DEBUG"):
            try:
                sys.stderr.write("[debug] SIGWINCH\n")
                sys.stderr.flush()
            except Exception:
                pass

    def _reader(self) -> None:
        """后台线程：读取标准输入原始字节。"""
        while self.app.running:
            try:
                data = os.read(0, 4096)
            except (OSError, ValueError):
                break
            if not data:
                self.key_queue.put([KeyEvent("key", name="eof")])
                break
            events, self._pending = parse_input(self._pending + data)
            if events:
                self.key_queue.put(events)
        self.app.running = False

    # -- 历史记录 ------------------------------------------------------
    def _history_path(self) -> str:
        return os.path.join(self.app.cfg_dir, "history.txt")

    def _load_history(self) -> List[str]:
        try:
            with open(self._history_path(), encoding="utf-8") as fh:
                lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
            return lines[-200:]
        except OSError:
            return []

    def _save_history(self, line: str) -> None:
        if not line.strip():
            return
        if self.history and self.history[-1] == line:
            return
        self.history.append(line)
        self.history = self.history[-200:]
        try:
            with open(self._history_path(), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    # -- 输入处理 ------------------------------------------------------
    def handle_events(self, events: List[KeyEvent]) -> None:
        if self.app.qr_active:
            for ev in events:
                self._handle_qr_event(ev)
            return
        if self.app.cm_active:
            for ev in events:
                self._handle_cm_event(ev)
            return
        if self.app.browse:
            for ev in events:
                self._handle_browse_event(ev)
            return
        if self.app.secret_active:
            for ev in events:
                if not self._handle_secret(ev):
                    return
            return
        for ev in events:
            if ev.kind == "key":
                if not self._handle_key(ev.name):
                    return
            elif ev.kind == "kind_char":
                self._insert(ev.char)

    # -- 全屏二维码视图（扫码登录） -------------------------------------
    def _close_qr(self) -> None:
        app = self.app
        if not app.qr_active:
            return
        app.qr_active = False
        if app.qr_url and app.qr_status and "过期" not in app.qr_status \
                and "成功" not in app.qr_status:
            app.println(f"（二维码视图已关闭，后台仍会继续等待扫码；"
                        f"输入 login qr 可重新打开视图）", "gray")
            app.println(f"备用：在手机浏览器/App 打开 {app.qr_url} 完成确认；"
                        f"若在电脑浏览器打开，会登录网页版（登录态留在浏览器），"
                        f"可用 login cookie <MUSIC_U> 导入程序", "gray")

    def _handle_qr_event(self, ev: KeyEvent) -> None:
        # 二维码视图只负责“看”：q / Esc / Enter 返回，其余忽略
        if ev.kind == "kind_char":
            if ev.char in ("q", "Q"):
                self._close_qr()
            return
        name = ev.name
        if name in ("q", "esc", "enter", "ctrl-c", "ctrl-b", "backspace"):
            self._close_qr()
        elif name == "ctrl-l":
            self._resized = True
        elif name == "eof":
            self.app.running = False

    def _build_qr_frame(self, cols: int, rows: int) -> List[str]:
        """整屏显示二维码 + 链接 + 状态；行数不够时给出明确提示而非残缺二维码。"""
        app = self.app
        frame = [""] * rows
        lines = app.qr_lines
        need = app.qr_need_rows or (len(lines) + 3)
        header = self.theme.paint(center_ansi(
            f" {APP_NAME} · 扫码登录 ", cols), "bold", "bcyan")
        status = (
            self.theme.paint(" ", "bold")
            + self.theme.paint(app.qr_status or "等待扫码…", "byellow", "bold")
            + self.theme.paint("　· 约 3 分钟有效 · q 返回（后台继续等待）", "gray")
        )
        avail_qr = rows - 3          # 表头 + 链接 + 状态
        if len(lines) > avail_qr:
            # 中途被缩小窗口：宁可显示“放不下”的说明，也不给半截二维码
            frame[0] = header
            warn = self.theme.paint(
                f" 终端行数不足：二维码需约 {need} 行，当前 {rows} 行，"
                f"显示不完整将无法扫描。请放大终端后重新 login qr", "bred")
            frame[1] = center_ansi(ansi_cut(warn, cols - 2), cols)
            frame[2] = center_ansi(self.theme.paint(app.qr_url, "bcyan"), cols)
            frame[rows - 1] = fit_line(status, cols)
            return frame
        frame[0] = header
        for i, ln in enumerate(lines):
            frame[1 + i] = center_ansi(ln, cols)
        url_row = 1 + len(lines)
        if url_row < rows - 1:
            frame[url_row] = center_ansi(
                self.theme.paint(app.qr_url, "dim"), cols)
        frame[rows - 1] = fit_line(status, cols)
        return frame

    # -- 全屏评论页 ----------------------------------------------------
    def _close_cm(self) -> None:
        app = self.app
        if not app.cm_active:
            return
        app.cm_active = False
        app.cm_composing = False
        song = app.cm_song
        if song:
            app.println(f"（评论页已关闭：《{song.name} - {song.artist}》"
                        f"共 {app.cm_total} 条，输入 cm 可再次打开）", "gray")

    def _cm_page_rows(self) -> int:
        return max(1, self.rows - 3)

    def _handle_cm_event(self, ev: KeyEvent) -> None:
        """评论页按键：浏览 j/k/PgUp/PgDn、l 加载更多、i 发表、q 返回。"""
        app = self.app
        if app.cm_composing:                     # 正在写评论
            if ev.kind == "kind_char":
                if ev.char.isprintable() and len(app.cm_input) < 300:
                    app.cm_input += ev.char
                return
            name = ev.name
            if name == "enter":
                send_comment(app)
            elif name == "backspace":
                app.cm_input = app.cm_input[:-1]
            elif name in ("esc", "ctrl-c"):
                app.cm_input = ""
                app.cm_composing = False
                app.cm_status = "已取消输入"
            elif name == "eof":
                app.running = False
            return

        page = self._cm_page_rows()
        total_rows = len(comment_page_lines(app))

        def move(delta: int) -> None:
            app.cm_offset = int(clamp((app.cm_offset or 0) + delta,
                                      0, max(0, total_rows - 1)))

        def load_more() -> None:
            if app.cm_loading:
                app.cm_status = "正在加载评论…"
                return
            if not app.cm_more:
                app.cm_status = "已到底了（r 可刷新）"
                return
            app.cm_status = "正在加载更多评论…"
            app.run_job("加载评论", lambda: load_comments(app, reset=False))

        if ev.kind == "kind_char":
            ch = ev.char
            if ch in ("q", "Q"):
                self._close_cm()
            elif ch == "j":
                move(+1)
            elif ch == "k":
                move(-1)
            elif ch == " ":
                move(+page)
            elif ch == "g":
                app.cm_offset = 0
            elif ch == "G":
                app.cm_offset = max(0, total_rows - page)
            elif ch == "l":
                load_more()
            elif ch in ("r", "R"):
                app.cm_status = "正在刷新…"
                app.run_job("加载评论", lambda: load_comments(app, reset=True))
            elif ch in ("i", "I", "c", "C"):
                app.cm_composing = True
                app.cm_input = ""
                app.cm_status = "输入内容后回车发表（Esc 取消）"
            return

        name = ev.name
        if name in ("q", "esc", "ctrl-b", "ctrl-c"):
            self._close_cm()
        elif name in ("pgdn",):
            move(+page)
        elif name == "pgup":
            move(-page)
        elif name == "down":
            move(+1)
        elif name == "up":
            move(-1)
        elif name == "home":
            app.cm_offset = 0
        elif name == "end":
            app.cm_offset = max(0, total_rows - page)
        elif name == "enter":
            load_more()
        elif name == "ctrl-l":
            self._resized = True
        elif name == "eof":
            app.running = False

    def _build_cm_frame(self, cols: int, rows: int) -> List[str]:
        """整屏评论页：表头 + 评论窗口 + 状态行 + 输入/提示行。"""
        app = self.app
        theme = self.theme
        frame = [""] * rows
        song = app.cm_song
        title = f" 评论 · {song.name if song else ''} - {song.artist if song else ''} "
        if app.cm_loading and not app.cm_comments:
            meta = "加载中…"
        else:
            meta = f"共 {app.cm_total} 条 · 已加载 {len(app.cm_comments)}"
            if app.cm_loading:
                meta += " · 加载中…"
        header = (theme.paint(slice_disp(title, max(12, cols // 2)), "bold", "bmagenta")
                  + theme.paint(slice_disp("· " + meta, cols // 2), "gray"))
        frame[0] = fit_line(header, cols)

        avail = max(1, rows - 3)
        lines = comment_page_lines(app)
        app.cm_offset = int(clamp(app.cm_offset or 0, 0, max(0, len(lines) - 1)))
        window = lines[app.cm_offset:app.cm_offset + avail]
        for i in range(avail):
            frame[1 + i] = fit_line(window[i] if i < len(window) else "", cols)

        status = (theme.paint(" ", "gray")
                  + theme.paint(app.cm_status or " ", "byellow"))
        frame[rows - 2] = fit_line(status, cols)
        if app.cm_composing:
            compose = (theme.paint(" 发表评论 > ", "bgreen", "bold")
                       + app.cm_input)
            frame[rows - 1] = fit_line(ansi_cut(compose, cols - 1)
                                       if ansi_width(compose) >= cols else compose,
                                       cols)
        else:
            hint = (" i 发表评论 · j/k 翻行 · PgUp/PgDn/空格 翻页 · "
                    "l 加载更多 · r 刷新 · q 返回")
            frame[rows - 1] = fit_line(theme.paint(hint, "gray"), cols)
        return frame

    # -- 全屏浏览（翻页） ----------------------------------------------
    def _page_rows(self) -> int:
        """浏览模式一屏能显示的行数。"""
        return max(1, self.rows - 1)

    def _open_browse(self, move: int = 0) -> None:
        """进入全屏浏览；move 为相对屏幕底部内容的行偏移。"""
        app = self.app
        page = self._page_rows()
        total = len(app.messages)
        if app.browse and app.browse_offset is not None:
            base = app.browse_offset
        else:
            base = max(0, total - page) + app.scroll
        app.browse = True
        app.browse_offset = int(clamp(base + move, 0, max(0, total - 1)))

    def _close_browse(self) -> None:
        self.app.browse = False
        self.app.browse_offset = None

    def _browse_page(self, delta_pages: int) -> None:
        app = self.app
        page = self._page_rows()
        total = len(app.messages)
        base = app.browse_offset if app.browse_offset is not None else 0
        app.browse_offset = int(clamp(base + delta_pages * page,
                                      0, max(0, total - 1)))

    def _handle_browse_event(self, ev: KeyEvent) -> None:
        """浏览模式按键：j/k 翻行、PgUp/PgDn/空格 翻页、g/G 首尾、q 返回。"""
        app = self.app
        total = len(app.messages)
        if ev.kind == "kind_char":
            ch = ev.char
            if ch in ("q", "Q"):
                self._close_browse()
            elif ch == "j":
                app.browse_offset = int(clamp(
                    (app.browse_offset or 0) + 1, 0, max(0, total - 1)))
            elif ch == "k":
                app.browse_offset = int(clamp(
                    (app.browse_offset or 0) - 1, 0, max(0, total - 1)))
            elif ch == " ":
                self._browse_page(+1)
            elif ch == "g":
                app.browse_offset = 0
            elif ch == "G":
                app.browse_offset = max(0, total - self._page_rows())
            return
        name = ev.name
        if name in ("q", "esc", "ctrl-b", "ctrl-c", "enter", "backspace"):
            self._close_browse()
        elif name == "pgdn":
            self._browse_page(+1)
        elif name == "pgup":
            self._browse_page(-1)
        elif name == "down":
            app.browse_offset = int(clamp(
                (app.browse_offset or 0) + 1, 0, max(0, total - 1)))
        elif name == "up":
            app.browse_offset = int(clamp(
                (app.browse_offset or 0) - 1, 0, max(0, total - 1)))
        elif name == "home":
            app.browse_offset = 0
        elif name == "end":
            app.browse_offset = max(0, total - self._page_rows())
        elif name == "ctrl-l":
            self._resized = True
        elif name == "eof":
            app.running = False

    def _handle_secret(self, ev: KeyEvent) -> bool:
        """密码等安全输入：只回显 ●，不写入历史。"""
        app = self.app
        if ev.kind == "kind_char":
            if ev.char.isprintable():
                app.secret_buf += ev.char
            return True
        name = ev.name
        if name == "enter":
            cb, buf = app.secret_cb, app.secret_buf
            app.secret_active = False
            app.secret_buf = ""
            app.secret_cb = None
            if cb:
                cb(buf)
            return True
        if name == "backspace":
            app.secret_buf = app.secret_buf[:-1]
        elif name in ("ctrl-c", "ctrl-d", "esc"):
            cb = app.secret_cb
            app.secret_active = False
            app.secret_buf = ""
            app.secret_cb = None
            app.warn("已取消输入")
            if cb:
                cb("")
        elif name == "eof":
            app.running = False
            return False
        return True

    def _handle_key(self, name: str) -> bool:
        """返回 False 表示请求退出循环。"""
        app = self.app
        if name == "enter":
            line = self.input_text
            self.input_text = ""
            self.cursor = 0
            self.hist_pos = None
            self._save_history(line)
            self.render(force=True)          # 先回显命令行
            if line.strip().lower() in ("quit", "exit", "q", "退出"):
                app.running = False
                return False
            app.execute(line)
            return True
        if name == "backspace":
            if self.cursor > 0:
                self.input_text = (self.input_text[:self.cursor - 1]
                                   + self.input_text[self.cursor:])
                self.cursor -= 1
            return True
        if name == "delete":
            if self.cursor < len(self.input_text):
                self.input_text = (self.input_text[:self.cursor]
                                   + self.input_text[self.cursor + 1:])
            return True
        if name == "left":
            self.cursor = max(0, self.cursor - 1)
        elif name == "right":
            self.cursor = min(len(self.input_text), self.cursor + 1)
        elif name == "home":
            self.cursor = 0
        elif name == "end":
            self.cursor = len(self.input_text)
        elif name == "up":
            self._history_move(-1)
        elif name == "down":
            self._history_move(+1)
        elif name == "pgup":
            self._open_browse(-max(3, (self.rows - 2) // 2))
        elif name == "pgdn":
            self._open_browse(max(3, (self.rows - 2) // 2))
        elif name == "ctrl-b":
            self._open_browse(0)
        elif name == "tab":
            self._complete()
        elif name == "ctrl-c":
            if self.input_text:
                self.input_text = ""
                self.cursor = 0
            else:
                self.app.info("（输入 quit 退出，help 查看帮助）")
            self.hist_pos = None
        elif name == "ctrl-d":
            if not self.input_text:
                app.running = False
                return False
        elif name == "ctrl-l":
            self._resized = True
        elif name == "ctrl-u":
            self.input_text = self.input_text[self.cursor:]
            self.cursor = 0
        elif name == "ctrl-w":
            head = self.input_text[:self.cursor].rstrip()
            if head:
                idx = max(head.rfind(" "), head.rfind("，"))
                head = head[:idx + 1] if idx >= 0 else ""
            self.input_text = head + self.input_text[self.cursor:]
            self.cursor = len(head)
        elif name == "ctrl-a":
            self.cursor = 0
        elif name == "ctrl-e":
            self.cursor = len(self.input_text)
        elif name == "ctrl-p":
            self._history_move(-1)
        elif name == "ctrl-n":
            self._history_move(+1)
        elif name == "eof":
            app.running = False
            return False
        return True

    def _history_move(self, delta: int) -> None:
        if not self.history:
            return
        if self.hist_pos is None:
            if delta > 0:
                return
            self.hist_stash = self.input_text
            self.hist_pos = len(self.history)
        self.hist_pos += delta
        if self.hist_pos >= len(self.history):
            self.hist_pos = None
            self.input_text = self.hist_stash
            self.cursor = len(self.input_text)
            return
        self.hist_pos = max(0, self.hist_pos)
        self.input_text = self.history[self.hist_pos]
        self.cursor = len(self.input_text)

    def _insert(self, char: str) -> None:
        if char.isprintable():
            self.input_text = (self.input_text[:self.cursor] + char
                               + self.input_text[self.cursor:])
            self.cursor += len(char)

    def _complete(self) -> None:
        """Tab 补全：命令名 / 常用子命令。"""
        text = self.input_text
        parts = text.split(" ")
        if len(parts) == 1:
            prefix = parts[0]
            if not prefix:
                return
            cands = sorted({c for c in list(COMMANDS) + list(ALIASES)
                            if c.startswith(prefix)})
            if len(cands) == 1:
                self.input_text = cands[0] + " "
                self.cursor = len(self.input_text)
            elif cands:
                self.app.info("可补全：" + "、".join(cands[:8]))
            return
        cmd = COMMANDS.get(parts[0]) or COMMANDS.get(ALIASES.get(parts[0], ""))
        subcmds = getattr(cmd, "subcmds", None) if cmd else None
        if subcmds:
            prefix = parts[-1]
            cands = sorted(c for c in subcmds if c.startswith(prefix))
            if len(cands) == 1:
                parts[-1] = cands[0]
                self.input_text = " ".join(parts)
                self.cursor = len(self.input_text)
            elif cands:
                self.app.info("可补全：" + "、".join(cands[:8]))

    # -- 渲染 ----------------------------------------------------------
    def build_frame(self) -> List[str]:
        app, engine, cfg = self.app, self.app.engine, self.app.cfg
        # 尺寸检测：优先响应 SIGWINCH，同时每帧轮询一次 ioctl 作为兜底
        # （有些终端复用器 / 容器环境不会投递 SIGWINCH）
        cols_now, rows_now = self._terminal_size()
        if (cols_now, rows_now) != (self.cols, self.rows):
            self.cols, self.rows = cols_now, rows_now
            self._resized = True
        if self._resized:
            self._resized = False
            self.prev_frame = []
            self._write("\x1b[2J")
        layout = adaptive_layout(self.cols, self.rows)
        cols, rows = layout["cols"], layout["rows"]
        if os.environ.get("NCM_DEBUG"):
            try:
                sys.stderr.write(f"[debug] resize -> {cols}x{rows}\n")
                sys.stderr.flush()
            except Exception:
                pass
        out_h, live_h = layout["out_h"], layout["live_h"]
        app.output_width = cols                      # 供命令排版使用
        app.output_height = out_h
        app.screen_rows = rows

        # 0a) 全屏二维码视图（扫码登录）优先级最高
        if app.qr_active:
            return self._build_qr_frame(cols, rows)

        # 0b) 全屏评论页（浏览全部 + 发表）
        if app.cm_active:
            return self._build_cm_frame(cols, rows)

        # 0) 全屏浏览模式：整屏显示输出历史 + 底部状态行
        if app.browse:
            return self._build_browse_frame(cols, rows)

        frame: List[str] = [""] * rows

        # 1) 输出区（自上而下滚动缓冲）
        msgs = app.messages
        if app.scroll <= 0:
            shown = msgs[-out_h:] if out_h else []
        else:
            end = max(out_h, len(msgs) - app.scroll)
            shown = msgs[max(0, end - out_h):end]
        out_lines = [""] * (out_h - len(shown)) + list(shown)
        for i in range(out_h):
            frame[i] = fit_line(out_lines[i] if i < len(out_lines) else "", cols)

        # 2) 实况区
        start = out_h
        mode = layout["mode"]
        if mode == "wide":
            left_w, right_w = layout["left_w"], layout["right_w"]
            left = render_player_panel(engine, left_w, live_h, self.theme, cfg)
            right = render_lyric_panel(engine, right_w, live_h, self.theme, cfg)
            for i in range(live_h):
                l = fit_line(left[i] if i < len(left) else "", left_w)
                r = fit_line(right[i] if i < len(right) else "", right_w)
                frame[start + i] = l + r
        elif mode == "stack" and live_h >= 8:
            player_h = max(5, live_h * 2 // 5)
            lyric_h = live_h - player_h
            top = render_player_panel(engine, cols, player_h, self.theme, cfg)
            bottom = render_lyric_panel(engine, cols, lyric_h, self.theme, cfg)
            for i in range(player_h):
                frame[start + i] = fit_line(top[i] if i < len(top) else "", cols)
            for i in range(lyric_h):
                frame[start + player_h + i] = fit_line(
                    bottom[i] if i < len(bottom) else "", cols)
        else:   # mini：极简两行状态 + 歌词行
            for i in range(live_h):
                frame[start + i] = fit_line(self._mini_line(i, cols, live_h), cols)

        # 3) 输入行
        frame[rows - 1] = self._prompt_line(cols)
        return frame

    def _build_browse_frame(self, cols: int, rows: int) -> List[str]:
        """全屏浏览：显示输出缓冲区 + 一行状态提示。"""
        app = self.app
        page = max(1, rows - 1)
        total = len(app.messages)
        offset = app.browse_offset
        if offset is None:
            offset = max(0, total - page)
        offset = int(clamp(offset, 0, max(0, total - 1)))
        app.browse_offset = offset
        window = app.messages[offset:offset + page]
        frame: List[str] = [""] * rows
        for i in range(page):
            frame[i] = fit_line(window[i] if i < len(window) else "", cols)
        first = offset + 1 if total else 0
        last = offset + len(window)
        status = (
            self.theme.paint(" 浏览 ", "bold", "bmagenta")
            + self.theme.paint(f"{first}-{last}/{total} 行", "bold")
            + self.theme.paint(
                "  j/k 翻行 · PgUp/PgDn/空格 翻页 · g/G 首尾 · q 返回", "gray")
        )
        frame[rows - 1] = fit_line(status, cols)
        return frame

    def _mini_line(self, idx: int, cols: int, live_h: int) -> str:
        engine, cfg = self.app.engine, self.app.cfg
        if idx == 0:
            song = engine.current
            if not song:
                return self.theme.paint("未在播放 · help 查看命令", "gray")
            icon = engine.status_icon()
            return (self.theme.paint(icon, "bgreen", "bold")
                    + f" {slice_disp(song.name, max(4, cols - 12))} - "
                    + slice_disp(song.artist, max(4, cols // 3)))
        if idx == 1:
            pos, dur = engine.position(), engine.duration
            bar = progress_bar(engine.progress(), max(8, cols - 20), self.theme)
            return (self.theme.paint(f"{fmt_time(pos)}/{fmt_time(dur)}", "yellow")
                    + " " + bar
                    + self.theme.paint(f" {engine.volume}%", "cyan"))
        sheet = engine.lyric
        n = idx - 2
        if sheet and not sheet.empty():
            i = sheet.index_at(engine.position())
            li = i + n - (live_h - 3) // 2 if i >= 0 else n
            if 0 <= li < len(sheet.lines):
                cur = (li == i)
                text = slice_disp(sheet.lines[li].main or "…", cols - 2)
                if cur:
                    return self.theme.paint(f"›{text}", "byellow", "bold")
                return self.theme.paint(f" {text}", "gray")
        return ""

    def _prompt_line(self, cols: int) -> str:
        if self.app.secret_active:
            label = self.app.secret_label or "请输入："
            if self.app.secret_mask:
                echo = "●" * len(self.app.secret_buf)
            else:
                echo = self.app.secret_buf
            line = self.theme.paint(label, "byellow") + echo
            return ansi_cut(line, cols - 1) if ansi_width(line) >= cols else line
        prompt = self.theme.paint("> ", "bgreen", "bold")
        text = self.input_text
        before, at, after = text[:self.cursor], text[self.cursor:self.cursor + 1], text[self.cursor + 1:]
        body = before + (self.theme.paint(at, "reverse") if at else "") + after
        line = prompt + body
        # 保留行末一个空格，避免光标换行
        if ansi_width(line) >= cols:
            line = ansi_cut(line, cols - 1)
        return line

    def _cursor_col(self) -> int:
        if self.app.secret_active:
            label = self.app.secret_label or "请输入："
            return min(self.cols, disp_width(label) + len(self.app.secret_buf) + 1)
        base = 2 + disp_width(self.input_text[:self.cursor])
        return min(self.cols, base + 1)

    def render(self, force: bool = False) -> None:
        if not self.app.running:
            return
        app = self.app
        if (app.browse_pending is not None and not app.qr_active
                and not app.cm_active):
            # 有超长输出待展示：进入全屏浏览并定位到该次输出的开头
            offset = int(clamp(app.browse_pending or 0, 0,
                               max(0, len(app.messages) - 1)))
            app.browse_pending = None
            app.browse = True
            app.browse_offset = offset
        try:
            frame = self.build_frame()
        except (OSError, ValueError):
            self.app.running = False
            return
        if force or frame != self.prev_frame:
            out: List[str] = []
            full = force or not self.prev_frame or len(frame) != len(self.prev_frame)
            if full:
                out.append("\x1b[2J")
                for i, line in enumerate(frame):
                    out.append(f"\x1b[{i + 1};1H\x1b[2K{line}")
            else:
                for i, (new, old) in enumerate(zip(frame, self.prev_frame)):
                    if new != old:
                        out.append(f"\x1b[{i + 1};1H\x1b[2K{new}")
            out.append(f"\x1b[{len(frame)};{self._cursor_col()}H\x1b[?25h")
            self._write("".join(out))
            self.prev_frame = frame

    # -- 主循环 --------------------------------------------------------
    def run(self) -> None:
        self._enter()
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        try:
            self.app.startup()
            self.render(force=True)
            while self.app.running:
                events: List[KeyEvent] = []
                try:
                    events = self.key_queue.get(timeout=self.TICK)
                except queue.Empty:
                    pass
                while True:                     # 一次循环内清空按键队列
                    try:
                        events += self.key_queue.get_nowait()
                    except queue.Empty:
                        break
                if events:
                    self.handle_events(events)
                    if not self.app.running:
                        break
                self.app.pump_events()
                self.render()
        except KeyboardInterrupt:
            self.app.info("（Ctrl-C）输入 quit 退出")
        finally:
            try:
                self.app.shutdown()
            finally:
                self._leave()


# --------------------------------------------------------------------------
# 纯文本模式（管道 / 脚本 / 无 TTY 环境）
# --------------------------------------------------------------------------

class PlainUI:
    def __init__(self, app: Application):
        self.app = app
        if not sys.stdout.isatty():
            app.theme.enable(False)
        self._printed = 0
        self._lyric_idx = -2
        self._last_progress = 0.0

    def _flush_messages(self) -> None:
        msgs = self.app.messages
        for line in msgs[self._printed:]:
            print(_CSI_RE.sub("", line))
        self._printed = len(msgs)

    def _tick(self) -> None:
        self.app.pump_events()
        self._flush_messages()
        engine = self.app.engine
        if engine.state in ("playing", "paused"):
            idx = engine.lyric_index()
            if idx != self._lyric_idx and idx >= 0 and engine.lyric.lines:
                self._lyric_idx = idx
                line = engine.lyric.lines[idx]
                text = line.main
                if line.trans and self.app.cfg.get("translation", True):
                    text = f"{text} / {line.trans}"
                print(f"♪ {text}")
                sys.stdout.flush()
            now = time.time()
            if now - self._last_progress >= 15:
                self._last_progress = now
                print(f"  {engine.status_icon()} {fmt_time(engine.position())}"
                      f"/{fmt_time(engine.duration)} "
                      f"({int(engine.progress() * 100)}%)")
                sys.stdout.flush()

    def run(self) -> None:
        app = self.app
        app.startup()
        self._flush_messages()
        eof = False
        idle_ticks = 0
        try:
            while app.running:
                if not eof:
                    try:
                        ready, _, _ = select.select([sys.stdin], [], [], 0.5)
                    except (OSError, ValueError):
                        ready = []
                        eof = True
                    if ready:
                        line = sys.stdin.readline()
                        if line == "":
                            eof = True
                            eng = app.engine
                            if not sys.stdin.isatty() and eng.queue:
                                # 脚本模式：把队列改成“顺序播完即停”，保证进程能自然退出
                                eng.mode = "seq"
                                if eng.index > 0:
                                    eng.queue = eng.queue[eng.index:]
                                    eng.index = 0
                                self.app.info(
                                    "（输入结束：按顺序播放一遍队列后自动退出，"
                                    "Ctrl-C 可立即退出）")
                            else:
                                self.app.info(
                                    "（输入结束，播放仍在继续，Ctrl-C 退出）")
                        else:
                            self._flush_messages()
                            app.execute(line.strip())
                            self._flush_messages()
                            if not app.running:
                                break
                else:
                    time.sleep(0.35)
                self._tick()
                # 输入结束后：若播放自然停稳（顺序播放到尾 / 无队列）且没有
                # 后台任务（例如 download），即可退出
                if eof:
                    if app.engine.state == "idle" and not app.has_pending_jobs():
                        idle_ticks += 1
                        if idle_ticks >= 3:
                            break
                    else:
                        idle_ticks = 0
        except KeyboardInterrupt:
            app.info("已中断")
        finally:
            self._flush_messages()
            app.shutdown()


# --------------------------------------------------------------------------
# 下载器：把歌曲保存成可直接播放的本地文件（NCM 自动解密）
# --------------------------------------------------------------------------

class DownloadManager:
    """下载单曲 / 歌单，负责格式识别、NCM 解密与文件命名。"""

    def __init__(self, app: "Application", directory: str,
                 quality: str = "exhigh"):
        self.app = app
        self.api = app.api
        self.directory = directory
        self.quality = quality
        self.success = 0
        self.skipped = 0
        self.failed = 0
        self.saved: List[str] = []

    # -- 文件命名 ------------------------------------------------------
    def _base_name(self, song: Song) -> str:
        artists = "、".join(a for a in song.artists if a)
        if artists:
            name = f"{artists} - {song.name}"
        else:
            name = song.name or f"歌曲 {song.id}"
        return sanitize_filename(name)

    def _find_existing(self, song: Song) -> Optional[str]:
        """按“歌手 - 歌名.扩展名”查找已下载的同名文件。"""
        base = self._base_name(song)
        for ext in AUDIO_EXTENSIONS:
            path = os.path.join(self.directory, f"{base}.{ext}")
            try:
                if os.path.isfile(path) and os.path.getsize(path) > 4096:
                    return path
            except OSError:
                pass
        return None

    def _target_path(self, song: Song, ext: str) -> str:
        ext = normalize_audio_format(ext) or "mp3"
        base = self._base_name(song)
        path = os.path.join(self.directory, f"{base}.{ext}")
        if not os.path.exists(path):
            return path
        try:
            if os.path.getsize(path) <= 4096:      # 覆盖上次失败的残缺文件
                return path
        except OSError:
            return path
        for i in range(2, 1000):
            cand = os.path.join(self.directory, f"{base} ({i}).{ext}")
            if not os.path.exists(cand):
                return cand
        raise DownloadError("同名文件过多，无法生成目标文件名")

    # -- 下载 ----------------------------------------------------------
    def download(self, song: Song, index: int = 0,
                 total: int = 0) -> Optional[str]:
        """下载一首歌；已存在则跳过。失败抛 DownloadError。"""
        prefix = f"[{index}/{total}] " if total > 1 else ""
        existing = self._find_existing(song)
        if existing:
            self.skipped += 1
            self.app.info(f"{prefix}已存在，跳过：{os.path.basename(existing)}")
            return existing

        resolver = getattr(self.api, "resolve_download_info", None)
        info = resolver(song, self.quality) if callable(resolver) else {}
        if not info or not info.get("url"):
            # 兼容只实现播放地址解析的客户端（老版本 / 自定义后端）
            try:
                info = self.api.resolve_song_url(song)
            except Exception:
                info = {}
        if not info or not info.get("url"):
            raise DownloadError("没有可用的下载地址（版权 / VIP / 地区限制）")

        trial_end = float(info.get("trial") or 0)
        trial_info = info.get("freeTrialInfo")
        if isinstance(trial_info, dict) and trial_info.get("end"):
            trial_end = max(trial_end, float(trial_info.get("end") or 0))
        if trial_end and song.duration_s and trial_end + 1 < song.duration_s:
            self.app.warn(f"{prefix}该歌曲仅能下载试听片段（{int(trial_end)} 秒）")

        self.app.busy = f"{prefix}下载 {song.name}…"
        fd, temp_path = tempfile.mkstemp(prefix=".ncm-dl-", suffix=".part",
                                         dir=self.directory)
        os.close(fd)
        size = 0
        try:
            resp = self.api.open_url(info["url"], timeout=30)
            try:
                try:
                    total_hint = int(resp.headers.get("Content-Length") or 0)
                except Exception:
                    total_hint = 0
                if total_hint > MAX_DOWNLOAD_BYTES:
                    raise DownloadError("文件超过下载上限")
                last_update = 0.0
                with open(temp_path, "wb") as fh:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise DownloadError("文件超过下载上限")
                        fh.write(chunk)
                        now = time.time()
                        if now - last_update >= 0.5:
                            last_update = now
                            pct = (f" {int(size * 100 / total_hint)}%"
                                   if total_hint else "")
                            self.app.busy = f"{prefix}下载 {song.name}{pct}"
            finally:
                resp.close()
            if size < 1024:
                raise DownloadError("下载内容过短")
            target, _meta = self._commit(song, temp_path, info)
            temp_path = ""              # 已改名，不需要再清理
        except DownloadError:
            raise
        except urllib.error.HTTPError as exc:
            raise DownloadError(f"音乐服务器返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DownloadError(f"网络错误：{exc}") from exc
        except OSError as exc:
            raise DownloadError(f"写入文件失败：{exc}") from exc
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        self.success += 1
        self.saved.append(target)
        self.app.ok(f"{prefix}已保存：{target}"
                    f"（{human_size(os.path.getsize(target))}）")
        return target

    def _commit(self, song: Song, temp_path: str,
                info: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """检测 NCM 并解密，按真实音频格式落盘。"""
        with open(temp_path, "rb") as fh:
            head = fh.read(16)
        if head.startswith(NCM_MAGIC):
            dec_path = temp_path + ".dec"
            try:
                meta = ncm_extract(temp_path, dec_path)
                with open(dec_path, "rb") as fh:
                    dec_head = fh.read(16)
                fmt = (audio_format_from_head(dec_head)
                       or normalize_audio_format(meta.get("format"))
                       or download_format_hint(info) or "mp3")
                target = self._target_path(song, fmt)
                os.replace(dec_path, target)
            finally:
                if os.path.exists(dec_path):
                    try:
                        os.remove(dec_path)
                    except OSError:
                        pass
            self.app.info(
                f"检测到 NCM 加密格式，已解密为 {fmt.upper()}："
                f"{os.path.basename(target)}")
            return target, meta

        fmt = (audio_format_from_head(head)
               or download_format_hint(info) or "mp3")
        target = self._target_path(song, fmt)
        os.replace(temp_path, target)
        return target, {}

    # -- 批量下载 ------------------------------------------------------
    def download_many(self, songs: List[Song], title: str = "") -> None:
        songs = [s for s in songs if s]
        if not songs:
            self.app.warn("没有可下载的歌曲")
            return
        ensure_writable_dir(self.directory)
        label = f"「{title}」" if title else ""
        self.app.info(f"开始下载{label}：共 {len(songs)} 首 → {self.directory}")
        total = len(songs)
        for i, song in enumerate(songs, 1):
            try:
                self.download(song, i, total)
            except DownloadError as exc:
                self.failed += 1
                self.app.error(f"[{i}/{total}] 下载失败：{song.display()}（{exc}）")
            except ApiError as exc:
                self.failed += 1
                self.app.error(f"[{i}/{total}] 下载失败：{song.display()}"
                               f"（{exc.message}）")
            except Exception as exc:          # 单曲失败不中断整张歌单
                self.failed += 1
                self.app.error(f"[{i}/{total}] 下载失败：{song.display()}（{exc}）")
        if total > 1:
            self.app.ok(f"下载结束：成功 {self.success}，跳过 {self.skipped}，"
                        f"失败 {self.failed}（目录：{self.directory}）")


# --------------------------------------------------------------------------
# 命令系统
# --------------------------------------------------------------------------

COMMANDS: Dict[str, Callable[..., None]] = {}
ALIASES: Dict[str, str] = {}


def command(name: str, desc: str, usage: str = "", aliases: Sequence[str] = (),
            subcmds: Sequence[str] = ()):
    """注册一条交互命令。"""
    def deco(fn: Callable[..., None]) -> Callable[..., None]:
        fn.desc = desc            # type: ignore[attr-defined]
        fn.usage = usage          # type: ignore[attr-defined]
        fn.subcmds = list(subcmds)  # type: ignore[attr-defined]
        COMMANDS[name] = fn
        for a in aliases:
            ALIASES[a] = name
        return fn
    return deco


SONG_KINDS = {"song", "tracks", "lyric"}
_KIND_CN = {"song": "单曲", "tracks": "曲目", "artist": "歌手",
            "album": "专辑", "playlist": "歌单"}
_KIND_ALIAS = {
    "song": "song", "单曲": "song", "s": "song",
    "artist": "artist", "歌手": "artist", "艺人": "artist",
    "album": "album", "专辑": "album",
    "playlist": "playlist", "歌单": "playlist",
    "lyric": "lyric", "歌词": "lyric",
}


def out_width(app: Application) -> int:
    return int(getattr(app, "output_width", 100) or 100)


def song_context(app: Application) -> Tuple[List[Song], bool]:
    """可播放上下文：优先搜索/歌单结果，其次播放队列。"""
    res = app.results
    if res is not None and res.kind in SONG_KINDS and res.items:
        return list(res.items), True
    return list(app.engine.queue), False


def print_songs(app: Application, songs: List[Song], start_index: int = 1,
                header: str = "", footer: str = "", limit: int = 0) -> None:
    """列出歌曲（默认全部；超出窗口时界面会自动切入全屏浏览）。"""
    width = out_width(app)
    if header:
        app.heading(header)
    shown = songs if limit <= 0 else songs[:limit]
    rows = [s.row(i + start_index, width) for i, s in enumerate(shown)]
    app.table(rows, footer=footer)


def print_playlists(app: Application, items: List[Playlist], header: str = "",
                    footer: str = "") -> None:
    """列出歌单（默认全部；超出窗口时界面会自动切入全屏浏览）。"""
    width = out_width(app)
    if header:
        app.heading(header)
    rows = [p.row(i + 1, width) for i, p in enumerate(items)]
    app.table(rows, footer=footer)


def _parse_pos(token: str) -> Optional[int]:
    token = token.strip().rstrip(".")
    if token.isdigit():
        return int(token)
    return None


def _big_id(token: str) -> bool:
    return token.isdigit() and int(token) > 100000


def resolve_playlist_id(app: Application, token: str) -> int:
    """把“结果序号 / 歌单 ID”解析成歌单 id。"""
    if _big_id(token):
        return int(token)
    idx = _parse_pos(token)
    res = app.results
    if idx is not None and res is not None and res.kind == "playlist" \
            and 1 <= idx <= len(res.items):
        return int(res.items[idx - 1].id)
    if idx is not None and not _big_id(token) and len(token) >= 5:
        return int(token)
    raise ApiError(f"无法识别歌单：{token}（可用 pl <序号> 或 pl <歌单ID>）")


def resolve_artist_id(app: Application, token: str) -> int:
    if _big_id(token):
        return int(token)
    idx = _parse_pos(token)
    res = app.results
    if idx is not None and res is not None and res.kind == "artist" \
            and 1 <= idx <= len(res.items):
        return int(res.items[idx - 1].id)
    if idx is not None and len(token) >= 4:
        return int(token)
    raise ApiError(f"无法识别歌手：{token}")


def resolve_album_id(app: Application, token: str) -> int:
    if _big_id(token):
        return int(token)
    idx = _parse_pos(token)
    res = app.results
    if idx is not None and res is not None and res.kind == "album" \
            and 1 <= idx <= len(res.items):
        return int(res.items[idx - 1].id)
    if idx is not None and len(token) >= 4:
        return int(token)
    raise ApiError(f"无法识别专辑：{token}")


# -- 帮助 ---------------------------------------------------------------

@command("help", "显示帮助", "help [命令]", aliases=("h", "?", "帮助"))
def cmd_help(app: Application, args: List[str], raw: str) -> None:
    if args:
        name = args[0].lower()
        name = ALIASES.get(name, name)
        fn = COMMANDS.get(name)
        if not fn:
            app.error(f"没有这条命令：{args[0]}")
            return
        app.heading(f"{name} — {getattr(fn, 'desc', '')}")
        if getattr(fn, "usage", ""):
            app.println("用法：" + fn.usage, "bcyan")
        subcmds = getattr(fn, "subcmds", [])
        if subcmds:
            app.println("子命令：" + "、".join(subcmds), "gray")
        return

    groups = [
        ("播放控制", [
            "search", "n", "play", "pause", "next", "prev", "stop",
            "seek", "vol", "mode", "retry", "list",
        ]),
        ("发现音乐", ["top", "pl", "artist", "album", "daily", "fm", "rec", "mine", "likes"]),
        ("歌词与信息", ["info", "lyric", "comment"]),
        ("下载", ["download"]),
        ("账号", ["login", "logout", "me"]),
        ("界面与其它", ["clear", "view", "theme", "backend", "quality", "about",
                    "quit"]),
    ]
    app.heading(f"{APP_NAME} v{APP_VERSION} · 命令一览")
    seen = set()
    for title, names in groups:
        app.println(f"── {title} " + "─" * max(0, out_width(app) - 4 - disp_width(title)),
                    "gray")
        for name in names:
            fn = COMMANDS.get(name)
            if not fn or name in seen:
                continue
            seen.add(name)
            alias = [a for a, t in ALIASES.items() if t == name]
            left = name + (f"/{alias[0]}" if alias else "")
            app.println(f"  {left:<14}{getattr(fn, 'desc', '')}", "white")
    app.blank()
    app.println("  细节：help <命令>；补全：Tab；PgUp/Ctrl-B 全屏回看输出（q 返回）；"
                "退出：quit", "gray")
    app.println("  例：search 海阔天空 → play 2 → lyric → list", "gray")


@command("about", "关于本程序", aliases=("version",))
def cmd_about(app: Application, args: List[str], raw: str) -> None:
    app.heading(f"{APP_NAME} v{APP_VERSION}")
    app.println("纯 Python 标准库实现 · 无第三方依赖 · 终端交互", "white")
    app.println(f"配置目录：{app.cfg_dir}", "gray")
    app.println(f"下载目录：{app.download_dir}", "gray")
    app.println(f"播放内核：{app.engine.backend_info()}", "gray")
    app.println(f"接口地址：{API_HOST}", "gray")


# -- 搜索 ---------------------------------------------------------------

@command("search", "搜索（单曲/歌手/专辑/歌单/歌词）",
         "search <关键词> [song|artist|album|playlist] [页码]",
         aliases=("s", "搜索"), subcmds=["song", "artist", "album", "playlist", "lyric"])
def cmd_search(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.error("用法：search <关键词> [类型] [页码]")
        return
    kind = "song"
    page = 1
    words: List[str] = []
    for tok in args:
        low = tok.lower()
        if low in _KIND_ALIAS:
            kind = _KIND_ALIAS[low]
        elif tok.isdigit() and int(tok) <= 100:
            page = max(1, int(tok))
        else:
            words.append(tok)
    keyword = " ".join(words).strip()
    if not keyword:
        app.error("关键词不能为空")
        return
    limit = int(app.cfg.get("page_size", 30))
    app.busy = f"搜索「{keyword}」…"
    res = app.api.search(keyword, kind, limit=limit, offset=(page - 1) * limit)
    app.results = res
    width = out_width(app)
    if not res.items:
        app.warn(f"没有找到与「{keyword}」相关的内容")
        return
    if res.kind in SONG_KINDS:
        print_songs(app, res.items, 1,
                    header=f"搜索「{keyword}」· 单曲 · 第 {res.page} 页（共 {res.total} 条）",
                    footer="输入 play <序号> 播放，play all 播放全部，more 下一页")
    else:
        print_playlists(app, res.items,
                        header=f"搜索「{keyword}」· {_KIND_CN.get(res.kind, res.kind)}"
                               f" · 第 {res.page} 页（共 {res.total} 条）",
                        footer="输入 pl <序号> 打开，more 下一页")


@command("more", "搜索结果翻下一页", "more", aliases=("nextpage",))
def cmd_more(app: Application, args: List[str], raw: str) -> None:
    res = app.results
    if res is None:
        app.error("当前没有可翻页的结果")
        return
    if not res.has_more:
        app.warn("已经是最后一页")
        return
    kind = "song" if res.kind in SONG_KINDS else res.kind
    nxt = app.api.search(res.query, kind, limit=res.page_size,
                         offset=res.offset + res.page_size)
    nxt.query = res.query
    app.results = nxt
    if not nxt.items:
        app.warn("没有更多结果了")
        return
    if nxt.kind in SONG_KINDS:
        print_songs(app, nxt.items, 1,
                    header=f"「{nxt.query}」第 {nxt.page} 页（共 {nxt.total} 条）",
                    footer="play <序号> 播放 · more 翻页")
    else:
        print_playlists(app, nxt.items,
                        header=f"「{nxt.query}」第 {nxt.page} 页（共 {nxt.total} 条）")


# -- 播放控制 -----------------------------------------------------------

@command("play", "播放（序号/全部/歌曲ID/关键词）",
         "play [序号 | all | 歌曲ID | 关键词]", aliases=("p", "播放"))
def cmd_play(app: Application, args: List[str], raw: str) -> None:
    eng = app.engine
    if not args:
        if eng.state == "paused":
            app.println(eng.toggle_pause())
            return
        if eng.queue:
            eng.play_index(eng.index if eng.index >= 0 else 0)
            return
        songs, _ = song_context(app)
        if songs:
            eng.set_queue(songs, start=0, source="results")
            return
        app.error("还没有可播放的歌曲，先 search 一下吧")
        return

    token = args[0]
    if token.lower() in ("all", "全部", "*"):
        songs, from_results = song_context(app)
        if not songs:
            app.error("没有可播放的歌曲")
            return
        eng.set_queue(songs, start=0,
                      source="results" if from_results else eng.queue_source)
        app.ok(f"已载入 {len(songs)} 首到播放队列")
        return
    if _parse_pos(token) is not None and not _big_id(token):
        idx = int(token)
        songs, from_results = song_context(app)
        if not songs:
            app.error("没有可播放的歌曲")
            return
        if not (1 <= idx <= len(songs)):
            app.error(f"序号超出范围（1-{len(songs)}）")
            return
        if from_results:
            eng.set_queue(songs, start=idx - 1, source="results")
        else:
            eng.play_index(idx - 1)
        return
    if _big_id(token):
        songs = app.api.song_detail([int(token)])
        if not songs:
            app.error(f"找不到歌曲 {token}")
            return
        eng.play_song(songs[0], source="id")
        return
    # 关键词：直接搜索并播放第一首
    keyword = " ".join(args)
    res = app.api.search(keyword, "song", limit=int(app.cfg.get("page_size", 30)))
    if not res.items:
        app.error(f"没有搜到「{keyword}」")
        return
    app.results = res
    app.info(f"为你搜到 {len(res.items)} 首「{keyword}」，开始播放第一首")
    print_songs(app, res.items[:8], 1, header="搜索结果（前 8 条）")
    eng.set_queue(res.items, start=0, source="search")


@command("pause", "暂停 / 继续", aliases=("resume", "toggle", "空格"))
def cmd_pause(app: Application, args: List[str], raw: str) -> None:
    app.println(app.engine.toggle_pause())


@command("next", "下一首", aliases=("n", "skip"))
def cmd_next(app: Application, args: List[str], raw: str) -> None:
    if not app.engine.queue:
        app.error("播放队列是空的")
        return
    app.engine.next()


@command("prev", "上一首", aliases=("previous", "b"))
def cmd_prev(app: Application, args: List[str], raw: str) -> None:
    if not app.engine.queue:
        app.error("播放队列是空的")
        return
    app.engine.prev()


@command("stop", "停止播放", aliases=("停止",))
def cmd_stop(app: Application, args: List[str], raw: str) -> None:
    app.engine.stop()


@command("retry", "重新加载当前歌曲", aliases=("reload",))
def cmd_retry(app: Application, args: List[str], raw: str) -> None:
    if not app.engine.current:
        app.error("当前没有歌曲")
        return
    app.engine.retry()


# -- 下载 ---------------------------------------------------------------

def _download_dir_command(app: Application, rest: List[str]) -> None:
    """查看 / 修改下载目录。"""
    if not rest:
        custom = bool(str(app.cfg.get("download_dir", "") or "").strip())
        app.println(f"下载目录：{app.download_dir}"
                    f"（{'自定义' if custom else '默认'}）")
        app.println(f"下载音质：{app.cfg.get('quality', 'exhigh')}"
                    "（用 quality 命令修改）", "gray")
        app.println("修改：download dir <路径>；恢复默认：download dir default",
                    "gray")
        return
    raw = " ".join(rest).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1].strip()
    if raw.lower() in ("default", "reset", "默认", "恢复默认"):
        default = default_download_dir()
        try:
            ensure_writable_dir(default)
        except OSError as exc:
            app.error(f"默认下载目录不可用：{default}（{exc}）")
            return
        app.cfg.set("download_dir", "")
        app.ok(f"下载目录已恢复默认：{default}")
        return
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if os.path.exists(path) and not os.path.isdir(path):
        app.error(f"不是目录：{path}")
        return
    try:
        ensure_writable_dir(path)
    except OSError as exc:
        app.error(f"无法使用该目录：{path}（{exc}）")
        return
    app.cfg.set("download_dir", path)
    app.ok(f"下载目录已设为：{path}")


def _download_playlist_command(app: Application, manager: DownloadManager,
                               rest: List[str]) -> None:
    """下载歌单：download pl [序号|歌单ID]。"""
    if rest:
        pid = resolve_playlist_id(app, rest[0])
        app.busy = "读取歌单…"

        def job() -> None:
            meta, songs = app.api.playlist_tracks(pid)
            name = str(meta.get("name") or f"歌单 {pid}")
            manager.download_many(songs, name)

        app.run_job("下载歌单", job)
        return

    res = app.results
    if (res is not None and res.kind in SONG_KINDS and res.items
            and app.pl_meta and res.query == app.pl_meta[1]):
        songs = list(res.items)
        name = app.pl_meta[1]
        app.busy = f"下载歌单「{name}」…"
        app.run_job("下载歌单",
                    lambda: manager.download_many(songs, name))
        return

    if app.pl_meta and (res is None or res.query == app.pl_meta[1]):
        pid, name = app.pl_meta
        app.busy = f"下载歌单「{name}」…"

        def job() -> None:
            meta, songs = app.api.playlist_tracks(pid)
            manager.download_many(songs, str(meta.get("name") or name))

        app.run_job("下载歌单", job)
        return

    source = str(app.engine.queue_source or "")
    if source.startswith("playlist:"):
        try:
            pid = int(source.split(":", 1)[1])
        except ValueError:
            pid = 0
        if pid:
            app.busy = "下载当前歌单…"
            app.run_job("下载歌单",
                        lambda: _download_playlist_by_id(app, manager, pid))
            return
    app.error("当前没有打开歌单：先 pl <序号|歌单ID>，"
              "或直接 download pl <序号|歌单ID>")


def _download_playlist_by_id(app: Application, manager: DownloadManager,
                             pid: int) -> None:
    meta, songs = app.api.playlist_tracks(pid)
    name = str(meta.get("name") or f"歌单 {pid}")
    manager.download_many(songs, name)


@command("download", "下载当前歌曲 / 歌单（自动转为可播放格式）",
         "download [当前 | 序号 | all | pl <序号|歌单ID> | dir [路径]]",
         aliases=("dl", "下载"),
         subcmds=["current", "all", "playlist", "dir"])
def cmd_download(app: Application, args: List[str], raw: str) -> None:
    if args and args[0].lower() in ("dir", "path", "folder", "directory",
                                    "目录", "文件夹", "--dir", "-d"):
        _download_dir_command(app, args[1:])
        return

    directory = app.download_dir
    try:
        ensure_writable_dir(directory)
    except OSError as exc:
        app.error(f"下载目录不可写：{directory}（{exc}）")
        app.info("可用 download dir <路径> 指定其它目录")
        return
    quality = str(app.cfg.get("quality", "exhigh"))
    manager = DownloadManager(app, directory, quality)

    if not args:
        song = app.engine.current
        if not song:
            app.error("当前没有正在播放的歌曲（先 play ...）")
            return
        app.busy = f"下载 {song.name}…"
        app.run_job("下载",
                    lambda: manager.download_many([song], song.display()))
        return

    token = args[0].lower()
    if token in ("current", "song", "now", "当前", "本曲"):
        return cmd_download(app, [], raw)

    if token in ("all", "全部", "*", "queue", "list", "队列"):
        res = app.results
        if res is not None and res.kind == "playlist" and res.items:
            app.error("当前是歌单列表：请用 download pl <序号|歌单ID> "
                      "下载整个歌单")
            return
        songs, from_results = song_context(app)
        if not songs:
            app.error("当前列表和播放队列都是空的")
            return
        title = (str(res.query) if from_results and res and res.query
                 else str(app.engine.queue_source or "当前列表"))
        app.busy = f"下载 {len(songs)} 首…"
        app.run_job("下载",
                    lambda: manager.download_many(list(songs), title))
        return

    if token in ("pl", "playlist", "playlists", "歌单"):
        _download_playlist_command(app, manager, args[1:])
        return

    if _big_id(args[0]):
        song_id = int(args[0])
        app.busy = "下载歌曲…"

        def job_by_id() -> None:
            songs = app.api.song_detail([song_id])
            if not songs:
                app.error(f"找不到歌曲 {song_id}")
                return
            manager.download_many([songs[0]], songs[0].display())

        app.run_job("下载", job_by_id)
        return

    idx = _parse_pos(args[0])
    if idx is not None:
        songs, _from_results = song_context(app)
        if not songs:
            app.error("当前没有可下载的列表，先 search 或 play")
            return
        if not 1 <= idx <= len(songs):
            app.error(f"序号超出范围（1-{len(songs)}）")
            return
        song = songs[idx - 1]
        app.busy = f"下载 {song.name}…"
        app.run_job("下载",
                    lambda: manager.download_many([song], song.display()))
        return

    # 关键词：搜索并下载第一首
    keyword = " ".join(args).strip()
    if not keyword:
        app.error("用法：download [当前 | 序号 | all | pl <序号|歌单ID> | "
                  "dir [路径]]")
        return
    app.busy = f"搜索并下载「{keyword}」…"

    def job_by_keyword() -> None:
        res = app.api.search(keyword, "song", limit=1)
        if not res.items:
            app.error(f"没有搜到「{keyword}」")
            return
        manager.download_many([res.items[0]], res.items[0].display())

    app.run_job("下载", job_by_keyword)


@command("seek", "跳转进度", "seek <+秒 | -秒 | mm:ss | 百分比>", aliases=("跳转",))
def cmd_seek(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.error("用法：seek 90 / seek -15 / seek 1:30 / seek 50%")
        return
    token = args[0]
    eng = app.engine
    if token.endswith("%"):
        try:
            pct = float(token[:-1]) / 100.0
        except ValueError:
            app.error("百分比格式不对")
            return
        app.println(eng.seek_to(eng.duration * pct))
        return
    if ":" in token:
        parts = token.split(":")
        try:
            secs = int(parts[0]) * 60 + int(parts[-1])
        except ValueError:
            app.error("时间格式应为 mm:ss")
            return
        app.println(eng.seek_to(float(secs)))
        return
    try:
        delta = float(token)
    except ValueError:
        app.error("请输入秒数，例如 seek 90 或 seek -15")
        return
    if token[0] in "+-":
        app.println(eng.seek_by(delta))
    else:
        app.println(eng.seek_to(delta))


@command("vol", "调节音量", "vol [0-100 | +N | -N]", aliases=("volume", "音量"))
def cmd_vol(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.println(f"当前音量 {app.engine.volume}%")
        return
    token = args[0]
    try:
        if token[0] in "+-":
            value = app.engine.volume + int(token)
        else:
            value = int(token)
    except (ValueError, IndexError):
        app.error("用法：vol 80 / vol +5 / vol -10")
        return
    app.println(app.engine.set_volume(value))


@command("mode", "播放模式", "mode [list|one|seq|shuffle]",
         aliases=("播放模式",), subcmds=["list", "one", "seq", "shuffle"])
def cmd_mode(app: Application, args: List[str], raw: str) -> None:
    mapping = {"列表": "list", "循环": "list", "单曲": "one", "顺序": "seq",
               "随机": "shuffle", "shuffle": "shuffle"}
    if not args:
        app.println("当前：" + PLAY_MODE_NAMES.get(app.engine.mode, app.engine.mode)
                    + "（mode list/one/seq/shuffle）")
        return
    token = mapping.get(args[0], args[0])
    app.println(app.engine.set_mode(token))


@command("list", "查看播放队列（全部歌曲）", "list [clear]",
         aliases=("queue", "ls", "播放列表"), subcmds=["clear"])
def cmd_list(app: Application, args: List[str], raw: str) -> None:
    eng = app.engine
    if args and args[0].lower() in ("clear", "c", "清空"):
        with eng._op_lock:
            total = len(eng.queue)
            if total == 0:
                app.warn("播放队列已经是空的")
                return
            eng.queue = []
            eng.index = -1
            eng.history = []
        eng.stop()
        app.ok(f"已清空播放队列（原 {total} 首）并停止播放")
        return
    if not eng.queue:
        app.warn("播放队列是空的（play / pl play 会自动填充）")
        return
    width = out_width(app)
    app.heading(f"播放队列（{len(eng.queue)} 首 · 模式 "
                f"{PLAY_MODE_NAMES.get(eng.mode, eng.mode)}"
                f" · 来源 {eng.queue_source or '自定义'}）")
    rows = []
    for i, item in enumerate(eng.queue):
        mark = "▶" if i == eng.index else " "
        line = item.row(i + 1, width)
        if line.startswith(" "):
            line = mark + line[1:]          # 行首空格换成播放标记，序号保留
        else:
            line = f"{mark}{line}"
        rows.append(line)
    app.table(rows)


# -- 发现音乐 -----------------------------------------------------------

@command("top", "排行榜", "top [序号]", aliases=("toplist", "排行榜"))
def cmd_top(app: Application, args: List[str], raw: str) -> None:
    items = app.api.toplists()
    if args:
        idx = _parse_pos(args[0])
        if idx is None or not (1 <= idx <= len(items)):
            app.error(f"序号应在 1-{len(items)}")
            return
        _open_playlist(app, items[idx - 1].id, args[1:] or [])
        return
    app.results = ResultSet(kind="playlist", items=items, total=len(items),
                            query="toplist")
    print_playlists(app, items, header=f"排行榜（{len(items)} 个）",
                    footer="pl <序号> 打开 · top <序号> 直接查看曲目")


@command("pl", "歌单（查看/播放）", "pl <序号|ID> [play]", aliases=("playlist", "歌单"))
def cmd_pl(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.error("用法：pl <序号|歌单ID> [play]　可先用 top / rec / mine 找歌单")
        return
    pid = resolve_playlist_id(app, args[0])
    _open_playlist(app, pid, args[1:])


def _open_playlist(app: Application, pid: int, extra: List[str]) -> None:
    app.busy = "读取歌单…"
    meta, songs = app.api.playlist_tracks(pid)
    name = str(meta.get("name") or f"歌单 {pid}")
    app.results = ResultSet(kind="tracks", items=songs, total=len(songs),
                            query=name)
    app.pl_meta = (pid, name)
    play_now = any(x in ("play", "播放", "go") for x in extra)
    header = f"{name} · {len(songs)} 首"
    freq = meta.get("updateFrequency")
    if freq:
        header += f" · {freq}"
    creator = (meta.get("creator") or {}).get("nickname")
    if creator:
        header += f" · by {creator}"
    print_songs(app, songs, 1, header=header,
                footer="play <序号>/play all 播放")
    if play_now and songs:
        app.engine.set_queue(songs, start=0, source=f"playlist:{pid}")
        app.ok(f"已开始播放「{name}」")


@command("artist", "歌手详情与热门歌曲", "artist <序号|ID>", aliases=("歌手",))
def cmd_artist(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.error("用法：artist <序号|歌手ID>（可先 search xx artist）")
        return
    aid = resolve_artist_id(app, args[0])
    info, songs = app.api.artist(aid)
    name = str(info.get("name") or "")
    app.heading(f"{name} · 热门歌曲")
    bits = []
    if info.get("musicSize"):
        bits.append(f"歌曲 {info['musicSize']}")
    if info.get("albumSize"):
        bits.append(f"专辑 {info['albumSize']}")
    if info.get("fansCount"):
        bits.append(f"粉丝 {info['fansCount']}")
    if bits:
        app.println(" · ".join(bits), "gray")
    brief = str(info.get("briefDesc") or "").strip()
    if brief:
        app.println(slice_disp(brief.replace("\n", " "), out_width(app)), "gray")
    app.results = ResultSet(kind="tracks", items=songs, total=len(songs),
                            query=name)
    print_songs(app, songs, 1, footer="play <序号> / play all 播放")


@command("album", "专辑详情", "album <序号|ID>", aliases=("专辑",))
def cmd_album(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.error("用法：album <序号|专辑ID>（可先 search xx album）")
        return
    alid = resolve_album_id(app, args[0])
    info, songs = app.api.album(alid)
    name = str(info.get("name") or "")
    artist = (info.get("artist") or {}).get("name", "")
    app.heading(f"{artist}《{name}》" if artist else name)
    if info.get("publishTime"):
        app.println(f"{len(songs)} 首 · 发行 {info.get('publishTime')}", "gray")
    app.results = ResultSet(kind="tracks", items=songs, total=len(songs),
                            query=name)
    print_songs(app, songs, 1, footer="play <序号> / play all 播放")


@command("daily", "每日推荐", "daily [pl]", aliases=("每日推荐",), subcmds=["pl"])
def cmd_daily(app: Application, args: List[str], raw: str) -> None:
    if args and args[0] in ("pl", "歌单"):
        items = app.api.daily_playlists()
        app.results = ResultSet(kind="playlist", items=items, total=len(items),
                                query="每日推荐歌单")
        print_playlists(app, items, header="每日推荐歌单",
                        footer="pl <序号> 打开")
        return
    songs = app.api.daily_songs()
    if not songs:
        app.warn("今天没有推荐内容（登录后更准）")
        return
    app.results = ResultSet(kind="tracks", items=songs, total=len(songs),
                            query="每日推荐")
    app.engine.set_queue(songs, start=0, source="daily")
    print_songs(app, songs, 1, header=f"每日推荐 · {len(songs)} 首（已开始播放）",
                footer="next 切歌 · list 查看队列")


@command("fm", "私人 FM", "fm [trash]", aliases=("私人fm",), subcmds=["trash"])
def cmd_fm(app: Application, args: List[str], raw: str) -> None:
    eng = app.engine
    if args and args[0] in ("trash", "垃圾桶", "delete"):
        if not eng.current:
            app.error("当前没有歌曲可丢进垃圾桶")
            return
        app.api.fm_trash(eng.current.id)
        app.ok(f"已丢弃：{eng.current.name}")
        eng.next()
        return
    songs = app.api.personal_fm()
    if not songs:
        app.error("私人 FM 需要登录后才能收听")
        return
    app.results = ResultSet(kind="tracks", items=songs, total=len(songs),
                            query="私人FM")
    eng.set_queue(songs, start=0, source="fm")
    print_songs(app, songs, 1, header=f"私人 FM（{len(songs)} 首，已开始播放）",
                footer="fm trash 丢弃当前歌曲")


@command("rec", "推荐歌单", aliases=("recommend", "推荐"))
def cmd_rec(app: Application, args: List[str], raw: str) -> None:
    items = app.api.personalized_playlists(16)
    app.results = ResultSet(kind="playlist", items=items, total=len(items),
                            query="推荐歌单")
    print_playlists(app, items, header="推荐歌单", footer="pl <序号> 打开")


@command("mine", "我的歌单（需登录）", aliases=("my", "我的"))
def cmd_mine(app: Application, args: List[str], raw: str) -> None:
    uid = _require_login(app)
    items = app.api.user_playlists(uid, limit=100)
    app.results = ResultSet(kind="playlist", items=items, total=len(items),
                            query="我的歌单")
    print_playlists(app, items, header=f"我的歌单（{len(items)} 个）",
                    footer="pl <序号> 打开，或 pl <序号> play 直接播放")


@command("likes", "我喜欢的音乐（需登录）", aliases=("红心", "love"))
def cmd_likes(app: Application, args: List[str], raw: str) -> None:
    uid = _require_login(app)
    ids = app.api.likelist(uid)
    if not ids:
        app.warn("还没有红心歌曲，用 like 给当前歌曲点个红心吧")
        return
    cap = 500                      # 超大歌单只取前 500 首，避免拖垮界面
    shown_ids = ids[:cap]
    songs = app.api.song_detail(shown_ids)
    app.results = ResultSet(kind="tracks", items=songs, total=len(ids),
                            query="我喜欢的音乐")
    suffix = f"，仅显示前 {cap} 首" if len(ids) > cap else ""
    print_songs(app, songs, 1, header=f"我喜欢的音乐（{len(ids)} 首{suffix}）",
                footer="play <序号> / play all 播放")


def _require_login(app: Application) -> int:
    if not app.api.logged_in:
        raise ApiError("该功能需要登录：输入 login qr 扫码登录")
    if app.user.get("userId"):
        return int(app.user["userId"])
    data = app.api.account()
    profile = data.get("profile") or {}
    app.user = profile
    uid = profile.get("userId")
    if not uid:
        raise ApiError("登录状态异常，请 logout 后重新登录")
    return int(uid)


# -- 歌词 / 信息 / 评论 ---------------------------------------------------

@command("info", "当前歌曲详情", aliases=("song", "歌曲信息"))
def cmd_info(app: Application, args: List[str], raw: str) -> None:
    song = app.engine.current
    if not song:
        app.error("当前没有播放的歌曲")
        return
    eng = app.engine
    app.heading(f"{song.name} - {song.artist}")
    rows = [
        f"专辑：{song.album_name}",
        f"时长：{fmt_time(song.duration_s)}　ID：{song.id}　MV：{song.mv_id or '无'}",
        f"音质：{eng.backend_name}　音量：{eng.volume}%　模式：{PLAY_MODE_NAMES.get(eng.mode, eng.mode)}",
        f"进度：{fmt_time(eng.position())} / {fmt_time(eng.duration)}"
        f"（{int(eng.progress() * 100)}%）",
    ]
    if song.fee:
        rows.append(f"版权：fee={song.fee}（{'VIP 专享' if song.fee == 1 else '付费/受限'}）")
    if song.alias:
        rows.append("别名：" + "、".join(song.alias))
    cached = eng._url_cache.get(song.id)
    if cached and cached.get("br"):
        note = "（试听片段）" if eng.duration and eng.duration + 1 < song.duration_s else ""
        rows.append(f"播放音质：{int(cached['br']) // 1000} kbps"
                    f"{'　缓存于本地' if eng._file_fallback else ''}{note}")
    app.table(rows)
    tr = eng.lyric
    if tr and not tr.empty():
        tags = []
        if tr.has_translation:
            tags.append("含翻译")
        if tr.has_roman:
            tags.append("含罗马音")
        app.println("歌词：" + ("、".join(tags) if tags else "有"), "gray")


@command("lyric", "歌词控制", "lyric [on|off|trans|roman|full|offset ±秒]",
         aliases=("lrc", "歌词"), subcmds=["on", "off", "trans", "roman", "full", "offset"])
def cmd_lyric(app: Application, args: List[str], raw: str) -> None:
    eng, cfg = app.engine, app.cfg
    if not args:
        tr = eng.lyric
        if tr is None or tr.empty():
            app.warn("当前没有可用歌词")
            return
        idx = eng.lyric_index()
        app.heading(f"歌词 · {eng.current.name if eng.current else ''}")
        start = max(0, idx - 8)
        for i in range(start, min(len(tr.lines), idx + 15)):
            ln = tr.lines[i]
            text = ln.main
            if tr.has_translation and cfg.get("translation", True) and ln.trans:
                text = f"{text}　{ln.trans}"
            if i == idx:
                app.println(f"› {text}", "byellow", "bold")
            else:
                app.println(f"  {text}", "gray")
        return
    key = args[0].lower()
    if key in ("on", "off"):
        app.cfg.set("translation", key == "on")
        app.println(f"歌词翻译显示：{'开' if key == 'on' else '关'}")
    elif key in ("trans", "翻译"):
        val = not bool(cfg.get("translation", True))
        cfg.set("translation", val)
        app.println(f"翻译显示：{'开' if val else '关'}")
    elif key in ("roman", "罗马音"):
        val = not bool(cfg.get("roman", False))
        cfg.set("roman", val)
        app.println(f"罗马音显示：{'开' if val else '关'}")
    elif key == "full":
        cmd_lyric(app, [], raw)
    elif key == "offset":
        if len(args) < 2:
            app.error("用法：lyric offset +0.5 / lyric offset -0.3")
            return
        try:
            delta = float(args[1])
        except ValueError:
            app.error("偏移量应为秒数，如 +0.5")
            return
        eng.lyric.offset += delta
        app.println(f"歌词偏移 {eng.lyric.offset:+.2f}s")
    else:
        app.error("用法：lyric [on|off|trans|roman|full|offset ±秒]")


# -- 评论页（全屏浏览 + 发表）----------------------------------------------

def comment_page_lines(app: Application) -> List[str]:
    """把评论页数据渲染成带样式的文本行（每条：标题行 + 折行内容 + 空行）。"""
    theme = app.theme
    width = max(30, app.output_width - 4)
    lines: List[str] = []
    for i, c in enumerate(app.cm_comments):
        user = ((c.get("user") or {}).get("nickname")) or "匿名"
        liked = int(c.get("likedCount") or 0)
        replies = int(c.get("replyCount") or 0)
        ts = int(c.get("time") or 0)
        try:
            date = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts / 1000)) \
                if ts else ""
        except Exception:
            date = ""
        loc = c.get("ipLocation")
        if isinstance(loc, dict):          # ipLocation 是 {ip, location} 结构
            ip = str(loc.get("location") or "").strip()
        else:
            ip = str(loc or "").strip()
        head = f"{i + 1:>3}. " + theme.paint(slice_disp(user, 24), "cyan", "bold") \
            + theme.paint(f" ♥{liked}", "magenta")
        if replies:
            head += theme.paint(f" 回复{replies}", "gray")
        if date:
            head += theme.paint(f"  {date}", "gray")
        if ip:
            head += theme.paint(f" 来自{slice_disp(ip, 10)}", "gray")
        lines.append(head)
        content = str(c.get("content") or "")
        for wrapped in wrap_display(content, width - 5):
            lines.append("     " + wrapped if wrapped else "")
        lines.append("")
    return lines


def load_comments(app: Application, reset: bool = False) -> None:
    """加载（或续加载/刷新）评论；reset=True 时热评置顶并重置偏移。"""
    song = app.cm_song
    if not song:
        return
    app.cm_loading = True
    offset = 0 if reset else app.cm_loaded
    try:
        res = app.api.comments(song.id, limit=app.cm_page, offset=offset)
    except ApiError as exc:
        app.cm_loading = False
        app.cm_status = f"加载失败：{exc.message}"
        return
    except Exception as exc:      # noqa: BLE001
        app.cm_loading = False
        app.cm_status = f"加载失败：{exc}"
        return
    items = res.get("comments") or []
    hot = res.get("hotComments") or []
    app.cm_total = int(res.get("total") or 0)
    app.cm_more = bool(res.get("more"))
    if reset:
        app.cm_comments = list(hot) + list(items)
        app.cm_loaded = len(items)
        app.cm_offset = 0
    else:
        app.cm_comments.extend(items)
        app.cm_loaded += len(items)
    app.cm_loading = False
    tail = "（还有更多，按 l 继续）" if app.cm_more else "（已到底）"
    app.cm_status = (f"已加载 {len(app.cm_comments)}/{app.cm_total} 条{tail}")


def send_comment(app: Application) -> None:
    """发表当前输入框里的评论（需登录）。"""
    text = app.cm_input.strip()
    app.cm_input = ""
    app.cm_composing = False
    if not text:
        app.cm_status = "评论内容不能为空（i 重新输入，Esc 取消）"
        return
    if len(text) > 300:
        app.cm_status = f"评论过长（{len(text)} 字 > 300），请精简后再发"
        return
    if not app.api.logged_in:
        app.cm_status = "发表评论需要登录：login qr / login sms / login cookie"
        return
    song = app.cm_song
    if not song:
        app.cm_status = "没有可评论的歌曲"
        return
    app.cm_status = "正在发表评论…"

    def work() -> None:
        try:
            res = app.api.post_comment(song.id, text)
        except ApiError as exc:
            app.cm_status = f"发表失败：{exc.message}"
            return
        except Exception as exc:      # noqa: BLE001
            app.cm_status = f"发表失败：{exc}"
            return
        if res.get("code") != 200:
            app.cm_status = "发表失败：" + friendly_code_message(
                res.get("code"), str(res.get("message") or res.get("msg") or ""))
            return
        app.cm_status = "已发表 ✓ 正在刷新列表…"
        load_comments(app, reset=True)

    app.run_job("发表评论", work)


def print_comments_plain(app: Application, song: Song) -> None:
    """纯文本模式：直接打印若干条评论。"""
    app.busy = "加载评论…"
    data = app.api.comments(song.id, limit=15)
    app.busy = ""
    items = list((data.get("hotComments") or [])) + list(data.get("comments") or [])
    total = data.get("total", 0)
    app.heading(f"《{song.name} - {song.artist}》评论（共 {total} 条）")
    width = out_width(app)
    for i, c in enumerate(items[:12], 1):
        user = ((c.get("user") or {}).get("nickname")) or "匿名"
        liked = c.get("likedCount") or 0
        text = str(c.get("content") or "").replace("\n", " ")
        app.println(f"{i}. {user} ♥{liked}", "cyan")
        app.println("   " + slice_disp(text, width - 3), "white")
    if not items:
        app.warn("暂无评论")
    else:
        app.println("提示：在交互界面输入 cm 可进入全屏评论页"
                    "（翻页浏览全部 + 发表评论）", "gray")


@command("comment", "评论：全屏浏览全部 + 发表", "cm [序号]",
         aliases=("cm", "评论"))
def cmd_comment(app: Application, args: List[str], raw: str) -> None:
    song = app.engine.current
    if args:
        songs, _ = song_context(app)
        idx = _parse_pos(args[0])
        if songs and idx and 1 <= idx <= len(songs):
            song = songs[idx - 1]
    if not song:
        app.error("没有可评论的歌曲（先 search / play 一首）")
        return
    if not getattr(app, "supports_browse", False):
        print_comments_plain(app, song)
        return
    # TUI：进入全屏评论页
    app.cm_song = song
    app.cm_comments = []
    app.cm_total = 0
    app.cm_loaded = 0
    app.cm_offset = 0
    app.cm_more = False
    app.cm_composing = False
    app.cm_input = ""
    app.cm_status = "正在加载评论…"
    app.browse = False
    app.qr_active = False
    app.cm_active = True
    app.run_job("加载评论", lambda: load_comments(app, reset=True))


# -- 账号 ---------------------------------------------------------------

def start_qr_login(app: Application, poll_id: Optional[int] = None) -> None:
    """扫码登录：二维码优先用全屏视图展示，并后台轮询状态。

    运行在独立线程里（不占任务队列），并以 poll_id 做代次互斥：
    再次 login qr 会生成新二维码并让旧轮询自然退出。
    """
    if poll_id is None:
        poll_id = app.qr_poll_id
    app.busy = "获取二维码…"
    try:
        key = app.api.qr_key()
    except ApiError as exc:
        app.busy = ""
        app.error(f"获取二维码失败：{exc.message}")
        return
    except Exception as exc:      # noqa: BLE001
        app.busy = ""
        app.error(f"获取二维码失败：{exc}")
        return
    if app.qr_poll_id != poll_id:
        app.busy = ""
        return                     # 已被更新的 login qr 取代
    url = NcmApi.qr_url(key)
    lines = qr_terminal_lines(url, app)
    need = (len(lines) + 3) if lines else 0

    view_ok = False
    if lines and getattr(app, "supports_browse", False) and app.screen_rows >= need:
        # TUI 且行数足够：进入全屏二维码视图（输出缓冲区太矮，装不下二维码）
        app.browse = False
        app.qr_active = True
        app.qr_lines = lines
        app.qr_url = url
        app.qr_need_rows = need
        app.qr_status = "等待扫码… 请用网易云音乐 App 扫描"
        view_ok = True
    elif lines and not getattr(app, "supports_browse", False) \
            and app.screen_rows >= need:
        # 纯文本模式（TTY）：逐行打印，依赖终端自然滚动
        app.heading("扫码登录")
        app.println("请用网易云音乐 App 扫描下方二维码，或在浏览器打开链接确认",
                    "white")
        for ln in lines:
            app.println(center_ansi(ln, out_width(app)))
        app.println(url, "bcyan")
        app.println("等待扫码…（二维码 3 分钟内有效）", "gray")
        view_ok = True

    if not view_ok:
        # 二维码放不下 / 无彩色 → 给出可直接打开的登录链接与明确原因
        app.heading("扫码登录")
        app.println(url, "bcyan")
        if not lines:
            if app.theme.enabled:
                reason = "终端过窄（宽度不足以容纳二维码）"
            elif os.environ.get("NO_COLOR"):
                reason = ("检测到 NO_COLOR=1 已禁用彩色输出，而二维码需要黑白底色"
                          "才能识别")
            else:
                reason = "彩色输出已关闭（--mono 或主题 mono），二维码需要彩色"
        else:
            reason = f"终端行数不足（需约 {need} 行，当前 {app.screen_rows} 行）"
        app.info(f"二维码未显示：{reason}。请放大终端后重试 login qr，"
                 f"或用手机打开上面的链接完成确认")
        app.println("    说明：在电脑浏览器打开链接登录的是网页版"
                    "（登录态留在浏览器）；想让程序内也登录，"
                    "可执行 login cookie <MUSIC_U>", "gray")
    app.busy = ""

    def poll() -> None:
        deadline = time.time() + 180
        last: Optional[int] = None

        def current() -> bool:
            # 只有“当代次”的轮询才允许改视图状态 / 输出消息，
            # 否则旧线程退出时会把新二维码视图误关掉
            return app.qr_poll_id == poll_id

        try:
            while (time.time() < deadline and app.running and current()):
                res = app.api.qr_check(key)
                if not current():
                    return
                code = res.get("code")
                if code == 803:
                    app.api.save_cookies()
                    app.qr_active = False
                    app.ok("登录成功，欢迎回来！")
                    app.run_job("读取账号", app._load_profile_quiet)
                    return
                if code == 802 and last != 802:
                    app.qr_status = "已扫码，请在手机上确认登录…"
                    app.info("已扫码，请在手机上确认登录…")
                elif code == 800:
                    app.qr_active = False
                    app.qr_status = "二维码已过期"
                    app.warn("二维码已过期，请重新输入 login qr")
                    return
                elif code not in (801, 802, 803, None) and code != last:
                    app.qr_active = False
                    app.error(f"扫码登录失败：{friendly_code_message(code, str(res.get('message') or ''))}")
                    return
                elif code == 801:
                    app.qr_status = "等待扫码… 请用网易云音乐 App 扫描"
                last = code
                time.sleep(2)
            if current():                # 超时（被新 login qr 取代时静默退出）
                app.qr_active = False
                app.warn("扫码登录超时，可重新输入 login qr")
        except Exception as exc:      # noqa: BLE001
            if current():
                app.qr_active = False
                app.error(f"扫码登录中断：{exc}")

    app.run_job("扫码登录", poll)


def parse_cookie_token(token: str) -> Tuple[str, str]:
    """从“MUSIC_U 值”或“整段 Cookie 串”里提取 (MUSIC_U, __csrf)。"""
    token = token.strip().strip('"').strip("'")
    music_u, csrf = token, ""
    if "=" in token and "MUSIC_U" in token:      # 允许直接粘贴整段 Cookie
        pairs: Dict[str, str] = {}
        for part in token.split(";"):            # 注意分号后常带空格
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()
        music_u = (pairs.get("MUSIC_U", "") or "").strip().strip('"').strip("'")
        csrf = (pairs.get("__csrf", "") or "").strip().strip('"').strip("'")
    return music_u, csrf


def import_browser_cookie(app: Application, token: str) -> None:
    """把浏览器里已登录的凭证（MUSIC_U，可选 __csrf）导入程序。"""
    music_u, csrf = parse_cookie_token(token)
    if not music_u:
        app.error("未在其中找到 MUSIC_U，请复制它的值后重试")
        return

    api = app.api
    backup = list(api.jar)

    def _set(name: str, value: str) -> None:
        api.jar.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=value, port=None,
            port_specified=False, domain=".music.163.com",
            domain_specified=True, domain_initial_dot=True, path="/",
            path_specified=True, secure=False, expires=None, discard=True,
            comment=None, comment_url=None, rest={}, rfc2109=False))

    def _restore() -> None:
        for c in list(api.jar):
            try:
                api.jar.clear(c.domain, c.path, c.name)
            except Exception:
                pass
        for c in backup:
            api.jar.set_cookie(c)

    def _apply(value: str) -> Optional[Dict[str, Any]]:
        for c in list(api.jar):
            if c.name in ("MUSIC_U", "__csrf"):
                try:
                    api.jar.clear(c.domain, c.path, c.name)
                except Exception:
                    pass
        _set("MUSIC_U", value)
        if csrf:
            _set("__csrf", csrf)
        try:
            data = api.account()
        except Exception:
            return None
        return (data.get("profile") or {}) or None

    profile = _apply(music_u)
    if profile is None and "%" not in music_u:
        # 浏览器开发者工具里看到的是解码值，直接发可能不被接受 → 再试 URL 编码
        profile = _apply(urllib.parse.quote(music_u, safe=""))
    if profile is None:
        _restore()
        app.error("Cookie 未生效：请确认复制的是 music.163.com 域下、"
                  "未过期的 MUSIC_U（网页版登录后 F12 → 应用/存储 → Cookie）")
        return
    app.user = profile
    api.save_cookies()
    app.ok(f"登录成功：{profile.get('nickname')}"
           f"（UID {profile.get('userId')}）")


def do_sms_login(app: Application, phone: str, code: str,
                 country: str = "86") -> None:
    """用短信验证码完成登录（也供命令行直接带验证码调用）。"""
    if not code.isdigit():
        app.error("验证码应为数字")
        return
    try:
        res = app.api.login_cellphone(phone, password="", captcha=code,
                                      countrycode=country)
    except ApiError as exc:
        app.error(f"登录失败：{exc.message}")
        return
    except Exception as exc:      # noqa: BLE001
        app.error(f"登录失败：{exc}")
        return
    if res.get("code") == 200:
        app.api.save_cookies()
        app.ok("登录成功！")
        app.run_job("读取账号", app._load_profile_quiet)
        return
    msg = str(res.get("message") or res.get("msg") or "")
    app.error(f"登录失败：" + friendly_code_message(res.get("code"), msg))
    if res.get("code") in (406, 405, 412, -460, -462):
        app.println("    提示：可能需要图形验证码或触发了风控，"
                    "可稍后再试，或改用 login qr / login cookie", "gray")


@command("login", "登录（扫码 / 短信验证码 / 密码 / 邮箱 / Cookie）",
         "login [qr | sms <手机号> [验证码] | pass <手机号> | "
         "email <邮箱> | cookie <MUSIC_U>]",
         aliases=("登录",),
         subcmds=["qr", "sms", "pass", "email", "cookie"])
def cmd_login(app: Application, args: List[str], raw: str) -> None:
    if app.api.logged_in and not args:
        app.warn("已经登录过了（先 logout 可退出当前账号）")
        return
    mode = args[0].lower() if args else "qr"
    if mode in ("qr", "scan", "扫码", "qrcode"):
        app.qr_poll_id += 1              # 取代上一次轮询
        threading.Thread(target=start_qr_login, args=(app, app.qr_poll_id),
                         daemon=True).start()
        return
    if mode in ("sms", "code", "验证码", "短信"):
        phone = args[1].strip() if len(args) > 1 else ""
        code = args[2].strip() if len(args) > 2 else ""
        if not phone:
            app.error("用法：login sms <手机号> [验证码]")
            app.println("    不带验证码时会先发送短信，"
                        "然后在程序内输入收到的验证码完成登录", "gray")
            app.println("    国家码默认 86；如需其它国家码请用 +区号 手机号"
                        "（如 +852…）", "gray")
            return
        country = "86"
        if phone.startswith("+"):
            digits = phone[1:]
            known = (
                "888", "886", "853", "852", "850", "880", "856", "855",
                "81", "82", "84", "65", "60", "62", "63", "64", "66", "67",
                "90", "91", "92", "93", "94", "95", "96", "97", "98", "99",
                "7", "20", "27", "30", "31", "32", "33", "34", "35", "36",
                "37", "38", "39", "40", "41", "42", "43", "44", "45", "46",
                "47", "48", "49", "50", "51", "52", "53", "54", "55", "56",
                "57", "58", "59", "211", "212", "213", "216", "218", "220",
                "221", "222", "223", "224", "225", "226", "227", "228", "229",
                "230", "231", "232", "233", "234", "235", "236", "237", "238",
                "239", "240", "241", "242", "243", "244", "245", "246", "247",
                "248", "249", "250", "251", "252", "253", "254", "255", "256",
                "257", "258", "260", "261", "262", "263", "264", "265", "266",
                "267", "268", "269", "290", "291", "297", "298", "299", "358",
                "370", "371", "372", "373", "374", "375", "376", "377", "378",
                "380", "381", "382", "383", "385", "386", "387", "389", "580",
                "590", "591", "592", "593", "594", "595", "596", "597", "598",
                "599", "672", "673", "674", "675", "676", "677", "678", "679",
                "680", "681", "682", "683", "685", "686", "687", "688", "689",
                "690", "691", "692", "851", "8531", "855", "856", "880", "881",
                "882", "883", "886", "888", "960", "961", "962", "963", "964",
                "965", "966", "967", "968", "970", "971", "972", "973", "974",
                "975", "976", "977", "979", "991", "992", "993", "994", "995",
                "996", "998", "977",
            )
            matched = None
            for cc in sorted(known, key=len, reverse=True):
                if digits.startswith(cc) and len(digits) - len(cc) >= 5:
                    matched = cc
                    break
            if matched is None:
                app.error("无法识别国家码：请只填数字手机号（默认 +86），"
                          "或使用 login qr / login cookie")
                return
            country, phone = matched, digits[len(matched):]
        if not phone.isdigit() or not (5 <= len(phone) <= 15):
            app.error(f"手机号格式不对：{phone}（请只填数字）")
            return
        if code:
            app.run_job("短信登录",
                        lambda: do_sms_login(app, phone, code, country))
            return

        def on_code(received: str) -> None:
            received = received.strip()
            if not received:
                app.warn("已取消输入验证码")
                return
            if not received.isdigit():
                app.error("验证码应为数字，请重新 login sms <手机号>")
                return
            app.run_job("短信登录",
                        lambda: do_sms_login(app, phone, received))

        def do_send() -> None:
            try:
                res = app.api.send_captcha(phone, ctcode=country)
            except ApiError as exc:
                app.error(f"发送验证码失败：{exc.message}")
                return
            except Exception as exc:      # noqa: BLE001
                app.error(f"发送验证码失败：{exc}")
                return
            if res.get("code") != 200:
                app.error("发送验证码失败：" + friendly_code_message(
                    res.get("code"), str(res.get("message") or res.get("msg") or "")))
                return
            app.ok("验证码已发送，请查收短信")
            prompt_secret(app, "请输入短信验证码：", on_code, secure=False)

        app.run_job("发送验证码", do_send)
        return
    if mode in ("cookie", "cookies", "token", "凭证"):
        token = " ".join(args[1:]).strip()
        if not token:
            app.error("用法：login cookie <MUSIC_U 或整段 Cookie 串>")
            app.println("    浏览器登录 music.163.com 后按 F12 →"
                        "应用/存储 → Cookie → 复制 MUSIC_U 的值"
                        "（可连 __csrf 一起整段粘贴）", "gray")
            app.println("    这样导入的是浏览器的登录态，"
                        "程序内即可使用每日推荐/我的歌单等功能", "gray")
            return
        app.run_job("导入 Cookie", lambda: import_browser_cookie(app, token))
        return
    if mode in ("pass", "phone", "password", "手机", "密码"):
        if len(args) < 2:
            app.error("用法：login pass <手机号>　（密码将在下一步安全输入）")
            return
        phone = args[1]
        def done(pwd: str) -> None:
            if not pwd:
                app.warn("已取消登录")
                return
            def work() -> None:
                res = app.api.login_cellphone(phone, pwd)
                if res.get("code") == 200:
                    app.ok("登录成功！")
                    app.api.save_cookies()
                    app.run_job("读取账号", app._load_profile_quiet)
                else:
                    app.error(friendly_code_message(res.get("code"),
                                                    str(res.get("message") or res.get("msg") or "")))
            app.run_job("登录", work)
        prompt_secret(app, "手机号登录 · 请输入密码：", done)
        return
    if mode in ("email", "邮箱"):
        if len(args) < 2:
            app.error("用法：login email <邮箱>")
            return
        email = args[1]
        def done_email(pwd: str) -> None:
            if not pwd:
                app.warn("已取消登录")
                return
            def work() -> None:
                res = app.api.login_email(email, pwd)
                if res.get("code") == 200:
                    app.ok("登录成功！")
                    app.api.save_cookies()
                    app.run_job("读取账号", app._load_profile_quiet)
                else:
                    app.error(friendly_code_message(res.get("code"),
                                                    str(res.get("message") or res.get("msg") or "")))
            app.run_job("登录", work)
        prompt_secret(app, "邮箱登录 · 请输入密码：", done_email)
        return
    app.error("用法：login [qr | pass <手机号> | email <邮箱>]")


@command("logout", "退出登录", aliases=("登出",))
def cmd_logout(app: Application, args: List[str], raw: str) -> None:
    if not app.api.logged_in:
        app.warn("当前未登录")
        return
    app.api.logout()
    app.user = {}
    app.ok("已退出登录")


@command("me", "查看账号信息", aliases=("whoami", "账号"))
def cmd_me(app: Application, args: List[str], raw: str) -> None:
    if not app.api.logged_in:
        app.warn("当前未登录（login qr 扫码登录）")
        return
    data = app.api.account()
    profile = data.get("profile") or {}
    app.user = profile
    app.heading(f"{profile.get('nickname', '未知用户')} 的账号")
    vip = profile.get("vipType") or 0
    rows = [
        f"UID：{profile.get('userId', '-')}",
        f"VIP：{'VIP' + str(vip) if vip else '普通用户'}",
        f"签名：{slice_disp(str(profile.get('signature') or '（无）'), out_width(app) - 4)}",
    ]
    if data.get("profile"):
        app.table(rows)
    app.println(f"cookie 已保存至 {app.api.cookie_path}", "gray")


# -- 界面 / 设置 ---------------------------------------------------------

@command("view", "全屏浏览输出历史（翻页）", "view", aliases=("pager", "浏览"))
def cmd_view(app: Application, args: List[str], raw: str) -> None:
    if not getattr(app, "supports_browse", False):
        app.error("当前模式不支持全屏浏览（管道模式下可直接重定向输出到文件）")
        return
    app.browse = True
    app.browse_offset = None        # 由界面定位到最新内容


@command("clear", "清空输出区", aliases=("cls", "清屏"))
def cmd_clear(app: Application, args: List[str], raw: str) -> None:
    app.messages = []
    app.scroll = 0


@command("theme", "主题切换", "theme [color|mono]", subcmds=["color", "mono"],
         aliases=("主题",))
def cmd_theme(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.println("当前主题：" + app.cfg.get("theme", "color") + "（color/mono）")
        return
    val = args[0].lower()
    if val not in ("color", "mono"):
        app.error("用法：theme color / theme mono")
        return
    app.cfg.set("theme", val)
    app.theme.enable(val == "color" and not os.environ.get("NO_COLOR"))
    app.println(f"主题已切换为 {val}")


@command("backend", "查看/切换播放内核", "backend [auto|mpv|ffplay|mpg123|sim]",
         subcmds=["auto", "mpv", "ffplay", "mpg123", "sim"], aliases=("内核",))
def cmd_backend(app: Application, args: List[str], raw: str) -> None:
    if not args:
        app.println("当前：" + app.engine.backend_info())
        pool = [c.label for c in available_backends()]
        app.println("可用：" + "、".join(pool) if pool else "（无）", "gray")
        app.println("切换：backend mpv / ffplay / mpg123 / sim / auto", "gray")
        return
    name = args[0].lower()
    if name not in ("auto", "mpv", "ffplay", "mpg123", "sim"):
        app.error("用法：backend [auto|mpv|ffplay|mpg123|sim]")
        return
    app.engine.stop()
    app.cfg.set("backend", name)
    app.engine._init_backend()
    chosen = type(app.engine.backend)
    ok, reason = chosen.probe()
    if not ok and chosen.name != "sim":
        app.warn(f"探测失败：{reason}")
        app.engine._init_backend()
    app.ok(f"已切换播放内核：{app.engine.backend_info()}")


@command("quality", "音质偏好", "quality [standard|higher|exhigh|lossless]",
         subcmds=["standard", "higher", "exhigh", "lossless"], aliases=("音质",))
def cmd_quality(app: Application, args: List[str], raw: str) -> None:
    names = ["lower", "standard", "higher", "exhigh", "hires", "lossless"]
    if not args:
        app.println("当前音质偏好：" + str(app.cfg.get("quality", "exhigh")))
        return
    val = args[0].lower()
    if val not in names:
        app.error("可选：" + "、".join(names))
        return
    app.cfg.set("quality", val)
    app.engine._url_cache.clear()
    app.ok(f"音质偏好：{val}（实际以账号权限与歌曲可用性为准）")


# --------------------------------------------------------------------------
# 内嵌第三方库：python-qrcode（BSD-3-Clause）
# 来源：https://github.com/lincolnloop/python-qrcode
# Copyright (c) 2011, Lincoln Loop.  仅用于生成扫码登录所需的 QR 矩阵，
# 只依赖 Python 标准库（不使用 PIL / pypng）。完整许可证见 _QRCODE_LICENSE。
# --------------------------------------------------------------------------

_QRCODE_LICENSE = '''Copyright (c) 2011, Lincoln Loop
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright notice,
      this list of conditions and the following disclaimer in the documentation
      and/or other materials provided with the distribution.
    * Neither the package name nor the names of its contributors may be
      used to endorse or promote products derived from this software without
      specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE 
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


-------------------------------------------------------------------------------


Original text and license from the pyqrnative package where this was forked
from (http://code.google.com/p/pyqrnative):

#Ported from the Javascript library by Sam Curren
#
#QRCode for Javascript
#http://d-project.googlecode.com/svn/trunk/misc/qrcode/js/qrcode.js
#
#Copyright (c) 2009 Kazuhiko Arase
#
#URL: http://www.d-project.com/
#
#Licensed under the MIT license:
#   http://www.opensource.org/licenses/mit-license.php
#
# The word "QR Code" is registered trademark of
# DENSO WAVE INCORPORATED
#   http://www.denso-wave.com/qrcode/faqpatent-e.html'''

_QRCODE_SRC: Dict[str, str] = {
    'constants': '''from importlib.util import find_spec

# QR error correct levels
ERROR_CORRECT_L = 1
ERROR_CORRECT_M = 0
ERROR_CORRECT_Q = 3
ERROR_CORRECT_H = 2

# Constant whether the PIL library is installed.
PIL_AVAILABLE = find_spec("PIL") is not None
''',
    'exceptions': '''class DataOverflowError(Exception):
    pass
''',
    'LUT': '''# Store all kinds of lookup table.


# # generate rsPoly lookup table.

# from qrcode import base

# def create_bytes(rs_blocks):
#     for r in range(len(rs_blocks)):
#         dcCount = rs_blocks[r].data_count
#         ecCount = rs_blocks[r].total_count - dcCount
#         rsPoly = base.Polynomial([1], 0)
#         for i in range(ecCount):
#             rsPoly = rsPoly * base.Polynomial([1, base.gexp(i)], 0)
#         return ecCount, rsPoly

# rsPoly_LUT = {}
# for version in range(1,41):
#     for error_correction in range(4):
#         rs_blocks_list = base.rs_blocks(version, error_correction)
#         ecCount, rsPoly = create_bytes(rs_blocks_list)
#         rsPoly_LUT[ecCount]=rsPoly.num
# print(rsPoly_LUT)

# Result. Usage: input: ecCount, output: Polynomial.num
# e.g. rsPoly = base.Polynomial(LUT.rsPoly_LUT[ecCount], 0)
rsPoly_LUT = {  # noqa: N816
    7: [1, 127, 122, 154, 164, 11, 68, 117],
    10: [1, 216, 194, 159, 111, 199, 94, 95, 113, 157, 193],
    13: [1, 137, 73, 227, 17, 177, 17, 52, 13, 46, 43, 83, 132, 120],
    15: [1, 29, 196, 111, 163, 112, 74, 10, 105, 105, 139, 132, 151, 32, 134, 26],
    16: [1, 59, 13, 104, 189, 68, 209, 30, 8, 163, 65, 41, 229, 98, 50, 36, 59],
    17: [1, 119, 66, 83, 120, 119, 22, 197, 83, 249, 41, 143, 134, 85, 53, 125, 99, 79],
    18: [
        1,
        239,
        251,
        183,
        113,
        149,
        175,
        199,
        215,
        240,
        220,
        73,
        82,
        173,
        75,
        32,
        67,
        217,
        146,
    ],
    20: [
        1,
        152,
        185,
        240,
        5,
        111,
        99,
        6,
        220,
        112,
        150,
        69,
        36,
        187,
        22,
        228,
        198,
        121,
        121,
        165,
        174,
    ],
    22: [
        1,
        89,
        179,
        131,
        176,
        182,
        244,
        19,
        189,
        69,
        40,
        28,
        137,
        29,
        123,
        67,
        253,
        86,
        218,
        230,
        26,
        145,
        245,
    ],
    24: [
        1,
        122,
        118,
        169,
        70,
        178,
        237,
        216,
        102,
        115,
        150,
        229,
        73,
        130,
        72,
        61,
        43,
        206,
        1,
        237,
        247,
        127,
        217,
        144,
        117,
    ],
    26: [
        1,
        246,
        51,
        183,
        4,
        136,
        98,
        199,
        152,
        77,
        56,
        206,
        24,
        145,
        40,
        209,
        117,
        233,
        42,
        135,
        68,
        70,
        144,
        146,
        77,
        43,
        94,
    ],
    28: [
        1,
        252,
        9,
        28,
        13,
        18,
        251,
        208,
        150,
        103,
        174,
        100,
        41,
        167,
        12,
        247,
        56,
        117,
        119,
        233,
        127,
        181,
        100,
        121,
        147,
        176,
        74,
        58,
        197,
    ],
    30: [
        1,
        212,
        246,
        77,
        73,
        195,
        192,
        75,
        98,
        5,
        70,
        103,
        177,
        22,
        217,
        138,
        51,
        181,
        246,
        72,
        25,
        18,
        46,
        228,
        74,
        216,
        195,
        11,
        106,
        130,
        150,
    ],
}
''',
    'base': '''from typing import NamedTuple

from qrcode import constants

EXP_TABLE = list(range(256))

LOG_TABLE = list(range(256))

for i in range(8):
    EXP_TABLE[i] = 1 << i

for i in range(8, 256):
    EXP_TABLE[i] = (
        EXP_TABLE[i - 4] ^ EXP_TABLE[i - 5] ^ EXP_TABLE[i - 6] ^ EXP_TABLE[i - 8]
    )

for i in range(255):
    LOG_TABLE[EXP_TABLE[i]] = i

RS_BLOCK_OFFSET = {
    constants.ERROR_CORRECT_L: 0,
    constants.ERROR_CORRECT_M: 1,
    constants.ERROR_CORRECT_Q: 2,
    constants.ERROR_CORRECT_H: 3,
}

RS_BLOCK_TABLE = (
    # L
    # M
    # Q
    # H
    # 1
    (1, 26, 19),
    (1, 26, 16),
    (1, 26, 13),
    (1, 26, 9),
    # 2
    (1, 44, 34),
    (1, 44, 28),
    (1, 44, 22),
    (1, 44, 16),
    # 3
    (1, 70, 55),
    (1, 70, 44),
    (2, 35, 17),
    (2, 35, 13),
    # 4
    (1, 100, 80),
    (2, 50, 32),
    (2, 50, 24),
    (4, 25, 9),
    # 5
    (1, 134, 108),
    (2, 67, 43),
    (2, 33, 15, 2, 34, 16),
    (2, 33, 11, 2, 34, 12),
    # 6
    (2, 86, 68),
    (4, 43, 27),
    (4, 43, 19),
    (4, 43, 15),
    # 7
    (2, 98, 78),
    (4, 49, 31),
    (2, 32, 14, 4, 33, 15),
    (4, 39, 13, 1, 40, 14),
    # 8
    (2, 121, 97),
    (2, 60, 38, 2, 61, 39),
    (4, 40, 18, 2, 41, 19),
    (4, 40, 14, 2, 41, 15),
    # 9
    (2, 146, 116),
    (3, 58, 36, 2, 59, 37),
    (4, 36, 16, 4, 37, 17),
    (4, 36, 12, 4, 37, 13),
    # 10
    (2, 86, 68, 2, 87, 69),
    (4, 69, 43, 1, 70, 44),
    (6, 43, 19, 2, 44, 20),
    (6, 43, 15, 2, 44, 16),
    # 11
    (4, 101, 81),
    (1, 80, 50, 4, 81, 51),
    (4, 50, 22, 4, 51, 23),
    (3, 36, 12, 8, 37, 13),
    # 12
    (2, 116, 92, 2, 117, 93),
    (6, 58, 36, 2, 59, 37),
    (4, 46, 20, 6, 47, 21),
    (7, 42, 14, 4, 43, 15),
    # 13
    (4, 133, 107),
    (8, 59, 37, 1, 60, 38),
    (8, 44, 20, 4, 45, 21),
    (12, 33, 11, 4, 34, 12),
    # 14
    (3, 145, 115, 1, 146, 116),
    (4, 64, 40, 5, 65, 41),
    (11, 36, 16, 5, 37, 17),
    (11, 36, 12, 5, 37, 13),
    # 15
    (5, 109, 87, 1, 110, 88),
    (5, 65, 41, 5, 66, 42),
    (5, 54, 24, 7, 55, 25),
    (11, 36, 12, 7, 37, 13),
    # 16
    (5, 122, 98, 1, 123, 99),
    (7, 73, 45, 3, 74, 46),
    (15, 43, 19, 2, 44, 20),
    (3, 45, 15, 13, 46, 16),
    # 17
    (1, 135, 107, 5, 136, 108),
    (10, 74, 46, 1, 75, 47),
    (1, 50, 22, 15, 51, 23),
    (2, 42, 14, 17, 43, 15),
    # 18
    (5, 150, 120, 1, 151, 121),
    (9, 69, 43, 4, 70, 44),
    (17, 50, 22, 1, 51, 23),
    (2, 42, 14, 19, 43, 15),
    # 19
    (3, 141, 113, 4, 142, 114),
    (3, 70, 44, 11, 71, 45),
    (17, 47, 21, 4, 48, 22),
    (9, 39, 13, 16, 40, 14),
    # 20
    (3, 135, 107, 5, 136, 108),
    (3, 67, 41, 13, 68, 42),
    (15, 54, 24, 5, 55, 25),
    (15, 43, 15, 10, 44, 16),
    # 21
    (4, 144, 116, 4, 145, 117),
    (17, 68, 42),
    (17, 50, 22, 6, 51, 23),
    (19, 46, 16, 6, 47, 17),
    # 22
    (2, 139, 111, 7, 140, 112),
    (17, 74, 46),
    (7, 54, 24, 16, 55, 25),
    (34, 37, 13),
    # 23
    (4, 151, 121, 5, 152, 122),
    (4, 75, 47, 14, 76, 48),
    (11, 54, 24, 14, 55, 25),
    (16, 45, 15, 14, 46, 16),
    # 24
    (6, 147, 117, 4, 148, 118),
    (6, 73, 45, 14, 74, 46),
    (11, 54, 24, 16, 55, 25),
    (30, 46, 16, 2, 47, 17),
    # 25
    (8, 132, 106, 4, 133, 107),
    (8, 75, 47, 13, 76, 48),
    (7, 54, 24, 22, 55, 25),
    (22, 45, 15, 13, 46, 16),
    # 26
    (10, 142, 114, 2, 143, 115),
    (19, 74, 46, 4, 75, 47),
    (28, 50, 22, 6, 51, 23),
    (33, 46, 16, 4, 47, 17),
    # 27
    (8, 152, 122, 4, 153, 123),
    (22, 73, 45, 3, 74, 46),
    (8, 53, 23, 26, 54, 24),
    (12, 45, 15, 28, 46, 16),
    # 28
    (3, 147, 117, 10, 148, 118),
    (3, 73, 45, 23, 74, 46),
    (4, 54, 24, 31, 55, 25),
    (11, 45, 15, 31, 46, 16),
    # 29
    (7, 146, 116, 7, 147, 117),
    (21, 73, 45, 7, 74, 46),
    (1, 53, 23, 37, 54, 24),
    (19, 45, 15, 26, 46, 16),
    # 30
    (5, 145, 115, 10, 146, 116),
    (19, 75, 47, 10, 76, 48),
    (15, 54, 24, 25, 55, 25),
    (23, 45, 15, 25, 46, 16),
    # 31
    (13, 145, 115, 3, 146, 116),
    (2, 74, 46, 29, 75, 47),
    (42, 54, 24, 1, 55, 25),
    (23, 45, 15, 28, 46, 16),
    # 32
    (17, 145, 115),
    (10, 74, 46, 23, 75, 47),
    (10, 54, 24, 35, 55, 25),
    (19, 45, 15, 35, 46, 16),
    # 33
    (17, 145, 115, 1, 146, 116),
    (14, 74, 46, 21, 75, 47),
    (29, 54, 24, 19, 55, 25),
    (11, 45, 15, 46, 46, 16),
    # 34
    (13, 145, 115, 6, 146, 116),
    (14, 74, 46, 23, 75, 47),
    (44, 54, 24, 7, 55, 25),
    (59, 46, 16, 1, 47, 17),
    # 35
    (12, 151, 121, 7, 152, 122),
    (12, 75, 47, 26, 76, 48),
    (39, 54, 24, 14, 55, 25),
    (22, 45, 15, 41, 46, 16),
    # 36
    (6, 151, 121, 14, 152, 122),
    (6, 75, 47, 34, 76, 48),
    (46, 54, 24, 10, 55, 25),
    (2, 45, 15, 64, 46, 16),
    # 37
    (17, 152, 122, 4, 153, 123),
    (29, 74, 46, 14, 75, 47),
    (49, 54, 24, 10, 55, 25),
    (24, 45, 15, 46, 46, 16),
    # 38
    (4, 152, 122, 18, 153, 123),
    (13, 74, 46, 32, 75, 47),
    (48, 54, 24, 14, 55, 25),
    (42, 45, 15, 32, 46, 16),
    # 39
    (20, 147, 117, 4, 148, 118),
    (40, 75, 47, 7, 76, 48),
    (43, 54, 24, 22, 55, 25),
    (10, 45, 15, 67, 46, 16),
    # 40
    (19, 148, 118, 6, 149, 119),
    (18, 75, 47, 31, 76, 48),
    (34, 54, 24, 34, 55, 25),
    (20, 45, 15, 61, 46, 16),
)


def glog(n):
    if n < 1:  # pragma: no cover
        raise ValueError(f"glog({n})")
    return LOG_TABLE[n]


def gexp(n):
    return EXP_TABLE[n % 255]


class Polynomial:
    def __init__(self, num, shift):
        if not num:  # pragma: no cover
            raise ValueError(f"{len(num)}/{shift}")

        offset = 0
        for offset in range(len(num)):
            if num[offset] != 0:
                break

        self.num = num[offset:] + [0] * shift

    def __getitem__(self, index):
        return self.num[index]

    def __iter__(self):
        return iter(self.num)

    def __len__(self):
        return len(self.num)

    def __mul__(self, other):
        num = [0] * (len(self) + len(other) - 1)

        for i, item in enumerate(self):
            for j, other_item in enumerate(other):
                if item == 0 or other_item == 0:
                    continue
                num[i + j] ^= gexp(glog(item) + glog(other_item))

        return Polynomial(num, 0)

    def __mod__(self, other):
        difference = len(self) - len(other)
        if difference < 0:
            return self

        if self[0] == 0:
            num = list(self[1:])
            if difference:
                num.append(0)
            return Polynomial(num, 0) % other

        ratio = glog(self[0]) - glog(other[0])

        num = [
            item ^ gexp(glog(other_item) + ratio)
            for item, other_item in zip(self, other, strict=False)
        ]
        if difference:
            num.extend(self[-difference:])

        # recursive call
        return Polynomial(num, 0) % other


class RSBlock(NamedTuple):
    total_count: int
    data_count: int


def rs_blocks(version, error_correction):
    if error_correction not in RS_BLOCK_OFFSET:  # pragma: no cover
        raise ValueError(
            f"bad rs block @ version: {version} / error_correction: {error_correction}"
        )
    offset = RS_BLOCK_OFFSET[error_correction]
    rs_block = RS_BLOCK_TABLE[(version - 1) * 4 + offset]

    blocks = []

    for i in range(0, len(rs_block), 3):
        count, total_count, data_count = rs_block[i : i + 3]
        for _ in range(count):
            blocks.append(RSBlock(total_count, data_count))

    return blocks
''',
    'util': '''import math
import re

from qrcode import LUT, base, exceptions
from qrcode.base import RSBlock

# QR encoding modes.
MODE_NUMBER = 1 << 0
MODE_ALPHA_NUM = 1 << 1
MODE_8BIT_BYTE = 1 << 2
MODE_KANJI = 1 << 3

# Encoding mode sizes.
MODE_SIZE_SMALL = {
    MODE_NUMBER: 10,
    MODE_ALPHA_NUM: 9,
    MODE_8BIT_BYTE: 8,
    MODE_KANJI: 8,
}
MODE_SIZE_MEDIUM = {
    MODE_NUMBER: 12,
    MODE_ALPHA_NUM: 11,
    MODE_8BIT_BYTE: 16,
    MODE_KANJI: 10,
}
MODE_SIZE_LARGE = {
    MODE_NUMBER: 14,
    MODE_ALPHA_NUM: 13,
    MODE_8BIT_BYTE: 16,
    MODE_KANJI: 12,
}

ALPHA_NUM = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
RE_ALPHA_NUM = re.compile(b"^[" + re.escape(ALPHA_NUM) + rb"]*\\Z")

# The number of bits for numeric delimited data lengths.
NUMBER_LENGTH = {3: 10, 2: 7, 1: 4}

PATTERN_POSITION_TABLE = [
    [],
    [6, 18],
    [6, 22],
    [6, 26],
    [6, 30],
    [6, 34],
    [6, 22, 38],
    [6, 24, 42],
    [6, 26, 46],
    [6, 28, 50],
    [6, 30, 54],
    [6, 32, 58],
    [6, 34, 62],
    [6, 26, 46, 66],
    [6, 26, 48, 70],
    [6, 26, 50, 74],
    [6, 30, 54, 78],
    [6, 30, 56, 82],
    [6, 30, 58, 86],
    [6, 34, 62, 90],
    [6, 28, 50, 72, 94],
    [6, 26, 50, 74, 98],
    [6, 30, 54, 78, 102],
    [6, 28, 54, 80, 106],
    [6, 32, 58, 84, 110],
    [6, 30, 58, 86, 114],
    [6, 34, 62, 90, 118],
    [6, 26, 50, 74, 98, 122],
    [6, 30, 54, 78, 102, 126],
    [6, 26, 52, 78, 104, 130],
    [6, 30, 56, 82, 108, 134],
    [6, 34, 60, 86, 112, 138],
    [6, 30, 58, 86, 114, 142],
    [6, 34, 62, 90, 118, 146],
    [6, 30, 54, 78, 102, 126, 150],
    [6, 24, 50, 76, 102, 128, 154],
    [6, 28, 54, 80, 106, 132, 158],
    [6, 32, 58, 84, 110, 136, 162],
    [6, 26, 54, 82, 110, 138, 166],
    [6, 30, 58, 86, 114, 142, 170],
]

G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | (1 << 0)
G18 = (
    (1 << 12)
    | (1 << 11)
    | (1 << 10)
    | (1 << 9)
    | (1 << 8)
    | (1 << 5)
    | (1 << 2)
    | (1 << 0)
)
G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1)

PAD0 = 0xEC
PAD1 = 0x11


# Precompute bit count limits, indexed by error correction level and code size
def _data_count(block):
    return block.data_count


BIT_LIMIT_TABLE = [
    [0]
    + [
        8 * sum(map(_data_count, base.rs_blocks(version, error_correction)))
        for version in range(1, 41)
    ]
    for error_correction in range(4)
]


def BCH_type_info(data):
    d = data << 10
    while BCH_digit(d) - BCH_digit(G15) >= 0:
        d ^= G15 << (BCH_digit(d) - BCH_digit(G15))

    return ((data << 10) | d) ^ G15_MASK


def BCH_type_number(data):
    d = data << 12
    while BCH_digit(d) - BCH_digit(G18) >= 0:
        d ^= G18 << (BCH_digit(d) - BCH_digit(G18))
    return (data << 12) | d


def BCH_digit(data):
    digit = 0
    while data != 0:
        digit += 1
        data >>= 1
    return digit


def pattern_position(version):
    return PATTERN_POSITION_TABLE[version - 1]


def mask_func(pattern):
    """
    Return the mask function for the given mask pattern.
    """
    if pattern == 0:  # 000
        return lambda i, j: (i + j) % 2 == 0
    if pattern == 1:  # 001
        return lambda i, j: i % 2 == 0
    if pattern == 2:  # 010
        return lambda i, j: j % 3 == 0
    if pattern == 3:  # 011
        return lambda i, j: (i + j) % 3 == 0
    if pattern == 4:  # 100
        return lambda i, j: (math.floor(i / 2) + math.floor(j / 3)) % 2 == 0
    if pattern == 5:  # 101
        return lambda i, j: (i * j) % 2 + (i * j) % 3 == 0
    if pattern == 6:  # 110
        return lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0
    if pattern == 7:  # 111
        return lambda i, j: ((i * j) % 3 + (i + j) % 2) % 2 == 0
    raise TypeError("Bad mask pattern: " + pattern)  # pragma: no cover


def mode_sizes_for_version(version):
    if version < 10:
        return MODE_SIZE_SMALL
    if version < 27:
        return MODE_SIZE_MEDIUM
    return MODE_SIZE_LARGE


def length_in_bits(mode, version):
    if mode not in (MODE_NUMBER, MODE_ALPHA_NUM, MODE_8BIT_BYTE, MODE_KANJI):
        raise TypeError(f"Invalid mode ({mode})")  # pragma: no cover

    check_version(version)

    return mode_sizes_for_version(version)[mode]


def check_version(version):
    if version < 1 or version > 40:
        raise ValueError(f"Invalid version (was {version}, expected 1 to 40)")


def lost_point(modules):
    modules_count = len(modules)

    lost_point = 0

    lost_point = _lost_point_level1(modules, modules_count)
    lost_point += _lost_point_level2(modules, modules_count)
    lost_point += _lost_point_level3(modules, modules_count)
    lost_point += _lost_point_level4(modules, modules_count)

    return lost_point


def _lost_point_level1(modules, modules_count):
    lost_point = 0

    modules_range = range(modules_count)
    container = [0] * (modules_count + 1)

    for row in modules_range:
        this_row = modules[row]
        previous_color = this_row[0]
        length = 0
        for col in modules_range:
            if this_row[col] == previous_color:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                previous_color = this_row[col]
        if length >= 5:
            container[length] += 1

    for col in modules_range:
        previous_color = modules[0][col]
        length = 0
        for row in modules_range:
            if modules[row][col] == previous_color:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                previous_color = modules[row][col]
        if length >= 5:
            container[length] += 1

    lost_point += sum(
        container[each_length] * (each_length - 2)
        for each_length in range(5, modules_count + 1)
    )

    return lost_point


def _lost_point_level2(modules, modules_count):
    lost_point = 0

    modules_range = range(modules_count - 1)
    for row in modules_range:
        this_row = modules[row]
        next_row = modules[row + 1]
        # use iter() and next() to skip next four-block. e.g.
        # d a f   if top-right a != b bottom-right,
        # c b e   then both abcd and abef won't lost any point.
        modules_range_iter = iter(modules_range)
        for col in modules_range_iter:
            top_right = this_row[col + 1]
            if top_right != next_row[col + 1]:
                # reduce 33.3% of runtime via next().
                # None: raise nothing if there is no next item.
                next(modules_range_iter, None)
            elif top_right != this_row[col] or top_right != next_row[col]:
                continue
            else:
                lost_point += 3

    return lost_point


def _lost_point_level3(modules, modules_count):
    # 1 : 1 : 3 : 1 : 1 ratio (dark:light:dark:light:dark) pattern in
    # row/column, preceded or followed by light area 4 modules wide. From ISOIEC.
    # pattern1:     10111010000
    # pattern2: 00001011101
    modules_range = range(modules_count)
    modules_range_short = range(modules_count - 10)
    lost_point = 0

    for row in modules_range:
        this_row = modules[row]
        modules_range_short_iter = iter(modules_range_short)
        col = 0
        for col in modules_range_short_iter:
            if (
                not this_row[col + 1]
                and this_row[col + 4]
                and not this_row[col + 5]
                and this_row[col + 6]
                and not this_row[col + 9]
                and (
                    (
                        this_row[col + 0]
                        and this_row[col + 2]
                        and this_row[col + 3]
                        and not this_row[col + 7]
                        and not this_row[col + 8]
                        and not this_row[col + 10]
                    )
                    or (
                        not this_row[col + 0]
                        and not this_row[col + 2]
                        and not this_row[col + 3]
                        and this_row[col + 7]
                        and this_row[col + 8]
                        and this_row[col + 10]
                    )
                )
            ):
                lost_point += 40
            # horspool algorithm.
            # if this_row[col + 10]:
            #   pattern1 shift 4, pattern2 shift 2. So min=2.
            # else:
            #   pattern1 shift 1, pattern2 shift 1. So min=1.
            if this_row[col + 10]:
                next(modules_range_short_iter, None)

    for col in modules_range:
        modules_range_short_iter = iter(modules_range_short)
        row = 0
        for row in modules_range_short_iter:
            if (
                not modules[row + 1][col]
                and modules[row + 4][col]
                and not modules[row + 5][col]
                and modules[row + 6][col]
                and not modules[row + 9][col]
                and (
                    (
                        modules[row + 0][col]
                        and modules[row + 2][col]
                        and modules[row + 3][col]
                        and not modules[row + 7][col]
                        and not modules[row + 8][col]
                        and not modules[row + 10][col]
                    )
                    or (
                        not modules[row + 0][col]
                        and not modules[row + 2][col]
                        and not modules[row + 3][col]
                        and modules[row + 7][col]
                        and modules[row + 8][col]
                        and modules[row + 10][col]
                    )
                )
            ):
                lost_point += 40
            if modules[row + 10][col]:
                next(modules_range_short_iter, None)

    return lost_point


def _lost_point_level4(modules, modules_count):
    dark_count = sum(map(sum, modules))
    percent = float(dark_count) / (modules_count**2)
    # Every 5% departure from 50%, rating++
    rating = int(abs(percent * 100 - 50) / 5)
    return rating * 10


def optimal_data_chunks(data, minimum=4):
    """
    An iterator returning QRData chunks optimized to the data content.

    :param minimum: The minimum number of bytes in a row to split as a chunk.
    """
    data = to_bytestring(data)
    num_pattern = rb"\\d"
    alpha_pattern = b"[" + re.escape(ALPHA_NUM) + b"]"
    if len(data) <= minimum:
        num_pattern = re.compile(b"^" + num_pattern + b"+$")
        alpha_pattern = re.compile(b"^" + alpha_pattern + b"+$")
    else:
        re_repeat = b"{" + str(minimum).encode("ascii") + b",}"
        num_pattern = re.compile(num_pattern + re_repeat)
        alpha_pattern = re.compile(alpha_pattern + re_repeat)
    num_bits = _optimal_split(data, num_pattern)
    for is_num, chunk in num_bits:
        if is_num:
            yield QRData(chunk, mode=MODE_NUMBER, check_data=False)
        else:
            for is_alpha, sub_chunk in _optimal_split(chunk, alpha_pattern):
                mode = MODE_ALPHA_NUM if is_alpha else MODE_8BIT_BYTE
                yield QRData(sub_chunk, mode=mode, check_data=False)


def _optimal_split(data, pattern):
    while data:
        match = re.search(pattern, data)
        if not match:
            break
        start, end = match.start(), match.end()
        if start:
            yield False, data[:start]
        yield True, data[start:end]
        data = data[end:]
    if data:
        yield False, data


def to_bytestring(data):
    """
    Convert data to a (utf-8 encoded) byte-string if it isn't a byte-string
    already.
    """
    if not isinstance(data, bytes):
        data = str(data).encode("utf-8")
    return data


def optimal_mode(data):
    """
    Calculate the optimal mode for this chunk of data.
    """
    if data.isdigit():
        return MODE_NUMBER
    if RE_ALPHA_NUM.match(data):
        return MODE_ALPHA_NUM
    return MODE_8BIT_BYTE


class QRData:
    """
    Data held in a QR compatible format.

    Doesn't currently handle KANJI.
    """

    def __init__(self, data, mode=None, check_data=True):
        """
        If ``mode`` isn't provided, the most compact QR data type possible is
        chosen.
        """
        if check_data:
            data = to_bytestring(data)

        if mode is None:
            self.mode = optimal_mode(data)
        else:
            self.mode = mode
            if mode not in (MODE_NUMBER, MODE_ALPHA_NUM, MODE_8BIT_BYTE):
                raise TypeError(f"Invalid mode ({mode})")  # pragma: no cover
            if check_data and mode < optimal_mode(data):  # pragma: no cover
                raise ValueError(f"Provided data can not be represented in mode {mode}")

        self.data = data

    def __len__(self):
        return len(self.data)

    def write(self, buffer):
        if self.mode == MODE_NUMBER:
            for i in range(0, len(self.data), 3):
                chars = self.data[i : i + 3]
                bit_length = NUMBER_LENGTH[len(chars)]
                buffer.put(int(chars), bit_length)
        elif self.mode == MODE_ALPHA_NUM:
            for i in range(0, len(self.data), 2):
                chars = self.data[i : i + 2]
                if len(chars) > 1:
                    buffer.put(
                        ALPHA_NUM.find(chars[0]) * 45 + ALPHA_NUM.find(chars[1]), 11
                    )
                else:
                    buffer.put(ALPHA_NUM.find(chars), 6)
        else:
            # Iterating a bytestring in Python 3 returns an integer,
            # no need to ord().
            data = self.data
            for c in data:
                buffer.put(c, 8)

    def __repr__(self):
        return repr(self.data)


class BitBuffer:
    def __init__(self):
        self.buffer: list[int] = []
        self.length = 0

    def __repr__(self):
        return ".".join([str(n) for n in self.buffer])

    def get(self, index):
        buf_index = math.floor(index / 8)
        return ((self.buffer[buf_index] >> (7 - index % 8)) & 1) == 1

    def put(self, num, length):
        for i in range(length):
            self.put_bit(((num >> (length - i - 1)) & 1) == 1)

    def __len__(self):
        return self.length

    def put_bit(self, bit):
        buf_index = self.length // 8
        if len(self.buffer) <= buf_index:
            self.buffer.append(0)
        if bit:
            self.buffer[buf_index] |= 0x80 >> (self.length % 8)
        self.length += 1


def create_bytes(buffer: BitBuffer, rs_blocks: list[RSBlock]):
    offset = 0

    maxDcCount = 0
    maxEcCount = 0

    dcdata: list[list[int]] = []
    ecdata: list[list[int]] = []

    for rs_block in rs_blocks:
        dcCount = rs_block.data_count
        ecCount = rs_block.total_count - dcCount

        maxDcCount = max(maxDcCount, dcCount)
        maxEcCount = max(maxEcCount, ecCount)

        current_dc = [0xFF & buffer.buffer[i + offset] for i in range(dcCount)]
        offset += dcCount

        # Get error correction polynomial.
        if ecCount in LUT.rsPoly_LUT:
            rsPoly = base.Polynomial(LUT.rsPoly_LUT[ecCount], 0)
        else:
            rsPoly = base.Polynomial([1], 0)
            for i in range(ecCount):
                rsPoly = rsPoly * base.Polynomial([1, base.gexp(i)], 0)

        rawPoly = base.Polynomial(current_dc, len(rsPoly) - 1)

        modPoly = rawPoly % rsPoly
        current_ec = []
        mod_offset = len(modPoly) - ecCount
        for i in range(ecCount):
            modIndex = i + mod_offset
            current_ec.append(modPoly[modIndex] if (modIndex >= 0) else 0)

        dcdata.append(current_dc)
        ecdata.append(current_ec)

    data = []
    for i in range(maxDcCount):
        for dc in dcdata:
            if i < len(dc):
                data.append(dc[i])
    for i in range(maxEcCount):
        for ec in ecdata:
            if i < len(ec):
                data.append(ec[i])

    return data


def create_data(version, error_correction, data_list):
    buffer = BitBuffer()
    for data in data_list:
        buffer.put(data.mode, 4)
        buffer.put(len(data), length_in_bits(data.mode, version))
        data.write(buffer)

    # Calculate the maximum number of bits for the given version.
    rs_blocks = base.rs_blocks(version, error_correction)
    bit_limit = sum(block.data_count * 8 for block in rs_blocks)
    if len(buffer) > bit_limit:
        raise exceptions.DataOverflowError(
            f"Code length overflow. Data size ({len(buffer)}) > size available ({bit_limit})"
        )

    # Terminate the bits (add up to four 0s).
    for _ in range(min(bit_limit - len(buffer), 4)):
        buffer.put_bit(False)

    # Delimit the string into 8-bit words, padding with 0s if necessary.
    delimit = len(buffer) % 8
    if delimit:
        for _ in range(8 - delimit):
            buffer.put_bit(False)

    # Add special alternating padding bitstrings until buffer is full.
    bytes_to_fill = (bit_limit - len(buffer)) // 8
    for i in range(bytes_to_fill):
        if i % 2 == 0:
            buffer.put(PAD0, 8)
        else:
            buffer.put(PAD1, 8)

    return create_bytes(buffer, rs_blocks)
''',
    'main': '''from __future__ import annotations

import sys
import warnings
from bisect import bisect_left
from typing import Generic, NamedTuple, TypeVar, cast, overload

from qrcode import constants, exceptions, util
from qrcode.image.base import BaseImage
from qrcode.image.pure import PyPNGImage

ModulesType = list[list[bool | None]]
# Cache modules generated just based on the QR Code version
precomputed_qr_blanks: dict[int, ModulesType] = {}


def make(data=None, **kwargs):
    qr = QRCode(**kwargs)
    qr.add_data(data)
    return qr.make_image()


def _check_box_size(size):
    if int(size) <= 0:
        raise ValueError(f"Invalid box size (was {size}, expected larger than 0)")


def _check_border(size):
    if int(size) < 0:
        raise ValueError(
            f"Invalid border value (was {size}, expected 0 or larger than that)"
        )


def _check_mask_pattern(mask_pattern):
    if mask_pattern is None:
        return
    if not isinstance(mask_pattern, int):
        raise TypeError(
            f"Invalid mask pattern (was {type(mask_pattern)}, expected int)"
        )
    if mask_pattern < 0 or mask_pattern > 7:
        raise ValueError(f"Mask pattern should be in range(8) (got {mask_pattern})")


def copy_2d_array(x):
    return [row[:] for row in x]


class ActiveWithNeighbors(NamedTuple):
    NW: bool
    N: bool
    NE: bool
    W: bool
    me: bool
    E: bool
    SW: bool
    S: bool
    SE: bool

    def __bool__(self) -> bool:
        return self.me


GenericImage = TypeVar("GenericImage", bound=BaseImage)
GenericImageLocal = TypeVar("GenericImageLocal", bound=BaseImage)


class QRCode(Generic[GenericImage]):
    modules: ModulesType
    _version: int | None = None

    def __init__(
        self,
        version=None,
        error_correction=constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
        image_factory: type[GenericImage] | None = None,
        mask_pattern=None,
    ):
        _check_box_size(box_size)
        _check_border(border)
        self.clear()
        self.version = version
        self.error_correction = int(error_correction)
        self.box_size = int(box_size)
        # Spec says border should be at least four boxes wide, but allow for
        # any (e.g. for producing printable QR codes).
        self.border = int(border)
        self.mask_pattern = mask_pattern
        self.image_factory = image_factory
        if image_factory is not None:
            assert issubclass(image_factory, BaseImage)

    @property
    def version(self) -> int:
        if self._version is None:
            self.best_fit()
        return cast("int", self._version)

    @version.setter
    def version(self, value) -> None:
        if value is not None:
            value = int(value)
            util.check_version(value)
        self._version = value

    @property
    def mask_pattern(self):
        return self._mask_pattern

    @mask_pattern.setter
    def mask_pattern(self, pattern):
        _check_mask_pattern(pattern)
        self._mask_pattern = pattern

    def clear(self):
        """
        Reset the internal data.
        """
        self.modules = [[]]
        self.modules_count = 0
        self.data_cache = None
        self.data_list = []
        self._version = None

    def add_data(self, data, optimize=20):
        """
        Add data to this QR Code.

        :param optimize: Data will be split into multiple chunks to optimize
            the QR size by finding to more compressed modes of at least this
            length. Set to ``0`` to avoid optimizing at all.
        """
        if isinstance(data, util.QRData):
            self.data_list.append(data)
        elif optimize:
            self.data_list.extend(util.optimal_data_chunks(data, minimum=optimize))
        else:
            self.data_list.append(util.QRData(data))
        self.data_cache = None

    def make(self, fit=True):
        """
        Compile the data into a QR Code array.

        :param fit: If ``True`` (or if a size has not been provided), find the
            best fit for the data to avoid data overflow errors.
        """
        if fit or (self.version is None):
            self.best_fit(start=self.version)
        if self.mask_pattern is None:
            self.makeImpl(self.best_mask_pattern())
        else:
            self.makeImpl(self.mask_pattern)

    def makeImpl(self, mask_pattern):
        self.modules_count = self.version * 4 + 17

        if self.version in precomputed_qr_blanks:
            self.modules = copy_2d_array(precomputed_qr_blanks[self.version])
        else:
            self.modules = [
                [None] * self.modules_count for i in range(self.modules_count)
            ]
            self.setup_position_probe_pattern(0, 0)
            self.setup_position_probe_pattern(self.modules_count - 7, 0)
            self.setup_position_probe_pattern(0, self.modules_count - 7)
            self.setup_position_adjust_pattern()
            self.setup_timing_pattern()

            precomputed_qr_blanks[self.version] = copy_2d_array(self.modules)

        self.setup_type_info(mask_pattern)

        if self.version >= 7:
            self.setup_type_number()

        if self.data_cache is None:
            self.data_cache = util.create_data(
                self.version, self.error_correction, self.data_list
            )
        self.map_data(self.data_cache, mask_pattern)

    def setup_position_probe_pattern(self, row, col):
        for r in range(-1, 8):
            if row + r <= -1 or self.modules_count <= row + r:
                continue

            for c in range(-1, 8):
                if col + c <= -1 or self.modules_count <= col + c:
                    continue

                if (
                    (0 <= r <= 6 and c in {0, 6})
                    or (0 <= c <= 6 and r in {0, 6})
                    or (2 <= r <= 4 and 2 <= c <= 4)
                ):
                    self.modules[row + r][col + c] = True
                else:
                    self.modules[row + r][col + c] = False

    def best_fit(self, start=None):
        """
        Find the minimum size required to fit in the data.
        """
        if start is None:
            start = 1
        util.check_version(start)

        # Corresponds to the code in util.create_data, except we don't yet know
        # version, so optimistically assume start and check later
        mode_sizes = util.mode_sizes_for_version(start)
        buffer = util.BitBuffer()
        for data in self.data_list:
            buffer.put(data.mode, 4)
            buffer.put(len(data), mode_sizes[data.mode])
            data.write(buffer)

        needed_bits = len(buffer)
        # Create a thread-local copy of the table to avoid concurrent access issues (Python 3.13+)
        self.version = bisect_left(
            util.BIT_LIMIT_TABLE[self.error_correction][:], needed_bits, start
        )
        if self.version == 41:
            raise exceptions.DataOverflowError

        # Now check whether we need more bits for the mode sizes, recursing if
        # our guess was too low
        if mode_sizes is not util.mode_sizes_for_version(self.version):
            self.best_fit(start=self.version)
        return self.version

    def best_mask_pattern(self):
        """
        Find the most efficient mask pattern.
        """
        min_lost_point = 0
        pattern = 0

        for i in range(8):
            self.makeImpl(i)

            lost_point = util.lost_point(self.modules)

            if i == 0 or min_lost_point > lost_point:
                min_lost_point = lost_point
                pattern = i

        return pattern

    def print_tty(self, out=None):
        """
        Output the QR Code only using TTY colors.

        If the data has not been compiled yet, make it first.
        """
        if out is None:
            out = sys.stdout

        if not out.isatty():
            raise OSError("Not a tty")

        if self.data_cache is None:
            self.make()

        modcount = self.modules_count
        out.write("\\x1b[1;47m" + (" " * (modcount * 2 + 4)) + "\\x1b[0m\\n")
        for r in range(modcount):
            out.write("\\x1b[1;47m  \\x1b[40m")
            for c in range(modcount):
                if self.modules[r][c]:
                    out.write("  ")
                else:
                    out.write("\\x1b[1;47m  \\x1b[40m")
            out.write("\\x1b[1;47m  \\x1b[0m\\n")
        out.write("\\x1b[1;47m" + (" " * (modcount * 2 + 4)) + "\\x1b[0m\\n")
        out.flush()

    def print_ascii(self, out=None, tty=False, invert=False):
        """
        Output the QR Code using ASCII characters.

        :param tty: use fixed TTY color codes (forces invert=True)
        :param invert: invert the ASCII characters (solid <-> transparent)
        """
        if out is None:
            out = sys.stdout

        if tty and not out.isatty():
            raise OSError("Not a tty")

        if self.data_cache is None:
            self.make()

        modcount = self.modules_count
        codes = [bytes((code,)).decode("cp437") for code in (255, 223, 220, 219)]
        if tty:
            invert = True
        if invert:
            codes.reverse()

        def get_module(x, y) -> int:
            if invert and self.border and max(x, y) >= modcount + self.border:
                return 1
            if min(x, y) < 0 or max(x, y) >= modcount:
                return 0
            return cast("int", self.modules[x][y])

        for r in range(-self.border, modcount + self.border, 2):
            if tty:
                if not invert or r < modcount + self.border - 1:
                    out.write("\\x1b[48;5;232m")  # Background black
                out.write("\\x1b[38;5;255m")  # Foreground white
            for c in range(-self.border, modcount + self.border):
                pos = get_module(r, c) + (get_module(r + 1, c) << 1)
                out.write(codes[pos])
            if tty:
                out.write("\\x1b[0m")
            out.write("\\n")
        out.flush()

    @overload
    def make_image(self, image_factory: None = None, **kwargs) -> GenericImage: ...

    @overload
    def make_image(
        self, image_factory: type[GenericImageLocal] | None = None, **kwargs
    ) -> GenericImageLocal: ...

    def make_image(self, image_factory=None, **kwargs):
        """
        Make an image from the QR Code data.

        If the data has not been compiled yet, make it first.
        """
        # Raise a warning that 'embeded' is still used
        if kwargs.get("embeded_image_path") or kwargs.get("embeded_image"):
            warnings.warn(
                "The 'embeded_*' parameters are deprecated. Use 'embedded_image_path' "
                "or 'embedded_image' instead. The 'embeded_*' parameters will be "
                "removed in v9.0.",
                category=DeprecationWarning,
                stacklevel=2,
            )

        # allow embeded_ parameters with typos for backwards compatibility
        if (
            kwargs.get("embedded_image_path")
            or kwargs.get("embedded_image")
            or kwargs.get("embeded_image_path")
            or kwargs.get("embeded_image")
        ) and self.error_correction != constants.ERROR_CORRECT_H:
            raise ValueError(
                "Error correction level must be ERROR_CORRECT_H if an embedded image is provided"
            )

        _check_box_size(self.box_size)
        if self.data_cache is None:
            self.make()

        if image_factory is not None:
            assert issubclass(image_factory, BaseImage)
        else:
            image_factory = self.image_factory
            if image_factory is None:
                from qrcode.image.pil import Image, PilImage  # noqa: PLC0415

                # Use PIL by default if available, otherwise use PyPNG.
                image_factory = PilImage if Image else PyPNGImage

        im = image_factory(
            self.border,
            self.modules_count,
            self.box_size,
            qrcode_modules=self.modules,
            **kwargs,
        )

        if im.needs_drawrect:
            for r in range(self.modules_count):
                for c in range(self.modules_count):
                    if im.needs_context:
                        im.drawrect_context(r, c, qr=self)
                    elif self.modules[r][c]:
                        im.drawrect(r, c)
        if im.needs_processing:
            im.process()

        return im

    # return true if and only if (row, col) is in the module
    def is_constrained(self, row: int, col: int) -> bool:
        return (
            row >= 0
            and row < len(self.modules)
            and col >= 0
            and col < len(self.modules[row])
        )

    def setup_timing_pattern(self):
        for r in range(8, self.modules_count - 8):
            if self.modules[r][6] is not None:
                continue
            self.modules[r][6] = r % 2 == 0

        for c in range(8, self.modules_count - 8):
            if self.modules[6][c] is not None:
                continue
            self.modules[6][c] = c % 2 == 0

    def setup_position_adjust_pattern(self):
        pos = util.pattern_position(self.version)

        for i in range(len(pos)):
            row = pos[i]

            for j in range(len(pos)):
                col = pos[j]

                if self.modules[row][col] is not None:
                    continue

                for r in range(-2, 3):
                    for c in range(-2, 3):
                        if r in (-2, 2) or c in (-2, 2) or (r == 0 and c == 0):
                            self.modules[row + r][col + c] = True
                        else:
                            self.modules[row + r][col + c] = False

    def setup_type_number(self):
        bits = util.BCH_type_number(self.version)

        for i in range(18):
            mod = ((bits >> i) & 1) == 1
            self.modules[i // 3][i % 3 + self.modules_count - 8 - 3] = mod

        for i in range(18):
            mod = ((bits >> i) & 1) == 1
            self.modules[i % 3 + self.modules_count - 8 - 3][i // 3] = mod

    def setup_type_info(self, mask_pattern):
        data = (self.error_correction << 3) | mask_pattern
        bits = util.BCH_type_info(data)

        # vertical
        for i in range(15):
            mod = ((bits >> i) & 1) == 1

            if i < 6:
                self.modules[i][8] = mod
            elif i < 8:
                self.modules[i + 1][8] = mod
            else:
                self.modules[self.modules_count - 15 + i][8] = mod

        # horizontal
        for i in range(15):
            mod = ((bits >> i) & 1) == 1

            if i < 8:
                self.modules[8][self.modules_count - i - 1] = mod
            elif i < 9:
                self.modules[8][15 - i - 1 + 1] = mod
            else:
                self.modules[8][15 - i - 1] = mod

        # fixed module
        self.modules[self.modules_count - 8][8] = True

    def map_data(self, data, mask_pattern):
        inc = -1
        row = self.modules_count - 1
        bitIndex = 7
        byteIndex = 0

        mask_func = util.mask_func(mask_pattern)

        data_len = len(data)

        for col in range(self.modules_count - 1, 0, -2):
            if col <= 6:
                col -= 1  # noqa: PLW2901

            col_range = (col, col - 1)

            while True:
                for c in col_range:
                    if self.modules[row][c] is None:
                        dark = False

                        if byteIndex < data_len:
                            dark = ((data[byteIndex] >> bitIndex) & 1) == 1

                        if mask_func(row, c):
                            dark = not dark

                        self.modules[row][c] = dark
                        bitIndex -= 1

                        if bitIndex == -1:
                            byteIndex += 1
                            bitIndex = 7

                row += inc

                if row < 0 or self.modules_count <= row:
                    row -= inc
                    inc = -inc
                    break

    def get_matrix(self):
        """
        Return the QR Code as a multidimensional array, including the border.

        To return the array without a border, set ``self.border`` to 0 first.
        """
        if self.data_cache is None:
            self.make()

        if not self.border:
            return self.modules

        width = len(self.modules) + self.border * 2
        code = [[False] * width] * self.border
        x_border = [False] * self.border
        for module in self.modules:
            code.append(x_border + cast("list[bool]", module) + x_border)
        code += [[False] * width] * self.border

        return code

    def active_with_neighbors(self, row: int, col: int) -> ActiveWithNeighbors:
        context: list[bool] = []
        for r in range(row - 1, row + 2):
            for c in range(col - 1, col + 2):
                context.append(self.is_constrained(r, c) and bool(self.modules[r][c]))
        return ActiveWithNeighbors(*context)
''',
}



# --------------------------------------------------------------------------
# 二维码渲染（内嵌 python-qrcode）
# --------------------------------------------------------------------------

def _qrcode_install() -> Any:
    """惰性装载内嵌的 python-qrcode（或环境里已安装的同名库）。"""
    import types as _types

    existing = sys.modules.get("qrcode")
    if existing is not None and hasattr(existing, "QRCode"):
        return existing                      # 环境自带 python-qrcode

    pkg = _types.ModuleType("qrcode")
    pkg.__path__ = []                        # type: ignore[attr-defined]
    pkg.__package__ = "qrcode"
    pkg._ncm_embedded = True                 # type: ignore[attr-defined]
    sys.modules["qrcode"] = pkg

    def _load(name: str) -> Any:
        mod = _types.ModuleType(f"qrcode.{name}")
        mod.__package__ = "qrcode"
        sys.modules[mod.__name__] = mod
        src = _QRCODE_SRC[name]
        exec(compile(src, f"<qrcode.{name}>", "exec"), mod.__dict__)
        setattr(pkg, name, mod)
        return mod

    # image 子包：仅满足 main.py 的类型引用，不做真实图像渲染
    img_pkg = _types.ModuleType("qrcode.image")
    img_pkg.__path__ = []                    # type: ignore[attr-defined]
    sys.modules["qrcode.image"] = img_pkg
    pkg.image = img_pkg                      # type: ignore[attr-defined]
    for sub in ("base", "pure"):
        sub_mod = _types.ModuleType(f"qrcode.image.{sub}")
        sub_mod.__package__ = "qrcode.image"
        sys.modules[sub_mod.__name__] = sub_mod
        setattr(img_pkg, sub, sub_mod)

        class _Stub:                         # noqa: N801
            def __init__(self, *a: Any, **kw: Any) -> None:
                raise RuntimeError("内嵌二维码库不需要图像渲染")

        setattr(sub_mod, "BaseImage" if sub == "base" else "PyPNGImage", _Stub)

    _load("constants")
    _load("exceptions")
    _load("LUT")
    _load("base")
    _load("util")
    main_mod = _load("main")
    pkg.QRCode = main_mod.QRCode             # type: ignore[attr-defined]
    return pkg


def qr_matrix(data: str, ec_level: str = "M") -> List[List[bool]]:
    """生成 QR 矩阵（含 quiet zone），True 表示深色模块。"""
    pkg = _qrcode_install()
    ec_map = {
        "L": pkg.constants.ERROR_CORRECT_L,
        "M": pkg.constants.ERROR_CORRECT_M,
        "Q": pkg.constants.ERROR_CORRECT_Q,
        "H": pkg.constants.ERROR_CORRECT_H,
    }
    qr = pkg.QRCode(error_correction=ec_map.get(ec_level, ec_map["M"]),
                    box_size=1, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    return [[bool(c) for c in row] for row in matrix]


def qr_terminal_lines(url: str, app: Application) -> Optional[List[str]]:
    """
    把二维码渲染成终端行（半块字符 + 黑白底色，两行模块合一字符）。
    终端太窄时返回 None，由调用方退化为打印链接。
    """
    if not app.theme.enabled:
        # 二维码靠黑白底色区分深浅，无彩色时打印出来无法识别 → 交给调用方输出链接
        return None
    width = out_width(app)
    matrix: Optional[List[List[bool]]] = None
    # L 级纠错体积最小（更易放进屏幕），放不下再逐级加冗余
    for level in ("L", "M", "Q", "H"):
        try:
            cand = qr_matrix(url, level)
        except Exception:
            continue
        if len(cand[0]) <= max(18, width - 2):
            matrix = cand
            break
    if matrix is None:
        return None
    rows, cols = len(matrix), len(matrix[0])
    theme = app.theme
    lines: List[str] = []
    for y in range(0, rows, 2):
        chars: List[str] = []
        for x in range(cols):
            top = matrix[y][x]
            bot = matrix[y + 1][x] if y + 1 < rows else top
            # fg 控制上半块颜色，bg 控制下半块颜色（30 黑 / 37 白）
            if top and bot:
                seq = "30;40"
            elif not top and not bot:
                seq = "37;47"
            elif top and not bot:
                seq = "30;47"      # 上黑下白
            else:
                seq = "37;40"      # 上白下黑（bg 40=黑，30 是前景色不可用作背景）
            chars.append(f"\x1b[{seq}m▀")
        lines.append("".join(chars) + "\x1b[0m")
    if not theme.enabled:
        lines = [_CSI_RE.sub("", ln) for ln in lines]
    return lines


def wrap_display(text: str, width: int) -> List[str]:
    """按显示宽度折行（全角字符占 2 列），保留原有换行。"""
    width = max(4, width)
    out: List[str] = []
    for para in str(text).split("\n"):
        para = para.rstrip()
        if not para:
            out.append("")
            continue
        line, w = "", 0
        for ch in para:
            cw = char_width(ch)
            if w + cw > width:
                out.append(line)
                line, w = ch, cw
            else:
                line += ch
                w += cw
        out.append(line)
    return out or [""]


def center_ansi(text: str, cols: int) -> str:
    """按显示宽度居中一行（保留 ANSI 颜色）。"""
    w = ansi_width(text)
    if w >= cols:
        return ansi_cut(text, cols)
    pad = (cols - w) // 2
    return " " * pad + text


def prompt_secret(app: Application, label: str,
                  callback: Callable[[str], None],
                  secure: bool = True) -> None:
    """请求一次“行内输入”：secure=True 用 ● 掩码（密码），
    secure=False 明文回显（短信验证码）；纯文本模式退化到 getpass/input。"""
    if getattr(app, "supports_secret", False):
        app.secret_active = True
        app.secret_label = label
        app.secret_buf = ""
        app.secret_mask = secure
        app.secret_cb = callback
        return
    try:
        value = getpass.getpass(label) if secure else input(label)
    except Exception:
        try:
            value = input(label)
        except (EOFError, KeyboardInterrupt):
            value = ""
    callback(value)


# --------------------------------------------------------------------------
# 自检与命令行入口
# --------------------------------------------------------------------------

def _self_qr_checks() -> None:
    url = "https://music.163.com/login?codekey=test-0000"
    matrix = qr_matrix(url, "L")               # 与渲染器的优先级别保持一致（L 优先）
    size = len(matrix)
    assert matrix and size == len(matrix[0]), "QR 矩阵应为方阵"
    assert 21 <= size <= 177, f"QR 尺寸异常：{size}"
    # 三个角的定位图案（7x7 + 分隔带），注意 quiet zone 会带来偏移
    finder = [
        "1111111", "1000001", "1011101", "1011101",
        "1011101", "1000001", "1111111",
    ]
    off = next(y for y in range(size) if any(matrix[y]))
    offx = next(x for x in range(size) if matrix[off][x])
    assert offx == off == 2, f"quiet zone 应为 2，实际 {offx}/{off}"

    def check_at(ox: int, oy: int) -> None:
        for dy, row in enumerate(finder):
            for dx, ch in enumerate(row):
                got = matrix[oy + dy][ox + dx]
                assert got == (ch == "1"), f"定位图案不符 ({ox + dx},{oy + dy})"

    check_at(offx, off)                                   # 左上
    check_at(size - offx - 7, off)                        # 右上
    check_at(offx, size - off - 7)                        # 左下
    assert not matrix[off + 7][offx + 7], "定位符分隔带应为浅色"

    # 终端渲染还原检查：每个字符的上下半块必须与矩阵一致
    class _FakeApp:
        output_width = 200
        theme = Theme(enabled=True)
        output_height = 0
    lines = qr_terminal_lines(url, _FakeApp())   # type: ignore[arg-type]
    assert lines, "二维码渲染失败"
    for li, line in enumerate(lines):
        visible = _CSI_RE.sub("", line)
        assert len(visible) == size, f"渲染宽度不符：{len(visible)} != {size}"
        y = li * 2
        for x, ch in enumerate(visible):
            assert ch == "▀", "应全部为半块字符"
            top = matrix[y][x]
            bot = matrix[y + 1][x] if y + 1 < size else top
            m = re.match(r"\x1b\[(\d+);(\d+)m▀", line[x * 9:x * 9 + 9])
            assert m, "颜色序列缺失"
            fg, bg = int(m.group(1)), int(m.group(2))
            assert (fg == 30) == top, "上半块颜色不符"
            assert (bg == 40) == bot, "下半块颜色不符"


def run_selftest() -> int:
    """离线自检：不需要联网即可验证核心算法。"""
    checks: List[Tuple[str, Callable[[], None]]] = [
        ("AES / weapi 加密（NIST 向量）", selftest_crypto),
        ("NCM 解密 / 下载格式识别", selftest_ncm),
        ("全角宽度与排版", lambda: (
            assert_eq(disp_width("海阔天空 Beyond"), 15),
            assert_eq(disp_width("abc"), 3),
            assert_eq(slice_disp("海阔天空", 7), "海阔天…"),
            assert_eq(slice_disp("海阔天空", 8), "海阔天空"),
            assert_eq(disp_width(slice_disp("海阔天空", 7)), 7),
            assert_eq(pad_disp("测试", 8, "center", "."), "..测试.."),
        )),
        ("时间格式化", lambda: (
            assert_eq(fmt_time(296), "04:56"),
            assert_eq(fmt_time(3725), "1:02:05"),
            assert_eq(fmt_time(-1), "--:--"),
        )),
        ("LRC 歌词解析", lambda: (
            assert_eq(len(LyricSheet.build("[00:01.00]a\n[00:02.50]b").lines), 2),
            assert_eq(LyricSheet.build("[00:01.00]a\n[00:02.50]b").index_at(2.0), 0),
            assert_eq(LyricSheet.build(
                "[00:01.00]a", "[00:01.00]甲").lines[0].trans, "甲"),
        )),
        ("终端布局自适应", lambda: (
            assert_eq(adaptive_layout(160, 40)["mode"], "wide"),
            assert_eq(adaptive_layout(80, 24)["mode"], "stack"),
            assert_eq(adaptive_layout(40, 10)["mode"], "mini"),
            assert_eq(adaptive_layout(80, 24)["out_h"] > 0, True),
        )),
        ("ANSI 保留截断", lambda: (
            assert_eq(ansi_cut("\x1b[31mabc\x1b[0m", 2), "\x1b[31ma…\x1b[0m"),
            assert_eq(ansi_width(ansi_cut("\x1b[31mabc\x1b[0m", 2)), 2),
            assert_eq(ansi_width("\x1b[31mabc\x1b[0m"), 3),
        )),
        ("二维码生成与渲染", _self_qr_checks),
        ("二维码尺寸（L 级，便于整屏显示）", lambda: (
            assert_eq(len(qr_matrix(
                "https://music.163.com/login?codekey="
                "00000000-0000-0000-0000-000000000000", "L")[0]) <= 45, True),
        )),
        ("Cookie 凭证解析", lambda: (
            assert_eq(parse_cookie_token("MUSIC_U=abc%2B12; __csrf=tok1"),
                      ("abc%2B12", "tok1")),
            assert_eq(parse_cookie_token("  plainValue  "), ("plainValue", "")),
            assert_eq(parse_cookie_token('MUSIC_U="x%3D"; a=b'), ("x%3D", "")),
        )),
        ("按键序列解析", lambda: (
            assert_eq([e.name for e in parse_input(b"\x1b[A")[0]], ["up"]),
            assert_eq([e.char for e in parse_input("中a".encode())[0]], ["中", "a"]),
            assert_eq(len(parse_input(b"\x1b[3~")[0]), 1),
            assert_eq([e.name for e in parse_input(b"\r\x7f")[0]],
                      ["enter", "backspace"]),
        )),
    ]
    passed = failed = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  [FAIL] {name}：{exc}")
            failed += 1
        except Exception as exc:            # noqa: BLE001
            print(f"  [ERROR] {name}：{type(exc).__name__}: {exc}")
            failed += 1
    print(f"自检完成：{passed} 通过，{failed} 失败")
    return 0 if failed == 0 else 1


def assert_eq(actual: Any, expected: Any) -> None:
    if actual != expected:
        raise AssertionError(f"{actual!r} != {expected!r}")


def build_arg_parser() -> Any:
    import argparse
    parser = argparse.ArgumentParser(
        prog=os.path.basename(__file__),
        description=f"{APP_NAME} v{APP_VERSION} —— 纯 Python 标准库实现的"
                    "网易云音乐终端播放器（搜索 / 歌单 / 登录 / 歌词 / 播放）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 netease_player.py\n"
            "  python3 netease_player.py -c 'search 海阔天空' -c play\n"
            "  python3 netease_player.py --backend sim --plain\n"
            "  echo help | python3 netease_player.py --plain\n"
            "\n进入界面后输入 help 查看全部命令。"),
    )
    parser.add_argument("-c", "--command", action="append", default=[],
                        metavar="CMD", help="启动后先执行的命令（可重复）")
    parser.add_argument("--plain", action="store_true",
                        help="纯文本模式（管道 / 脚本 / 无 TTY 环境）")
    parser.add_argument("--backend", default=None,
                        choices=["auto", "mpv", "ffplay", "mpg123", "sim"],
                        help="指定播放内核（默认 auto 自动探测）")
    parser.add_argument("--mono", action="store_true", help="禁用彩色输出")
    parser.add_argument("--selftest", action="store_true",
                        help="运行离线自检并退出")
    parser.add_argument("--version", action="version",
                        version=f"{APP_NAME} {APP_VERSION}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.selftest:
        return run_selftest()
    try:
        app = Application(args)
    except Exception as exc:                # noqa: BLE001
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1
    interactive = (not args.plain) and sys.stdin.isatty() and sys.stdout.isatty()
    ui: Any = TerminalUI(app) if interactive else PlainUI(app)
    try:
        ui.run()
    except KeyboardInterrupt:
        print("\n已退出")
        try:
            app.shutdown()
        except Exception:
            pass
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
