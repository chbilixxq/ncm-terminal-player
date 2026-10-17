#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""截取 TUI 实际画面：跑一段真实会话，抓取一次全量重绘的 ANSI 帧，
转成带颜色的 HTML，供浏览器截图。

用法：python3 tools/make_shots.py
输出：screenshots/tui-wide.html / screenshots/tui-narrow.html
"""
import html
import os
import re
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER = os.path.join(ROOT, "netease_player.py")
SHOT_DIR = os.path.join(ROOT, "screenshots")

sys.path.insert(0, os.path.join(ROOT, "tools"))
import importlib.util

_spec = importlib.util.spec_from_file_location("tui_test_mod",
                                               os.path.join(ROOT, "tools", "tui_test.py"))
_tmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tmod)
Screen = _tmod.Screen

_spec2 = importlib.util.spec_from_file_location("ncm_player", PLAYER)
np = importlib.util.module_from_spec(_spec2)
sys.modules["ncm_player"] = np
_spec2.loader.exec_module(np)
os.environ.setdefault("NCM_CONFIG_DIR",
                      os.path.join(os.environ.get("TMPDIR", "/tmp"),
                                  "ncm-test-config"))

FG = {
    "30": "#0c0c0c", "31": "#cd3131", "32": "#0dbc79", "33": "#e5e510",
    "34": "#2472c8", "35": "#bc3fbc", "36": "#11a8cd", "37": "#e5e5e5",
    "90": "#666666", "91": "#f14c4c", "92": "#23d18b", "93": "#f5f543",
    "94": "#3b8eea", "95": "#d670d6", "96": "#29b8db", "97": "#ffffff",
}
BG = {
    "40": "#0c0c0c", "41": "#cd3131", "42": "#0dbc79", "43": "#e5e510",
    "44": "#2472c8", "45": "#bc3fbc", "46": "#11a8cd", "47": "#e5e5e5",
    "100": "#666666", "101": "#f14c4c", "102": "#23d18b", "103": "#f5f543",
    "104": "#3b8eea", "105": "#d670d6", "106": "#29b8db", "107": "#ffffff",
}
SGR = re.compile(r"\x1b\[([0-9;]*)m")


def row_to_html(content: str) -> str:
    """一行带 SGR 的文本 → HTML。"""
    out = []
    pos = 0
    fg = bg = None
    bold = rev = False

    def flush(seg: str) -> None:
        if not seg:
            return
        f, b = fg, bg
        if rev:
            f, b = b or "#e5e5e5", f or "#0c0c0c"
        style = []
        if f:
            style.append(f"color:{f}")
        if b:
            style.append(f"background:{b}")
        if bold:
            style.append("font-weight:700")
        text = html.escape(seg)
        out.append(f'<span style="{";".join(style)}">{text}</span>' if style
                   else f"<span>{text}</span>")

    # 先去掉本行开头的擦除序列
    content = content.replace("\x1b[2K", "")
    while True:
        m = SGR.search(content, pos)
        if not m:
            flush(content[pos:])
            break
        flush(content[pos:m.start()])
        codes = [c for c in m.group(1).split(";") if c != ""] or ["0"]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == "0":
                fg = bg = None
                bold = rev = False
            elif c == "1":
                bold = True
            elif c == "7":
                rev = True
            elif c == "22":
                bold = False
            elif c == "27":
                rev = False
            elif c in FG:
                fg = FG[c]
            elif c in BG:
                bg = BG[c]
            elif c == "39":
                fg = None
            elif c == "49":
                bg = None
            i += 1
        pos = m.end()
    return "".join(out)


def frame_to_html(raw: str, cols: int, rows: int, title: str) -> str:
    """解析一次“全量重绘”的 ANSI 帧（\\x1b[r;1H\\x1b[2K + 内容）。"""
    grid: dict = {}
    parts = re.split(r"\x1b\[(\d+);1H", raw)
    # parts: [prefix, row, chunk, row, chunk, ...]
    for i in range(1, len(parts) - 1, 2):
        try:
            r = int(parts[i])
        except ValueError:
            continue
        chunk = parts[i + 1]
        chunk = re.sub(r"\x1b\[\d+;\d+H.*$", "", chunk, flags=re.S)
        grid[r - 1] = chunk
    lines = []
    for r in range(rows):
        content = grid.get(r, "")
        lines.append(row_to_html(content))
    body = "\n".join(f'<div class="line">{ln or "&nbsp;"}</div>' for ln in lines)
    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
 body {{ background:#1b1b1b; margin:0; padding:28px; font-family:
   "Sarasa Mono SC","Cascadia Mono","DejaVu Sans Mono",Consolas,monospace; }}
 .term {{ background:#0c0c0c; border-radius:10px; overflow:hidden;
   box-shadow:0 12px 40px rgba(0,0,0,.55); }}
 .bar {{ display:flex; align-items:center; gap:8px; padding:10px 14px;
   background:#2a2a2a; color:#bbb; font-size:13px; }}
 .dot {{ width:12px; height:12px; border-radius:50%; display:inline-block; }}
 .screen {{ padding:10px 14px 14px; font-size:14px; line-height:1.32;
   color:#e5e5e5; background:#0c0c0c; }}
 .line {{ white-space:pre; }}
</style></head><body>
<div class="term">
  <div class="bar"><span class="dot" style="background:#f14c4c"></span>
  <span class="dot" style="background:#e5e510"></span>
  <span class="dot" style="background:#23d18b"></span>
  <span>{html.escape(title)}</span></div>
  <div class="screen">{body}</div>
</div>
</body></html>
"""


def capture(args, cols, rows, actions, title, out_name):
    os.environ.pop("NO_COLOR", None)     # 测试环境可能注入 NO_COLOR
    os.environ["COLUMNS"] = str(cols)
    os.environ["LINES"] = str(rows)
    try:
        devnull = os.open("/dev/null", os.O_RDONLY)
        os.dup2(devnull, 0)
        os.close(devnull)
    except OSError:
        pass

    a = type("Args", (), {"backend": "ffplay", "mono": False,
                          "command": [], "plain": False})()
    app = np.Application(a)
    ui = np.TerminalUI(app)
    emu = Screen(cols, rows)
    raw: list = []
    ui._write = lambda t: (raw.append(t), emu.feed(t.encode("utf-8", "replace")))
    ui._reader = lambda: None
    ui._enter = lambda: signal.signal(signal.SIGWINCH, ui._on_winch)
    ui._leave = lambda: None

    def key(name="", char=""):
        ev = np.KeyEvent("kind_char", char=char) if char else np.KeyEvent("key", name=name)
        ui.key_queue.put([ev])

    def type_line(s):
        for ch in s:
            key(char=ch)
            time.sleep(0.012)
        key("enter")

    errors = []

    def driver():
        try:
            time.sleep(1.0)
            for act in actions:
                if act.startswith("sleep:"):
                    time.sleep(float(act.split(":")[1]))
                elif act.startswith("key:"):
                    key(act.split(":", 1)[1])
                else:
                    type_line(act)
                    time.sleep(0.4)
            time.sleep(1.0)
            raw.clear()
            ui.prev_frame = []          # 触发一次全量重绘，抓完整帧
            time.sleep(1.0)
        except Exception as exc:        # noqa: BLE001
            errors.append(str(exc))
        app.running = False

    import threading
    t = threading.Thread(target=driver, daemon=True)
    t.start()
    ui.run()
    t.join(timeout=3)

    frame = "".join(raw)
    os.makedirs(SHOT_DIR, exist_ok=True)
    out = os.path.join(SHOT_DIR, out_name)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(frame_to_html(frame, cols, rows, title))
    print(f"written: {out} ({len(frame)} bytes ANSI)")
    if errors:
        print("  driver errors:", errors)


def main() -> int:
    capture(
        cols=140, rows=40, args=None,
        title="netease_player.py — 140×40 宽屏双栏",
        out_name="tui-wide.html",
        actions=[
            "search 海阔天空",
            "sleep:3.5",
            "key:q",            # 长输出已自动进入浏览：先退出
            "sleep:0.6",
            "play 1",
            "sleep:8",
        ],
    )
    capture(
        cols=140, rows=40, args=None,
        title="netease_player.py — 全屏浏览（PgUp 翻页回看输出）",
        out_name="tui-browse.html",
        actions=[
            "search 海阔天空",
            "sleep:3.5",
            "list" if False else "sleep:0.5",
            "key:pgup",
            "key:pgup",
            "key:pgup",
            "sleep:1.0",
        ],
    )
    capture(
        cols=100, rows=32, args=None,
        title="netease_player.py — 扫码登录（全屏二维码）",
        out_name="tui-qr.html",
        actions=[
            "login qr",
            "sleep:6",
        ],
    )
    capture(
        cols=110, rows=34, args=None,
        title="netease_player.py — 评论页（全屏浏览 + 发表）",
        out_name="tui-comment.html",
        actions=[
            "play 1357375695",
            "sleep:5",
            "cm",
            "sleep:6",
        ],
    )
    capture(
        cols=64, rows=22, args=None,
        title="netease_player.py — 64×22 中屏堆叠",
        out_name="tui-narrow.html",
        actions=[
            "search 光辉岁月",
            "sleep:3.5",
            "key:q",            # 长输出已自动进入浏览：先退出
            "sleep:0.6",
            "play 1",
            "sleep:6",
        ],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
