#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载功能离线测试：不需要网易云账号，也不需要联网。

覆盖：NCM 解密 / 音频格式识别 / 文件命名 / 单曲下载 / 批量下载 /
已存在跳过 / 下载目录配置。
"""
import base64
import importlib.util
import json
import os
import struct
import sys
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER = os.path.join(ROOT, "netease_player.py")

_spec = importlib.util.spec_from_file_location("ncm_player_dl", PLAYER)
np = importlib.util.module_from_spec(_spec)
sys.modules["ncm_player_dl"] = np
_spec.loader.exec_module(np)

FAILURES: list = []


def check(cond: bool, label: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + label, flush=True)
    if not cond:
        FAILURES.append(label)


def make_ncm(audio: bytes, meta: dict) -> bytes:
    """按 NCM 格式构造一个加密样本（与真实文件结构一致）。"""
    key_plain = np.NCM_KEY_PREFIX + bytes((i * 37 + 11) & 0xFF for i in range(111))
    enc_key = bytes(b ^ 0x64 for b in np.aes_ecb_encrypt(
        key_plain, np.NCM_CORE_KEY, pad=False))
    meta_plain = b"music:" + json.dumps(
        meta, ensure_ascii=False).encode("utf-8")
    meta_blob = np.NCM_META_PREFIX + base64.b64encode(
        np.aes_ecb_encrypt(meta_plain, np.NCM_META_KEY, pad=True))
    enc_meta = bytes(b ^ 0x63 for b in meta_blob)
    enc_audio = np._ncm_xor(
        audio, np._ncm_keybox(key_plain[len(np.NCM_KEY_PREFIX):]))
    blob = bytearray()
    blob += np.NCM_MAGIC + b"\x00\x00"
    blob += struct.pack("<I", len(enc_key)) + enc_key
    blob += struct.pack("<I", len(enc_meta)) + enc_meta
    blob += b"\x00" * 4 + b"\x00" * 5
    blob += struct.pack("<I", 0)
    blob += enc_audio
    return bytes(blob)


class _Handler(BaseHTTPRequestHandler):
    payloads: dict = {}

    def do_GET(self):                                  # noqa: N802
        data = self.payloads.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        pass


class FakeApi:
    def __init__(self, urls: dict):
        self.urls = urls

    def resolve_download_info(self, song, quality="exhigh"):
        return self.urls[song.id]

    def open_url(self, url, timeout=30):
        return urllib.request.urlopen(url)


class FakeApp:
    def __init__(self, api: FakeApi):
        self.api = api
        self.busy = ""
        self.logs: list = []

    def _log(self, level, text):
        self.logs.append((level, text))

    def info(self, text):
        self._log("info", text)

    def ok(self, text):
        self._log("ok", text)

    def warn(self, text):
        self._log("warn", text)

    def error(self, text):
        self._log("error", text)

    def println(self, text="", *_styles):
        self._log("println", text)


def main() -> int:
    print("下载功能离线测试", flush=True)
    mp3 = (b"ID3\x04\x00\x00\x00\x00\x00\x00"
           + bytes((i * 5) & 0xFF for i in range(9000)))
    flac = b"fLaC" + bytes((i * 7) & 0xFF for i in range(9000))
    ncm = make_ncm(flac, {"format": "flac", "musicName": "测试歌曲"})

    _Handler.payloads = {"/song.mp3": mp3, "/song.ncm": ncm}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    api = FakeApi({
        1: {"url": base + "/song.mp3", "type": "mp3", "br": 320000},
        2: {"url": base + "/song.ncm", "type": "ncm", "br": 999000},
        3: {"url": base + "/song.mp3", "type": "mp3", "br": 128000,
            "freeTrialInfo": {"start": 0, "end": 30}},
    })
    app = FakeApp(api)
    song1 = np.Song(id=1, name="晴天", artists=["周杰伦"], duration=269000)
    song2 = np.Song(id=2, name='A/B:C*?"<>|', artists=["歌手/二"],
                    duration=200000)
    song3 = np.Song(id=3, name="试听片段", artists=["X"], duration=200000)

    try:
        with tempfile.TemporaryDirectory() as tmp:
            # 1) 单曲 mp3
            mgr = np.DownloadManager(app, tmp)
            p1 = mgr.download(song1)
            check(p1 is not None and p1.endswith("周杰伦 - 晴天.mp3"),
                  "单曲下载：生成「歌手 - 歌名.mp3」")
            with open(p1, "rb") as fh:
                check(fh.read() == mp3, "单曲下载：内容与源文件一致")

            # 2) NCM 自动解密为 FLAC
            p2 = mgr.download(song2)
            check(p2 is not None and p2.endswith(".flac"),
                  "NCM 下载：解密后按真实格式保存为 .flac")
            with open(p2, "rb") as fh:
                check(fh.read() == flac, "NCM 下载：解密后音频与原始 FLAC 一致")
            check("/" not in os.path.basename(p2)
                  and ":" not in os.path.basename(p2),
                  "文件名过滤路径分隔符与非法字符")

            # 3) 已存在跳过
            before = mgr.success
            again = mgr.download(song1)
            check(again == p1 and mgr.success == before and mgr.skipped == 1,
                  "重复下载：自动跳过已存在文件")

            # 4) 批量下载 / 失败不中断
            api.urls[99] = {"url": base + "/not-found.mp3", "type": "mp3"}
            missing = np.Song(id=99, name="坏链接", artists=["X"], duration=1000)
            mgr2 = np.DownloadManager(app, tmp)
            mgr2.download_many([song1, song2, missing, song3], "测试歌单")
            check(mgr2.skipped == 2 and mgr2.failed == 1 and mgr2.success == 1,
                  "批量下载：统计成功 / 跳过 / 失败，单曲失败不中断")
            check(any(lv == "warn" and "试听" in txt for lv, txt in app.logs),
                  "试听片段：给出提示")

            # 5) NCM 解密函数直接验证
            with tempfile.TemporaryDirectory() as tmp2:
                src = os.path.join(tmp2, "x.ncm")
                dst = os.path.join(tmp2, "x.flac")
                with open(src, "wb") as fh:
                    fh.write(ncm)
                meta = np.ncm_extract(src, dst)
                with open(dst, "rb") as fh:
                    check(fh.read() == flac, "ncm_extract：解密结果正确")
                check(meta.get("format") == "flac", "ncm_extract：读取元数据")

            # 6) 默认下载目录 / 自定义目录
            class FakeCfg:
                def __init__(self, value=""):
                    self.value = value

                def get(self, _key, default=None):
                    return self.value

            check(np.default_download_dir()
                  == os.path.join(ROOT, "download"),
                  "默认下载目录：程序目录 / download")
            check(np.configured_download_dir(FakeCfg(""))
                  == np.default_download_dir(),
                  "下载目录：配置为空时使用默认目录")
            custom = os.path.join(tmp, "custom")
            check(np.configured_download_dir(FakeCfg(custom)) == custom,
                  "下载目录：支持配置自定义路径")

            # 7) 音质回退阶梯 / 格式识别
            check(np.quality_ladder("lossless")[0][1] == 999000
                  and np.quality_ladder("exhigh")[0][1] == 320000
                  and np.audio_format_from_head(b"fLaC....") == "flac"
                  and np.audio_format_from_head(b"ID3....") == "mp3",
                  "音质回退阶梯与音频格式识别")
    finally:
        server.shutdown()

    if FAILURES:
        print(f"\n下载测试失败：{len(FAILURES)} 项", flush=True)
        return 1
    print("\n下载测试全部通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
